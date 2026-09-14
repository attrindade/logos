"""Etapa 2: vocabulário de rotas (tarefa, ideia, reunião) e canal pelo nome do arquivo."""
from datetime import datetime

import pytest

from logos.triage import Route, canal_from_filename, triage, triage_from_filename


@pytest.mark.parametrize("texto, categoria, prioridade", [
    ("tarefa comprar pão", "tarefa", ""),
    ("Tarefa urgente: ligar para o banco", "tarefa", "alta"),
    ("tarefa, urgente, ligar", "tarefa", "alta"),
    ("tarefa ligar urgente", "tarefa", ""),
    ("Ideia: app de filmes", "ideia", ""),
    ("ideia urgente de app", "ideia", ""),
    ("Reunião com o Eduardo amanhã", "reuniao", ""),
    ("reuniao de equipe", "reuniao", ""),
    ("Hoje eu preciso comprar pão", "", ""),
    ("", "", ""),
])
def test_categoria_e_prioridade_pela_primeira_palavra(texto, categoria, prioridade):
    t = triage("2026-09-14 10-00-00.txt", texto)
    assert t.route == Route.INBOX
    assert (t.categoria, t.prioridade) == (categoria, prioridade)


def test_diario_e_planejamento_nao_ganham_categoria():
    assert triage("x.txt", "diário tarefa urgente").categoria == ""
    assert triage("x.txt", "planejamento tarefa").route == Route.PLANEJAMENTO


def test_canal_pelo_sufixo_tg():
    assert canal_from_filename("2026-09-14 15-02-11 tg.txt") == "telegram"
    assert canal_from_filename("2026-09-14 15-02-11 tg.oga") == "telegram"
    assert canal_from_filename("2026-09-14 15-02-11.m4a") == "syncthing"
    assert canal_from_filename("2026-08-23 22-46-52 D.m4a") == "syncthing"
    assert canal_from_filename("nota solta.txt") == "syncthing"


def test_fallback_de_timestamp():
    fallback = datetime(2026, 1, 2, 3, 4, 5)
    assert triage_from_filename("nota solta.txt", fallback) == fallback
    assert triage_from_filename("2026-09-14 15-02-11 tg.txt", fallback) == datetime(2026, 9, 14, 15, 2, 11)
