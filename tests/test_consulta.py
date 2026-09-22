"""Consulta de leitura ao vault pelo Telegram: allowlist, resolução de nome, busca e formato.

O teste que importa mais é `test_diario_nunca_aparece`: a allowlist do `consulta.py` é o que
impede o diário e a blocklist de reuniões de saírem pelo chat, e ela não pode se afrouxar sem
a suíte reclamar.
"""
import pytest

from logos import config, consulta


VAULT = {
    "work/active/PPJ.md": (
        "---\n"
        "description: \"Políticas Públicas da Juventude — dados do SIOP\"\n"
        "status: active\n"
        "quarter: Q3-2026\n"
        "---\n"
        "\n"
        "# 🏛️ PPJ — Políticas Públicas da Juventude\n"
        "\n"
        "Projeto da [[attrindade]] para o [[Instituto Cíclica|Cíclica]].\n"
        "\n"
        "## 📁 Repositórios\n"
        "\n"
        "| Repositório | Papel |\n"
        "| :--- | :--- |\n"
        "| `ppj-entrevistas-vis` | Visualização |\n"
        "\n"
        "## 🎯 Objetivos\n"
        "\n"
        "- [ ] Automação do SIOP com **prioridade** alta.\n"
        "\n"
        "## Reunião 2026-09-21\n"
        "\n"
        "> [!note] O número vem do Tactiq\n"
        "> Trinta e duas reuniões.\n"
    ),
    "work/active/Climatizando.md": "---\nstatus: active\n---\n\n# Climatizando\n\nAtos normativos.\n",
    "work/active/Logos/Logos.md": "# Logos\n\nNota interna do projeto.\n",
    "brain/Gotchas.md": "# Gotchas\n\n## Renato\n\nO handle aberto engana.\n",
    "briefings/2026-09-20.md": "# Briefing 20\n\nVelho.\n",
    "briefings/2026-09-21.md": "# Briefing 21\n\nHoje tem reunião com <Renato> & equipe.\n",
    "capturas/Inbox/2026-09-18 - Renato.md": "# Renato\n\nLembrar do submit.\n",
    "capturas/Planejamento/2026-09-01.md": "# Planejamento\n\nSemana.\n",
    # Fora da allowlist — nada abaixo pode aparecer em resposta nenhuma. Todos citam "Renato",
    # que é termo de busca legítimo, para que o teste pegue o vazamento pela porta da busca.
    "capturas/Diario/2026-09-20 - Diario.md": "# Diário\n\nSENTINELA do diário sobre o Renato.\n",
    "thinking/session-logs/log.md": "# Log\n\nSENTINELA do thinking, com Renato.\n",
    "memories/2026/m.md": "# Memória\n\nSENTINELA das memories, com Renato.\n",
    "work/meetings/Terapia.md": "# Terapia\n\nSENTINELA clínica, com Renato.\n",
    ".claude/om-jobs-state/meeting-blocklist.md": "# Blocklist\n\nSENTINELA da blocklist, Renato.\n",
}

# Conteúdo e caminhos que nenhuma resposta pode conter. O termo digitado pelo usuário volta no
# eco ("não achei nota com 'terapia'"), e isso não é vazamento: vazamento é o bot revelar que o
# arquivo existe, onde ele está ou o que tem dentro.
PROIBIDO = ("SENTINELA", "Diario/", "capturas/Diario", "om-jobs-state",
            "thinking/", "memories/", "work/meetings")


@pytest.fixture
def vault(tmp_path, monkeypatch):
    for caminho, conteudo in VAULT.items():
        destino = tmp_path / caminho
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(conteudo, encoding="utf-8")
    # Temporário do Syncthing: nunca pode virar resultado (já gerou nota duplicada antes).
    (tmp_path / "capturas/Inbox/~syncthing~oculto.md").write_text("Renato oculto", encoding="utf-8")
    monkeypatch.setattr(config, "VAULT_DIR", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Invariante 1: allowlist
# ---------------------------------------------------------------------------

def test_diario_nunca_aparece(vault):
    """A trava do módulo inteiro. Nenhum comando pode devolver conteúdo, nome ou caminho de
    pasta fora da allowlist — o diário, a terapia e a blocklist com o nome da terapeuta."""
    respostas = [
        consulta.cmd_ver(""),
        consulta.cmd_ver("diario"),
        consulta.cmd_ver("terapia"),
        consulta.cmd_buscar("Renato"),
        consulta.cmd_buscar("clínica"),
        consulta.cmd_hoje(),
    ]
    for resposta in respostas:
        for termo in PROIBIDO:
            assert termo.lower() not in resposta.lower(), f"vazou “{termo}” em: {resposta[:200]}"

    # E o bot nem admite que os arquivos existem.
    assert consulta.cmd_ver("diario").startswith("Não achei")
    assert consulta.cmd_ver("terapia").startswith("Não achei")


def test_allowlist_cobre_apenas_as_pastas_declaradas(vault):
    for path in consulta.iter_notas():
        relativo = consulta.rel(path)
        assert relativo.startswith(consulta.ALLOWED_SUBDIRS), relativo


def test_travessia_de_caminho_e_recusada(vault):
    assert consulta.cmd_ver("../../capturas/Diario/2026-09-20 - Diario").startswith("Não achei")
    with pytest.raises(consulta.ConsultaError):
        consulta.carregar(vault / "capturas/Diario/2026-09-20 - Diario.md")


def test_temporario_do_syncthing_ignorado(vault):
    assert not any(p.name.startswith("~syncthing~") for p in consulta.iter_notas())


def test_pasta_ausente_nao_derruba(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VAULT_DIR", tmp_path / "vazio")
    assert consulta.iter_notas() == []
    assert "Nenhum projeto ativo" in consulta.cmd_ver("")


# ---------------------------------------------------------------------------
# /ver
# ---------------------------------------------------------------------------

def test_ver_lista_projetos_ativos(vault):
    resposta = consulta.cmd_ver("")
    assert "PPJ" in resposta and "Climatizando" in resposta
    assert "Gotchas" not in resposta  # brain não é projeto


def test_ver_abre_por_nome_sem_acento_e_sem_caixa(vault):
    assert "Políticas Públicas da Juventude" in consulta.cmd_ver("ppj")
    assert "Climatizando" in consulta.cmd_ver("CLIMATIZANDO")


def test_ver_projeto_ativo_ganha_de_nota_homonima(vault):
    """'/ver logos' tem de ser a nota do projeto, não outra que case por substring."""
    (vault / "capturas/Inbox/Logos no celular.md").write_text("# Logos no celular\n", encoding="utf-8")
    assert "Nota interna do projeto" in consulta.cmd_ver("logos")


def test_nome_exato_ganha_do_prefixo(vault):
    """'Climatizando' casa exato mesmo existindo 'Climatizando notas'."""
    (vault / "brain/Climatizando notas.md").write_text("# Clima notas\n", encoding="utf-8")
    assert "Atos normativos" in consulta.cmd_ver("climatizando")


def test_ver_nome_ambiguo_lista_candidatos(vault):
    (vault / "brain/Climatizando notas.md").write_text("# Clima notas\n", encoding="utf-8")
    resposta = consulta.cmd_ver("clima")
    assert "mais de uma nota" in resposta
    assert "Climatizando" in resposta and "Climatizando notas" in resposta


def test_ver_nome_inexistente(vault):
    assert "Não achei" in consulta.cmd_ver("inexistente")


def test_nota_longa_vira_indice_de_secoes(vault):
    (vault / "work/active/PPJ.md").write_text(
        "---\nstatus: active\n---\n\n# PPJ\n\n" + "".join(
            f"## Seção {n}\n\n{'texto ' * 120}\n\n" for n in range(1, 6)
        ),
        encoding="utf-8",
    )
    resposta = consulta.cmd_ver("ppj")
    assert "Seções" in resposta and "1. Seção 1" in resposta and "5. Seção 5" in resposta
    assert "texto texto" not in resposta  # o índice não despeja o corpo


def test_ver_secao_por_numero(vault):
    resposta = consulta.cmd_ver("ppj 3")
    assert "Reunião 2026-09-21" in resposta and "Trinta e duas reuniões" in resposta


def test_ver_secao_fora_da_faixa(vault):
    """ConsultaError sobe até o bot, que a devolve como texto no chat — ver Bot._consultar."""
    with pytest.raises(consulta.ConsultaError, match="3 seções"):
        consulta.cmd_ver("ppj 99")


def test_nota_curta_sai_inteira(vault):
    resposta = consulta.cmd_ver("climatizando")
    assert "Atos normativos" in resposta


# ---------------------------------------------------------------------------
# /buscar e /hoje
# ---------------------------------------------------------------------------

def test_buscar_agrupa_por_arquivo(vault):
    resposta = consulta.cmd_buscar("Renato")
    assert "Gotchas" in resposta and "2026-09-18 - Renato" in resposta


def test_buscar_ignora_acento_e_caixa(vault):
    assert "PPJ" in consulta.cmd_buscar("reuniao")


def test_buscar_ignora_o_frontmatter(vault):
    """A nota do PPJ tem 'Políticas Públicas' na description e no corpo. O resultado é o corpo."""
    resposta = consulta.cmd_buscar("Políticas Públicas")
    assert "description:" not in resposta


def test_buscar_sem_resultado(vault):
    assert "Nada com" in consulta.cmd_buscar("xyzzy")


def test_buscar_termo_curto(vault):
    with pytest.raises(consulta.ConsultaError):
        consulta.buscar("a")


def test_hoje_pega_o_briefing_mais_recente(vault):
    resposta = consulta.cmd_hoje()
    assert "Briefing 21" in resposta and "Briefing 20" not in resposta


# ---------------------------------------------------------------------------
# Formato: legível, sem emoji, HTML válido
# ---------------------------------------------------------------------------

def test_emoji_do_template_sai_dos_titulos(vault):
    resposta = consulta.cmd_ver("ppj")
    assert "🏛️" not in resposta and "📁" not in resposta and "🎯" not in resposta


def test_emoji_no_corpo_tambem_sai_sem_desmontar_a_lista(vault):
    (vault / "work/active/Climatizando.md").write_text(
        "---\nstatus: active\n---\n\n# Climatizando\n\n"
        "⚠️ Relançar o watcher uma vez só.\n\n"
        "- ✅ Primeiro item\n"
        "  - 🎯 Item aninhado\n",
        encoding="utf-8",
    )
    resposta = consulta.cmd_ver("climatizando")
    assert "⚠️" not in resposta and "✅" not in resposta and "🎯" not in resposta
    assert "Relançar o watcher uma vez só." in resposta
    assert "- Primeiro item" in resposta
    assert "  - Item aninhado" in resposta  # a indentação da lista sobrevive


def test_seta_sobrevive(vault):
    """Seta não é emoji: 'Tactiq → Drive' perde o sentido sem ela."""
    assert consulta.limpar("Tactiq → Drive → vault") == "Tactiq → Drive → vault"


def test_markdown_vira_texto_legivel(vault):
    resposta = consulta.cmd_ver("ppj")
    assert "Cíclica" in resposta and "[[" not in resposta
    assert "**" not in resposta
    assert "| :--- |" not in resposta


def test_callout_vira_linha_simples(vault):
    resposta = consulta.cmd_ver("ppj 3")
    assert "[!note]" not in resposta
    assert "Note: O número vem do Tactiq" in resposta


def test_conteudo_do_vault_e_escapado(vault):
    """O briefing tem '<Renato> & equipe'. Sem escapar, a Bot API devolve 400."""
    resposta = consulta.cmd_hoje()
    assert "&lt;Renato&gt; &amp; equipe" in resposta
    assert "<Renato>" not in resposta


def test_partir_nao_quebra_tag_ao_meio(vault):
    texto = "\n".join(f"<b>linha {n}</b> " + "x" * 200 for n in range(60))
    partes = consulta.partir(texto, limite=1000)
    assert len(partes) > 1
    for parte in partes:
        assert len(parte) <= 1000
        assert parte.count("<b>") == parte.count("</b>")


def test_partir_corta_linha_maior_que_o_limite(vault):
    partes = consulta.partir("palavra " * 500, limite=200)
    assert all(len(p) <= 200 for p in partes)


def test_sem_tags_desfaz_a_formatacao():
    assert consulta.sem_tags("<b>PPJ</b> &amp; <code>x.md</code>") == "PPJ & x.md"
