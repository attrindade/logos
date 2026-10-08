"""Rota declarada no nome do arquivo: o Diário gravado pelo Grava não depende da fala."""
import pytest

from logos.triage import Route, route_from_filename, triage


@pytest.mark.parametrize("nome", [
    "2026-10-07 22-30-00 diario.m4a",
    "2026-10-07 22-30-00 diario.aac",
    "2026-10-07 22-30-00 Diário.m4a",          # nome dado à mão no Easy Voice Recorder
    "2026-10-07 22-30-00 diarios extra.m4a",
])
def test_nome_com_diario_manda_sobre_a_fala(nome):
    t = triage(nome, "Hoje eu preciso comprar pão")
    assert t.route == Route.DIARIO
    assert t.categoria == ""  # nada de categoria de Inbox num Diário


@pytest.mark.parametrize("nome, fala, rota", [
    ("2026-10-07 22-30-00.m4a", "Diário de hoje", Route.DIARIO),
    ("2026-10-07 22-30-00.m4a", "Planejamento da semana", Route.PLANEJAMENTO),
    ("2026-10-07 22-30-00.m4a", "Hoje eu preciso comprar pão", Route.INBOX),
    ("2026-10-07 22-30-00 entrevista.m4a", "Hoje eu preciso comprar pão", Route.INBOX),
    ("2026-10-07 22-30-00 entrevista.m4a", "Diário de hoje", Route.DIARIO),
    ("2026-09-14 15-02-11 tg 123.txt", "tarefa ligar", Route.INBOX),
    ("20261004_225057.m4a", "Hoje", Route.INBOX),  # fora do padrão: segue pela fala
])
def test_sem_diario_no_nome_segue_pela_fala(nome, fala, rota):
    assert triage(nome, fala).route == rota


def test_rota_do_nome_sem_tag():
    assert route_from_filename("2026-10-07 22-30-00.m4a") is None
    assert route_from_filename("qualquer coisa.m4a") is None


def test_falha_antes_da_transcricao_ja_marca_diario():
    # _mark_failed chama triage sem transcrição: o ledger já registra a rota certa.
    assert triage("2026-10-07 22-30-00 diario.m4a").route == Route.DIARIO
