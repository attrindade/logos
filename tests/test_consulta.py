"""Consulta de leitura ao vault pelo Telegram: allowlist, resolução de nome, busca e formato.

O teste que importa mais é `test_diario_nunca_aparece`: a allowlist do `consulta.py` é o que
impede o diário e a blocklist de reuniões de saírem pelo chat, e ela não pode se afrouxar sem
a suíte reclamar.
"""
import re

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


# As telas têm texto e botões. Quase todo teste quer "o que ele vê": o texto e os rótulos. O
# dado dos botões fica de fora — é código interno, não aparece na tela.
def _t(tela: consulta.Tela) -> str:
    return "\n".join([tela.texto] + [rotulo for linha in tela.botoes for rotulo, _ in linha])


def _botoes(tela: consulta.Tela) -> list[tuple[str, str]]:
    return [botao for linha in tela.botoes for botao in linha]


def _tocar(tela: consulta.Tela, trecho: str, ctx) -> consulta.Tela:
    """Toca o primeiro botão cujo rótulo contém o trecho — o que o dedo dele faria."""
    for rotulo, dado in _botoes(tela):
        if trecho in rotulo:
            return consulta.cmd_botao(dado, ctx)
    raise AssertionError(f"nenhum botão com “{trecho}” em {[r for r, _ in _botoes(tela)]}")


def ver(arg, ctx):
    return _t(consulta.cmd_ver(arg, ctx))


def buscar(termo, ctx):
    return _t(consulta.cmd_buscar(termo, ctx))


def hoje(ctx):
    return _t(consulta.cmd_hoje(ctx))


def inicio(ctx):
    return _t(consulta.cmd_inicio(ctx))


def pastas(ctx):
    return _t(consulta.cmd_pastas(ctx))


def ajuda():
    return _t(consulta.cmd_ajuda())


# ---------------------------------------------------------------------------
# Invariante 1: allowlist
# ---------------------------------------------------------------------------

def test_diario_nunca_aparece(vault, ctx):
    """A trava do módulo inteiro. Nenhum comando pode devolver conteúdo, nome ou caminho de
    pasta fora da allowlist — o diário, a terapia e a blocklist com o nome da terapeuta."""
    respostas = [
        ver("", ctx),
        ver("diario", ctx),
        ver("terapia", ctx),
        buscar("Renato", ctx),
        buscar("clínica", ctx),
        hoje(ctx),
    ]
    for resposta in respostas:
        for termo in PROIBIDO:
            assert termo.lower() not in resposta.lower(), f"vazou “{termo}” em: {resposta[:200]}"

    # E o bot nem admite que os arquivos existem.
    assert "Não achei" in ver("diario", ctx)
    assert "Não achei" in ver("terapia", ctx)


def test_allowlist_cobre_apenas_as_pastas_declaradas(vault):
    for path in consulta.iter_notas():
        relativo = consulta.rel(path)
        assert relativo.startswith(consulta.ALLOWED_SUBDIRS), relativo


def test_travessia_de_caminho_e_recusada(vault, ctx):
    assert "Não achei" in ver("../../capturas/Diario/2026-09-20 - Diario", ctx)
    with pytest.raises(consulta.ConsultaError):
        consulta.carregar(vault / "capturas/Diario/2026-09-20 - Diario.md")


def test_temporario_do_syncthing_ignorado(vault):
    assert not any(p.name.startswith("~syncthing~") for p in consulta.iter_notas())


def test_pasta_ausente_nao_derruba(tmp_path, monkeypatch, ctx):
    monkeypatch.setattr(config, "VAULT_DIR", tmp_path / "vazio")
    assert consulta.iter_notas() == []
    assert "Nenhum projeto ativo" in ver("", ctx)


# ---------------------------------------------------------------------------
# /ver
# ---------------------------------------------------------------------------

def test_ver_lista_projetos_ativos(vault, ctx):
    resposta = ver("", ctx)
    assert "PPJ" in resposta and "Climatizando" in resposta
    assert "Gotchas" not in resposta  # brain não é projeto


def test_ver_abre_por_nome_sem_acento_e_sem_caixa(vault, ctx):
    assert "Políticas Públicas da Juventude" in ver("ppj", ctx)
    assert "Climatizando" in ver("CLIMATIZANDO", ctx)


def test_ver_projeto_ativo_ganha_de_nota_homonima(vault, ctx):
    """'/ver logos' tem de ser a nota do projeto, não outra que case por substring."""
    (vault / "capturas/Inbox/Logos no celular.md").write_text("# Logos no celular\n", encoding="utf-8")
    assert "Nota interna do projeto" in ver("logos", ctx)


def test_nome_exato_ganha_do_prefixo(vault, ctx):
    """'Climatizando' casa exato mesmo existindo 'Climatizando notas'."""
    (vault / "brain/Climatizando notas.md").write_text("# Clima notas\n", encoding="utf-8")
    assert "Atos normativos" in ver("climatizando", ctx)


def test_ver_nome_ambiguo_lista_candidatos(vault, ctx):
    (vault / "brain/Climatizando notas.md").write_text("# Clima notas\n", encoding="utf-8")
    resposta = ver("clima", ctx)
    assert "mais de uma nota" in resposta
    assert "Climatizando" in resposta and "Climatizando notas" in resposta


def test_ver_nome_inexistente(vault, ctx):
    assert "Não achei" in ver("inexistente", ctx)


def test_nota_longa_vira_indice_de_secoes(vault, ctx):
    (vault / "work/active/PPJ.md").write_text(
        "---\nstatus: active\n---\n\n# PPJ\n\n" + "".join(
            f"## Seção {n}\n\n{'texto ' * 120}\n\n" for n in range(1, 6)
        ),
        encoding="utf-8",
    )
    resposta = ver("ppj", ctx)
    assert "5 seções" in resposta
    assert "Seção 1" in resposta and "Seção 5" in resposta
    assert "texto texto" not in resposta  # o índice não despeja o corpo


def test_ver_secao_por_numero(vault, ctx):
    resposta = ver("ppj 3", ctx)
    assert "Reunião 2026-09-21" in resposta and "Trinta e duas reuniões" in resposta


def test_ver_secao_fora_da_faixa(vault, ctx):
    """ConsultaError sobe até o bot, que a devolve como texto no chat — ver Bot._consultar."""
    with pytest.raises(consulta.ConsultaError, match="3 seções"):
        ver("ppj 99", ctx)


def test_nota_curta_sai_inteira(vault, ctx):
    resposta = ver("climatizando", ctx)
    assert "Atos normativos" in resposta


# ---------------------------------------------------------------------------
# /buscar e /hoje
# ---------------------------------------------------------------------------

def test_buscar_agrupa_por_arquivo(vault, ctx):
    resposta = buscar("Renato", ctx)
    assert "Gotchas" in resposta and "2026-09-18 - Renato" in resposta


def test_buscar_ignora_acento_e_caixa(vault, ctx):
    assert "PPJ" in buscar("reuniao", ctx)


def test_buscar_ignora_o_frontmatter(vault, ctx):
    """A nota do PPJ tem 'Políticas Públicas' na description e no corpo. O resultado é o corpo."""
    resposta = buscar("Políticas Públicas", ctx)
    assert "description:" not in resposta


def test_buscar_sem_resultado(vault, ctx):
    assert "Nada com" in buscar("xyzzy", ctx)


def test_buscar_termo_curto(vault):
    with pytest.raises(consulta.ConsultaError):
        consulta.buscar("a")


def test_hoje_pega_o_briefing_mais_recente(vault, ctx):
    resposta = hoje(ctx)
    assert "Briefing 21" in resposta and "Briefing 20" not in resposta


# ---------------------------------------------------------------------------
# Formato: legível, sem emoji, HTML válido
# ---------------------------------------------------------------------------

def test_emoji_do_template_sai_dos_titulos(vault, ctx):
    resposta = ver("ppj", ctx)
    assert "🏛️" not in resposta and "📁" not in resposta and "🎯" not in resposta


def test_emoji_no_corpo_tambem_sai_sem_desmontar_a_lista(vault, ctx):
    (vault / "work/active/Climatizando.md").write_text(
        "---\nstatus: active\n---\n\n# Climatizando\n\n"
        "⚠️ Relançar o watcher uma vez só.\n\n"
        "- ✅ Primeiro item\n"
        "  - 🎯 Item aninhado\n",
        encoding="utf-8",
    )
    resposta = ver("climatizando", ctx)
    assert "⚠️" not in resposta and "✅" not in resposta and "🎯" not in resposta
    assert "Relançar o watcher uma vez só." in resposta
    assert "- Primeiro item" in resposta
    assert "  - Item aninhado" in resposta  # a indentação da lista sobrevive


def test_seta_sobrevive(vault):
    """Seta não é emoji: 'Tactiq → Drive' perde o sentido sem ela."""
    assert consulta.limpar("Tactiq → Drive → vault") == "Tactiq → Drive → vault"


def test_markdown_vira_texto_legivel(vault, ctx):
    resposta = ver("ppj", ctx)
    assert "Cíclica" in resposta and "[[" not in resposta
    assert "**" not in resposta
    assert "| :--- |" not in resposta


def test_callout_vira_linha_simples(vault, ctx):
    resposta = ver("ppj 3", ctx)
    assert "[!note]" not in resposta
    assert "Note: O número vem do Tactiq" in resposta


def test_conteudo_do_vault_e_escapado(vault, ctx):
    """O briefing tem '<Renato> & equipe'. Sem escapar, a Bot API devolve 400."""
    resposta = hoje(ctx)
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
# Botões: o fluxo inteiro sem digitar nada
# ---------------------------------------------------------------------------

def test_listar_e_abrir_pelo_botao(vault, ctx):
    """O fluxo que ele vai usar: Início → projeto → seção, só tocando."""
    tela = consulta.cmd_inicio(ctx)
    primeiro = consulta.projetos()[0].stem
    nota = _tocar(tela, primeiro, ctx)
    assert primeiro in nota.texto


def test_secao_pelo_botao_e_volta_ao_indice(vault, ctx):
    (vault / "work/active/PPJ.md").write_text(
        "---\nstatus: active\n---\n\n# PPJ\n\n" + "".join(
            f"## Seção {n}\n\n{'texto ' * 200}\n\n" for n in range(1, 4)
        ),
        encoding="utf-8",
    )
    indice = consulta.cmd_ver("ppj", ctx)
    secao = _tocar(indice, "Seção 2", ctx)
    assert "2 de 3" in secao.texto
    assert "Seção 3" in _tocar(secao, "›", ctx).texto        # próxima
    assert "1 de 3" in _tocar(secao, "Anterior", ctx).texto
    assert "3 seções" in _tocar(secao, "Índice", ctx).texto


def test_botao_antigo_abre_sempre_a_mesma_nota(vault, ctx):
    """Com botões, a mensagem velha continua tocável. O botão tem de apontar para a nota, não
    para "o primeiro da última lista" — senão abriria outra nota sem avisar."""
    velha = consulta.cmd_ver("", ctx)
    rotulo, dado = _botoes(velha)[0]
    consulta.cmd_buscar("Renato", ctx)       # outra lista no meio
    consulta.cmd_pastas(ctx)
    assert rotulo.split(" · ")[0].split(" ", 1)[1] in consulta.cmd_botao(dado, ctx).texto


def test_botao_de_antes_do_reinicio_venceu(vault, ctx):
    """Processo novo, códigos novos. O botão velho diz que venceu em vez de abrir outra nota."""
    dado = _botoes(consulta.cmd_ver("", ctx))[0][1]
    novo = consulta.Contexto()
    consulta.cmd_ver("", novo)               # o processo novo já numerou as mesmas notas
    with pytest.raises(consulta.ConsultaError, match="reinício"):
        consulta.cmd_botao(dado, novo)


def test_botao_desconhecido_explica(vault, ctx):
    with pytest.raises(consulta.ConsultaError):
        consulta.cmd_botao("zz", ctx)
    with pytest.raises(consulta.ConsultaError):
        consulta.cmd_botao("s:xx:abc", ctx)


def test_dado_de_todo_botao_cabe_no_limite_da_api(vault_ligado, ctx):
    """A Bot API recusa `callback_data` acima de 64 bytes — e recusa a mensagem inteira."""
    telas = [consulta.cmd_inicio(ctx), consulta.cmd_ver("", ctx), consulta.cmd_pastas(ctx),
             consulta.cmd_buscar("Renato", ctx), consulta.cmd_ver("key decisions", ctx),
             consulta.cmd_ver("um nome comprido que não existe em nota nenhuma do vault", ctx)]
    for tela in telas:
        for rotulo, dado in _botoes(tela):
            assert 1 <= len(dado.encode("utf-8")) <= 64, dado
            assert rotulo.strip()


def test_toda_tela_tem_saida(vault, ctx):
    """Botão é o mecanismo de navegação: tela sem botão é beco sem saída."""
    for tela in (
        consulta.cmd_ver("", ctx),
        consulta.cmd_ver("climatizando", ctx),
        consulta.cmd_buscar("Renato", ctx),
        consulta.cmd_buscar("xyzzy", ctx),
        consulta.cmd_ver("inexistente", ctx),
        consulta.cmd_inicio(ctx),
        consulta.cmd_ajuda(),
        consulta.cmd_hoje(ctx),
        consulta.cmd_pastas(ctx),
    ):
        assert tela.botoes, tela.texto[:80]


def test_texto_nao_tem_mais_numero_comando(vault, ctx):
    """O pedido de 22/09/2026: nada de /p1, /s2, /r1 na tela."""
    for tela in (consulta.cmd_ver("", ctx), consulta.cmd_ver("ppj", ctx),
                 consulta.cmd_buscar("Renato", ctx), consulta.cmd_inicio(ctx),
                 consulta.cmd_pastas(ctx)):
        assert not re.search(r"/[psrdl]\d", _t(tela)), tela.texto[:120]


def test_nao_achei_oferece_buscar_o_termo(vault, ctx):
    tela = consulta.cmd_ver("submit", ctx)
    assert "Não achei" in tela.texto
    assert "Renato" in _tocar(tela, "Buscar", ctx).texto


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
    tela = consulta.cmd_pastas(ctx)
    for termo in PROIBIDO:
        assert termo.lower() not in _t(tela).lower(), termo
    assert "work/active" in _t(tela)
    pastas_ = [b for b in _botoes(tela) if b[1].startswith("d:")]
    assert len(pastas_) == len(consulta.allowed_roots())


def test_pasta_mostra_subpastas_e_notas(vault_ligado, ctx):
    active = _tocar(consulta.cmd_pastas(ctx), "work/active", ctx)
    assert "Logos" in _t(active) and "PPJ" in _t(active)
    sub = _tocar(active, "Logos", ctx)
    assert "work/active/Logos" in sub.texto and "Plano" in _t(sub)
    # A nota aberta de dentro da pasta volta para ela; a pasta volta para a de cima.
    nota = _tocar(sub, "Plano", ctx)
    assert "work/active/Logos" in _tocar(nota, consulta.MARCA_VOLTA, ctx).texto
    assert "<b>work/active</b>" in _tocar(sub, consulta.MARCA_VOLTA, ctx).texto
    # E a raiz legível volta para a lista de pastas, nunca para a mãe fora da allowlist.
    assert "Pastas que eu leio" in _tocar(active, consulta.MARCA_VOLTA, ctx).texto


def test_pasta_grande_mostra_as_recentes_e_aponta_a_busca(vault, ctx, monkeypatch):
    monkeypatch.setattr(consulta, "MAX_NOTAS_PASTA", 1)
    tela = consulta.render_pasta(vault / "briefings", *consulta.conteudo_pasta(vault / "briefings"), ctx)
    notas = [b for b in _botoes(tela) if b[1].startswith("o:")]
    assert len(notas) == 1
    assert "outras 1" in tela.texto and "busca" in tela.texto


def test_pasta_fora_da_allowlist_e_recusada(vault, ctx):
    with pytest.raises(consulta.ConsultaError, match="fora das que eu leio"):
        consulta.conteudo_pasta(vault / "capturas" / "Diario")
    with pytest.raises(consulta.ConsultaError, match="fora das que eu leio"):
        consulta.conteudo_pasta(vault / "capturas")   # a mãe do Diário também não


def test_links_resolvem_por_nome_e_alias_e_somem_fora_da_allowlist(vault_ligado, ctx):
    tela = consulta.cmd_ver("key decisions", ctx)
    # O corpo da nota cita o diário pelo nome, e o corpo sempre foi mostrado como está; o que
    # não pode é um botão transformar esse nome num atalho que abre alguma coisa.
    links = [r for r, d in _botoes(tela) if r.startswith((consulta.MARCA_LINK, consulta.MARCA_CITADA))]
    for termo in PROIBIDO + ("2026-09-20", "Inexistente"):
        assert not any(termo.lower() in r.lower() for r in links), termo
    # PPJ pelo nome, Clima pelo alias; Clima também cita esta nota, mas não aparece duas vezes.
    assert [r.split(" ", 1)[1] for r in links] == ["PPJ", "Clima"]
    assert "<b>Clima</b>" in _tocar(tela, "Clima", ctx).texto


def test_citada_em_lista_quem_aponta_para_a_nota(vault_ligado, ctx):
    tela = consulta.cmd_ver("ppj", ctx)
    assert "citada em 1" in tela.texto
    assert f"{consulta.MARCA_CITADA} Key Decisions" in _t(tela)


def test_muitas_ligacoes_viram_um_botao_so(vault_ligado, ctx, monkeypatch):
    monkeypatch.setattr(consulta, "MAX_LIGACOES_NA_NOTA", 1)
    tela = consulta.cmd_ver("key decisions", ctx)
    assert not any(r.startswith(consulta.MARCA_CITADA) for r, _ in _botoes(tela))
    lista = _tocar(tela, "Ligações (2)", ctx)
    assert "Clima" in _t(lista) and "PPJ" in _t(lista)
    assert "<b>Key Decisions</b>" in _tocar(lista, consulta.MARCA_VOLTA, ctx).texto


def test_aliases_em_lista_e_entre_colchetes():
    assert consulta._aliases("---\naliases:\n  - A\n  - 'B'\ntags: x\n---\n") == ["A", "B"]
    assert consulta._aliases("---\naliases: [A, \"B\"]\n---\n") == ["A", "B"]
    assert consulta._aliases("# sem frontmatter\naliases:\n  - A\n") == []


def test_navegacao_nao_vaza_o_diario(vault_ligado, ctx):
    """A trava de `test_diario_nunca_aparece`, passada por todo botão alcançável: percorre a
    árvore inteira a partir do Início e das pastas, tocando em tudo."""
    vistos: set[str] = set()
    fila = [consulta.cmd_inicio(ctx), consulta.cmd_pastas(ctx)]
    for nome in ("key decisions", "clima", "ppj", "logos"):
        fila.append(consulta.cmd_ver(nome, ctx))
    telas = 0
    while fila:
        tela = fila.pop()
        telas += 1
        for termo in PROIBIDO:
            assert termo.lower() not in _t(tela).lower(), f"vazou “{termo}” em: {_t(tela)[:200]}"
        for _, dado in _botoes(tela):
            if dado in vistos or dado in ("b", "j", "a") or dado.startswith("q:"):
                continue
            vistos.add(dado)
            fila.append(consulta.cmd_botao(dado, ctx))
    assert telas > 15   # percorreu de verdade, não parou na primeira tela


def test_resultado_de_busca_abre_pelo_botao(vault, ctx):
    busca = consulta.cmd_buscar("Renato", ctx)
    assert "Lembrar do submit" in _tocar(busca, "2026-09-18 - Renato", ctx).texto


def test_nome_ambiguo_vira_botoes(vault, ctx):
    (vault / "brain/Climatizando notas.md").write_text("# Clima notas\n", encoding="utf-8")
    tela = consulta.cmd_ver("clima", ctx)
    assert "Clima notas" in _tocar(tela, "Climatizando notas", ctx).texto


def test_inicio_orienta_e_os_botoes_funcionam(vault, ctx):
    tela = consulta.cmd_inicio(ctx)
    assert "Para onde" in tela.texto
    assert "Briefing 21" in _tocar(tela, "Briefing", ctx).texto
    assert "Pastas que eu leio" in _tocar(tela, "Pastas", ctx).texto
    assert ("🔎 Buscar", "b") in _botoes(tela)
    assert any(d == "j" for _, d in _botoes(tela))


def test_inicio_com_vault_vazio(tmp_path, monkeypatch, ctx):
    monkeypatch.setattr(config, "VAULT_DIR", tmp_path / "vazio")
    tela = consulta.cmd_inicio(ctx)
    assert "Para onde" in tela.texto and ("🔎 Buscar", "b") in _botoes(tela)


def test_ajuda_cabe_numa_tela(vault):
    ajuda_ = consulta.cmd_ajuda()
    assert len(ajuda_.texto) < consulta.LIMITE_MENSAGEM
    assert "/inicio" in ajuda_.texto


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
    secao = _tocar(consulta.cmd_ver("ppj", ctx), "Seção 2", ctx)
    for parte in consulta.partir(secao.texto):
        assert len(parte) <= consulta.LIMITE_MENSAGEM


def test_marcadores_sao_estaveis(vault, ctx):
    """Marcador é semântica: o mesmo tipo de resposta abre sempre com o mesmo símbolo."""
    assert ver("", ctx).startswith(consulta.MARCA_PROJETO)
    assert buscar("Renato", ctx).startswith(consulta.MARCA_BUSCA)
    assert hoje(ctx).startswith(consulta.MARCA_BRIEFING)
    assert inicio(ctx).startswith(consulta.MARCA_INICIO)
    assert ver("climatizando", ctx).startswith(consulta.MARCA_NOTA)


def test_indice_do_briefing_volta_com_a_marca_do_briefing(vault, ctx):
    """Marcador é semântica: briefing → seção → índice não pode devolver o briefing como nota
    comum. A marca sai do caminho, porque o botão de voltar não sabe de onde a nota veio."""
    (vault / "briefings/2026-09-21.md").write_text(
        "# Briefing 21\n\n" + "".join(
            f"## Bloco {n}\n\n{'texto ' * 200}\n\n" for n in range(1, 5)
        ),
        encoding="utf-8",
    )
    tela = consulta.cmd_hoje(ctx)
    assert tela.texto.startswith(consulta.MARCA_BRIEFING)
    secao = _tocar(tela, "Bloco 1", ctx)
    assert _tocar(secao, "Índice", ctx).texto.startswith(consulta.MARCA_BRIEFING)


def test_indice_de_projeto_segue_com_a_marca_de_nota(vault, ctx):
    secao = consulta.cmd_ver("ppj 1", ctx)
    assert _tocar(secao, "Índice", ctx).texto.startswith(consulta.MARCA_NOTA)


def test_wikilink_com_ancora_perde_a_cerquilha(vault):
    assert consulta.limpar("ver [[#Reunião 2026-08-31]]") == "ver Reunião 2026-08-31"
    assert consulta.limpar("ver [[PPJ#Equipe]]") == "ver Equipe"
    assert consulta.limpar("ver [[PPJ#Equipe|a equipe]]") == "ver a equipe"
    assert consulta.limpar("ver [[PPJ]]") == "ver PPJ"

