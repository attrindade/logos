"""Leitura do banco do Gadgetbridge (Huawei Watch Fit) e nota do dia em atividade/."""
import sqlite3
from datetime import date, datetime

import pytest

from logos import relogio

DIA = date(2026, 10, 13)


def _ts(h, m=0, d=DIA):
    return int(datetime(d.year, d.month, d.day, h, m).astimezone().timestamp())


def _banco(tmp_path, *, truesleep=True):
    caminho = tmp_path / "Gadgetbridge.db"
    con = sqlite3.connect(caminho)
    con.execute(
        "CREATE TABLE HUAWEI_ACTIVITY_SAMPLE (TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER, "
        "OTHER_TIMESTAMP INTEGER, SOURCE INTEGER, RAW_KIND INTEGER, RAW_INTENSITY INTEGER, STEPS INTEGER, "
        "CALORIES INTEGER, DISTANCE INTEGER, SPO INTEGER, HEART_RATE INTEGER, RESTING_HEART_RATE INTEGER)"
    )
    if truesleep:
        con.execute("CREATE TABLE HUAWEI_SLEEP_STATS_SAMPLE (TIMESTAMP INTEGER, DEVICE_ID INTEGER, "
                    "USER_ID INTEGER, SLEEP_SCORE INTEGER, WAKEUP_TIME INTEGER, WAKE_COUNT INTEGER)")
        con.execute("CREATE TABLE HUAWEI_SLEEP_STAGE_SAMPLE (TIMESTAMP INTEGER, DEVICE_ID INTEGER, "
                    "USER_ID INTEGER, STAGE INTEGER)")
    return caminho, con


def _passo(con, ts, steps, hr, repouso):
    # Marcador de início com os dados e marcador de fim vazio, como o HuaweiSampleProvider grava.
    con.execute("INSERT INTO HUAWEI_ACTIVITY_SAMPLE VALUES (?,1,1,?,11,-1,1,?,0,0,-1,?,?)",
                (ts, ts + 60, steps, hr, repouso))
    con.execute("INSERT INTO HUAWEI_ACTIVITY_SAMPLE VALUES (?,1,1,?,11,-1,1,-1,-1,-1,-1,-1,-1)", (ts + 60, ts))


def _noite_truesleep(con, ini, fim, estagios, nota=82, acordou=2):
    con.execute("INSERT INTO HUAWEI_SLEEP_STATS_SAMPLE VALUES (?,1,1,?,?,?)", (ini * 1000, nota, fim * 1000, acordou))
    t = ini
    for estagio, minutos in estagios:
        for _ in range(minutos):
            con.execute("INSERT INTO HUAWEI_SLEEP_STAGE_SAMPLE VALUES (?,1,1,?)", (t * 1000, estagio))
            t += 60


def test_dia_com_truesleep(tmp_path):
    caminho, con = _banco(tmp_path)
    ontem = date(2026, 10, 12)
    # 23:30 → 07:00: 4h leve, 1h30 profundo, 1h20 REM, 40 min acordado.
    _noite_truesleep(con, _ts(23, 30, ontem), _ts(7, 0), [(1, 240), (3, 90), (4, 40), (2, 80)])
    _passo(con, _ts(9), 500, 95, 58)
    _passo(con, _ts(10), 1200, -116, 57)  # 140 bpm gravado como byte com sinal
    _passo(con, _ts(11), 0, -1, -1)
    con.commit()
    con.close()

    con = relogio.abrir(caminho)
    dia = relogio.ler_dia(con, DIA)
    con.close()

    assert dia.noite is not None
    assert dia.noite.dormindo_min == 240 + 90 + 80
    assert dia.noite.estagios_min == {"leve": 240, "profundo": 90, "acordado": 40, "REM": 80}
    assert (dia.noite.acordou, dia.noite.nota) == (2, 82)
    assert dia.passos == 1700
    assert (dia.fc_min, dia.fc_max, dia.fc_repouso) == (95, 140, 57)
    assert relogio.linha_noite(dia) == "Sono: 6h50 (23:30 → 07:00), acordou 2x, nota 82, FC repouso 57."


def test_sem_truesleep_sono_sai_das_amostras_de_atividade(tmp_path):
    caminho, con = _banco(tmp_path, truesleep=False)
    ontem = date(2026, 10, 12)
    sql = "INSERT INTO HUAWEI_ACTIVITY_SAMPLE VALUES (?,1,1,?,13,?,1,-1,-1,-1,-1,-1,-1)"
    # Dois trechos com 20 min entre eles: mesma noite.
    con.execute(sql, (_ts(0, 0), _ts(3, 0), relogio.KIND_LEVE))
    con.execute(sql, (_ts(3, 20), _ts(6, 20), relogio.KIND_PROFUNDO))
    con.execute(sql, (_ts(6, 20), _ts(0, 0), relogio.KIND_PROFUNDO))  # marcador de fim: ignorado
    # Cochilo à tarde, isolado.
    con.execute(sql, (_ts(14, 0), _ts(14, 40), relogio.KIND_LEVE))
    # Noite anterior, que termina na véspera: não entra.
    con.execute(sql, (_ts(1, 0, ontem), _ts(7, 0, ontem), relogio.KIND_LEVE))
    con.commit()
    con.close()

    con = relogio.abrir(caminho)
    dia = relogio.ler_dia(con, DIA)
    con.close()

    assert dia.noite is not None
    assert dia.noite.dormindo_min == 360
    assert f"{dia.noite.inicio:%H:%M}-{dia.noite.fim:%H:%M}" == "00:00-06:20"
    assert [c.dormindo_min for c in dia.cochilos] == [40]
    assert dia.passos is None


def _trecho(con, ini, fim, kind=relogio.KIND_LEVE):
    con.execute("INSERT INTO HUAWEI_ACTIVITY_SAMPLE VALUES (?,1,1,?,13,?,1,-1,-1,-1,-1,-1,-1)", (ini, fim, kind))


def _ler(tmp_path, *trechos):
    caminho, con = _banco(tmp_path, truesleep=False)
    for ini, fim in trechos:
        _trecho(con, ini, fim)
    con.commit()
    con.close()
    con = relogio.abrir(caminho)
    try:
        return relogio.ler_dia(con, DIA)
    finally:
        con.close()


def _hm(s):
    return f"{s.inicio:%H:%M}-{s.fim:%H:%M}"


def test_acordar_de_madrugada_e_voltar_a_dormir_e_a_mesma_noite(tmp_path):
    # A noite de 13/10: 00:35-03:52, 74 min acordado, 05:06-09:53.
    dia = _ler(tmp_path, (_ts(0, 35), _ts(3, 52)), (_ts(5, 6), _ts(9, 53)))
    assert _hm(dia.noite) == "00:35-09:53"
    assert dia.noite.dormindo_min == 197 + 287
    assert (dia.noite.interrupcoes, dia.noite.fora_min) == (1, 74)
    assert dia.cochilos == []
    assert relogio.linha_noite(dia) == "Sono: 8h04 (00:35 → 09:53), 1 interrupção (1h14 acordado)."


def test_noite_que_comeca_na_vespera(tmp_path):
    # 02/10: dormiu 22:18 na véspera, acordou 03:12, voltou 04:24-08:59.
    ontem = date(2026, 10, 12)
    dia = _ler(tmp_path, (_ts(22, 18, ontem), _ts(3, 12)), (_ts(4, 24), _ts(8, 59)))
    assert _hm(dia.noite) == "22:18-08:59"
    assert dia.noite.dormindo_min == 294 + 275


def test_depois_das_6h_so_intervalo_curto_emenda(tmp_path):
    # 05/10: 02:07-04:42, 07:14-10:00 e um trecho 31 min depois de acordar, que é cochilo.
    dia = _ler(tmp_path, (_ts(2, 7), _ts(4, 42)), (_ts(7, 14), _ts(10, 0)), (_ts(10, 31), _ts(11, 5)))
    assert _hm(dia.noite) == "02:07-10:00"
    assert [_hm(c) for c in dia.cochilos] == ["10:31-11:05"]


def test_cochilos_da_tarde_e_do_comeco_da_noite_ficam_de_fora(tmp_path):
    ontem = date(2026, 10, 12)
    dia = _ler(
        tmp_path,
        (_ts(19, 0, ontem), _ts(19, 40, ontem)),  # cochilo no começo da noite anterior: não emenda
        (_ts(1, 0), _ts(8, 0)),
        (_ts(16, 21), _ts(17, 49)),               # cochilo da tarde
        (_ts(18, 30), _ts(19, 10)),               # depois das 18h já é do dia seguinte
    )
    assert _hm(dia.noite) == "01:00-08:00"
    assert dia.noite.interrupcoes == 0
    assert [_hm(c) for c in dia.cochilos] == ["19:00-19:40", "16:21-17:49"]


def test_trecho_curto_nao_vira_noite(tmp_path):
    dia = _ler(tmp_path, (_ts(3, 0), _ts(3, 40)))
    assert dia.noite is None
    assert [_hm(c) for c in dia.cochilos] == ["03:00-03:40"]


def test_nota_refaz_o_que_e_seu_e_guarda_o_resto(tmp_path):
    caminho, con = _banco(tmp_path)
    _passo(con, _ts(9), 800, 90, 60)
    con.commit()
    con.close()
    vault = tmp_path / "vault"
    agora = datetime(2026, 10, 13, 21, 0).astimezone()

    relogio.rodar(caminho, vault, dias=1, ate=DIA, agora=agora)
    nota = vault / "atividade" / "Relógio — 2026-10-13.md"
    texto = nota.read_text(encoding="utf-8")
    assert "passos: 800" in texto
    assert "- private" in texto
    assert "[[Logos — Relógio]]" in texto
    assert "Noite: sem dado." in texto

    # Outro dono acrescentou uma chave; a próxima execução não a apaga.
    nota.write_text(texto.replace("fonte: gadgetbridge", "fonte: gadgetbridge\npromoted: brain/x"), encoding="utf-8")
    con = sqlite3.connect(caminho)
    _passo(con, _ts(10), 200, 90, 60)
    con.commit()
    con.close()
    relogio.rodar(caminho, vault, dias=1, ate=DIA, agora=agora)
    texto = nota.read_text(encoding="utf-8")
    assert "passos: 1000" in texto
    assert "promoted: brain/x" in texto


def test_dia_sem_dado_nao_vira_nota(tmp_path):
    caminho, con = _banco(tmp_path)
    con.commit()
    con.close()
    vault = tmp_path / "vault"
    lidos = relogio.rodar(caminho, vault, dias=2, ate=DIA)
    assert [d.data for d in lidos] == [date(2026, 10, 12), DIA]
    assert not (vault / "atividade").exists() or not any((vault / "atividade").iterdir())
    assert relogio.linha_noite(lidos[-1]).startswith("Sono: sem dado")


def test_arquivo_que_nao_e_banco(tmp_path):
    falso = tmp_path / "Gadgetbridge.zip"
    falso.write_bytes(b"PK\x03\x04 nada de sqlite aqui" * 10)
    with pytest.raises(relogio.RelogioError):
        relogio.ler_dia(relogio.abrir(falso), DIA)
    with pytest.raises(relogio.RelogioError):
        relogio.abrir(tmp_path / "nao-existe.db")


def test_cli_linha(tmp_path, capsys):
    caminho, con = _banco(tmp_path)
    _noite_truesleep(con, _ts(0, 30), _ts(7, 30), [(1, 300), (3, 120)], nota=0, acordou=0)
    con.commit()
    con.close()
    rc = relogio.main(["--db", str(caminho), "--vault", str(tmp_path / "v"), "--data", DIA.isoformat(), "--linha"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "Sono: 7h00 (00:30 → 07:30)."
