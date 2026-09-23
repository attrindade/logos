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
def ctx():
    return consulta.Contexto()


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

def test_diario_nunca_aparece(vault, ctx):
    """A trava do módulo inteiro. Nenhum comando pode devolver conteúdo, nome ou caminho de
    pasta fora da allowlist — o diário, a terapia e a blocklist com o nome da terapeuta."""
    respostas = [
        consulta.cmd_ver("", ctx),
        consulta.cmd_ver("diario", ctx),
        consulta.cmd_ver("terapia", ctx),
        consulta.cmd_buscar("Renato", ctx),
        consulta.cmd_buscar("clínica", ctx),
        consulta.cmd_hoje(ctx),
    ]
    for resposta in respostas:
        for termo in PROIBIDO:
            assert termo.lower() not in resposta.lower(), f"vazou “{termo}” em: {resposta[:200]}"

    # E o bot nem admite que os arquivos existem.
    assert "Não achei" in consulta.cmd_ver("diario", ctx)
    assert "Não achei" in consulta.cmd_ver("terapia", ctx)


def test_allowlist_cobre_apenas_as_pastas_declaradas(vault):
    for path in consulta.iter_notas():
        relativo = consulta.rel(path)
        assert relativo.startswith(consulta.ALLOWED_SUBDIRS), relativo


def test_travessia_de_caminho_e_recusada(vault, ctx):
    assert "Não achei" in consulta.cmd_ver("../../capturas/Diario/2026-09-20 - Diario", ctx)
    with pytest.raises(consulta.ConsultaError):
        consulta.carregar(vault / "capturas/Diario/2026-09-20 - Diario.md")


def test_temporario_do_syncthing_ignorado(vault):
    assert not any(p.name.startswith("~syncthing~") for p in consulta.iter_notas())


def test_pasta_ausente_nao_derruba(tmp_path, monkeypatch, ctx):
    monkeypatch.setattr(config, "VAULT_DIR", tmp_path / "vazio")
    assert consulta.iter_notas() == []
    assert "Nenhum projeto ativo" in consulta.cmd_ver("", ctx)


# ---------------------------------------------------------------------------
# /ver
# ---------------------------------------------------------------------------

def test_ver_lista_projetos_ativos(vault, ctx):
    resposta = consulta.cmd_ver("", ctx)
    assert "PPJ" in resposta and "Climatizando" in resposta
    assert "Gotchas" not in resposta  # brain não é projeto


def test_ver_abre_por_nome_sem_acento_e_sem_caixa(vault, ctx):
    assert "Políticas Públicas da Juventude" in consulta.cmd_ver("ppj", ctx)
    assert "Climatizando" in consulta.cmd_ver("CLIMATIZANDO", ctx)


def test_ver_projeto_ativo_ganha_de_nota_homonima(vault, ctx):
    """'/ver logos' tem de ser a nota do projeto, não outra que case por substring."""
    (vault / "capturas/Inbox/Logos no celular.md").write_text("# Logos no celular\n", encoding="utf-8")
    assert "Nota interna do projeto" in consulta.cmd_ver("logos", ctx)


def test_nome_exato_ganha_do_prefixo(vault, ctx):
    """'Climatizando' casa exato mesmo existindo 'Climatizando notas'."""
    (vault / "brain/Climatizando notas.md").write_text("# Clima notas\n", encoding="utf-8")
    assert "Atos normativos" in consulta.cmd_ver("climatizando", ctx)


def test_ver_nome_ambiguo_lista_candidatos(vault, ctx):
    (vault / "brain/Climatizando notas.md").write_text("# Clima notas\n", encoding="utf-8")
    resposta = consulta.cmd_ver("clima", ctx)
    assert "mais de uma nota" in resposta
    assert "Climatizando" in resposta and "Climatizando notas" in resposta


def test_ver_nome_inexistente(vault, ctx):
    assert "Não achei" in consulta.cmd_ver("inexistente", ctx)


def test_nota_longa_vira_indice_de_secoes(vault, ctx):
    (vault / "work/active/PPJ.md").write_text(
        "---\nstatus: active\n---\n\n# PPJ\n\n" + "".join(
            f"## Seção {n}\n\n{'texto ' * 120}\n\n" for n in range(1, 6)
        ),
        encoding="utf-8",
    )
    resposta = consulta.cmd_ver("ppj", ctx)
    assert "5 seções" in resposta
    assert "/s1" in resposta and "Seção 1" in resposta
    assert "/s5" in resposta and "Seção 5" in resposta
    assert "texto texto" not in resposta  # o índice não despeja o corpo


def test_ver_secao_por_numero(vault, ctx):
    resposta = consulta.cmd_ver("ppj 3", ctx)
    assert "Reunião 2026-09-21" in resposta and "Trinta e duas reuniões" in resposta


def test_ver_secao_fora_da_faixa(vault, ctx):
    """ConsultaError sobe até o bot, que a devolve como texto no chat — ver Bot._consultar."""
    with pytest.raises(consulta.ConsultaError, match="3 seções"):
        consulta.cmd_ver("ppj 99", ctx)


def test_nota_curta_sai_inteira(vault, ctx):
    resposta = consulta.cmd_ver("climatizando", ctx)
    assert "Atos normativos" in resposta


# ---------------------------------------------------------------------------
# /buscar e /hoje
# ---------------------------------------------------------------------------

def test_buscar_agrupa_por_arquivo(vault, ctx):
    resposta = consulta.cmd_buscar("Renato", ctx)
    assert "Gotchas" in resposta and "2026-09-18 - Renato" in resposta


def test_buscar_ignora_acento_e_caixa(vault, ctx):
    assert "PPJ" in consulta.cmd_buscar("reuniao", ctx)


def test_buscar_ignora_o_frontmatter(vault, ctx):
    """A nota do PPJ tem 'Políticas Públicas' na description e no corpo. O resultado é o corpo."""
    resposta = consulta.cmd_buscar("Políticas Públicas", ctx)
    assert "description:" not in resposta


def test_buscar_sem_resultado(vault, ctx):
    assert "Nada com" in consulta.cmd_buscar("xyzzy", ctx)


def test_buscar_termo_curto(vault):
    with pytest.raises(consulta.ConsultaError):
        consulta.buscar("a")


def test_hoje_pega_o_briefing_mais_recente(vault, ctx):
    resposta = consulta.cmd_hoje(ctx)
    assert "Briefing 21" in resposta and "Briefing 20" not in resposta


# ---------------------------------------------------------------------------
# Formato: legível, sem emoji, HTML válido
# ---------------------------------------------------------------------------

def test_emoji_do_template_sai_dos_titulos(vault, ctx):
    resposta = consulta.cmd_ver("ppj", ctx)
    assert "🏛️" not in resposta and "📁" not in resposta and "🎯" not in resposta


def test_emoji_no_corpo_tambem_sai_sem_desmontar_a_lista(vault, ctx):
    (vault / "work/active/Climatizando.md").write_text(
        "---\nstatus: active\n---\n\n# Climatizando\n\n"
        "⚠️ Relançar o watcher uma vez só.\n\n"
        "- ✅ Primeiro item\n"
        "  - 🎯 Item aninhado\n",
        encoding="utf-8",
    )
    resposta = consulta.cmd_ver("climatizando", ctx)
    assert "⚠️" not in resposta and "✅" not in resposta and "🎯" not in resposta
    assert "Relançar o watcher uma vez só." in resposta
    assert "- Primeiro item" in resposta
    assert "  - Item aninhado" in resposta  # a indentação da lista sobrevive


def test_seta_sobrevive(vault):
    """Seta não é emoji: 'Tactiq → Drive' perde o sentido sem ela."""
    assert consulta.limpar("Tactiq → Drive → vault") == "Tactiq → Drive → vault"


def test_markdown_vira_texto_legivel(vault, ctx):
    resposta = consulta.cmd_ver("ppj", ctx)
    assert "Cíclica" in resposta and "[[" not in resposta
    assert "**" not in resposta
    assert "| :--- |" not in resposta


def test_callout_vira_linha_simples(vault, ctx):
    resposta = consulta.cmd_ver("ppj 3", ctx)
    assert "[!note]" not in resposta
    assert "Note: O número vem do Tactiq" in resposta


def test_conteudo_do_vault_e_escapado(vault, ctx):
    """O briefing tem '<Renato> & equipe'. Sem escapar, a Bot API devolve 400."""
    resposta = consulta.cmd_hoje(ctx)
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


# ---------------------------------------------------------------------------
# Atalhos numerados: /p1, /s2, /r3 e a tela inicial
# ---------------------------------------------------------------------------

def test_listar_e_abrir_pelo_numero(vault, ctx):
    """O fluxo inteiro que ele vai usar: /ver, /p1, /s2 — sem digitar nome nenhum."""
    lista = consulta.cmd_ver("", ctx)
    assert "/p1" in lista
    primeiro = consulta.cmd_indice("p", 1, ctx)
    assert ctx.nota is not None
    assert ctx.nota.stem == consulta.projetos()[0].stem
    assert consulta.projetos()[0].stem in primeiro or ctx.nota.stem in primeiro


def test_secao_pelo_numero_usa_a_nota_aberta(vault, ctx):
    consulta.cmd_ver("ppj", ctx)
    resposta = consulta.cmd_indice("s", 3, ctx)
    assert "Trinta e duas reuniões" in resposta


def test_s0_volta_ao_indice_da_nota(vault, ctx):
    consulta.cmd_ver("ppj", ctx)
    consulta.cmd_indice("s", 1, ctx)
    volta = consulta.cmd_indice("s", 0, ctx)
    assert "PPJ" in volta


def test_secao_sem_nota_aberta(vault, ctx):
    with pytest.raises(consulta.ConsultaError, match="Nenhuma nota aberta"):
        consulta.cmd_indice("s", 1, ctx)


def test_indice_de_lista_que_venceu(vault, ctx):
    """Depois de um relançamento o contexto nasce vazio: /p3 tem de explicar, não explodir."""
    with pytest.raises(consulta.ConsultaError, match="Mande /ver de novo"):
        consulta.cmd_indice("p", 3, ctx)
    with pytest.raises(consulta.ConsultaError, match="Mande /buscar ou /pastas"):
        consulta.cmd_indice("r", 1, ctx)
    with pytest.raises(consulta.ConsultaError, match="Mande /pastas"):
        consulta.cmd_indice("d", 2, ctx)
    with pytest.raises(consulta.ConsultaError, match="Nenhuma pasta aberta"):
        consulta.cmd_indice("d", 0, ctx)
    with pytest.raises(consulta.ConsultaError, match="Abra uma nota"):
        consulta.cmd_indice("l", 1, ctx)


# ---------------------------------------------------------------------------
# Navegação: pastas e links
# ---------------------------------------------------------------------------

@pytest.fixture
def vault_ligado(vault):
    """O vault de teste com links de verdade: um que resolve pelo nome, um por alias, um para
    fora da allowlist e um de âncora, que aponta para a própria nota."""
    (vault / "brain/Key Decisions.md").write_text(
        "# Key Decisions\n\n"
        "- Ver [[PPJ]] e [[Govinho|o apelido]].\n"
        "- O [[2026-09-20 - Diario]] não pode aparecer.\n"
        "- Interno: [[#Outra seção]] e [[Inexistente]].\n",
        encoding="utf-8",
    )
    (vault / "org").mkdir()
    (vault / "org/Clima.md").write_text(
        "---\naliases:\n  - Govinho\n  - GC\n---\n\n# Clima\n\nCita [[Key Decisions]].\n",
        encoding="utf-8",
    )
    (vault / "work/active/Logos/Plano.md").write_text("# Plano\n\nVolta ao [[Logos]].\n",
                                                      encoding="utf-8")
    return vault


def test_pastas_lista_so_a_allowlist(vault, ctx):
    resposta = consulta.cmd_pastas(ctx)
    for termo in PROIBIDO:
        assert termo.lower() not in resposta.lower(), termo
    assert "/d1" in resposta and "work/active" in resposta
    assert len(ctx.pastas) == len(consulta.allowed_roots())


def test_pasta_mostra_subpastas_e_notas_numeradas(vault_ligado, ctx):
    consulta.cmd_pastas(ctx)
    indice = [consulta.rel(p) for p in ctx.pastas].index("work/active") + 1
    resposta = consulta.cmd_indice("d", indice, ctx)
    assert "/d1" in resposta and "Logos" in resposta       # subpasta
    assert "/r1" in resposta and "PPJ" in resposta         # nota
    assert [p.name for p in ctx.pastas] == ["Logos"]

    sub = consulta.cmd_indice("d", 1, ctx)
    assert "work/active/Logos" in sub and "Plano" in sub
    # /d0 reabre a pasta aberta, e a nota aberta de dentro dela oferece a volta.
    nota = consulta.cmd_indice("r", 1, ctx)
    assert "/d0 volta à pasta" in nota
    assert "work/active/Logos" in consulta.cmd_indice("d", 0, ctx)


def test_pasta_grande_mostra_as_recentes_e_aponta_a_busca(vault, ctx, monkeypatch):
    monkeypatch.setattr(consulta, "MAX_NOTAS_PASTA", 1)
    resposta = consulta.render_pasta(vault / "briefings", *consulta.conteudo_pasta(vault / "briefings"))
    assert "/r1" in resposta and "/r2" not in resposta
    assert "e mais 1" in resposta and "/b" in resposta


def test_pasta_fora_da_allowlist_e_recusada(vault, ctx):
    with pytest.raises(consulta.ConsultaError, match="fora das que eu leio"):
        consulta.conteudo_pasta(vault / "capturas" / "Diario")
    with pytest.raises(consulta.ConsultaError, match="fora das que eu leio"):
        consulta.conteudo_pasta(vault / "capturas")   # a mãe do Diário também não


def test_links_resolvem_por_nome_e_alias_e_somem_fora_da_allowlist(vault_ligado, ctx):
    resposta = consulta.cmd_ver("key decisions", ctx)
    # O corpo da nota cita o diário pelo nome, e o corpo sempre foi mostrado como está; o que
    # não pode é a lista de links transformar esse nome num atalho que abre alguma coisa.
    links = resposta.split("<b>Links</b>")[-1]
    for termo in PROIBIDO + ("2026-09-20", "Inexistente"):
        assert termo.lower() not in links.lower(), termo
    nomes = [p.stem for p in ctx.links]
    # PPJ pelo nome, Clima pelo alias; Clima também cita esta nota, mas não aparece duas vezes.
    assert nomes == ["PPJ", "Clima"]
    assert "/l1" in resposta and "/l2" in resposta

    seguido = consulta.cmd_indice("l", 2, ctx)
    assert "<b>Clima</b>" in seguido


def test_citada_em_lista_quem_aponta_para_a_nota(vault_ligado, ctx):
    resposta = consulta.cmd_ver("ppj", ctx)
    assert "Citada em" in resposta and "Key Decisions" in resposta
    assert "Key Decisions" in [p.stem for p in ctx.links]


def test_abrir_secao_preserva_os_links_da_nota(vault_ligado, ctx):
    consulta.cmd_ver("ppj", ctx)
    antes = list(ctx.links)
    consulta.cmd_indice("s", 1, ctx)
    assert ctx.links == antes and antes


def test_aliases_em_lista_e_entre_colchetes():
    assert consulta._aliases("---\naliases:\n  - A\n  - 'B'\ntags: x\n---\n") == ["A", "B"]
    assert consulta._aliases("---\naliases: [A, \"B\"]\n---\n") == ["A", "B"]
    assert consulta._aliases("# sem frontmatter\naliases:\n  - A\n") == []


def test_navegacao_nao_vaza_o_diario(vault_ligado, ctx):
    """A trava de `test_diario_nunca_aparece`, passada por todo caminho de navegação."""
    respostas = [consulta.cmd_pastas(ctx)]
    pastas = list(ctx.pastas)
    while pastas:
        pasta = pastas.pop()
        respostas.append(consulta._abrir_pasta(pasta, ctx))
        pastas.extend(ctx.pastas)
    for nome in ("key decisions", "clima", "ppj", "logos"):
        respostas.append(consulta.cmd_ver(nome, ctx))
        for destino in list(ctx.links):
            respostas.append(consulta._abrir(destino, ctx))
    for resposta in respostas:
        for termo in PROIBIDO:
            assert termo.lower() not in resposta.lower(), f"vazou “{termo}” em: {resposta[:200]}"


def test_indice_fora_da_lista(vault, ctx):
    consulta.cmd_ver("", ctx)
    with pytest.raises(consulta.ConsultaError, match="itens"):
        consulta.cmd_indice("p", 99, ctx)


def test_resultado_de_busca_abre_pelo_numero(vault, ctx):
    busca = consulta.cmd_buscar("Renato", ctx)
    assert "/r1" in busca
    assert consulta.cmd_indice("r", 1, ctx)
    assert ctx.nota is not None


def test_nome_ambiguo_tambem_vira_lista_numerada(vault, ctx):
    (vault / "brain/Climatizando notas.md").write_text("# Clima notas\n", encoding="utf-8")
    resposta = consulta.cmd_ver("clima", ctx)
    assert "/r1" in resposta and "/r2" in resposta
    assert consulta.cmd_indice("r", 2, ctx)


def test_inicio_orienta_e_deixa_os_atalhos_prontos(vault, ctx):
    resposta = consulta.cmd_inicio(ctx)
    assert "Para onde" in resposta
    assert "/p1" in resposta and "/h" in resposta and "/b" in resposta
    # O /p1 anunciado ali tem de funcionar na mensagem seguinte.
    assert consulta.cmd_indice("p", 1, ctx)


def test_inicio_com_vault_vazio(tmp_path, monkeypatch, ctx):
    monkeypatch.setattr(config, "VAULT_DIR", tmp_path / "vazio")
    resposta = consulta.cmd_inicio(ctx)
    assert "Para onde" in resposta and "/b" in resposta


def test_ajuda_cabe_numa_tela_e_ensina_o_fluxo(vault):
    ajuda = consulta.cmd_ajuda()
    assert len(ajuda) < consulta.LIMITE_MENSAGEM
    for atalho in ("/0", "/v", "/b", "/h", "/p1", "/s1", "/r1"):
        assert atalho in ajuda


# ---------------------------------------------------------------------------
# Citação: o que deixa a leitura agradável, e o que ela não pode quebrar
# ---------------------------------------------------------------------------

def test_citacao_longa_nasce_recolhida(vault):
    curta = consulta.citacao("oi")
    longa = consulta.citacao("x" * (consulta.LIMITE_CITACAO + 1))
    assert curta.startswith("<blockquote>")
    assert longa.startswith("<blockquote expandable>")


def test_partir_fecha_e_reabre_a_citacao(vault):
    """Citação maior que uma mensagem vira uma citação por mensagem, nunca uma tag partida."""
    conteudo = "\n".join(f"linha {n} " + "y" * 120 for n in range(40))
    texto = "cabeça\n" + consulta.citacao(conteudo)
    partes = consulta.partir(texto, limite=900)
    assert len(partes) > 1
    for parte in partes:
        assert len(parte) <= 900
        abre = parte.count("<blockquote")
        fecha = parte.count("</blockquote>")
        assert abre == fecha, parte[:120]
        assert "<blockquo" not in parte.replace("<blockquote expandable>", "").replace(
            "<blockquote>", "").replace("</blockquote>", "")


def test_citacao_curta_nao_e_partida(vault):
    texto = consulta.citacao("uma linha só")
    assert consulta.partir(texto, limite=900) == [texto]


def test_nota_longa_de_verdade_cabe_nas_mensagens(vault, ctx):
    (vault / "work/active/PPJ.md").write_text(
        "---\nstatus: active\n---\n\n# PPJ\n\n" + "".join(
            f"## Seção {n}\n\n{'palavra ' * 400}\n\n" for n in range(1, 12)
        ),
        encoding="utf-8",
    )
    consulta.cmd_ver("ppj", ctx)
    for parte in consulta.partir(consulta.cmd_indice("s", 2, ctx)):
        assert len(parte) <= consulta.LIMITE_MENSAGEM


def test_marcadores_sao_estaveis(vault, ctx):
    """Marcador é semântica: o mesmo tipo de resposta abre sempre com o mesmo símbolo."""
    assert consulta.cmd_ver("", ctx).startswith(consulta.MARCA_PROJETO)
    assert consulta.cmd_buscar("Renato", ctx).startswith(consulta.MARCA_BUSCA)
    assert consulta.cmd_hoje(ctx).startswith(consulta.MARCA_BRIEFING)
    assert consulta.cmd_inicio(ctx).startswith(consulta.MARCA_INICIO)
    assert consulta.cmd_ver("climatizando", ctx).startswith(consulta.MARCA_NOTA)


def test_toda_resposta_termina_com_um_proximo_passo(vault, ctx):
    """A dica no rodapé é o mecanismo de aprendizado: sem ela o bot vira adivinhação."""
    for resposta in (
        consulta.cmd_ver("", ctx),
        consulta.cmd_ver("climatizando", ctx),
        consulta.cmd_buscar("Renato", ctx),
        consulta.cmd_buscar("xyzzy", ctx),
        consulta.cmd_ver("inexistente", ctx),
        consulta.cmd_inicio(ctx),
    ):
        assert consulta.MARCA_DICA in resposta
        assert "/" in resposta.rsplit(consulta.MARCA_DICA, 1)[1]


def test_wikilink_com_ancora_perde_a_cerquilha(vault):
    assert consulta.limpar("ver [[#Reunião 2026-08-31]]") == "ver Reunião 2026-08-31"
    assert consulta.limpar("ver [[PPJ#Equipe]]") == "ver Equipe"
    assert consulta.limpar("ver [[PPJ#Equipe|a equipe]]") == "ver a equipe"
    assert consulta.limpar("ver [[PPJ]]") == "ver PPJ"

def test_s0_do_briefing_volta_com_a_marca_do_briefing(vault, ctx):
    """Marcador é semântica: /h → /s1 → /s0 não pode devolver o briefing como nota comum."""
    (vault / "briefings/2026-09-21.md").write_text(
        "# Briefing 21\n\n" + "".join(
            f"## Bloco {n}\n\n{'texto ' * 200}\n\n" for n in range(1, 5)
        ),
        encoding="utf-8",
    )
    assert consulta.cmd_hoje(ctx).startswith(consulta.MARCA_BRIEFING)
    consulta.cmd_indice("s", 1, ctx)
    assert consulta.cmd_indice("s", 0, ctx).startswith(consulta.MARCA_BRIEFING)


def test_s0_de_projeto_segue_com_a_marca_de_nota(vault, ctx):
    consulta.cmd_ver("ppj", ctx)
    assert consulta.cmd_indice("s", 0, ctx).startswith(consulta.MARCA_NOTA)
