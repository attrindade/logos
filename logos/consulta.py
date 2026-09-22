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


def limpar(linha: str) -> str:
    """Markdown -> texto legível. Não escapa HTML: quem monta a mensagem escapa depois.

    O pictograma sai em qualquer posição, mas o começo da linha é preservado no resto: tirar o
    '-' de um item de lista junto com o emoji destruiria a estrutura que torna a nota legível."""
    recuo = linha[: len(linha) - len(linha.lstrip(" \t"))]
    linha = recuo + _PICTOGRAMAS.sub("", linha[len(recuo):]).lstrip(" \t")
    linha = _WIKILINK.sub(lambda m: (m.group(2) or m.group(1)).strip(), linha)
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

def esc(text: str) -> str:
    return html.escape(text, quote=False)


def sem_tags(text: str) -> str:
    """Desfaz a formatação. Só o caminho de degradação do bot usa: se a API recusar o HTML,
    a resposta vai crua em vez de não ir."""
    return html.unescape(re.sub(r"</?(?:b|i|u|s|code|pre|a)\b[^>]*>", "", text))


def _data(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%d/%m")


def _cortar(text: str, limite: int = MAX_LINHA) -> str:
    text = text.strip()
    return text if len(text) <= limite else text[: limite - 1].rstrip() + "…"


def partir(texto: str, limite: int = LIMITE_MENSAGEM) -> list[str]:
    """Divide em mensagens por linha inteira. Nunca corta no meio de uma tag: as tags deste
    módulo abrem e fecham dentro de uma linha só, então a fronteira de linha é segura —
    diferente do `_chunks` do telegram.py, que corta por caractere."""
    partes, atual = [], ""
    for linha in texto.splitlines(keepends=True):
        while len(linha) > limite:  # linha monstruosa: corta no espaço mais próximo do fim
            if atual:
                partes.append(atual.rstrip())
                atual = ""
            corte = linha.rfind(" ", 0, limite)
            corte = corte if corte > limite // 2 else limite
            partes.append(linha[:corte].rstrip())
            linha = linha[corte:].lstrip()
        if len(atual) + len(linha) > limite:
            partes.append(atual.rstrip())
            atual = ""
        atual += linha
    if atual.strip():
        partes.append(atual.rstrip())
    return partes or [""]


def _meta_linha(nota: Nota) -> str:
    campos = [nota.meta.get(c) for c in ("status", "quarter")]
    campos = [c for c in campos if c]
    campos.append(f"atualizada {nota.atualizada.strftime('%d/%m')}")
    return " · ".join(campos)


def render_projetos(notas: list[Path]) -> str:
    if not notas:
        return "Nenhum projeto ativo em <code>work/active</code>."
    linhas = ["<b>Projetos ativos</b>", ""]
    for path in notas[:MAX_PROJETOS]:
        linhas.append(f"{esc(path.stem)} — {_data(path)}")
    linhas += ["", "<code>/ver &lt;nome&gt;</code> abre um deles."]
    return "\n".join(linhas)


def _primeira_palavra(nome: str) -> str:
    return _fold(nome.split()[0]) if nome.split() else _fold(nome)


def render_nota(nota: Nota) -> str:
    cabeca = [f"<b>{esc(nota.titulo)}</b>", esc(_meta_linha(nota))]
    descricao = nota.meta.get("description")
    if descricao:
        cabeca += ["", esc(_cortar(descricao, 300))]

    inteira = "\n\n".join(
        [nota.intro] + [f"{titulo}\n{_corpo(linhas)}" for titulo, linhas in nota.secoes]
    ).strip()

    if len(inteira) <= LIMITE_NOTA_INTEIRA:
        corpo = esc(inteira) if inteira else "Nota sem conteúdo além do frontmatter."
        return "\n".join(cabeca + ["", corpo])

    # Nota longa vira índice: 14 KB de uma vez, no celular, não é leitura.
    partes = list(cabeca)
    if nota.intro:
        partes += ["", esc(_cortar(nota.intro, 600))]
    partes += ["", "<b>Seções</b>"]
    for indice, (titulo, linhas) in enumerate(nota.secoes, 1):
        partes.append(f"{indice}. {esc(titulo)}")
    partes += ["", f"<code>/ver {esc(_primeira_palavra(nota.nome))} &lt;n&gt;</code> abre uma seção."]
    return "\n".join(partes)


def render_secao(nota: Nota, indice: int) -> str:
    if not nota.secoes:
        raise ConsultaError(f"{nota.titulo} não tem seções.")
    if not 1 <= indice <= len(nota.secoes):
        raise ConsultaError(f"{nota.titulo} tem {len(nota.secoes)} seções, e você pediu a {indice}.")
    titulo, linhas = nota.secoes[indice - 1]
    corpo = _corpo(linhas) or "Seção vazia."
    return f"<b>{esc(nota.titulo)} — {esc(titulo)}</b>\n\n{esc(corpo)}"


def render_busca(termo: str, achados: list[tuple[Path, list[tuple[int, str]]]]) -> str:
    if not achados:
        return f"Nada com “{esc(termo)}” nas notas que eu leio."
    partes = [f"<b>{esc(termo)}</b> — {len(achados)} arquivo(s)", ""]
    for path, linhas in achados:
        partes.append(f"<b>{esc(path.stem)}</b>  <code>{esc(rel(path))}</code>")
        for _, linha in linhas:
            partes.append(f"  {esc(_cortar(linha))}")
        partes.append("")
    partes.append("<code>/ver &lt;nome&gt;</code> abre um deles.")
    return "\n".join(partes)


def render_ambiguo(nome: str, candidatos: list[Path]) -> str:
    partes = [f"“{esc(nome)}” casa com mais de uma nota:", ""]
    for path in candidatos[:10]:
        partes.append(f"{esc(path.stem)}  <code>{esc(rel(path))}</code>")
    if len(candidatos) > 10:
        partes.append(f"… e mais {len(candidatos) - 10}.")
    return "\n".join(partes)


# ---------------------------------------------------------------------------
# Comandos (o bot só despacha; a lógica e as mensagens moram aqui)
# ---------------------------------------------------------------------------

def cmd_ver(argumento: str) -> str:
    argumento = (argumento or "").strip()
    if not argumento:
        return render_projetos(projetos())

    nome, indice = argumento, None
    partes = argumento.rsplit(maxsplit=1)
    if len(partes) == 2 and partes[1].isdigit():
        nome, indice = partes[0], int(partes[1])

    candidatos = resolver(nome)
    if not candidatos:
        return f"Não achei nota com “{esc(nome)}”.\n\n<code>/ver</code> lista os projetos ativos."
    if len(candidatos) > 1:
        return render_ambiguo(nome, candidatos)

    nota = carregar(candidatos[0])
    return render_secao(nota, indice) if indice is not None else render_nota(nota)


def cmd_buscar(termo: str) -> str:
    termo = (termo or "").strip()
    if not termo:
        return "Uso: <code>/buscar &lt;termo&gt;</code>"
    return render_busca(termo, buscar(termo))


def cmd_hoje() -> str:
    path = briefing_recente()
    if path is None:
        return "Nenhum briefing em <code>briefings/</code>."
    return f"{render_nota(carregar(path))}\n\n<code>{esc(rel(path))}</code>"
