"""Mensagem da noite, relatórios de sono × PC e sono × Diário, e o gatilho do banco novo."""
import os
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from logos import config, dia as diario, relogio, sono
from logos.triage import Route

DIA = date(2026, 10, 13)


def _dt(h, m=0, d=DIA):
    return datetime(d.year, d.month, d.day, h, m).astimezone()


def _noite(d, ini, fim, minutos=None, fc=None):
    """Dia com uma noite de um trecho só; `ini` e `fim` são datetimes."""
    n = relogio.Sono(inicio=ini, fim=fim, dormindo_min=minutos or int((fim - ini).total_seconds() // 60))
    return relogio.Dia(data=d, noite=n, fc_repouso=fc)


def _anteriores(n, minutos=7 * 60, deitou_h=1):
    dias = []
    for i in range(n, 0, -1):
        d = DIA - timedelta(days=i)
        ini = _dt(deitou_h, 0, d)
        dias.append(_noite(d, ini, ini + timedelta(minutes=minutos), fc=60))
    return dias


# ---------------------------------------------------------------------------------------------
# Quando a noite está fechada
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("fim, minutos, cobertura, esperado", [
    ((9, 53), 480, (10, 30), False),   # ainda não passou uma hora do fim
    ((9, 53), 480, (10, 55), True),
    ((3, 52), 197, (6, 0), False),     # acordou de madrugada: pode voltar a dormir
    ((3, 52), 197, (12, 1), True),     # meio-dia fecha qualquer noite
    ((8, 0), 180, (9, 30), False),     # noite curta espera o meio-dia
])
def test_noite_fechada(fim, minutos, cobertura, esperado):
    f = _dt(*fim)
    dia = _noite(DIA, f - timedelta(minutes=minutos), f, minutos)
    assert sono.noite_fechada(dia, _dt(*cobertura)) is esperado


def test_sem_noite_ou_sem_cobertura_nao_fecha():
    assert not sono.noite_fechada(relogio.Dia(data=DIA), _dt(13))
    assert not sono.noite_fechada(_noite(DIA, _dt(1), _dt(9)), None)


# ---------------------------------------------------------------------------------------------
# Mensagem
# ---------------------------------------------------------------------------------------------

def test_mensagem_compara_com_a_media_e_lembra_do_planejamento():
    dia = _noite(DIA, _dt(3, 10), _dt(8, 10), fc=66)
    texto = sono.mensagem(dia, _anteriores(7), pc_ate=_dt(2, 50))
    assert texto.splitlines()[0] == "🌙 Noite de seg para ter"
    assert "5h00 dormindo, das 03:10 às 08:10." in texto
    assert "Média das 7 noites anteriores: 7h00 (2h00 abaixo)." in texto
    assert "Adormeceu 2h10 mais tarde que de costume (01:00)." in texto
    assert "FC de repouso 66 (média 60)." in texto
    assert "PC: último uso às 02:50, 20 min antes de dormir." in texto
    assert texto.endswith("/planejamento")


def test_mensagem_de_noite_boa_nao_sugere_nada():
    dia = _noite(DIA, _dt(1, 0), _dt(8, 30))
    texto = sono.mensagem(dia, _anteriores(4), pc_ate=None)
    assert "(na média)" not in texto and "acima" in texto
    assert "mais tarde" not in texto and "mais cedo" not in texto
    assert "/planejamento" not in texto
    assert "PC:" not in texto


def test_mensagem_sem_historico_diz_quando_a_comparacao_comeca():
    texto = sono.mensagem(_noite(DIA, _dt(0, 30), _dt(7, 30)), _anteriores(2), None)
    assert "começa com 3 noites anteriores (há 2)" in texto


# ---------------------------------------------------------------------------------------------
# PC
# ---------------------------------------------------------------------------------------------

def test_pc_da_noite_para_na_hora_de_dormir():
    dia = _noite(DIA, _dt(1, 30), _dt(9))
    ontem = DIA - timedelta(days=1)
    trechos = [(_dt(16, 0, ontem), _dt(17, 0, ontem)),   # antes das 18h: fora
               (_dt(22, 0, ontem), _dt(23, 30, ontem)),
               (_dt(23, 50, ontem), _dt(0, 40)),
               (_dt(1, 20), _dt(2, 0))]                    # passa da hora de dormir: corta
    p = sono.pc_da_noite(dia, trechos)
    assert p.ultimo == _dt(1, 30)
    assert p.depois_da_meia_noite_min == 40 + 10


def test_relatorio_pc_agrupa_por_faixa(tmp_path):
    noites = [_noite(DIA - timedelta(days=2), _dt(0, 30, DIA - timedelta(days=2)), _dt(8, 30, DIA - timedelta(days=2))),
              _noite(DIA - timedelta(days=1), _dt(3, 0, DIA - timedelta(days=1)), _dt(9, 0, DIA - timedelta(days=1))),
              _noite(DIA, _dt(1, 0), _dt(8, 0))]
    trechos = [(_dt(23, 0, DIA - timedelta(days=3)), _dt(23, 50, DIA - timedelta(days=3))),
               (_dt(2, 0, DIA - timedelta(days=1)), _dt(2, 40, DIA - timedelta(days=1)))]
    caminho = sono.relatorio_pc(noites, trechos, tmp_path)
    texto = caminho.read_text(encoding="utf-8")
    assert caminho == tmp_path / "atividade" / "Sono e PC.md"
    assert "| PC até 0h | 1 | 8h00 | 00:30 |" in texto
    assert "| PC depois das 2h | 1 | 6h00 | 03:00 |" in texto
    assert "| sem PC à noite | 1 | 7h00 | 01:00 |" in texto
    assert "Ainda poucos dados" in texto
    assert "| ter 13/10 | — | — | — | 01:00 | 7h00 |" in texto
    assert "- private" in texto


# ---------------------------------------------------------------------------------------------
# Diário
# ---------------------------------------------------------------------------------------------

@pytest.fixture
def notas(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "NOTES_DIR", tmp_path / "capturas")
    return tmp_path / "capturas"


def _diario(d, **fm):
    caminho = diario.caminho_nota(Route.DIARIO, d)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    linhas = "".join(f"{k}: {v!r}\n" if isinstance(v, str) else f"{k}: {v}\n" for k, v in fm.items())
    caminho.write_text(f"---\n{linhas}---\n\n# Diário\n", encoding="utf-8")


def test_relatorio_diario_fica_na_pasta_do_diario(notas):
    d1, d2, d3 = DIA - timedelta(days=2), DIA - timedelta(days=1), DIA
    noites = [_noite(d1, _dt(2, 0, d1), _dt(7, 0, d1)),     # 5h: curta
              _noite(d2, _dt(0, 0, d2), _dt(8, 0, d2)),     # 8h
              _noite(d3, _dt(1, 0, d3), _dt(8, 30, d3))]    # sem diário
    _diario(d1, humor_nota=2, energia=2, sono="5–6h", estresse="alto", dia_foi="pesado")
    _diario(d2, humor_nota=4, energia=5, sono="<5h")

    caminho = sono.relatorio_diario(noites)
    assert caminho == notas / "Diario" / "Sono e Diário.md"
    texto = caminho.read_text(encoding="utf-8")
    assert "| menos de 6h | 1 | 2,0 | 2,0 |" in texto
    assert "| 6h ou mais | 1 | 4,0 | 5,0 |" in texto
    assert "Em 1 de 2 dias a faixa marcada no /diario bate com a do relógio." in texto
    assert "Ainda poucos dados" in texto
    assert "ter 13/10" not in texto
    assert "- private" in texto


def test_sem_diario_nao_escreve(notas):
    assert sono.relatorio_diario([_noite(DIA, _dt(1), _dt(8))]) is None
    assert not (notas / "Diario").exists()


def test_faixa_do_relogio_na_escala_do_diario():
    assert [sono._faixa_do_relogio(m) for m in (299, 300, 389, 390, 509, 510)] == \
        ["<5h", "5–6h", "5–6h", "7–8h", "7–8h", ">8h"]


# ---------------------------------------------------------------------------------------------
# Gatilho: banco novo
# ---------------------------------------------------------------------------------------------

def _banco(tmp_path):
    caminho = tmp_path / "Gadgetbridge.db"
    con = sqlite3.connect(caminho)
    con.execute(
        "CREATE TABLE HUAWEI_ACTIVITY_SAMPLE (TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER, "
        "OTHER_TIMESTAMP INTEGER, SOURCE INTEGER, RAW_KIND INTEGER, RAW_INTENSITY INTEGER, STEPS INTEGER, "
        "CALORIES INTEGER, DISTANCE INTEGER, SPO INTEGER, HEART_RATE INTEGER, RESTING_HEART_RATE INTEGER)"
    )
    sql = "INSERT INTO HUAWEI_ACTIVITY_SAMPLE VALUES (?,1,1,?,13,?,1,-1,-1,-1,-1,-1,-1)"
    con.execute(sql, (int(_dt(0, 35).timestamp()), int(_dt(3, 52).timestamp()), relogio.KIND_LEVE))
    con.execute(sql, (int(_dt(5, 6).timestamp()), int(_dt(9, 53).timestamp()), relogio.KIND_PROFUNDO))
    ts = int(_dt(11).timestamp())
    con.execute("INSERT INTO HUAWEI_ACTIVITY_SAMPLE VALUES (?,1,1,?,11,-1,1,300,0,0,-1,80,60)", (ts, ts + 60))
    con.commit()
    con.close()
    return caminho


@pytest.fixture
def ambiente(tmp_path, monkeypatch, notas):
    db = _banco(tmp_path)
    monkeypatch.setattr(config, "GADGETBRIDGE_DB", db)
    monkeypatch.setattr(config, "VAULT_DIR", tmp_path / "vault")
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(sono, "_ultima_checagem", 0.0)
    enviadas = []

    def avisar(texto):
        enviadas.append(texto)
        return True

    def checar(agora):
        sono._ultima_checagem = 0.0
        sono.verificar(agora, avisar, uso_pc=lambda ini, fim: [(_dt(0, 0), _dt(0, 20))])

    return db, enviadas, checar


def test_banco_novo_manda_a_mensagem_uma_vez(ambiente, tmp_path):
    db, enviadas, checar = ambiente
    checar(_dt(11, 30))
    assert len(enviadas) == 1
    assert "8h04 dormindo, das 00:35 às 09:53, com 1 interrupção (1h14 acordado)." in enviadas[0]
    assert "PC: último uso às 00:20, 15 min antes de dormir." in enviadas[0]
    assert (tmp_path / "vault" / "atividade" / "Relógio — 2026-10-13.md").exists()
    assert (tmp_path / "vault" / "atividade" / "Sono e PC.md").exists()

    checar(_dt(11, 45))                      # mesmo banco: nada
    st = db.stat()
    os.utime(db, (st.st_atime, st.st_mtime + 3600))
    checar(_dt(12, 45))                      # banco novo, mesmo dia: não repete
    assert len(enviadas) == 1


def test_noite_ainda_aberta_espera_o_proximo_banco(ambiente):
    db, enviadas, checar = ambiente
    con = sqlite3.connect(db)
    con.execute("DELETE FROM HUAWEI_ACTIVITY_SAMPLE WHERE SOURCE = 11")
    ts = int(_dt(10, 15).timestamp())  # a sincronização parou 22 min depois de acordar
    con.execute("INSERT INTO HUAWEI_ACTIVITY_SAMPLE VALUES (?,1,1,?,11,-1,1,300,0,0,-1,80,60)", (ts, ts + 60))
    con.commit()
    con.close()
    checar(_dt(10, 30))
    assert enviadas == []

    con = sqlite3.connect(db)
    ts = int(_dt(11).timestamp())
    con.execute("INSERT INTO HUAWEI_ACTIVITY_SAMPLE VALUES (?,1,1,?,11,-1,1,300,0,0,-1,80,60)", (ts, ts + 60))
    con.commit()
    con.close()
    st = db.stat()
    os.utime(db, (st.st_atime, st.st_mtime + 3600))
    checar(_dt(11, 10))
    assert len(enviadas) == 1
    assert sono._ler_estado()["enviada"] == DIA.isoformat()


def test_depois_das_18h_ainda_registra(ambiente):
    # 28/10/2026: o banco chegava depois das 18h e a mensagem nunca entrava no registro (LOG 4).
    _, enviadas, checar = ambiente
    checar(_dt(19, 0))
    assert len(enviadas) == 1


def test_noite_fechada_vira_um_evento_de_sono_uma_vez_so(ambiente, caplog):
    """LOG 4: com um banco de exemplo de noite fechada, o caminho de verdade (o `_avisar` da
    produção) grava 1 evento `origem: sono`, o sono.json ganha `enviada`, e outra passada não repete."""
    import logging
    from logos import eventos
    db, _, _ = ambiente

    def passar(agora):
        sono._ultima_checagem = 0.0
        sono.verificar(agora, uso_pc=lambda ini, fim: [(_dt(0, 0), _dt(0, 20))])  # avisar = o de produção

    with caplog.at_level(logging.INFO, logger="logos.sono"):
        passar(_dt(11, 30))
    evs = [e for e in eventos.ultimos() if e["origem"] == "sono"]
    assert len(evs) == 1 and "8h04 dormindo" in evs[0]["texto"]
    assert sono._ler_estado()["enviada"] == DIA.isoformat()
    assert "noite de 13/10 registrada" in caplog.text and "8h04" not in caplog.text
    st = db.stat()
    os.utime(db, (st.st_atime, st.st_mtime + 3600))
    passar(_dt(12, 30))
    assert len([e for e in eventos.ultimos() if e["origem"] == "sono"]) == 1


def test_noite_que_nao_fecha_fica_no_log(ambiente, caplog):
    import logging
    db, enviadas, checar = ambiente
    con = sqlite3.connect(db)
    con.execute("DELETE FROM HUAWEI_ACTIVITY_SAMPLE WHERE SOURCE = 11")
    ts = int(_dt(10, 15).timestamp())
    con.execute("INSERT INTO HUAWEI_ACTIVITY_SAMPLE VALUES (?,1,1,?,11,-1,1,300,0,0,-1,80,60)", (ts, ts + 60))
    con.commit()
    con.close()
    with caplog.at_level(logging.INFO, logger="logos.sono"):
        checar(_dt(10, 30))
    assert enviadas == [] and "noite de 13/10 ainda não fechou" in caplog.text
    assert "manha" not in sono._ler_estado()  # LOG 12: sem noite fechada, nenhuma linha


def test_noite_fechada_deixa_o_numero_da_manha(ambiente):
    """LOG 12 (D3 = (a)): a noite fechada deixa só o número em `sono.json` para a linha `manha`."""
    _, _, checar = ambiente
    checar(_dt(11, 30))
    assert sono._ler_estado()["manha"] == {"dia": DIA.isoformat(), "sono_min": 8 * 60 + 4}
    assert sono.manha_de_hoje(_dt(11, 30)) == {"dia": DIA.isoformat(), "sono_min": 484}
    from datetime import timedelta
    assert sono.manha_de_hoje(_dt(11, 30) + timedelta(days=1)) is None  # amanhã, a noite de hoje é velha


def test_sem_banco_configurado_nao_faz_nada(monkeypatch):
    monkeypatch.setattr(config, "GADGETBRIDGE_DB", None)
    monkeypatch.setattr(sono, "_ultima_checagem", 0.0)
    sono.verificar(_dt(11), lambda t: pytest.fail("não devia avisar"))
