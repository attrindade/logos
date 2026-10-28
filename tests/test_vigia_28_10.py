"""Checagens novas do Vigia (LOG 3, 28/10/2026): relógio, coletores do ActivityWatch, briefing de hoje
e os dois códigos de desligamento."""
import sqlite3
from datetime import datetime

from logos import config, vigia

# Momento das fixtures: 28/10/2026 11:30, como no levantamento.
AGORA = datetime(2026, 10, 28, 11, 30).timestamp()


def _banco(tmp_path, ultima: datetime | None):
    db = tmp_path / "Gadgetbridge.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE HUAWEI_ACTIVITY_SAMPLE (TIMESTAMP INTEGER, HEART_RATE INTEGER)")
    if ultima is not None:
        con.execute("INSERT INTO HUAWEI_ACTIVITY_SAMPLE VALUES (?, 60)", (int(ultima.timestamp()) - 86400,))
        con.execute("INSERT INTO HUAWEI_ACTIVITY_SAMPLE VALUES (?, 60)", (int(ultima.timestamp()),))
    con.commit()
    con.close()
    return db


# --------------------------------------------------------------------------- relógio

def test_relogio_parado_desde_22_10_e_atencao(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VIGIA_RELOGIO", True)
    db = _banco(tmp_path, datetime(2026, 10, 22, 10, 22))
    it = vigia.chk_relogio(AGORA, db)
    assert it["estado"] == "atencao" and it["detalhe"].startswith("Sem dados desde 22/10")
    assert it["nome"] == "Relógio"


def test_relogio_em_dia_e_ok_e_o_banco_fica_intacto(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VIGIA_RELOGIO", True)
    db = _banco(tmp_path, datetime(2026, 10, 28, 8, 10))
    antes = db.read_bytes()
    assert vigia.chk_relogio(AGORA, db)["estado"] == "ok"
    assert db.read_bytes() == antes  # aberto só para leitura


def test_relogio_desligado_ou_sem_banco_some_da_lista(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VIGIA_RELOGIO", False)
    assert vigia.chk_relogio(AGORA, _banco(tmp_path, None)) is None
    monkeypatch.setattr(config, "VIGIA_RELOGIO", True)
    monkeypatch.setattr(config, "GADGETBRIDGE_DB", None)
    assert vigia.chk_relogio(AGORA) is None
    assert vigia.chk_relogio(AGORA, tmp_path / "nao-existe.db")["estado"] == "atencao"
    vazio = tmp_path / "vazio"
    vazio.mkdir()
    assert vigia.chk_relogio(AGORA, _banco(vazio, None))["estado"] == "atencao"  # banco vazio


# --------------------------------------------------------------------------- coletores do AW

def _buckets():
    # Os de 28/10 (GET /api/0/buckets/, só as datas).
    return {
        "aw-watcher-afk_att": {"last_updated": "2026-10-28T14:47:53+00:00"},
        "aw-watcher-window_att": {"last_updated": "2026-10-28T14:47:57+00:00"},
        "aw-watcher-input_att": {"last_updated": "2026-10-08T19:05:45+00:00"},
        "aw-watcher-web-chrome": {"last_updated": "2026-09-14T19:23:42+00:00"},  # o velho, sem _att
        "aw-watcher-web-chrome_att": {"last_updated": "2026-10-28T14:47:34+00:00"},
        "aw-watcher-obsidian_att": {"last_updated": None},
    }


def test_coletores_de_28_10_input_parado_e_obsidian_vazio(monkeypatch):
    monkeypatch.setattr(config, "VIGIA_AW_ESPERADOS",
                        ["aw-watcher-afk_", "aw-watcher-input_", "aw-watcher-web-chrome_", "aw-watcher-obsidian_"])
    it = vigia.chk_aw_coletores(AGORA, _buckets())
    assert it["estado"] == "atencao"
    assert "Input parado desde 08/10" in it["detalhe"] and "obsidian nunca registrou" in it["detalhe"]
    assert "chrome" not in it["detalhe"] and "afk" not in it["detalhe"]


def test_pc_desligado_no_fim_de_semana_nao_vira_alarme(monkeypatch):
    monkeypatch.setattr(config, "VIGIA_AW_ESPERADOS", ["aw-watcher-afk_"])
    b = {"aw-watcher-window_att": {"last_updated": "2026-10-24T22:00:00+00:00"},
         "aw-watcher-afk_att": {"last_updated": "2026-10-24T21:59:00+00:00"}}
    # Segunda de manhã, 3 dias depois: a referência é a última janela, não o relógio de parede.
    assert vigia.chk_aw_coletores(datetime(2026, 10, 27, 10, 0).timestamp(), b)["estado"] == "ok"


def test_coletor_esperado_que_nao_existe_e_atencao(monkeypatch):
    monkeypatch.setattr(config, "VIGIA_AW_ESPERADOS", ["aw-watcher-input_"])
    it = vigia.chk_aw_coletores(AGORA, {"aw-watcher-window_att": {"last_updated": "2026-10-28T14:00:00+00:00"}})
    assert it["estado"] == "atencao" and "não existe" in it["detalhe"]


def test_aw_fora_do_ar_nao_duplica_o_aviso(monkeypatch):
    monkeypatch.setattr(vigia, "_get_json", lambda *a, **k: (_ for _ in ()).throw(OSError("recusada")))
    assert vigia.chk_aw_coletores(AGORA) is None


# --------------------------------------------------------------------------- briefing de hoje

def test_briefing_que_nao_saiu_as_11h(tmp_path):
    (tmp_path / "briefings").mkdir()
    boot = datetime(2026, 10, 28, 10, 0).timestamp()
    it = vigia.chk_briefing(AGORA, boot, tmp_path)
    assert it == vigia._item("briefing", "Briefing de hoje", "atencao", "Briefing de hoje não saiu.")


def test_briefing_que_saiu_ou_ainda_cedo_ou_pc_ligado_tarde(tmp_path):
    (tmp_path / "briefings").mkdir()
    cedo = datetime(2026, 10, 28, 10, 45).timestamp()
    boot = datetime(2026, 10, 28, 8, 0).timestamp()
    assert vigia.chk_briefing(cedo, boot, tmp_path) is None  # antes das 11h nem aparece
    meia_noite = datetime(2026, 10, 29, 0, 5).timestamp()  # a atenção de ontem não vira "voltou ao normal"
    assert vigia.chk_briefing(meia_noite, boot, tmp_path) is None
    tarde = datetime(2026, 10, 28, 10, 40).timestamp()
    assert vigia.chk_briefing(AGORA, tarde, tmp_path)["estado"] == "ok"
    (tmp_path / "briefings" / "2026-10-28.md").write_text("x", encoding="utf-8")
    assert vigia.chk_briefing(AGORA, boot, tmp_path) == vigia._item("briefing", "Briefing de hoje", "ok", "Saiu.")


# --------------------------------------------------------------------------- códigos de desligamento

def test_codigos_de_desligamento_sao_normais():
    lista = [{"nome": n, "estado": "Ready", "ultima": "2026-10-27T23:10:00-03:00", "proxima": None, "resultado": r}
             for n, r in (("AW Alertas", 1073807364), ("AW Checkin", 3221225786), ("AW Negativo", -1073741510))]
    for it in vigia.chk_tarefas(lista, AGORA):
        assert it["estado"] == "ok" and "desligamento" in it["detalhe"], it


def test_checagem_nova_com_defeito_nao_derruba_as_outras(monkeypatch):
    monkeypatch.setattr(vigia, "processos", lambda: [])
    monkeypatch.setattr(vigia, "_get_json", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    monkeypatch.setattr(vigia, "chk_disco", lambda: vigia._item("disco", "Disco C:", "ok"))
    monkeypatch.setattr(vigia, "chk_relogio", lambda agora: 1 / 0)
    itens = vigia.checar(AGORA, lista=[])
    assert not [i for i in itens if i["id"] == "relogio"]
    assert [i for i in itens if i["id"] == "watcher"]
