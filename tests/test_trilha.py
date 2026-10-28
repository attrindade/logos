"""A trilha das capturas do celular (LOG 9 da onda 4, 28/10/2026): só horários e id curto, sem mudar
o caminho da captura, e a atenção do Vigia quando passa de 30 min com o PC ligado."""
import json
import logging
from datetime import datetime

import pytest

from logos import config, organiza, trilha, vigia


@pytest.fixture
def estado(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    return tmp_path


def ts(h, m, s=0):
    return datetime(2026, 10, 27, h, m, s).timestamp()


def cap(cid="1a2b3c4d-celular-1", aberta="2026-10-27T14:33:50", fechada="2026-10-27T14:34:10"):
    return {"id": cid, "tipo": "nota", "aberta_em": aberta, "fechada_em": fechada,
            "partes": [{"id": "p1", "ordem": 1, "tipo": "texto", "texto": "conteúdo que não vai para a trilha"}]}


def _ciclo(t, descanso=True):
    trilha.volta(t)
    if descanso:
        trilha.descanso(t + 5)


# ---- PC ligado: as voltas do drenador ----

def test_ligado_desde_so_zera_quando_o_drenador_ficou_parado(estado, caplog):
    _ciclo(ts(9, 0))
    _ciclo(ts(9, 1))
    assert trilha.ligado_desde() == ts(9, 0)
    with caplog.at_level(logging.INFO, logger="logos.trilha"):
        _ciclo(ts(11, 0))  # duas horas sem volta: PC dormindo ou desligado
    assert trilha.ligado_desde() == ts(11, 0)
    assert "sem volta" in caplog.text


def test_volta_longa_nao_zera_o_pc_ligado(estado, caplog):
    """O drenador ocupado (ou travado num pedido) 90 min: o PC estava ligado, e isso vai para o log."""
    _ciclo(ts(14, 0))
    trilha.volta(ts(14, 1))
    trilha.descanso(ts(15, 31))  # a volta levou 90 min
    with caplog.at_level(logging.INFO, logger="logos.trilha"):
        trilha.volta(ts(15, 32))
    assert trilha.ligado_desde() == ts(14, 0)
    assert "a volta anterior levou 91 min" in caplog.text


# ---- os quatro horários ----

def test_trilha_guarda_so_horarios_e_id_curto(estado, caplog):
    _ciclo(ts(14, 0))
    trilha.vistas([cap()], ts(14, 35))
    trilha.vistas([cap()], ts(14, 36))  # a 2ª vez não muda a hora em que foi vista
    trilha.no_pc("1a2b3c4d-celular-1", ts(14, 36, 30))
    with caplog.at_level(logging.INFO, logger="logos.trilha"):
        trilha.nota("1a2b3c4d-celular-1", ts(14, 38))
    bruto = trilha.caminho().read_text(encoding="utf-8")
    assert "conteúdo" not in bruto and "celular-1" not in bruto
    t = json.loads(bruto)["capturas"]["1a2b3c4d"]
    assert t["celular"] == ts(14, 34, 10) and t["aberta"] == ts(14, 33, 50)
    assert t["vista"] == ts(14, 35) and t["no_pc"] == ts(14, 36, 30) and t["nota"] == ts(14, 38)
    assert t["ligado_desde"] == ts(14, 0)
    assert ("Trilha 1a2b3c4d: celular 14:34:10 → vista 14:35:00 → no PC 14:36:30 → nota 14:38:00 "
            "(4 min do celular à nota; PC ligado desde 14:00:00)") in caplog.text


def test_drenador_marca_vista_no_pc_e_nota_sem_pedido_a_mais(estado, monkeypatch):
    pedidos = []

    def falso(metodo, url, key, corpo=None):
        pedidos.append((metodo, url.rsplit("/", 1)[-1]))
        if url.endswith("/pendentes") or "/pendentes?" in url:
            return {"capturas": [cap()]} if sum(1 for p in pedidos if p[1].startswith("pendentes")) == 1 else {"capturas": []}
        return {"ok": True}

    monkeypatch.setattr(organiza, "_pedido", falso)
    monkeypatch.setattr(organiza, "caminho_chave_existe", lambda: False)
    monkeypatch.setattr(organiza, "fechar_descartadas", lambda key: 0)
    ledger = estado / "ledger.json"
    assert organiza.passo("k", estado / "Inbox", estado / "m", ledger) == (1, 0)
    assert [p for p in pedidos if p[0] != "GET"] == [("PUT", "estado"), ("PUT", "estado")]  # pegando, pc
    nota = estado / "nota.md"
    nota.write_text("---\ntitulo: T\n---\n", encoding="utf-8")
    nome = organiza.nome_pacote(cap())
    ledger.write_text(json.dumps({"x": {"filename": nome, "state": "enriched", "note_path": str(nota)}}), encoding="utf-8")
    assert organiza.passo("k", estado / "Inbox", estado / "m", ledger) == (0, 1)
    t = trilha.ler()["capturas"]["1a2b3c4d"]
    assert t["vista"] <= t["no_pc"] <= t["nota"]


def test_trilha_estragada_nao_derruba_o_drenador(estado):
    trilha.caminho().parent.mkdir(parents=True)
    trilha.caminho().write_text("[1, 2", encoding="utf-8")
    trilha.volta(ts(9, 0))
    trilha.vistas([cap(), {"id": None}, {}], ts(9, 1))
    trilha.no_pc("x", ts(9, 2))
    trilha.nota("x", ts(9, 3))
    trilha.caminho().write_text('{"capturas": {"1a2b3c4d": "não é dict"}}', encoding="utf-8")
    trilha.nota("1a2b3c4d", ts(9, 4))  # o erro vira DEBUG, nada sobe
    assert vigia._seguro(vigia.chk_trilha, ts(9, 5)) is None  # no Vigia, a checagem só some da lista


# ---- a atenção do Vigia ----

def test_94_min_com_o_pc_ligado_vira_atencao(estado):
    """O caso de 27/10: falada às 14h34, no PC às 16h08, com o drenador rodando o tempo todo."""
    _ciclo(ts(9, 0))
    trilha.vistas([cap()], ts(16, 7))
    trilha.no_pc(cap()["id"], ts(16, 8, 10))
    _ciclo(ts(16, 9))
    it = vigia.chk_trilha(ts(16, 10))
    assert it["estado"] == "atencao" and it["id"] == "trilha"
    assert it["detalhe"] == "A captura 1a2b3c4d levou 94 min do celular ao PC, com o PC ligado (vista na Semana às 16:07)."
    assert "conteúdo" not in json.dumps(it)


def test_pc_dormindo_nao_conta(estado):
    _ciclo(ts(9, 0))
    _ciclo(ts(16, 5))  # 7 h sem volta: o PC estava dormindo
    trilha.vistas([cap()], ts(16, 6))
    trilha.no_pc(cap()["id"], ts(16, 7))
    assert vigia.chk_trilha(ts(16, 10))["estado"] == "ok"


def test_esperando_na_semana_ha_mais_de_30_min_vira_atencao_e_some_quando_sai_da_lista(estado):
    _ciclo(ts(14, 0))
    trilha.vistas([cap()], ts(14, 35))
    trilha.descanso(ts(15, 9))  # o drenador seguiu rodando (uma volta longa, sem parar)
    trilha.volta(ts(15, 10))
    trilha.vistas([cap()], ts(15, 10))  # continua na lista
    it = vigia.chk_trilha(ts(15, 11))
    assert it["estado"] == "atencao" and "espera há 37 min na Semana" in it["detalhe"]
    trilha.volta(ts(15, 11))
    trilha.vistas([], ts(15, 11))  # saiu da lista sem vir (vazia, falha definitiva, pulada)
    assert vigia.chk_trilha(ts(15, 12))["estado"] == "ok"


def test_atraso_resolvido_some_depois_de_6_h(estado):
    _ciclo(ts(9, 0))
    trilha.vistas([cap()], ts(16, 7))
    trilha.no_pc(cap()["id"], ts(16, 8))
    assert vigia.chk_trilha(ts(22, 0))["estado"] == "atencao"
    assert vigia.chk_trilha(ts(22, 9))["estado"] == "ok"


def test_sem_trilha_o_vigia_nao_mostra_o_item(estado):
    assert vigia.chk_trilha(ts(9, 0)) is None
