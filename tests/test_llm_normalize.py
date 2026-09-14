"""Normalização do YAML devolvido pela LLM (tags aninhadas e grafia dos wikilinks)."""
from logos.llm import parse_enrichment

# YAML reconstruído da captura de Planejamento de 2026-09-01 10:48:19: a nota saiu com
# `- - - PPJ` nas tags e `[[renato barcelos advogados]]` em minúsculas.
DEFEITUOSO = """titulo: "Priorizar Submissão Técnica e Alinhamentos Estratégicos de Projetos"
tags:
  - planejamento
  - [[PPJ]]
  - [[Climatizando]]
  - governança
prioridade: alta
status: ativo
pessoas:
  - "[[Bruna]]"
  - Fred
  - Lorena
projetos:
  - "[[Climatizando]]"
  - [[PPJ]]
  - "[[Governa Clima]]"
  - "[[renato barcelos advogados]]"
"""


def test_tags_aninhadas_viram_lista_plana_minuscula():
    data = parse_enrichment(DEFEITUOSO)
    assert data["tags"] == ["planejamento", "ppj", "climatizando", "governança"]


def test_wikilink_de_projeto_recupera_grafia_do_contexto():
    data = parse_enrichment(DEFEITUOSO)
    assert data["projetos"] == ["[[Climatizando]]", "[[PPJ]]", "[[Governa Clima]]", "[[Renato Barcelos Advogados]]"]


def test_pessoas_sem_wikilink_ficam_como_texto():
    data = parse_enrichment(DEFEITUOSO)
    assert data["pessoas"] == ["[[Bruna]]", "Fred", "Lorena"]


def test_tags_com_hash_espaco_e_duplicadas():
    data = parse_enrichment('titulo: "x"\ntags: ["#PPJ", " ppj ", "Governa Clima", "#logos"]\n')
    assert data["tags"] == ["ppj", "governa-clima", "logos"]


def test_grafias_distintas_que_existem_no_contexto_sao_preservadas():
    data = parse_enrichment('titulo: "x"\nprojetos: ["[[minimundo]]", "[[Minimundo]]", "[[TMC]]", "[[tmc]]"]\n')
    assert data["projetos"] == ["[[minimundo]]", "[[Minimundo]]", "[[TMC]]"]


def test_fallback_regex_tambem_normaliza():
    data = parse_enrichment('titulo: "Nota: com dois pontos: quebra"\ntags: [a\nresumo: "r"\n')
    assert data["titulo"]
    assert data["tags"] == []
