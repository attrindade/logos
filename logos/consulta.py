"""Consulta de leitura ao vault pelo bot do Telegram: `/ver`, `/buscar` e `/hoje`.

Por que existe: o vault só produzia valor para quem abrisse o Obsidian, e o dono não abre.
O briefing das 8h, a nota pronta e os avisos já chegam pelo chat; faltava o caminho inverso —
perguntar ao vault sem sair do Telegram. A Fase 3 do plano previa "vault no celular (leitura)"
por uma terceira pasta Syncthing; isto entrega a leitura sem sincronizar arquivo nenhum para o
aparelho, o que também dissolve a objeção de privacidade registrada lá.

Três invariantes, nesta ordem de importância:

1. **Allowlist, nunca denylist.** Só as pastas de `ALLOWED_SUBDIRS` são lidas. `capturas/Diario/`
   (o diário), `.claude/om-jobs-state/` (a blocklist de reuniões, com nome de terapeuta),
   `thinking/` e `memories/` ficam de fora por construção, não por exclusão — uma pasta nova no
   vault nasce invisível para o bot, e é assim que tem de ser. Todo caminho devolvido passa por
   `_is_allowed`, inclusive os que nascem de texto do usuário (`/ver ../../capturas/Diario`).
2. **Somente leitura.** Nenhuma função deste módulo escreve, move ou apaga.
3. **Sem LLM.** Listar, abrir e procurar são determinísticos. Consulta atrás do Ollama herdaria
   a falha silenciosa que custou a sessão de 2026-09-21; pergunta em linguagem livre é outro
   recurso, não este.

A formatação sai em HTML da Bot API (`parse_mode="HTML"`), e só por este caminho: `send()` e
`notify()` seguem em texto puro, porque o briefing e os jobs mandam `<` e `&` sem escapar.
"""
import html
import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

# Pastas lidas, relativas à raiz do vault. Ver invariante 1 no topo do módulo.
ALLOWED_SUBDIRS = (
    "work/active",
    "brain",
    "org",
    "briefings",
    "capturas/Inbox",
    "capturas/Planejamento",
)

# `work/meetings` fica fora de propósito: transcrição de reunião é o material com maior chance
# de conter terapia se a blocklist do drive-meetings.ps1 falhar, e o que interessa dela já foi
# promovido para a nota do projeto pelo /om-intake.

LIMITE_MENSAGEM = 4000       # margem sobre os 4096 da API
LIMITE_NOTA_INTEIRA = 3000   # acima disso a nota vira índice de seções em vez de texto corrido
MAX_ARQUIVOS_BUSCA = 8
MAX_LINHAS_POR_ARQUIVO = 4
MAX_LINHA = 200
MAX_PROJETOS = 20

_SYNCTHING_TMP = re.compile(r"^~syncthing~")
_EMOJI_INICIAL = re.compile(r"^[\s\W_]*?([\wÀ-ɏ])", re.UNICODE)
# Pictogramas, em qualquer posição. O dono pediu leitura sem emoji, e o template herdado os
# espalha por títulos e linhas de corpo. As setas (←-⇿) ficam de fora da faixa de
# propósito: "Tactiq -> Drive -> work/meetings" vira ruído sem elas.
_PICTOGRAMAS = re.compile(
    "["
    "\U0001f000-\U0001faff"  # emoji, bandeiras, símbolos suplementares
    "☀-➿"          # miscelânea e dingbats: ⚠ ⚖ ✅ ✍
    "⬀-⯿"          # setas e formas decorativas
    "️⃣"           # seletor de variação e teclado combinante
    "]"
)
_CABECALHO = re.compile(r"^(#{1,6})\s+(.*)$")
_SEPARADOR_TABELA = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_LINK_MD = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_NEGRITO = re.compile(r"\*\*([^*]+)\*\*")
_RISCADO = re.compile(r"~~([^~]+)~~")
_CALLOUT = re.compile(r"^>\s*\[!(\w+)\][-+]?\s*(.*)$")


class ConsultaError(Exception):
    """Pedido que o usuário pode corrigir. A mensagem vai direto para o chat."""


# ---------------------------------------------------------------------------
# Raiz do vault e allowlist
# ---------------------------------------------------------------------------

def vault_root() -> Path:
    """Mesma regra do writer: `vault_dir` do config, ou a pasta-mãe de `notes_dir`."""
    return (config.VAULT_DIR or config.NOTES_DIR.parent).resolve()


def allowed_roots() -> list[Path]:
    """Só as pastas de ALLOWED_SUBDIRS que existem de fato. Pasta ausente some da busca em vez
    de derrubá-la: o vault do dono e o vault de teste não têm as mesmas pastas."""
    root = vault_root()
    return [d for d in ((root / sub) for sub in ALLOWED_SUBDIRS) if d.is_dir()]


def _is_allowed(path: Path) -> bool:
    """Guarda final. Roda em todo caminho que sai deste módulo, inclusive nos que nascem de
    texto do usuário — `/ver ../../capturas/Diario/x` resolve para fora e é recusado aqui."""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any(resolved == root or resolved.is_relative_to(root) for root in allowed_roots())


def _visivel(path: Path) -> bool:
    name = path.name
    return (
        path.suffix.lower() == ".md"
        and not name.startswith(".")
        and not _SYNCTHING_TMP.match(name)
    )


def iter_notas() -> list[Path]:
    """Todas as notas legíveis, sem ordem garantida."""
    notas = []
    for root in allowed_roots():
        for path in root.rglob("*.md"):
            if path.is_file() and _visivel(path) and _is_allowed(path):
                notas.append(path)
    return notas


def rel(path: Path) -> str:
    """Caminho para mostrar no chat, sempre com barra normal."""
    try:
        return path.resolve().relative_to(vault_root()).as_posix()
    except ValueError:
        return path.name


# ---------------------------------------------------------------------------
# Texto: normalização e limpeza de Markdown
# ---------------------------------------------------------------------------

def _fold(text: str) -> str:
    """Minúsculas sem acento, para comparar o que foi digitado com o nome do arquivo."""
    nfkd = unicodedata.normalize("NFKD", (text or "").strip().lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _sem_emoji(text: str) -> str:
    """Tira os pictogramas e o espaço que eles deixam no começo. Um título do template herdado
    ('<pictograma> PPJ') sai como 'PPJ'."""
    text = _PICTOGRAMAS.sub("", text)
    match = _EMOJI_INICIAL.search(text)
    return text[match.start(1):].strip() if match else text.strip()


def _texto_do_wikilink(match: re.Match) -> str:
    """O que se lê de um `[[alvo#ancora|alias]]`: o alias, senão o alvo, senão a âncora.

    O `#` sozinho é o caso de link interno (`[[#Reunião 2026-08-31]]`), que sem este tratamento
    chegava no chat como "ver # Reunião 2026-08-31" — a cerquilha não significa nada para quem lê."""
    alias = (match.group(2) or "").strip()
    if alias:
        return alias
    alvo = (match.group(1) or "").strip()
    return alvo.split("#", 1)[-1].strip() if "#" in alvo else alvo


def limpar(linha: str) -> str:
    """Markdown -> texto legível. Não escapa HTML: quem monta a mensagem escapa depois.

    O pictograma sai em qualquer posição, mas o começo da linha é preservado no resto: tirar o
    '-' de um item de lista junto com o emoji destruiria a estrutura que torna a nota legível."""
    recuo = linha[: len(linha) - len(linha.lstrip(" \t"))]
    linha = recuo + _PICTOGRAMAS.sub("", linha[len(recuo):]).lstrip(" \t")
    linha = _WIKILINK.sub(_texto_do_wikilink, linha)
    linha = _LINK_MD.sub(r"\1", linha)
    linha = _NEGRITO.sub(r"\1", linha)
    linha = _RISCADO.sub(r"\1", linha)
    callout = _CALLOUT.match(linha)
    if callout:
        titulo = callout.group(2).strip()
        rotulo = callout.group(1).capitalize()
        return f"{rotulo}: {titulo}" if titulo else f"{rotulo}:"
    if linha.startswith(">"):
        linha = linha[1:].lstrip()
    # Espaço duplo que o pictograma removido deixou para trás. Só depois do recuo, recalculado
    # aqui porque o ramo da citação já pode tê-lo comido: a indentação inicial é o que segura
    # lista aninhada de pé.
    sobra = linha[: len(linha) - len(linha.lstrip(" \t"))]
    return sobra + re.sub(r"[ \t]{2,}", " ", linha[len(sobra):]).rstrip()


def _sem_frontmatter(texto: str) -> list[str]:
    """Linhas do corpo, com as do frontmatter em branco para o número da linha não escorregar.
    Sem isto, `/buscar renato` devolve a linha `description:` da nota em vez do conteúdo dela."""
    linhas = texto.splitlines()
    if not linhas or linhas[0].strip() != "---":
        return linhas
    for fim, linha in enumerate(linhas[1:], 1):
        if linha.strip() == "---":
            return [""] * (fim + 1) + linhas[fim + 1:]
    return linhas


def _corpo(linhas: list[str]) -> str:
    """Junta linhas de conteúdo, tirando separador de tabela e linha em branco repetida."""
    saida, vazias = [], 0
    for bruta in linhas:
        if _SEPARADOR_TABELA.match(bruta):
            continue
        cabecalho = _CABECALHO.match(bruta)
        linha = _sem_emoji(cabecalho.group(2)) if cabecalho else limpar(bruta)
        if not linha.strip():
            vazias += 1
            if vazias > 1:
                continue
        else:
            vazias = 0
        saida.append(linha)
    return "\n".join(saida).strip()


# ---------------------------------------------------------------------------
# Leitura de nota
# ---------------------------------------------------------------------------

class Nota:
    """Uma nota do vault dividida em frontmatter, introdução e seções de nível 2."""

    def __init__(self, path: Path, texto: str):
        self.path = path
        self.meta, corpo = self._separar_frontmatter(texto)
        self.titulo, self.intro, self.secoes = self._dividir(corpo)

    @staticmethod
    def _separar_frontmatter(texto: str) -> tuple[dict, str]:
        if not texto.startswith("---"):
            return {}, texto
        partes = texto.split("\n---", 1)
        if len(partes) < 2:
            return {}, texto
        meta = {}
        for linha in partes[0].splitlines()[1:]:
            if ":" in linha and not linha.startswith((" ", "-", "\t")):
                chave, _, valor = linha.partition(":")
                meta[chave.strip()] = valor.strip().strip('"').strip("'")
        return meta, partes[1].lstrip("-\n")

    def _dividir(self, corpo: str) -> tuple[str, str, list[tuple[str, list[str]]]]:
        titulo = ""
        intro: list[str] = []
        secoes: list[tuple[str, list[str]]] = []
        atual: list[str] | None = None
        for linha in corpo.splitlines():
            cabecalho = _CABECALHO.match(linha)
            nivel = len(cabecalho.group(1)) if cabecalho else 0
            if nivel == 1 and not titulo:
                titulo = _sem_emoji(cabecalho.group(2))
                continue
            if nivel == 2:
                atual = []
                secoes.append((_sem_emoji(cabecalho.group(2)), atual))
                continue
            (atual if atual is not None else intro).append(linha)
        return titulo or self.path.stem, _corpo(intro), secoes

    @property
    def nome(self) -> str:
        return self.path.stem

    @property
    def atualizada(self) -> datetime:
        return datetime.fromtimestamp(self.path.stat().st_mtime)


def carregar(path: Path) -> Nota:
    if not _is_allowed(path):
        raise ConsultaError("Essa nota está fora das pastas que eu leio.")
    return Nota(path, path.read_text(encoding="utf-8", errors="replace"))


# ---------------------------------------------------------------------------
# Resolução de nome
# ---------------------------------------------------------------------------

def projetos() -> list[Path]:
    """Notas de projeto ativo, da mexida mais recente para a mais antiga."""
    pasta = vault_root() / "work" / "active"
    if not pasta.is_dir():
        return []
    notas = [p for p in pasta.glob("*.md") if p.is_file() and _visivel(p)]
    return sorted(notas, key=lambda p: p.stat().st_mtime, reverse=True)


def resolver(nome: str) -> list[Path]:
    """Casa o texto digitado com o nome do arquivo, sem acento e sem caixa. Projeto ativo ganha
    do resto: '/ver logos' é a nota do projeto, não uma captura que fala de Logos.

    Devolve lista: zero, um ou vários candidatos — quem chama decide o que dizer."""
    alvo = _fold(nome)
    if not alvo:
        return []
    ativos = projetos()
    universo = ativos + [p for p in iter_notas() if p not in ativos]

    exatos = [p for p in universo if _fold(p.stem) == alvo]
    if exatos:
        return exatos[:1]
    prefixo = [p for p in universo if _fold(p.stem).startswith(alvo)]
    if prefixo:
        return prefixo
    return [p for p in universo if alvo in _fold(p.stem)]


def briefing_recente() -> Path | None:
    """O briefing mais novo pelo nome do arquivo (YYYY-MM-DD.md ordena sozinho)."""
    pasta = vault_root() / "briefings"
    if not pasta.is_dir():
        return None
    arquivos = sorted((p for p in pasta.glob("*.md") if p.is_file() and _visivel(p)), key=lambda p: p.name)
    return arquivos[-1] if arquivos else None


# ---------------------------------------------------------------------------
# Busca
# ---------------------------------------------------------------------------

def buscar(termo: str) -> list[tuple[Path, list[tuple[int, str]]]]:
    """Linhas que contêm o termo, agrupadas por arquivo, do arquivo mexido mais recentemente
    para o mais antigo. Sem acento e sem caixa dos dois lados, para 'reuniao' achar 'reunião'."""
    alvo = _fold(termo)
    if len(alvo) < 2:
        raise ConsultaError("Busca precisa de pelo menos dois caracteres.")

    achados: list[tuple[Path, list[tuple[int, str]], float]] = []
    for path in iter_notas():
        try:
            texto = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        linhas = []
        for numero, bruta in enumerate(_sem_frontmatter(texto), 1):
            if alvo in _fold(bruta):
                limpa = limpar(bruta).strip().lstrip("#").strip()
                if limpa:
                    linhas.append((numero, limpa))
                if len(linhas) >= MAX_LINHAS_POR_ARQUIVO:
                    break
        if linhas:
            achados.append((path, linhas, path.stat().st_mtime))

    achados.sort(key=lambda item: item[2], reverse=True)
    return [(path, linhas) for path, linhas, _ in achados[:MAX_ARQUIVOS_BUSCA]]


# ---------------------------------------------------------------------------
# Formatação para o chat (HTML da Bot API)
# ---------------------------------------------------------------------------

# Marcadores: um por tipo de linha, sempre o mesmo. Emoji aqui é semântica, não enfeite — é o
# que deixa a resposta reconhecível antes de ser lida. O que vem do vault continua passando por
# `_PICTOGRAMAS`, porque o emoji do template herdado não marca coisa nenhuma.
MARCA_PROJETO = "\U0001f4c1"
MARCA_NOTA = "\U0001f4c4"
MARCA_SECAO = "\u25b8"
MARCA_BUSCA = "\U0001f50e"
MARCA_BRIEFING = "\u2600\ufe0f"
MARCA_DICA = "\U0001f4a1"
MARCA_NADA = "\u2205"
MARCA_INICIO = "\U0001f9ed"

MAX_ATALHOS_INICIO = 5

LIMITE_CITACAO = 700   # acima disso a citação nasce recolhida, com um toque para expandir

_TAG_CITACAO = re.compile(r"<blockquote(?: expandable)?>|</blockquote>")


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def sem_tags(text: str) -> str:
    """Desfaz a formatação. Só o caminho de degradação do bot usa: se a API recusar o HTML,
    a resposta vai crua em vez de não ir."""
    return html.unescape(re.sub(r"</?(?:b|i|u|s|code|pre|a|blockquote)\b[^>]*>", "", text))


def _data(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%d/%m")


def _cortar(text: str, limite: int = MAX_LINHA) -> str:
    text = text.strip()
    return text if len(text) <= limite else text[: limite - 1].rstrip() + "\u2026"


def _quebrar_linha(linha: str, limite: int) -> list[str]:
    """Parte uma linha maior que o limite, sempre num espaço."""
    pedacos = []
    while len(linha) > limite:
        corte = linha.rfind(" ", 0, limite)
        corte = corte if corte > limite // 2 else limite
        pedacos.append(linha[:corte].rstrip())
        linha = linha[corte:].lstrip()
    pedacos.append(linha)
    return pedacos


def citacao(texto: str, expansivel: bool | None = None) -> str:
    """Corpo em `<blockquote>`, recolhido quando é longo.

    O expansível resolve o dilema do índice contra o despejo: a nota chega inteira, mas ocupa
    quatro linhas na tela até ele tocar. Nenhuma linha de dentro pode passar do limite de uma
    mensagem, senão o `partir` teria de cortar no meio de uma tag."""
    if expansivel is None:
        expansivel = len(texto) > LIMITE_CITACAO
    linhas = []
    for linha in texto.splitlines():
        linhas.extend(_quebrar_linha(linha, LIMITE_MENSAGEM - 120))
    corpo = "\n".join(linhas)
    abertura = "<blockquote expandable>" if expansivel else "<blockquote>"
    return f"{abertura}{corpo}</blockquote>"


def _estado_citacao(linha: str, aberto: str | None) -> str | None:
    for m in _TAG_CITACAO.finditer(linha):
        aberto = None if m.group(0).startswith("</") else m.group(0)
    return aberto


def partir(texto: str, limite: int = LIMITE_MENSAGEM) -> list[str]:
    """Divide em mensagens por linha inteira, fechando e reabrindo a citação na fronteira.

    Cortar por caractere, como o `_chunks` do telegram.py faz, partiria uma tag ao meio e a API
    recusaria a mensagem. Uma citação que não cabe numa mensagem vira uma citação por mensagem,
    o que lê melhor do que um bloco truncado."""
    partes: list[str] = []
    prefixo = ""       # citação reaberta no começo desta parte
    atual = ""
    aberto: str | None = None   # citação aberta no fim de `atual`

    def fechar() -> None:
        nonlocal prefixo, atual
        if atual.strip():
            partes.append(prefixo + atual.rstrip() + ("</blockquote>" if aberto else ""))
            prefixo = aberto or ""
        atual = ""

    for bruta in texto.splitlines(keepends=True):
        fim = "\n" if bruta.endswith("\n") else ""
        for pedaco in _quebrar_linha(bruta.rstrip("\n"), limite - 60):
            linha = pedaco + fim
            if len(prefixo) + len(atual) + len(linha) > limite:
                fechar()
            atual += linha
            aberto = _estado_citacao(linha, aberto)
    fechar()
    return partes or [""]


def _meta_linha(nota: Nota) -> str:
    campos = [nota.meta.get(c) for c in ("status", "quarter")]
    campos = [c for c in campos if c]
    campos.append(f"atualizada {nota.atualizada.strftime('%d/%m')}")
    return " \u00b7 ".join(campos)


def dica(*sugestoes: str) -> str:
    """Rodapé com o próximo passo. Toda resposta tem um: é assim que ele aprende o vocabulário
    sem nunca ler a ajuda."""
    return f"\n{MARCA_DICA} " + "  \u00b7  ".join(s for s in sugestoes if s)


# ---------------------------------------------------------------------------
# Contexto: o que faz /p1, /s2 e /r3 significarem alguma coisa
# ---------------------------------------------------------------------------

class Contexto:
    """Memória curta do chat: a última lista de projetos, a nota aberta e a última busca.

    Em memória e sem persistência, de propósito: o bot é de uma pessoa só, e um índice que
    sobrevive a um relançamento é pior que nenhum — apontaria para outra nota sem avisar. Depois
    de uma queda, `/p3` responde que a lista venceu."""

    def __init__(self) -> None:
        self.projetos: list[Path] = []
        self.resultados: list[Path] = []
        self.nota: Path | None = None
        self.secoes: list[str] = []

    def lembrar_projetos(self, notas: list[Path]) -> None:
        self.projetos = list(notas)

    def lembrar_resultados(self, notas: list[Path]) -> None:
        self.resultados = list(notas)

    def lembrar_nota(self, nota: "Nota") -> None:
        self.nota = nota.path
        self.secoes = [titulo for titulo, _ in nota.secoes]

    def escolher(self, letra: str, numero: int) -> Path:
        lista = {"p": self.projetos, "r": self.resultados}[letra]
        rotulo = {"p": "lista de projetos", "r": "busca"}[letra]
        if not lista:
            raise ConsultaError(
                f"Não tenho essa {rotulo} aqui. Mande /ver ou /buscar de novo."
            )
        if not 1 <= numero <= len(lista):
            raise ConsultaError(f"A {rotulo} tem {len(lista)} itens, e você pediu o {numero}.")
        return lista[numero - 1]


# ---------------------------------------------------------------------------
# Renderização
# ---------------------------------------------------------------------------

def render_projetos(notas: list[Path]) -> str:
    if not notas:
        return f"{MARCA_NADA} Nenhum projeto ativo em <code>work/active</code>."
    linhas = [f"{MARCA_PROJETO} <b>Projetos ativos</b>", ""]
    for indice, path in enumerate(notas[:MAX_PROJETOS], 1):
        linhas.append(f"/p{indice}  <b>{esc(path.stem)}</b>  <i>{_data(path)}</i>")
    return "\n".join(linhas) + dica("/p1 abre o primeiro", "/buscar termo", "/ajuda")


def render_nota(nota: Nota, marca: str = MARCA_NOTA) -> str:
    cabeca = [f"{marca} <b>{esc(nota.titulo)}</b>", f"<i>{esc(_meta_linha(nota))}</i>"]
    descricao = nota.meta.get("description")

    inteira = "\n\n".join(
        [nota.intro] + [f"{titulo}\n{_corpo(linhas)}" for titulo, linhas in nota.secoes]
    ).strip()

    if len(inteira) <= LIMITE_NOTA_INTEIRA:
        corpo = citacao(esc(inteira)) if inteira else "<i>Nota sem conteúdo além do frontmatter.</i>"
        return "\n".join(cabeca + ["", corpo]) + dica("/ver volta aos projetos", "/ajuda")

    # Nota longa: as seções viram comandos. Ninguém lê 14 KB de uma vez no celular, e digitar
    # o nome da seção seria pior do que rolar a nota no Obsidian.
    partes = list(cabeca)
    if descricao:
        partes += ["", citacao(esc(_cortar(descricao, 400)), expansivel=False)]
    elif nota.intro:
        partes += ["", citacao(esc(_cortar(nota.intro, 400)), expansivel=False)]
    partes += ["", f"<b>{len(nota.secoes)} seções</b>"]
    for indice, (titulo, _) in enumerate(nota.secoes, 1):
        partes.append(f"/s{indice}  {MARCA_SECAO} {esc(titulo)}")
    return "\n".join(partes) + dica("/s1 abre a primeira", "/ver volta aos projetos")


def render_secao(nota: Nota, indice: int) -> str:
    if not nota.secoes:
        raise ConsultaError(f"{nota.titulo} não tem seções. A nota inteira já veio acima.")
    if not 1 <= indice <= len(nota.secoes):
        raise ConsultaError(f"{nota.titulo} tem {len(nota.secoes)} seções, e você pediu a {indice}.")
    titulo, linhas = nota.secoes[indice - 1]
    corpo = _corpo(linhas) or "Seção vazia."
    cabeca = (
        f"{MARCA_SECAO} <b>{esc(titulo)}</b>\n"
        f"<i>{esc(nota.titulo)} \u00b7 {indice} de {len(nota.secoes)}</i>"
    )
    vizinhas = []
    if indice < len(nota.secoes):
        vizinhas.append(f"/s{indice + 1} {esc(nota.secoes[indice][0])}")
    if indice > 1:
        vizinhas.append(f"/s{indice - 1} anterior")
    vizinhas.append("/s0 índice")
    return f"{cabeca}\n\n{citacao(esc(corpo))}" + dica(*vizinhas)


def render_busca(termo: str, achados: list[tuple[Path, list[tuple[int, str]]]]) -> str:
    if not achados:
        return (
            f"{MARCA_NADA} Nada com <b>{esc(termo)}</b> nas notas que eu leio."
            + dica("/ver lista os projetos", "/ajuda")
        )
    partes = [f"{MARCA_BUSCA} <b>{esc(termo)}</b> \u2014 {len(achados)} arquivo(s)", ""]
    for indice, (path, linhas) in enumerate(achados, 1):
        partes.append(f"/r{indice}  <b>{esc(path.stem)}</b>  <i>{esc(_pasta(path))}</i>")
        trecho = "\n".join(esc(_cortar(linha)) for _, linha in linhas)
        partes.append(citacao(trecho))
        partes.append("")
    return "\n".join(partes).rstrip() + dica("/r1 abre o primeiro", "/ver lista os projetos")


def render_ambiguo(nome: str, candidatos: list[Path]) -> str:
    partes = [f"<b>{esc(nome)}</b> casa com mais de uma nota:", ""]
    for indice, path in enumerate(candidatos[:10], 1):
        partes.append(f"/r{indice}  <b>{esc(path.stem)}</b>  <i>{esc(_pasta(path))}</i>")
    if len(candidatos) > 10:
        partes.append(f"<i>\u2026 e mais {len(candidatos) - 10}.</i>")
    return "\n".join(partes) + dica("/r1 abre o primeiro")


def _pasta(path: Path) -> str:
    caminho = rel(path)
    return caminho.rsplit("/", 1)[0] if "/" in caminho else caminho


def render_inicio(notas: list[Path], briefing: Path | None) -> str:
    """Tela inicial do `/0`: para onde ir, com os atalhos já carregados.

    Não pergunta e espera resposta — mostra as saídas possíveis já numeradas, que é mais rápido
    do que perguntar. Os `/pN` daqui valem na hora, porque o contexto foi preenchido junto."""
    partes = [f"{MARCA_INICIO} <b>Para onde?</b>", ""]
    if notas:
        partes.append(f"{MARCA_PROJETO} <b>Projetos</b>")
        for indice, path in enumerate(notas[:MAX_ATALHOS_INICIO], 1):
            partes.append(f"/p{indice}  {esc(path.stem)}  <i>{_data(path)}</i>")
        if len(notas) > MAX_ATALHOS_INICIO:
            partes.append(f"/v  <i>e mais {len(notas) - MAX_ATALHOS_INICIO}</i>")
        partes.append("")
    if briefing is not None:
        partes.append(f"{MARCA_BRIEFING} /h  <b>Briefing</b>  <i>{_data(briefing)}</i>")
    partes.append(f"{MARCA_BUSCA} /b &lt;termo&gt;  <b>Procurar</b> em tudo que eu leio")
    return "\n".join(partes) + dica("/a explica o resto", "ou mande texto/voz para capturar")


def render_ajuda() -> str:
    """Uma tela. É o único texto que ele vai ler para aprender o bot inteiro."""
    return (
        f"{MARCA_DICA} <b>Como mexer aqui</b>\n"
        "\n"
        f"{MARCA_INICIO} <code>/0</code> \u2014 na dúvida, sempre este. Mostra para onde ir.\n"
        "\n"
        "<b>Consultar</b> \u2014 um verbo e um alvo:\n"
        "<code>/ver</code> projetos ativos\n"
        "<code>/ver ppj</code> abre uma nota\n"
        "<code>/buscar renato</code> procura nas notas\n"
        "<code>/hoje</code> o briefing mais recente\n"
        "\n"
        "<b>Os números das respostas são comandos.</b> Toque ou digite:\n"
        f"{MARCA_PROJETO} <code>/p1</code> projeto da lista\n"
        f"{MARCA_SECAO} <code>/s1</code> seção da nota aberta  \u00b7  <code>/s0</code> índice\n"
        f"{MARCA_BUSCA} <code>/r1</code> resultado da busca\n"
        "\n"
        "<b>Atalhos de uma letra:</b> <code>/v</code> <code>/b</code> <code>/h</code> "
        "<code>/a</code>\n"
        "\n"
        "<b>Um fluxo inteiro:</b>\n"
        "<code>/0</code> \u2192 <code>/p1</code> \u2192 <code>/s3</code>\n"
        "\n"
        "<b>Capturar</b> segue igual: mande texto, voz, foto, PDF ou link.\n"
        "<code>/tarefa</code> <code>/ideia</code> <code>/planejamento</code> "
        "<code>/diario</code>  \u00b7  <code>/nova</code> fecha a sessão."
    )


# ---------------------------------------------------------------------------
# Comandos (o bot só despacha; a lógica e as mensagens moram aqui)
# ---------------------------------------------------------------------------

def _abrir(path: Path, ctx: Contexto, marca: str = MARCA_NOTA) -> str:
    nota = carregar(path)
    ctx.lembrar_nota(nota)
    return render_nota(nota, marca)


def cmd_ver(argumento: str, ctx: Contexto) -> str:
    argumento = (argumento or "").strip()
    if not argumento:
        lista = projetos()
        ctx.lembrar_projetos(lista)
        return render_projetos(lista)

    nome, indice = argumento, None
    partes = argumento.rsplit(maxsplit=1)
    if len(partes) == 2 and partes[1].isdigit():
        nome, indice = partes[0], int(partes[1])

    candidatos = resolver(nome)
    if not candidatos:
        return (
            f"{MARCA_NADA} Não achei nota com <b>{esc(nome)}</b>."
            + dica("/ver lista os projetos", "/buscar " + esc(nome))
        )
    if len(candidatos) > 1:
        ctx.lembrar_resultados(candidatos)
        return render_ambiguo(nome, candidatos)

    if indice is None:
        return _abrir(candidatos[0], ctx)
    nota = carregar(candidatos[0])
    ctx.lembrar_nota(nota)
    return render_secao(nota, indice)


def cmd_buscar(termo: str, ctx: Contexto) -> str:
    termo = (termo or "").strip()
    if not termo:
        return "Uso: <code>/buscar &lt;termo&gt;</code>" + dica("/buscar renato", "/ajuda")
    achados = buscar(termo)
    ctx.lembrar_resultados([path for path, _ in achados])
    return render_busca(termo, achados)


def cmd_hoje(ctx: Contexto) -> str:
    path = briefing_recente()
    if path is None:
        return f"{MARCA_NADA} Nenhum briefing em <code>briefings/</code>."
    return _abrir(path, ctx, MARCA_BRIEFING)


def cmd_indice(letra: str, numero: int, ctx: Contexto) -> str:
    """`/p3`, `/s2`, `/r1`. O número que apareceu na resposta anterior é o comando seguinte —
    é isso que torna o bot aprendível sem ler a ajuda."""
    if letra == "s":
        if ctx.nota is None:
            raise ConsultaError("Nenhuma nota aberta. Mande /ver e escolha uma.")
        nota = carregar(ctx.nota)
        ctx.lembrar_nota(nota)
        return render_nota(nota) if numero == 0 else render_secao(nota, numero)
    return _abrir(ctx.escolher(letra, numero), ctx)


def cmd_inicio(ctx: Contexto) -> str:
    lista = projetos()
    ctx.lembrar_projetos(lista)
    return render_inicio(lista, briefing_recente())


def cmd_ajuda() -> str:
    return render_ajuda()
