"""Categoria pelo apelido do tipo no nome do arquivo do Grava ("... reuniao Conselho.m4a")."""
import pytest

from logos.triage import Route, categoria_from_filename, triage


@pytest.mark.parametrize("nome, categoria", [
    ("2026-10-15 22-21-50 reuniao Conselho.m4a", "reuniao"),
    ("2026-10-15 22-21-50 ideia.m4a", "ideia"),
    ("2026-10-15 22-21-50 entrevista Rosalina.m4a", "entrevista"),
    ("2026-10-15 22-21-50 rpg Golpe do Dragao.m4a", "rpg"),
])
def test_apelido_do_grava_fixa_a_categoria(nome, categoria):
    t = triage(nome, "Hoje eu preciso comprar pão")
    assert t.route == Route.INBOX
    assert t.categoria == categoria
    assert t.prioridade == ""


def test_apelido_manda_sobre_a_primeira_palavra():
    # O tipo foi escolhido num botão do app; "tarefa urgente" dito ali não vira prioridade.
    t = triage("2026-10-15 22-21-50 reuniao Conselho.m4a", "tarefa urgente ligar para a Júlia")
    assert (t.categoria, t.prioridade) == ("reuniao", "")


def test_sem_apelido_conhecido_segue_pela_fala():
    assert triage("2026-10-15 22-21-50 culinaria.m4a", "ideia de receita").categoria == "ideia"
    assert triage("2026-10-15 22-21-50.m4a", "tarefa urgente ligar").prioridade == "alta"
    assert triage("2026-10-15 22-21-50 culinaria.m4a", "Hoje eu fiz pão").categoria == ""


def test_rota_nao_muda_pelo_apelido():
    # Só "diario" no nome manda na rota; os outros apelidos não tiram a palavra falada da rota.
    t = triage("2026-10-15 22-21-50 entrevista.m4a", "Diário de hoje")
    assert t.route == Route.DIARIO
    assert t.categoria == ""
    assert triage("2026-10-15 22-21-50 rpg.m4a", "Planejamento da semana").route == Route.PLANEJAMENTO


def test_diario_no_nome_nunca_ganha_categoria():
    assert triage("2026-10-15 22-21-50 diario.m4a", "ideia de app").categoria == ""


@pytest.mark.parametrize("nome", [
    "2026-10-15 22-21-50.m4a",
    "2026-09-14 15-02-11 tg 123.txt",
    "2026-10-14 01-25-11 app f1cd4982",
    "Entrevista Rosalina.m4a",               # fora do padrão de nome
    "2026-10-15 22-21-50 culinaria.m4a",     # apelido criado pelo dono, sem categoria
])
def test_nomes_sem_categoria(nome):
    assert categoria_from_filename(nome) == ""


def test_falha_antes_da_transcricao_ja_tem_categoria():
    # _mark_failed chama triage sem transcrição.
    assert triage("2026-10-15 22-21-50 entrevista.m4a").categoria == "entrevista"


def test_entrevista_e_rpg_tambem_valem_ditos():
    assert triage("2026-10-15 22-21-50.m4a", "Entrevista com a Rosalina").categoria == "entrevista"
    assert triage("2026-10-15 22-21-50.m4a", "RPG de sexta").categoria == "rpg"
