"""Leitura do banco do Gadgetbridge (Huawei Watch Fit 3) e nota do dia em atividade/."""
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
