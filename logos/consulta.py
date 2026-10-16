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
import secrets
import unicodedata
from dataclasses import dataclass, field
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
_COMENTARIO_HTML = re.compile(r"^\s*<!--.*-->\s*$")


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
    # Comentário HTML de uma linha (os `<!-- logos:entrada {...} -->` das notas do dia) é dado
    # para máquina: no chat viraria um bloco de JSON, e a busca casaria dentro dele.
    linhas = ["" if _COMENTARIO_HTML.match(l) else l for l in texto.splitlines()]
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
        if _SEPARADOR_TABELA.match(bruta) or _COMENTARIO_HTML.match(bruta):
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


def _universo() -> list[Path]:
    """Todas as notas legíveis, com os projetos ativos na frente — é a ordem de desempate."""
    ativos = projetos()
    return ativos + [p for p in iter_notas() if p not in ativos]


def resolver(nome: str) -> list[Path]:
    """Casa o texto digitado com o nome do arquivo, sem acento e sem caixa. Projeto ativo ganha
    do resto: '/ver logos' é a nota do projeto, não uma captura que fala de Logos.

    Devolve lista: zero, um ou vários candidatos — quem chama decide o que dizer."""
    alvo = _fold(nome)
    if not alvo:
        return []
    universo = _universo()

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
# Pastas
# ---------------------------------------------------------------------------

def _contar(pasta: Path) -> int:
    return sum(1 for p in pasta.rglob("*.md") if p.is_file() and _visivel(p))


def conteudo_pasta(pasta: Path) -> tuple[list[Path], list[Path]]:
    """(subpastas, notas) de uma pasta legível. Subpasta sem nota nenhuma não aparece: seria
    um toque para ver "vazio". Notas da mexida mais recente para a mais antiga."""
    if not pasta.is_dir() or not _is_allowed(pasta):
        raise ConsultaError("Essa pasta está fora das que eu leio.")
    subpastas = sorted(
        (d for d in pasta.iterdir()
         if d.is_dir() and not d.name.startswith((".", "~")) and _is_allowed(d) and _contar(d)),
        key=lambda d: _fold(d.name),
    )
    notas = sorted(
        (p for p in pasta.glob("*.md") if p.is_file() and _visivel(p)),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    return subpastas, notas


# ---------------------------------------------------------------------------
# Ligações: os [[wikilinks]] que saem da nota e as notas que apontam para ela
# ---------------------------------------------------------------------------

MAX_LIGACOES = 10

_ALIASES_INLINE = re.compile(r"^aliases:\s*\[(.*)\]\s*$")


def _aliases(texto: str) -> list[str]:
    """Os `aliases:` do frontmatter, em lista YAML ou entre colchetes. O parser da `Nota` só lê
    `chave: valor` numa linha, e há link no vault que só resolve por alias."""
    linhas = texto.splitlines()
    if not linhas or linhas[0].strip() != "---":
        return []
    nomes: list[str] = []
    dentro = False
    for linha in linhas[1:]:
        if linha.strip() == "---":
            break
        inline = _ALIASES_INLINE.match(linha)
        if inline:
            return [a.strip().strip("'\"") for a in inline.group(1).split(",") if a.strip()]
        if linha.startswith("aliases:"):
            dentro = True
        elif dentro and linha.lstrip().startswith("- "):
            nomes.append(linha.lstrip()[2:].strip().strip("'\""))
        elif dentro and linha.strip():
            break
    return nomes


def _alvos(texto: str) -> list[str]:
    """Nomes para onde os wikilinks apontam, sem âncora, alias, pasta nem `.md`. Link só de
    âncora (`[[#Seção]]`) é a própria nota e fica fora."""
    alvos = []
    for match in _WIKILINK.finditer(texto):
        alvo = match.group(1).split("#", 1)[0].strip().rsplit("/", 1)[-1]
        if alvo.lower().endswith(".md"):
            alvo = alvo[:-3]
        if alvo:
            alvos.append(alvo)
    return alvos


def ligacoes(path: Path) -> tuple[list[Path], list[Path]]:
    """(notas para onde esta aponta, notas que apontam para esta), só entre as que eu leio.

    O link resolve como no Obsidian: pelo nome do arquivo, senão por alias. Link para nota fora
    da allowlist some sem aviso — até o nome dela sairia pelo chat, e o nome de uma entrada do
    diário já diz mais do que deveria. Lê o vault inteiro a cada nota aberta; com uma centena
    de notas isso fica na casa dos milissegundos, e um índice em cache envelheceria calado."""
    textos: dict[Path, str] = {}
    for nota in _universo():
        try:
            textos[nota.resolve()] = nota.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

    indice: dict[str, Path] = {}
    for nota in textos:
        indice.setdefault(_fold(nota.stem), nota)
    for nota, texto in textos.items():
        for alias in _aliases(texto):
            indice.setdefault(_fold(alias), nota)

    esta = path.resolve()
    saem: list[Path] = []
    for alvo in _alvos(textos.get(esta, "")):
        destino = indice.get(_fold(alvo))
        if destino is not None and destino != esta and destino not in saem:
            saem.append(destino)

    entram = [
        nota for nota, texto in textos.items()
        if nota != esta and nota not in saem
        and any(indice.get(_fold(alvo)) == esta for alvo in _alvos(texto))
    ]
    entram.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return saem[:MAX_LIGACOES], entram[:MAX_LIGACOES]


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
MARCA_SECAO = "▸"
MARCA_BUSCA = "\U0001f50e"
MARCA_BRIEFING = "☀️"
MARCA_DICA = "\U0001f4a1"
MARCA_NADA = "∅"
MARCA_INICIO = "\U0001f9ed"
MARCA_JOB = "⚙️"
MARCA_PASTA = "\U0001f4c2"
MARCA_LINK = "↪"
MARCA_CITADA = "↩"
MARCA_VOLTA = "⬅"

MAX_ATALHOS_INICIO = 5
MAX_NOTAS_PASTA = 15
MAX_ROTULO = 40        # o app corta o texto do botão; melhor cortar aqui, com reticências
MAX_ROTULO_PAR = 22    # botão que divide a linha com outro
MAX_LIGACOES_NA_NOTA = 4

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
    return text if len(text) <= limite else text[: limite - 1].rstrip() + "…"


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
    return " · ".join(campos)


def dica(*sugestoes: str) -> str:
    """Rodapé de uma linha, para o que botão não resolve (o aviso de que o job terminou chega
    depois, por exemplo)."""
    return f"\n{MARCA_DICA} " + "  ·  ".join(s for s in sugestoes if s)


# ---------------------------------------------------------------------------
# Telas: o texto e os botões embaixo dele
# ---------------------------------------------------------------------------

# (rótulo, dado). O dado é o que o Telegram devolve quando ele toca — até 64 bytes. Ver
# `cmd_botao` para o vocabulário.
Botao = tuple[str, str]

INICIO: Botao = (f"{MARCA_INICIO} Início", "i")
BUSCAR: Botao = (f"{MARCA_BUSCA} Buscar", "b")
MARCA_TIPOS = "\U0001f5c2"
TIPOS: Botao = (f"{MARCA_TIPOS} Tipos", "t")


@dataclass
class Tela:
    """Uma resposta da consulta.

    Os botões substituíram os `/p3`, `/s2`, `/r1` de antes. O número-comando resolvia "o
    próximo passo está na tela", mas pedia leitura: achar o número, lembrar a letra, digitar.
    Ele achou cansativo e feio (22/09/2026). O botão mantém a ideia — o próximo passo está na
    tela — e tira a leitura: é só tocar."""
    texto: str
    botoes: list[list[Botao]] = field(default_factory=list)


def _rotulo(texto: str, limite: int = MAX_ROTULO) -> str:
    return _cortar(_sem_emoji(texto) or texto, limite)


def _em_pares(botoes: list[Botao]) -> list[list[Botao]]:
    """Dois por linha: seção e link têm nome curto, e uma coluna só empurraria a navegação
    para fora da tela."""
    return [botoes[i:i + 2] for i in range(0, len(botoes), 2)]


def _marca(path: Path) -> str:
    """Briefing tem marca própria. Sai do caminho, não de quem abriu: o botão de voltar ao
    índice não sabe se a nota veio do /hoje."""
    try:
        path.resolve().relative_to(vault_root() / "briefings")
        return MARCA_BRIEFING
    except ValueError:
        return MARCA_NOTA


# ---------------------------------------------------------------------------
# Contexto: liga cada botão a uma nota ou pasta
# ---------------------------------------------------------------------------

class Contexto:
    """Dá a cada caminho mostrado um código curto; o botão carrega o código.

    Por que não o número da lista, como no `/p3`: com botões, a mensagem antiga continua na
    tela e continua tocável. "O terceiro da última lista" abriria outra nota sem avisar. O código
    é do caminho, então um botão antigo abre sempre a mesma nota.

    Em memória e com uma geração aleatória por processo, de propósito: depois de um relançamento
    os códigos recomeçam, e sem a geração o botão velho `o:3` abriria a nota que ganhou o 3 no
    processo novo. Com ela, o botão velho não casa com nada e diz que venceu."""

    def __init__(self) -> None:
        self.geracao = secrets.token_hex(2)
        self._caminhos: dict[str, Path] = {}
        self._codigos: dict[Path, str] = {}

    def ref(self, path: Path) -> str:
        chave = path.resolve()
        codigo = self._codigos.get(chave)
        if codigo is None:
            codigo = f"{self.geracao}.{len(self._codigos) + 1:x}"
            self._codigos[chave] = codigo
            self._caminhos[codigo] = chave
        return codigo

    def caminho(self, codigo: str) -> Path:
        path = self._caminhos.get(codigo)
        if path is None:
            raise ConsultaError("Esse botão é de antes do último reinício do Logos. Toque em Início.")
        return path


# ---------------------------------------------------------------------------
# Renderização
# ---------------------------------------------------------------------------

def _botao_nota(path: Path, ctx: Contexto, marca: str = MARCA_NOTA, data: bool = True,
                limite: int = MAX_ROTULO) -> Botao:
    sufixo = f" · {_data(path)}" if data else ""
    return (f"{marca} {_rotulo(path.stem, limite - len(sufixo))}{sufixo}", f"o:{ctx.ref(path)}")


def _botao_pasta(pasta: Path, ctx: Contexto, nome: str | None = None) -> Botao:
    return (f"{MARCA_PASTA} {_rotulo(nome or pasta.name)} · {_contar(pasta)}", f"d:{ctx.ref(pasta)}")


def _voltar(path: Path, ctx: Contexto) -> Botao:
    """Volta sempre para a pasta de cima, venha de onde vier: previsível vale mais do que
    lembrar o caminho percorrido. A raiz de cada pasta legível volta para a lista de pastas."""
    pasta = path.parent
    if not _is_allowed(pasta):
        return (f"{MARCA_VOLTA} Pastas", "n")
    return (f"{MARCA_VOLTA} {_rotulo(rel(pasta), MAX_ROTULO_PAR)}", f"d:{ctx.ref(pasta)}")


def render_projetos(notas: list[Path], ctx: Contexto) -> Tela:
    if not notas:
        return Tela(f"{MARCA_NADA} Nenhum projeto ativo em <code>work/active</code>.", [[INICIO]])
    botoes = [[_botao_nota(p, ctx, MARCA_PROJETO)] for p in notas[:MAX_PROJETOS]]
    return Tela(f"{MARCA_PROJETO} <b>Projetos ativos</b>", botoes + [[BUSCAR, INICIO]])


def _botoes_ligacoes(saem: list[Path], entram: list[Path], ctx: Contexto,
                     limite: int = MAX_ROTULO_PAR) -> list[Botao]:
    """Os links que saem com ↪, as notas que citam esta com ↩. Mesma ação nos dois: quem lê
    não precisa saber de que lado o link está para segui-lo."""
    botoes = [(f"{MARCA_LINK} {_rotulo(p.stem, limite)}", f"o:{ctx.ref(p)}") for p in saem]
    botoes += [(f"{MARCA_CITADA} {_rotulo(p.stem, limite)}", f"o:{ctx.ref(p)}") for p in entram]
    return botoes


def _ligacoes_na_nota(nota: Nota, saem: list[Path], entram: list[Path], ctx: Contexto) -> list[list[Botao]]:
    """Poucas ligações vão direto na nota. Muitas viram um botão só: a nota do Logos tem vinte,
    e vinte botões de nome cortado empurravam as seções para fora da tela."""
    total = len(saem) + len(entram)
    if total <= MAX_LIGACOES_NA_NOTA:
        return _em_pares(_botoes_ligacoes(saem, entram, ctx))
    return [[(f"{MARCA_LINK} Ligações ({total})", f"l:{ctx.ref(nota.path)}")]]


def render_ligacoes(nota: Nota, saem: list[Path], entram: list[Path], ctx: Contexto) -> Tela:
    """Uma por linha, com o nome inteiro: aqui não há seção disputando espaço."""
    texto = (f"{MARCA_LINK} <b>Ligações de {esc(nota.titulo)}</b>\n"
             f"<i>{MARCA_LINK} para onde ela aponta  ·  {MARCA_CITADA} quem a cita</i>")
    botoes = [[b] for b in _botoes_ligacoes(saem, entram, ctx, MAX_ROTULO)]
    volta = (f"{MARCA_VOLTA} {_rotulo(nota.titulo, MAX_ROTULO_PAR)}", f"o:{ctx.ref(nota.path)}")
    return Tela(texto, botoes + [[volta, INICIO]])


def _linha_ligacoes(saem: list[Path], entram: list[Path]) -> str:
    partes = []
    if saem:
        partes.append(f"{MARCA_LINK} {len(saem)} link(s)")
    if entram:
        partes.append(f"{MARCA_CITADA} citada em {len(entram)}")
    return "<i>" + "  ·  ".join(partes) + "</i>" if partes else ""


def render_nota(nota: Nota, ctx: Contexto, saem: list[Path] | None = None,
                entram: list[Path] | None = None) -> Tela:
    saem, entram = saem or [], entram or []
    marca = _marca(nota.path)
    cabeca = [f"{marca} <b>{esc(nota.titulo)}</b>", f"<i>{esc(_meta_linha(nota))}</i>"]
    ligacoes_ = _ligacoes_na_nota(nota, saem, entram, ctx)
    rodape = [_linha_ligacoes(saem, entram)] if ligacoes_ else []
    navegar = [[_voltar(nota.path, ctx), INICIO]]

    inteira = "\n\n".join(
        [nota.intro] + [f"{titulo}\n{_corpo(linhas)}" for titulo, linhas in nota.secoes]
    ).strip()

    if len(inteira) <= LIMITE_NOTA_INTEIRA:
        corpo = citacao(esc(inteira)) if inteira else "<i>Nota sem conteúdo além do frontmatter.</i>"
        texto = "\n".join(cabeca + ["", corpo] + ([""] + rodape if rodape else []))
        return Tela(texto, ligacoes_ + navegar)

    # Nota longa: as seções viram botões. Ninguém lê 14 KB de uma vez no celular.
    partes = list(cabeca)
    descricao = nota.meta.get("description")
    if descricao:
        partes += ["", citacao(esc(_cortar(descricao, 400)), expansivel=False)]
    elif nota.intro:
        partes += ["", citacao(esc(_cortar(nota.intro, 400)), expansivel=False)]
    partes += ["", f"<b>{len(nota.secoes)} seções</b> — toque para abrir"] + rodape
    ref = ctx.ref(nota.path)
    secoes = _em_pares([
        (f"{MARCA_SECAO} {_rotulo(titulo, MAX_ROTULO_PAR)}", f"s:{ref}:{indice}")
        for indice, (titulo, _) in enumerate(nota.secoes, 1)
    ])
    return Tela("\n".join(partes), secoes + ligacoes_ + navegar)


def render_secao(nota: Nota, indice: int, ctx: Contexto) -> Tela:
    if not nota.secoes:
        raise ConsultaError(f"{nota.titulo} não tem seções. A nota inteira já veio acima.")
    if not 1 <= indice <= len(nota.secoes):
        raise ConsultaError(f"{nota.titulo} tem {len(nota.secoes)} seções, e você pediu a {indice}.")
    titulo, linhas = nota.secoes[indice - 1]
    corpo = _corpo(linhas) or "Seção vazia."
    cabeca = (
        f"{MARCA_SECAO} <b>{esc(titulo)}</b>\n"
        f"<i>{esc(nota.titulo)} · {indice} de {len(nota.secoes)}</i>"
    )
    ref = ctx.ref(nota.path)
    vizinhas = []
    if indice > 1:
        vizinhas.append(("‹ Anterior", f"s:{ref}:{indice - 1}"))
    if indice < len(nota.secoes):
        vizinhas.append((f"{_rotulo(nota.secoes[indice][0], MAX_ROTULO_PAR)} ›", f"s:{ref}:{indice + 1}"))
    botoes = [vizinhas] if vizinhas else []
    botoes.append([(f"{MARCA_VOLTA} Índice", f"s:{ref}:0"), INICIO])
    return Tela(f"{cabeca}\n\n{citacao(esc(corpo))}", botoes)


def _pasta(path: Path) -> str:
    caminho = rel(path)
    return caminho.rsplit("/", 1)[0] if "/" in caminho else caminho


def render_busca(termo: str, achados: list[tuple[Path, list[tuple[int, str]]]], ctx: Contexto) -> Tela:
    if not achados:
        return Tela(
            f"{MARCA_NADA} Nada com <b>{esc(termo)}</b> nas notas que eu leio.",
            [[(f"{MARCA_BUSCA} Outra busca", "b"), INICIO]],
        )
    partes = [f"{MARCA_BUSCA} <b>{esc(termo)}</b> — {len(achados)} arquivo(s)", ""]
    botoes = []
    for indice, (path, linhas) in enumerate(achados, 1):
        partes.append(f"<b>{indice}. {esc(path.stem)}</b>  <i>{esc(_pasta(path))}</i>")
        trecho = "\n".join(esc(_cortar(linha)) for _, linha in linhas)
        partes.append(citacao(trecho))
        partes.append("")
        botoes.append((f"{indice}. {_rotulo(path.stem, MAX_ROTULO_PAR)}", f"o:{ctx.ref(path)}"))
    return Tela("\n".join(partes).rstrip(), _em_pares(botoes) + [[(f"{MARCA_BUSCA} Outra busca", "b"), INICIO]])


def render_ambiguo(nome: str, candidatos: list[Path], ctx: Contexto) -> Tela:
    texto = f"<b>{esc(nome)}</b> casa com mais de uma nota:"
    if len(candidatos) > 10:
        texto += f"\n<i>Mostrando 10 de {len(candidatos)}. Um nome mais comprido estreita.</i>"
    botoes = [
        [(f"{MARCA_NOTA} {_rotulo(p.stem)} · {_rotulo(_pasta(p), MAX_ROTULO_PAR)}", f"o:{ctx.ref(p)}")]
        for p in candidatos[:10]
    ]
    return Tela(texto, botoes + [[INICIO]])


def render_pastas(raizes: list[Path], ctx: Contexto) -> Tela:
    if not raizes:
        return Tela(f"{MARCA_NADA} Nenhuma das pastas que eu leio existe neste vault.", [[INICIO]])
    botoes = [[_botao_pasta(p, ctx, rel(p))] for p in raizes]
    return Tela(f"{MARCA_PASTA} <b>Pastas que eu leio</b>", botoes + [[BUSCAR, INICIO]])


def render_pasta(pasta: Path, subpastas: list[Path], notas: list[Path], ctx: Contexto) -> Tela:
    """Pasta grande mostra só as notas mais recentes: rolar trinta capturas no celular é pior
    que procurar, então o texto aponta para a busca."""
    linhas = [f"{MARCA_PASTA} <b>{esc(rel(pasta))}</b>"]
    if len(notas) > MAX_NOTAS_PASTA:
        linhas.append(f"<i>As {MAX_NOTAS_PASTA} mais recentes; as outras {len(notas) - MAX_NOTAS_PASTA} "
                      "aparecem pela busca.</i>")
    if not subpastas and not notas:
        linhas.append("<i>Pasta vazia.</i>")
    botoes = _em_pares([_botao_pasta(sub, ctx) for sub in subpastas])
    botoes += [[_botao_nota(p, ctx)] for p in notas[:MAX_NOTAS_PASTA]]
    botoes.append([_voltar(pasta, ctx), INICIO])
    return Tela("\n".join(linhas), botoes)


def render_inicio(notas: list[Path], briefing: Path | None, ctx: Contexto) -> Tela:
    """A tela do /inicio: as saídas possíveis já como botões, com os projetos mais mexidos na
    frente — é por onde ele quase sempre começa."""
    texto = (
        f"{MARCA_INICIO} <b>Para onde?</b>\n"
        "<i>Para capturar, é só mandar texto, voz ou foto.</i>"
    )
    botoes = [[_botao_nota(p, ctx, MARCA_PROJETO)] for p in notas[:MAX_ATALHOS_INICIO]]
    if len(notas) > MAX_ATALHOS_INICIO:
        botoes.append([(f"{MARCA_PROJETO} Todos os projetos ({len(notas)})", "v")])
    linha = []
    if briefing is not None:
        linha.append((f"{MARCA_BRIEFING} Briefing {_data(briefing)}", "h"))
    linha.append(TIPOS)
    botoes.append(linha)
    botoes.append([(f"{MARCA_PASTA} Pastas", "n"), BUSCAR])
    botoes.append([(f"{MARCA_JOB} Rodar no PC", "j"), ("Ajuda", "a")])
    return Tela(texto, botoes)


# ---------------------------------------------------------------------------
# Tipos de nota: um atalho por espécie, atravessando as pastas
# ---------------------------------------------------------------------------

# O "tipo" sai do que o vault já registra, não de uma classificação nova: a pasta diz se é
# pessoa, time, briefing; nas capturas, o campo `categoria` que o pipeline preenche diz se é
# tarefa, insight, reunião. Categoria nova aparece sozinha — é lida das notas a cada consulta.
ROTULOS_CATEGORIA = {
    "tarefa": "Tarefas", "ideia": "Ideias", "insight": "Insights", "reuniao": "Reuniões",
    "referencia": "Referências", "entrevista": "Entrevistas", "rpg": "RPG",
}
MARCA_PESSOA = "\U0001f464"
MARCA_TIME = "\U0001f465"
MARCA_CONHECIMENTO = "\U0001f9e0"
MARCA_PLANEJAMENTO = "\U0001f5d3"
MARCA_CAPTURA = "\U0001f4e5"
MAX_NOTAS_TIPO = 15


@dataclass
class Tipo:
    chave: str          # vai no dado do botão: curta e só ASCII
    rotulo: str
    marca: str
    notas: list[Path]
    por_nome: bool = False   # pessoas e times se procuram pelo nome, não pela data


def _notas_em(sub: str) -> list[Path]:
    pasta = vault_root() / sub
    if not pasta.is_dir() or not _is_allowed(pasta):
        return []
    return [p for p in pasta.glob("*.md") if p.is_file() and _visivel(p)]


def _recentes(notas: list[Path]) -> list[Path]:
    return sorted(notas, key=lambda p: p.stat().st_mtime, reverse=True)


def tipos() -> list[Tipo]:
    """Os tipos que têm ao menos uma nota, na ordem em que aparecem na tela."""
    lista = [
        Tipo("proj", "Projetos", MARCA_PROJETO, projetos()),
        Tipo("pess", "Pessoas", MARCA_PESSOA,
             sorted(_notas_em("org/people"), key=lambda p: _fold(p.stem)), por_nome=True),
        Tipo("time", "Times", MARCA_TIME,
             sorted(_notas_em("org/teams"), key=lambda p: _fold(p.stem)), por_nome=True),
        Tipo("brief", "Briefings", MARCA_BRIEFING, _recentes(_notas_em("briefings"))),
        Tipo("brain", "Conhecimento", MARCA_CONHECIMENTO,
             sorted(_notas_em("brain"), key=lambda p: _fold(p.stem)), por_nome=True),
        Tipo("plan", "Planejamento", MARCA_PLANEJAMENTO, _recentes(_notas_em("capturas/Planejamento"))),
    ]
    por_categoria: dict[str, list[Path]] = {}
    for path in _notas_em("capturas/Inbox"):
        try:
            meta, _ = Nota._separar_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        categoria = re.sub(r"[^a-z0-9]", "", _fold(meta.get("categoria", "")))[:20]
        por_categoria.setdefault(categoria, []).append(path)
    for categoria in sorted(por_categoria, key=lambda c: (c == "", -len(por_categoria[c]), c)):
        rotulo = ROTULOS_CATEGORIA.get(categoria, categoria.capitalize()) if categoria else "Sem categoria"
        lista.append(Tipo(f"c-{categoria}", rotulo, MARCA_CAPTURA, _recentes(por_categoria[categoria])))
    return [t for t in lista if t.notas]


def render_tipos(lista: list[Tipo]) -> Tela:
    if not lista:
        return Tela(f"{MARCA_NADA} Nenhuma nota nas pastas que eu leio.", [[INICIO]])
    botoes = _em_pares([(f"{t.marca} {_rotulo(t.rotulo, MAX_ROTULO_PAR - 5)} · {len(t.notas)}", f"t:{t.chave}:0")
                        for t in lista])
    return Tela(f"{MARCA_TIPOS} <b>Tipos de nota</b>", botoes + [[BUSCAR, INICIO]])


def render_tipo(tipo: Tipo, pagina: int, ctx: Contexto) -> Tela:
    """Uma página de 15. Pessoas em ordem alfabética, o resto das mais recentes para trás."""
    paginas = max(1, -(-len(tipo.notas) // MAX_NOTAS_TIPO))
    if not 0 <= pagina < paginas:
        raise ConsultaError("Essa página não existe mais. Toque em Tipos.")
    inicio = pagina * MAX_NOTAS_TIPO
    fatia = tipo.notas[inicio:inicio + MAX_NOTAS_TIPO]
    ordem = "em ordem alfabética" if tipo.por_nome else "das mais recentes"
    texto = f"{tipo.marca} <b>{esc(tipo.rotulo)}</b> · {len(tipo.notas)} notas"
    if paginas > 1:
        texto += f"\n<i>{inicio + 1}–{inicio + len(fatia)}, {ordem}</i>"
    botoes = [[_botao_nota(p, ctx, tipo.marca, data=not tipo.por_nome)] for p in fatia]
    paginar = []
    if pagina > 0:
        paginar.append(("‹ Anteriores", f"t:{tipo.chave}:{pagina - 1}"))
    if pagina < paginas - 1:
        paginar.append(("Mais ›", f"t:{tipo.chave}:{pagina + 1}"))
    if paginar:
        botoes.append(paginar)
    botoes.append([(f"{MARCA_VOLTA} Tipos", "t"), INICIO])
    return Tela(texto, botoes)


def cmd_tipos() -> Tela:
    return render_tipos(tipos())


def cmd_tipo(chave: str, pagina: int, ctx: Contexto) -> Tela:
    for tipo in tipos():
        if tipo.chave == chave:
            return render_tipo(tipo, pagina, ctx)
    raise ConsultaError("Esse tipo não tem mais notas. Toque em Tipos.")


def render_ajuda() -> Tela:
    """Uma tela curta: com botões, quase nada precisa ser explicado."""
    texto = (
        f"{MARCA_DICA} <b>Como mexer aqui</b>\n"
        "\n"
        f"<b>Consultar:</b> {INICIO[0]}, o botão fixo embaixo do campo de texto (ou /inicio), "
        f"e depois só botões. Tocar num botão troca esta mesma "
        f"mensagem; {MARCA_VOLTA} volta um nível e {MARCA_INICIO} recomeça.\n"
        "\n"
        "<b>Atalhos digitados</b>, se preferir:\n"
        "<code>/ver ppj</code> abre uma nota pelo nome\n"
        "<code>/buscar renato</code> procura nas notas\n"
        "<code>/hoje</code> o briefing mais recente\n"
        "\n"
        "<b>Capturar</b> segue igual: mande texto, voz, foto, PDF ou link.\n"
        "<code>/tarefa</code> <code>/ideia</code> <code>/planejamento</code> "
        "<code>/diario</code>  ·  <code>/nova</code> fecha a sessão."
    )
    return Tela(texto, [[INICIO]])


# ---------------------------------------------------------------------------
# Comandos (o bot só despacha; a lógica e as mensagens moram aqui)
# ---------------------------------------------------------------------------

def _abrir(path: Path, ctx: Contexto) -> Tela:
    return _mostrar(carregar(path), ctx)


def _mostrar(nota: Nota, ctx: Contexto) -> Tela:
    saem, entram = ligacoes(nota.path)
    return render_nota(nota, ctx, saem, entram)


def _abrir_pasta(pasta: Path, ctx: Contexto) -> Tela:
    subpastas, notas = conteudo_pasta(pasta)
    return render_pasta(pasta, subpastas, notas, ctx)


def cmd_pastas(ctx: Contexto) -> Tela:
    return render_pastas(allowed_roots(), ctx)


def cmd_ver(argumento: str, ctx: Contexto) -> Tela:
    argumento = (argumento or "").strip()
    if not argumento:
        return render_projetos(projetos(), ctx)

    nome, indice = argumento, None
    partes = argumento.rsplit(maxsplit=1)
    if len(partes) == 2 and partes[1].isdigit():
        nome, indice = partes[0], int(partes[1])

    candidatos = resolver(nome)
    if not candidatos:
        botoes = [[(f"{MARCA_BUSCA} Buscar “{_rotulo(nome, MAX_ROTULO_PAR)}”", f"q:{nome}")]]
        if len(botoes[0][0][1].encode("utf-8")) > 64:
            botoes = [[BUSCAR]]
        return Tela(f"{MARCA_NADA} Não achei nota com <b>{esc(nome)}</b>.", botoes + [[INICIO]])
    if len(candidatos) > 1:
        return render_ambiguo(nome, candidatos, ctx)

    if indice is None:
        return _abrir(candidatos[0], ctx)
    return render_secao(carregar(candidatos[0]), indice, ctx)


def cmd_buscar(termo: str, ctx: Contexto) -> Tela:
    termo = (termo or "").strip()
    return render_busca(termo, buscar(termo), ctx)


def cmd_hoje(ctx: Contexto) -> Tela:
    path = briefing_recente()
    if path is None:
        return Tela(f"{MARCA_NADA} Nenhum briefing em <code>briefings/</code>.", [[INICIO]])
    return _abrir(path, ctx)


def cmd_inicio(ctx: Contexto) -> Tela:
    return render_inicio(projetos(), briefing_recente(), ctx)


def cmd_ajuda() -> Tela:
    return render_ajuda()


def cmd_botao(dado: str, ctx: Contexto) -> Tela:
    """O toque num botão. Vocabulário do `dado`:

    `i` início · `v` projetos · `h` briefing · `n` pastas · `a` ajuda · `q:<termo>` busca ·
    `o:<ref>` abre nota · `d:<ref>` abre pasta · `l:<ref>` ligações da nota ·
    `s:<ref>:<k>` seção k (0 = índice da nota) · `t` tipos · `t:<chave>:<p>` página p do tipo.

    `b` (pedir o termo da busca) e `j…` (jobs) não passam por aqui: dependem do bot."""
    verbo, _, resto = dado.partition(":")
    if verbo == "i":
        return cmd_inicio(ctx)
    if verbo == "v":
        return cmd_ver("", ctx)
    if verbo == "h":
        return cmd_hoje(ctx)
    if verbo == "n":
        return cmd_pastas(ctx)
    if verbo == "a":
        return cmd_ajuda()
    if verbo == "q":
        return cmd_buscar(resto, ctx)
    if verbo == "o":
        return _abrir(ctx.caminho(resto), ctx)
    if verbo == "d":
        return _abrir_pasta(ctx.caminho(resto), ctx)
    if verbo == "t":
        if not resto:
            return cmd_tipos()
        chave, _, pagina = resto.rpartition(":")
        if not pagina.isdigit():
            raise ConsultaError("Botão com defeito. Toque em Tipos.")
        return cmd_tipo(chave, int(pagina), ctx)
    if verbo == "l":
        nota = carregar(ctx.caminho(resto))
        return render_ligacoes(nota, *ligacoes(nota.path), ctx)
    if verbo == "s":
        ref, _, numero = resto.rpartition(":")
        if not numero.isdigit():
            raise ConsultaError("Botão com defeito. Toque em Início.")
        nota = carregar(ctx.caminho(ref))
        return _mostrar(nota, ctx) if int(numero) == 0 else render_secao(nota, int(numero), ctx)
    raise ConsultaError("Não conheço esse botão. Toque em Início.")
