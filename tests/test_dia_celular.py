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
    assert "- c → para 2026-10-15 (quinta, o próximo dia de trabalho)" in prompts[0] and "(não use estes ids): b." in prompts[0]
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


# ---------------------------------------------------------------- rodada de 25/10/2026 (noite, manhã)

MADRUGADA = datetime(2026, 10, 22, 1, 30, tzinfo=dc.FUSO)  # quinta 01:30: o dia lógico ainda é quarta

SEMANA_NOVA = {
    "recursos": ["guardar", "resumo", "encaixe", "propostas", "fora", "incorpora"],
    "semana": {"titulo": "Semana de 19/10", "corte": ["Contagem nova", "Leituras"]},
    "dias": [
        {"id": "2026-10-21", "min": "texto antigo", "blocos": [
            {"i": "10:00", "f": "10:20", "k": "leve", "nome": "WhatsApp", "hab": "whats"},
            {"i": "10:20", "f": "12:00", "k": "ren", "nome": "Renato", "tarefas": [
                {"id": "r1", "t": "Mandar o roteiro 2", "min": "roteiro 2 ao Renato"}, {"id": "r2", "t": "Cobrar o 1"}]},
            {"i": "15:30", "f": "16:30", "k": "leve", "nome": "Imprevistos", "folga": True},
            {"i": "18:00", "f": "19:00", "k": "gov", "nome": "Reunião", "dl": True, "tarefas": [{"id": "g1", "t": "TEMAS"}]},
        ]},
        {"id": "2026-10-22", "blocos": [
            {"i": "10:00", "f": "12:00", "k": "gov", "nome": "Pós-defeso", "cont": "gov-posdefeso",
             "tarefas": [{"id": "g2", "t": "rodar URLs"}]},
        ]},
        {"id": "2026-10-23", "blocos": [
            {"i": "10:00", "f": "12:00", "k": "ppj", "nome": "PPJ", "tarefas": [{"id": "p1", "t": "fechar PPJ"}]},
        ]},
    ],
    "checks": {},
    "cortadas": [],
    "guardadas": [{"id": "x9", "t": "Ler o artigo", "dia": "2026-10-20", "k": "pesq", "em": "2026-10-20T20:00:00Z"}],
}


def _http_com(monkeypatch, semana):
    feitos = []

    def http(metodo, caminho, key, corpo=None):
        if metodo == "GET":
            return {"/api/semana": semana, "/api/dia": {"conversa": []}, "/api/remarcar": {"remarcar": []}}[caminho]
        feitos.append(corpo)
        return {"ok": True, "estado": corpo.get("estado")}

    monkeypatch.setattr(dc, "_http", http)
    monkeypatch.setattr(dc, "ordem_de_corte", lambda *a: ["Do HTML"])
    return feitos


def test_p1_fechamento_da_madrugada_usa_o_para_e_o_dia_logico():
    fala = {"id": "f-1", "tipo": "fechamento", "dia": "2026-10-21", "texto": "",
            "contexto": {"decisoes": [{"tarefa": "r2", "destino": "amanha", "para": "2026-10-23"}]}}
    p = dc.montar_prompt(fala, SEMANA_NOVA, [], [], [], MADRUGADA)
    assert "- r2 → para 2026-10-23 (sexta" in p
    assert "2026-10-24" not in p  # o dia seguinte ao relógio + 1 nunca aparece
    assert "hoje ainda é quarta 2026-10-21" in p
    # à 1h30 a quarta ainda é hoje: o plano dela vem inteiro, não como "já passou"
    assert "## 2026-10-21 quarta\n" in dc.plano_em_texto(SEMANA_NOVA, MADRUGADA)


def test_p1_fala_antiga_sem_para_amanha_e_o_dia_seguinte_ao_da_fala():
    fala = {"id": "f-2", "tipo": "fechamento", "dia": "2026-10-21", "texto": "",
            "contexto": {"decisoes": [{"tarefa": "r2", "destino": "amanha"}]}}
    p = dc.montar_prompt(fala, SEMANA_NOVA, [], [], [], MADRUGADA)
    assert "- r2 → para 2026-10-22 (quinta" in p and "2026-10-23 (sexta" not in p


def test_p1_planeja_antigo_depois_da_meia_noite_amanha_e_pelo_dia_logico():
    # Revisão de 25/10, B7: o Planeja antigo manda em `dia` a data do relógio. Fechamento à 1h30 de
    # quinta (04:30 UTC): `dia` = quinta, mas o dia lógico é quarta, e o "amanhã" é quinta.
    fala = {"id": "f-2b", "tipo": "fechamento", "dia": "2026-10-22", "texto": "", "criada": "2026-10-22T04:30:00.000Z",
            "contexto": {"decisoes": [{"tarefa": "r2", "destino": "amanha"}]}}
    p = dc.montar_prompt(fala, SEMANA_NOVA, [], [], [], MADRUGADA)
    assert "- r2 → para 2026-10-22 (quinta" in p and "2026-10-23 (sexta" not in p
    # de dia (14h de quinta, 17:00 UTC), nada muda: o amanhã é sexta
    fala = dict(fala, criada="2026-10-22T17:00:00.000Z")
    assert "- r2 → para 2026-10-23 (sexta" in dc.montar_prompt(fala, SEMANA_NOVA, [], [], [], MADRUGADA)


def test_virada_as_4h():
    assert dc._hoje(datetime(2026, 10, 22, 3, 59, tzinfo=dc.FUSO)) == "2026-10-21"
    assert dc._hoje(datetime(2026, 10, 22, 4, 0, tzinfo=dc.FUSO)) == "2026-10-22"
    assert "vira às 4h" not in dc._linha_agora(datetime(2026, 10, 22, 4, 0, tzinfo=dc.FUSO))


def test_primeira_vira_uma_linha_do_prompt():
    fala = {"id": "f-3", "tipo": "fechamento", "dia": "2026-10-21", "texto": "",
            "contexto": {"decisoes": [], "primeira": "começar pelo relatório do Igor"}}
    p = dc.montar_prompt(fala, SEMANA_NOVA, [], [], [], MADRUGADA)
    assert ("Ele quer começar o próximo dia de trabalho por: começar pelo relatório do Igor; ponha isso no"
            " primeiro bloco de trabalho desse dia.") in p


def test_p2_guardar_vira_op_guardar_sem_claude(monkeypatch):
    feitos = _http_com(monkeypatch, SEMANA_NOVA)
    prompts = []

    def claude(p):
        prompts.append(p)
        return {"resposta": "ok", "mudancas": [], "ops": []}

    fala = {"id": "f-4", "tipo": "fechamento", "dia": "2026-10-21", "texto": "",
            "contexto": {"decisoes": [{"tarefa": "r2", "destino": "guardar"}, {"tarefa": "r1", "destino": "cortar"}]}}
    dc.atender("k", fala, claude=claude, agora=MADRUGADA)
    final = feitos[-1]
    assert final["ops"][:2] == [{"op": "guardar", "tarefa": "r2"}, {"op": "cortar", "tarefa": "r1"}]
    assert final["mudancas"][:2] == ["Vai para a semana que vem: Cobrar o 1", "Sai da semana: Mandar o roteiro 2"]
    assert "(não use estes ids): r2, r1." in prompts[0]


def test_p2_api_antiga_guardar_fica_sem_decisao(monkeypatch):
    # Revisão de 25/10, B8: virar "cortar" com a frase "Vai para a semana que vem" prometia o que a
    # API antiga não faz. Sem o recurso `guardar`, a tarefa fica onde está.
    antiga = {k: v for k, v in SEMANA_NOVA.items() if k not in ("recursos", "semana", "guardadas")}
    feitos = _http_com(monkeypatch, antiga)
    prompts = []

    def claude(p):
        prompts.append(p)
        return {"resposta": "ok", "mudancas": [],
                "ops": [{"op": "resumo", "dia": "2026-10-22", "tema": "x"}]}  # a API antiga não sabe

    fala = {"id": "f-5", "tipo": "fechamento", "dia": "2026-10-21", "texto": "",
            "contexto": {"decisoes": [{"tarefa": "r2", "destino": "guardar"}]}}
    dc.atender("k", fala, claude=claude, agora=MADRUGADA)
    assert feitos[-1]["ops"] == []
    assert feitos[-1]["mudancas"] == []
    assert not any("semana que vem: " in m for f in feitos if f for m in f.get("mudancas") or [])
    assert "(não use estes ids): nenhuma." in prompts[0]
    assert "ficam onde estão, sem decisão" in prompts[0] and "semana que vem): r2." in prompts[0]
    assert "1. Do HTML" in prompts[0]  # ordem de corte de reserva
    assert "resumo" not in prompts[0]


def test_resumo_so_com_o_recurso():
    antiga = {"dias": []}
    assert "resumo" not in dc.schema(antiga)["properties"]["ops"]["items"]["properties"]["op"]["enum"]
    assert '"op":"resumo"' not in dc.sistema(antiga)
    nova = {"recursos": ["resumo"]}
    assert "resumo" in dc.schema(nova)["properties"]["ops"]["items"]["properties"]["op"]["enum"]
    assert '"op":"resumo"' in dc.sistema(nova)
    _, _, ops = dc._limpar({"ops": [{"op": "resumo", "dia": "2026-10-22", "tema": "a" * 400},
                                    {"op": "resumo", "dia": "2026-10-22"}]}, dc.ops_do_claude(nova))
    assert ops == [{"op": "resumo", "dia": "2026-10-22", "tema": "a" * 300}]  # cortado em 300; vazio sai


def test_corte_vem_da_api_e_o_html_e_reserva(monkeypatch):
    monkeypatch.setattr(dc, "ordem_de_corte", lambda *a: ["Do HTML"])
    assert dc.corte_da_semana(SEMANA_NOVA) == ["Contagem nova", "Leituras"]
    assert dc.corte_da_semana({"semana": {"corte": []}}) == ["Do HTML"]
    assert dc.corte_da_semana({}) == ["Do HTML"]


def test_plano_mostra_minimo_calculado_habito_folga_e_guardadas():
    t = dc.plano_em_texto(SEMANA_NOVA, datetime(2026, 10, 21, 9, 0, tzinfo=dc.FUSO))
    assert "Mínimo do dia: roteiro 2 ao Renato" in t and "texto antigo" not in t
    assert "[leve] WhatsApp · hábito" in t and "[leve] Imprevistos · imprevistos" in t
    assert "[gov] Reunião · fixo" in t
    assert "r1 :: Mandar o roteiro 2 (mínimo: roteiro 2 ao Renato)" in t
    assert "Guardado para o fim de semana (não é pendência): Ler o artigo" in t
    # contínua de um dia que passou não vira "o que ficou"
    sexta = dc.plano_em_texto(SEMANA_NOVA, datetime(2026, 10, 23, 9, 0, tzinfo=dc.FUSO))
    assert "Pós-defeso" not in sexta
    # dia sem nenhum min marcado: o texto do dia é a reserva
    assert dc.minimo_do_dia({"min": "O roteiro.", "blocos": []}) == "O roteiro."
