"""Assistente do dia do Planeja: prompt com o plano certo, cortes sem Claude, 422 volta ao Claude."""
from datetime import datetime

import pytest

from logos import dia_celular as dc

AGORA = datetime(2026, 10, 15, 12, 10, tzinfo=dc.FUSO)

SEMANA = {
    "dias": [
        {"id": "2026-10-14", "min": "A reunião.", "blocos": [
            {"i": "09:00", "f": "10:00", "k": "gov", "nome": "Feito", "tarefas": [{"id": "a", "t": "feita"}]},
            {"i": "14:00", "f": "16:00", "k": "ren", "nome": "Capas", "tarefas": [{"id": "b", "t": "capa do vídeo"}]},
        ]},
        {"id": "2026-10-15", "min": "O roteiro.", "blocos": [
            {"i": "13:30", "f": "16:00", "k": "gov", "nome": "Roteiro", "tag": "primeiro",
             "tarefas": [{"id": "c", "t": "rascunho do roteiro"}]},
            {"i": "16:00", "f": "16:30", "k": "pausa", "nome": "Pausa"},
        ]},
    ],
    "checks": {"a": True},
    "cortadas": [],
}


def _semana_falsa(monkeypatch, recusas=0):
    feitos = []
    estado = {"recusas": recusas}

    def http(metodo, caminho, key, corpo=None):
        if metodo == "GET":
            return {"/api/semana": SEMANA, "/api/dia": {"conversa": []}, "/api/remarcar": {"remarcar": []}}[caminho]
        feitos.append(corpo)
        if corpo.get("estado") == "pronta" and estado["recusas"]:
            estado["recusas"] -= 1
            raise dc.SemanaRecusou("não há bloco às 18:00 em 2026-10-15")
        return {"ok": True, "estado": "aplicada" if corpo.get("ops") and corpo["id"].startswith("f") else corpo["estado"]}

    monkeypatch.setattr(dc, "_http", http)
    monkeypatch.setattr(dc, "ordem_de_corte", lambda *a: ["Contagem de empresas"])
    return feitos


def test_plano_em_texto_mostra_so_o_que_ficou_dos_dias_que_passaram():
    t = dc.plano_em_texto(SEMANA, AGORA)
    assert "Feito" not in t and "b :: capa do vídeo" in t
    assert "Mínimo do dia: O roteiro." in t and "Mínimo do dia: A reunião." not in t
    assert "13:30–16:00 [gov] Roteiro · primeiro" in t


def test_fala_do_dia_vira_pronta_com_as_ops_do_claude(monkeypatch):
    feitos = _semana_falsa(monkeypatch)
    vistos = []

    def claude(prompt):
        vistos.append(prompt)
        return {"resposta": "Passe o roteiro para as 14h.", "mudancas": ["Roteiro às 14h"],
                "ops": [{"op": "horario", "dia": "2026-10-15", "bloco": "13:30", "i": "14:00", "f": "16:00", "para": ""}]}

    fala = {"id": "d-00000001", "tipo": "dia", "dia": "2026-10-15", "texto": "a reunião foi até meio-dia", "contexto": None}
    assert dc.atender("k", fala, claude=claude, agora=AGORA) == "pronta"
    assert "a reunião foi até meio-dia" in vistos[0] and "quinta 2026-10-15, 12:10" in vistos[0]
    assert "1. Contagem de empresas" in vistos[0]
    assert feitos[0] == {"id": "d-00000001", "estado": "pensando"}
    assert feitos[1]["ops"] == [{"op": "horario", "dia": "2026-10-15", "bloco": "13:30", "i": "14:00", "f": "16:00"}]


def test_fechamento_corta_sem_claude_e_o_422_volta_ao_claude(monkeypatch):
    feitos = _semana_falsa(monkeypatch, recusas=1)
    prompts = []

    def claude(prompt):
        prompts.append(prompt)
        return {"resposta": "Amanhã começa pelo roteiro.", "mudancas": [], "ops": []}

    fala = {"id": "f-00000001", "tipo": "fechamento", "dia": "2026-10-14", "texto": "",
            "contexto": {"decisoes": [{"tarefa": "b", "destino": "cortar"}, {"tarefa": "c", "destino": "amanha"}]}}
    assert dc.atender("k", fala, claude=claude, agora=AGORA) == "aplicada"
    assert len(prompts) == 2 and "A página recusou as operações" in prompts[1]
    assert "- c → amanhã" in prompts[0] and "(não use estes ids): b." in prompts[0]
    final = feitos[-1]
    assert final["ops"][0] == {"op": "cortar", "tarefa": "b"}
    assert final["mudancas"][0] == "Sai da semana: capa do vídeo"


def test_duas_recusas_viram_falha(monkeypatch):
    feitos = _semana_falsa(monkeypatch, recusas=2)
    fala = {"id": "d-00000002", "tipo": "dia", "dia": "2026-10-15", "texto": "x", "contexto": None}
    claude = lambda p: {"resposta": "r", "mudancas": [], "ops": [{"op": "tirar", "dia": "2026-10-15", "bloco": "18:00"}]}
    assert dc.atender("k", fala, claude=claude, agora=AGORA) == "falha"
    assert feitos[-1]["estado"] == "falha" and "18:00" in feitos[-1]["resposta"]


def test_ordem_de_corte_lida_do_index(tmp_path):
    p = tmp_path / "index.html"
    p.write_text('<ol class="corte">\n<li>Um <b>x</b>.</li>\n<li>Dois.</li></ol>', encoding="utf-8")
    assert dc.ordem_de_corte(p) == ["Um x.", "Dois."]
    assert dc.ordem_de_corte(tmp_path / "nao.html") == []


def test_passo_nao_abre_segunda_thread(monkeypatch):
    monkeypatch.setattr(dc, "_http", lambda *a, **k: pytest.fail("não devia buscar"))
    assert dc._trabalhando.acquire(blocking=False)
    try:
        assert dc.passo("k") == 0
    finally:
        dc._trabalhando.release()
