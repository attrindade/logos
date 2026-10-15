"""Nota por dia para o Diário e o Planejamento.

Vários diários no mesmo dia viram uma nota só, `Diário — 2026-09-24.md`, com uma seção por
entrada. Os planos seguem o mesmo esquema em `Plano — 2026-09-25.md`, datados pelo dia que o plano
cobre e não pelo dia em que foram feitos. O topo da nota é um consolidado refeito a cada entrada.

A nota é a única fonte da verdade. Cada seção carrega os próprios dados num comentário HTML,
`<!-- logos:entrada {...} -->`, que não aparece na leitura. Um estado paralelo em STATE_DIR ficaria
fora do backup (o restic cobre C:\\Workspace, o STATE_DIR não), e um consolidado refeito a partir de
uma lista perdida apagaria o dia sem avisar.
"""
import json
import logging
import os
import re
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from . import config
from .triage import Route
from .writer import _attachment_lines, _map_line

logger = logging.getLogger(__name__)

_COMENTARIO_RE = re.compile(r"^<!-- logos:entrada (\{.*\}) -->[ \t]*$", re.MULTILINE)
CONS_INI = "<!-- logos:consolidado -->"
CONS_FIM = "<!-- /logos:consolidado -->"
_FEITA_RE = re.compile(r"^\s*- \[[xX]\] (.+)$", re.MULTILINE)
_ABERTA_RE = re.compile(r"^\s*- \[ \] (.+)$", re.MULTILINE)
_SUFIXOS_ACAO = re.compile(r"\s*(⏫|\(trazida de [^)]*\))\s*$")

LOCK_VENCE_S = 300
LOCK_ESPERA_S = 60
DIAS_SEMANA = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")
MAX_PENDENTES = 8

# Chaves do frontmatter que o Logos refaz a cada entrada. O resto (promoted:, promovido_ate:,
# correcoes:, description, cumprido...) é de outro dono e fica como está.
_CHAVES_COMUNS = ("date", "tipo", "titulo", "entradas", "horas", "modelo_llm")
_CHAVES_DIARIO = ("humor", "humor_nota", "humor_notas", "energia", "sono", "estresse", "dia_foi", "plano_cumprido")
_CHAVES_PLANO = ("para", "foco", "energia", "tempo_livre")
# Entidades só crescem: uma pessoa tirada à mão (o caso Vini) não volta porque a entrada 1 a citou.
_CHAVES_UNIAO = ("tags", "pessoas", "projetos")

ROTULOS_CUMPRIDO = {"sim": "cumpri", "parcial": "em parte", "nao": "não"}


class DiaError(Exception):
    pass


@dataclass
class Registro:
    caminho: Path
    numero: int
    total: int
    aviso: str = ""


# ---------------------------------------------------------------------------
# Datas e nomes
# ---------------------------------------------------------------------------

def dia_logico(momento: datetime) -> date:
    return (momento - timedelta(hours=config.VIRADA_DO_DIA_H)).date()


def rotulo_dia(dia: date) -> str:
    return f"{DIAS_SEMANA[dia.weekday()]} {dia:%d/%m}"


def caminho_nota(route: Route, dia: date) -> Path:
    nome = f"Diário — {dia.isoformat()}.md" if route == Route.DIARIO else f"Plano — {dia.isoformat()}.md"
    return config.NOTES_DIR / route.value / nome


def _normalizar(texto: str) -> str:
    texto = _SUFIXOS_ACAO.sub("", str(texto)).strip().lower()
    texto = "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))
    texto = re.sub(r"[^\w\s]", " ", texto)
    return " ".join(texto.split())


# ---------------------------------------------------------------------------
# Leitura e escrita da nota
# ---------------------------------------------------------------------------

@dataclass
class _Nota:
    frontmatter: dict
    topo: str  # tudo entre o frontmatter e a primeira entrada: título, consolidado, texto dele
    secoes: list[tuple[dict, str]]  # (dados do comentário, texto da seção)


def _comentario(dados: dict) -> str:
    # `>` só aparece dentro de strings no JSON; escapado, o texto nunca fecha o comentário.
    return f"<!-- logos:entrada {json.dumps(dados, ensure_ascii=False).replace('>', chr(92) + 'u003e')} -->"


def _separar_frontmatter(texto: str) -> tuple[dict, str]:
    if not texto.startswith("---\n"):
        return {}, texto
    fim = texto.find("\n---\n", 4)
    if fim < 0:
        return {}, texto
    try:
        fm = yaml.safe_load(texto[4:fim]) or {}
    except yaml.YAMLError:
        fm = {}
    return (fm if isinstance(fm, dict) else {}), texto[fim + 5:]


def _ler(caminho: Path) -> _Nota:
    if not caminho.exists():
        return _Nota({}, "", [])
    fm, corpo = _separar_frontmatter(caminho.read_text(encoding="utf-8"))
    corpo = corpo.lstrip("\n")  # sem isto, cada regravação somaria uma linha em branco no topo
    inicios: list[tuple[int, dict]] = []
    for m in _COMENTARIO_RE.finditer(corpo):
        try:
            dados = json.loads(m.group(1))
        except ValueError:
            continue
        # A seção começa no cabeçalho `## ` da linha de cima.
        cabecalho = corpo.rfind("\n", 0, max(m.start() - 1, 0)) + 1
        if not corpo.startswith("## ", cabecalho):
            continue
        inicios.append((cabecalho, dados))
    if not inicios:
        return _Nota(fm, corpo, [])
    secoes = []
    for i, (inicio, dados) in enumerate(inicios):
        fim = inicios[i + 1][0] if i + 1 < len(inicios) else len(corpo)
        secoes.append((dados, corpo[inicio:fim].rstrip("\n") + "\n"))
    return _Nota(fm, corpo[:inicios[0][0]], secoes)


def ler_entradas(route: Route, dia: date) -> list[dict]:
    return [dados for dados, _ in _ler(caminho_nota(route, dia)).secoes]


def _escrever(caminho: Path, nota: _Nota) -> None:
    fm = yaml.safe_dump(nota.frontmatter, allow_unicode=True, sort_keys=False)
    topo = nota.topo.rstrip("\n") + "\n\n"
    corpo = "\n".join(texto.rstrip("\n") + "\n" for _, texto in nota.secoes)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_name(f".{caminho.name}.tmp")
    tmp.write_text(f"---\n{fm}---\n\n{topo}{corpo}", encoding="utf-8")
    os.replace(tmp, caminho)


@contextmanager
def _trava(route: Route, dia: date):
    """Uma escrita por dia. A nota passa a ser lida e reescrita a cada entrada, e duas instâncias
    do watcher (armadilha já vista em 31/08) perderiam uma entrada na corrida."""
    caminho = config.STATE_DIR / "dias" / f"{route.value}-{dia.isoformat()}.lock"
    caminho.parent.mkdir(parents=True, exist_ok=True)
    inicio = time.monotonic()
    while True:
        try:
            fd = os.open(caminho, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                idade = time.time() - caminho.stat().st_mtime
            except FileNotFoundError:
                continue
            if idade > LOCK_VENCE_S:
                logger.warning(f"Lock vencido da nota do dia retomado: {caminho.name}")
                caminho.unlink(missing_ok=True)
                continue
            if time.monotonic() - inicio > LOCK_ESPERA_S:
                raise DiaError(f"nota do dia ocupada por outro processo ({caminho.name})")
            time.sleep(0.2)
    try:
        yield
    finally:
        caminho.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def _lista(valor) -> list[str]:
    if valor is None:
        return []
    if isinstance(valor, (list, tuple)):
        return [str(v).strip() for v in valor if str(v).strip()]
    texto = str(valor).strip()
    return [texto] if texto else []


def _texto(valor) -> str:
    return " ".join(_lista(valor)) if isinstance(valor, (list, tuple)) else str(valor or "").strip()


def linha_checkin(formulario: dict) -> str:
    f = formulario or {}
    partes = []
    if f.get("humor_nota"):
        partes.append(f"humor {f['humor_nota']}/5")
    if f.get("energia"):
        partes.append(f"energia {f['energia']}/5")
    if f.get("sono"):
        partes.append(f"sono {f['sono']}")
    if f.get("estresse"):
        partes.append(f"estresse {f['estresse']}")
    if f.get("dia_foi"):
        partes.append(f"dia {f['dia_foi']}")
    if f.get("plano_cumprido"):
        partes.append(f"plano: {ROTULOS_CUMPRIDO.get(f['plano_cumprido'], f['plano_cumprido'])}")
    if f.get("tempo_livre"):
        partes.append(f"tempo livre {f['tempo_livre']}")
    if f.get("foco"):
        partes.append(f"foco {f['foco']}")
    return " · ".join(partes)


def _blocos(valor) -> dict:
    if not isinstance(valor, dict):
        return {}
    return {k: _texto(valor.get(k)) for k in ("manha", "tarde", "noite") if _texto(valor.get(k))}


_ROTULO_BLOCO = {"manha": "Manhã", "tarde": "Tarde", "noite": "Noite"}


def _secao(numero: int, dados: dict, texto_md: str, anexos: list[dict], local: dict | None) -> str:
    enr = dados.get("enriquecimento") or {}
    titulo = _texto(enr.get("titulo"))
    cabecalho = f"## {dados['hora']} — entrada {numero}" + (f" · {titulo}" if titulo else "")
    linhas = [cabecalho, _comentario(dados), ""]
    checkin = linha_checkin(dados.get("formulario"))
    origem = "telegram" if dados.get("origem") == "telegram" else "gravador"
    linhas += [f"📊 {checkin} · via {origem}" if checkin else f"*via {origem}*", ""]
    if dados.get("degradado"):
        linhas += ["> [!warning] Enriquecimento não aconteceu", f"> {dados['degradado']}", ""]

    def item(rotulo: str, valor) -> None:
        texto = _texto(valor)
        if texto:
            linhas.extend([f"**{rotulo}:** {texto}", ""])

    def itens(rotulo: str, valor) -> None:
        lista = _lista(valor)
        if lista:
            linhas.extend([f"**{rotulo}:**", *(f"- {v}" for v in lista), ""])

    item("A UMA coisa", enr.get("intencao"))
    item("Próxima ação", enr.get("proxima_acao"))
    itens("Ações", enr.get("acoes"))
    blocos = _blocos(enr.get("blocos"))
    if blocos:
        linhas.extend(["**Blocos:**", *(f"- {_ROTULO_BLOCO[k]}: {v}" for k, v in blocos.items()), ""])
    itens("Fica de fora", enr.get("fica_de_fora"))
    itens("Decisões", enr.get("decisoes"))
    itens("Bloqueios", enr.get("bloqueios"))
    itens("Vitórias", enr.get("vitorias"))
    itens("Gratidão", enr.get("gratidao"))
    itens("Tensões", enr.get("tensoes"))
    itens("Pontos", enr.get("pontos"))
    item("O que muda amanhã", enr.get("amanha"))
    resumo = _texto(enr.get("resumo"))
    if resumo and _normalizar(resumo) != _normalizar(texto_md):
        item("Resumo", resumo)

    carregar = dados.get("carregar") or {}
    if carregar.get("trazidas"):
        linhas.extend([f"**Trazidas do plano de {carregar.get('de', 'ontem')}:**",
                       *(f"- {a}" for a in carregar["trazidas"]), ""])
    if carregar.get("do_diario"):
        linhas.extend([f"**Do diário:** {carregar['do_diario']}", ""])

    if anexos or local:
        linhas += ["### 📎 Anexos", *_attachment_lines(anexos)]
        if local:
            linhas.append(_map_line(local))
        linhas.append("")
    titulo_texto = "### 💬 Mensagens" if origem == "telegram" else "### 🎙️ Transcrição"
    linhas += [titulo_texto, "", texto_md.strip() if texto_md.strip() else "*(só o check-in)*", ""]
    return "\n".join(linhas)


def _ultimo(entradas: list[dict], *caminho) -> object:
    for dados in reversed(entradas):
        valor = dados
        for chave in caminho:
            valor = valor.get(chave) if isinstance(valor, dict) else None
        if valor not in (None, "", [], {}):
            return valor
    return None


def _somados(entradas: list[dict], campo: str) -> list[str]:
    vistos, itens = set(), []
    for dados in entradas:
        for v in _lista((dados.get("enriquecimento") or {}).get(campo)):
            chave = _normalizar(v)
            if chave and chave not in vistos:
                vistos.add(chave)
                itens.append(v)
    return itens


def acoes_consolidadas(entradas: list[dict]) -> list[str]:
    """Ações do dia, sem repetir: as trazidas do plano anterior, depois as de cada entrada."""
    vistos, acoes = set(), []

    def somar(texto: str, sufixo: str = "") -> None:
        chave = _normalizar(texto)
        if chave and chave not in vistos:
            vistos.add(chave)
            acoes.append(texto + sufixo)

    for dados in entradas:
        carregar = dados.get("carregar") or {}
        for a in carregar.get("trazidas") or []:
            somar(a, f" (trazida de {carregar.get('de', 'ontem')})")
    for dados in entradas:
        for a in _lista((dados.get("enriquecimento") or {}).get("acoes")):
            somar(a)
    return acoes


def _consolidado(route: Route, dia: date, entradas: list[dict], fm: dict, feitas: set[str]) -> list[str]:
    def caixa(texto: str, sufixo: str = "") -> str:
        marca = "x" if _normalizar(texto) in feitas else " "
        return f"- [{marca}] {texto}{sufixo}"

    linhas = [CONS_INI]
    if route == Route.PLANEJAMENTO:
        foco = _ultimo(entradas, "formulario", "foco")
        if not foco or foco == "outro":
            foco = _ultimo(entradas, "enriquecimento", "foco") or foco
        cabeca = [f"foco {foco}" if foco else ""]
        energia = _ultimo(entradas, "formulario", "energia")
        tempo = _ultimo(entradas, "formulario", "tempo_livre")
        cabeca += [f"energia {energia}/5" if energia else "", f"tempo livre {tempo}" if tempo else ""]
        cabeca = " · ".join(c for c in cabeca if c)
        intencao = _texto(_ultimo(entradas, "enriquecimento", "intencao"))
        if cabeca or intencao:
            linhas += ["## 🎯 Foco do dia"]
            if cabeca:
                linhas.append(cabeca)
            if intencao:
                linhas += ["", f"**A UMA coisa:** {intencao}"]
            linhas.append("")
        proxima = _texto(_ultimo(entradas, "enriquecimento", "proxima_acao"))
        if proxima:
            linhas += ["## ⚡ Próxima Ação Imediata", caixa(proxima, " ⏫"), ""]
        acoes = acoes_consolidadas(entradas)
        if acoes:
            linhas += ["## 📋 Ações & Tarefas", *(caixa(a) for a in acoes), ""]
        blocos: dict[str, str] = {}
        for dados in entradas:
            blocos.update(_blocos((dados.get("enriquecimento") or {}).get("blocos")))
        if blocos:
            linhas += ["## 🕒 Blocos do dia", *(f"- **{_ROTULO_BLOCO[k]}:** {blocos[k]}"
                                                for k in ("manha", "tarde", "noite") if k in blocos), ""]
        fora = _somados(entradas, "fica_de_fora")
        if fora:
            linhas += ["## 🚪 Fica de fora", *(f"- {f}" for f in fora), ""]
        for campo, titulo in (("decisoes", "⚖️ Decisões Tomadas"), ("bloqueios", "🚫 Bloqueios & Dependências")):
            itens = _somados(entradas, campo)
            if itens:
                linhas += [f"## {titulo}", *(f"- {i}" for i in itens), ""]
        if fm.get("cumprido"):
            rotulo = ROTULOS_CUMPRIDO.get(fm["cumprido"], fm["cumprido"])
            linhas += ["## ✅ Cumprido", f"{rotulo} (conferido no diário)", ""]
    else:
        checkins = [f"- {d['hora']} · {linha_checkin(d.get('formulario'))}"
                    for d in entradas if linha_checkin(d.get("formulario"))]
        if checkins:
            linhas += ["## 📊 Check-ins", *checkins, ""]
        for campo, titulo in (
            ("vitorias", "🏆 Vitórias & Progresso (Brag Doc)"),
            ("gratidao", "🙏 Gratidão & Apreciação"),
            ("tensoes", "🌧️ Tensões & Preocupações"),
            ("pontos", "💡 Pontos & Reflexões"),
        ):
            itens = _somados(entradas, campo)
            if itens:
                linhas += [f"## {titulo}", *(f"- {i}" for i in itens), ""]
        amanha = [_texto((d.get("enriquecimento") or {}).get("amanha")) for d in entradas]
        amanha = [a for a in amanha if a]
        if amanha:
            linhas += ["## 🔭 O que muda amanhã", *(f"- {a}" for a in amanha), ""]
    if len(linhas) == 1:
        linhas += ["*(o resumo aparece quando houver conteúdo)*", ""]
    linhas.append(CONS_FIM)
    return linhas


def _titulo_nota(route: Route, dia: date) -> str:
    return f"Diário de {rotulo_dia(dia)}" if route == Route.DIARIO else f"Plano para {rotulo_dia(dia)}"


def _refazer_topo(route: Route, dia: date, topo: str, entradas: list[dict], fm: dict) -> str:
    ini, fim = topo.find(CONS_INI), topo.find(CONS_FIM)
    antigo = topo[ini:fim] if 0 <= ini < fim else ""
    feitas = {_normalizar(m.group(1)) for m in _FEITA_RE.finditer(antigo)}
    bloco = "\n".join(_consolidado(route, dia, entradas, fm, feitas))
    if 0 <= ini < fim:
        return topo[:ini] + bloco + topo[fim + len(CONS_FIM):]
    if topo.strip():
        return topo.rstrip("\n") + "\n\n" + bloco + "\n\n---\n"
    return f"# {_titulo_nota(route, dia)}\n\n{bloco}\n\n---\n"


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------

def _frontmatter(route: Route, dia: date, fm: dict, entradas: list[dict], nova: dict) -> dict:
    fm = dict(fm)
    proprias = {
        "date": dia.isoformat(),
        "tipo": "diario" if route == Route.DIARIO else "planejamento",
        "titulo": _titulo_nota(route, dia),
        "entradas": len(entradas),
        "horas": [d["hora"] for d in entradas],
        "modelo_llm": _ultimo(entradas, "modelo_llm"),
    }
    if route == Route.DIARIO:
        notas = [d["formulario"]["humor_nota"] for d in entradas if (d.get("formulario") or {}).get("humor_nota")]
        proprias.update({
            "humor": _texto(_ultimo(entradas, "enriquecimento", "humor")),
            "humor_nota": notas[-1] if notas else None,
            "humor_notas": notas,
            "energia": _ultimo(entradas, "formulario", "energia"),
            "sono": _ultimo(entradas, "formulario", "sono"),
            "estresse": _ultimo(entradas, "formulario", "estresse"),
            "dia_foi": _ultimo(entradas, "formulario", "dia_foi"),
            "plano_cumprido": _ultimo(entradas, "formulario", "plano_cumprido"),
        })
    else:
        foco = _ultimo(entradas, "formulario", "foco")
        if not foco or foco == "outro":
            foco = _ultimo(entradas, "enriquecimento", "foco") or foco
        proprias.update({
            "para": dia.isoformat(),
            "foco": foco,
            "energia": _ultimo(entradas, "formulario", "energia"),
            "tempo_livre": _ultimo(entradas, "formulario", "tempo_livre"),
        })
    for chave, valor in proprias.items():
        if valor in (None, "", [], {}):
            fm.pop(chave, None)
        else:
            fm[chave] = valor
    enr = nova.get("enriquecimento") or {}
    for chave in _CHAVES_UNIAO:
        atuais = _lista(fm.get(chave))
        novos = [v for v in _lista(enr.get(chave)) if v not in atuais]
        if atuais or novos:
            fm[chave] = atuais + novos
    if any(d.get("degradado") for d in entradas):
        fm["status"] = "degradado"
    elif fm.get("status") == "degradado":
        fm.pop("status")
    return fm


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------

def registrar_entrada(
    route: Route,
    dia: date,
    dados: dict,
    texto_md: str,
    anexos: list[dict] | None = None,
    local: dict | None = None,
) -> Registro:
    """Soma uma entrada à nota do dia. A mesma `fonte` de novo substitui a entrada que já está lá:
    reprocessar um pacote, ou o mesmo áudio chegar duas vezes, não soma duas vezes."""
    if route not in (Route.DIARIO, Route.PLANEJAMENTO):
        raise ValueError(f"rota sem nota por dia: {route}")
    caminho = caminho_nota(route, dia)
    with _trava(route, dia):
        nota = _ler(caminho)
        existentes = [d for d, _ in nota.secoes]
        registradas = int(nota.frontmatter.get("entradas") or 0)
        inconsistente = registradas > len(existentes)
        posicao = next((i for i, d in enumerate(existentes) if d.get("fonte") == dados["fonte"]), None)
        if posicao is not None:
            numero = int(existentes[posicao].get("numero") or posicao + 1)
        else:
            numero = max(registradas, len(existentes)) + 1
        dados = {**dados, "numero": numero}
        secao = _secao(numero, dados, texto_md, anexos or [], local)
        if posicao is not None:
            nota.secoes[posicao] = (dados, secao)
        else:
            nota.secoes.append((dados, secao))
        entradas = [d for d, _ in nota.secoes]

        aviso = ""
        fm = _frontmatter(route, dia, nota.frontmatter, entradas, dados)
        if inconsistente:
            # Alguém apagou um comentário de entrada: refazer o topo a partir de uma lista
            # incompleta apagaria parte do dia. Só acrescenta, e avisa.
            fm["entradas"] = max(registradas + (posicao is None), len(entradas))
            aviso = ("A nota do dia tinha entradas que o Logos não reconhece mais; o resumo do topo "
                     "não foi refeito. Confira a nota.")
            logger.warning(f"Nota do dia inconsistente, consolidado preservado: {caminho.name}")
            topo = nota.topo or f"# {_titulo_nota(route, dia)}\n\n---\n"
        else:
            topo = _refazer_topo(route, dia, nota.topo, entradas, fm)
        _escrever(caminho, _Nota(fm, topo, nota.secoes))
    return Registro(caminho, numero, int(fm["entradas"]), aviso)


def marcar_cumprido(dia: date, valor: str) -> Path | None:
    """O diário conferiu o plano do dia. Só o veredito entra no plano: nada do diário."""
    caminho = caminho_nota(Route.PLANEJAMENTO, dia)
    if not caminho.exists():
        return None
    with _trava(Route.PLANEJAMENTO, dia):
        nota = _ler(caminho)
        fm = dict(nota.frontmatter)
        fm["cumprido"] = valor
        entradas = [d for d, _ in nota.secoes]
        topo = nota.topo
        if int(fm.get("entradas") or 0) <= len(entradas) and entradas:
            topo = _refazer_topo(Route.PLANEJAMENTO, dia, nota.topo, entradas, fm)
        _escrever(caminho, _Nota(fm, topo, nota.secoes))
    return caminho


# ---------------------------------------------------------------------------
# Contexto para o bot (integrações)
# ---------------------------------------------------------------------------

def plano_de(dia: date) -> dict | None:
    """O que o plano do dia diz, para o diário conferir e para o próximo plano somar."""
    caminho = caminho_nota(Route.PLANEJAMENTO, dia)
    nota = _ler(caminho)
    if not nota.secoes:
        return None
    entradas = [d for d, _ in nota.secoes]
    foco = _ultimo(entradas, "formulario", "foco")
    if not foco or foco == "outro":
        foco = _ultimo(entradas, "enriquecimento", "foco") or ""
    return {
        "entradas": len(entradas),
        "foco": str(foco or ""),
        "intencao": _texto(_ultimo(entradas, "enriquecimento", "intencao")),
        "proxima_acao": _texto(_ultimo(entradas, "enriquecimento", "proxima_acao")),
        "acoes": acoes_consolidadas(entradas),
    }


def entradas_do_diario(dia: date) -> int:
    return len(ler_entradas(Route.DIARIO, dia))


def escalas_do_diario(dia: date) -> dict:
    """O frontmatter do diário do dia (humor_nota, energia, sono, estresse...), ou {} sem diário."""
    caminho = caminho_nota(Route.DIARIO, dia)
    if not caminho.exists():
        return {}
    fm, _ = _separar_frontmatter(caminho.read_text(encoding="utf-8"))
    return fm


def amanha_do_diario(dia: date) -> str:
    """O "o que muda amanhã" dos diários do dia, para o plano do dia seguinte."""
    itens = [_texto((d.get("enriquecimento") or {}).get("amanha")) for d in ler_entradas(Route.DIARIO, dia)]
    return " · ".join(i for i in itens if i)


def _alvos_promovidos(fm: dict) -> list[Path]:
    raiz = (config.VAULT_DIR or config.NOTES_DIR.parent).resolve()
    ativos = (raiz / "work" / "active").resolve()
    alvos = []
    for item in _lista(fm.get("promoted")):
        link = re.search(r"\[\[([^\]|#]+)", item)
        if link:
            nome = link.group(1).strip()
            candidatos = list(ativos.rglob(f"{Path(nome).name}.md")) if ativos.is_dir() else []
        else:
            candidatos = [raiz / item.strip().strip('"')]
        for c in candidatos:
            try:
                resolvido = c.resolve()
            except OSError:
                continue
            if resolvido.is_file() and resolvido.is_relative_to(ativos):
                alvos.append(resolvido)
    return alvos


def acoes_pendentes(dia: date) -> list[str]:
    """Ações ainda abertas no plano do dia. Ação promovida conta como feita quando está marcada
    na nota de projeto: é lá que ele marca, e sem essa conferência o lembrete ofereceria para
    sempre coisa já feita."""
    caminho = caminho_nota(Route.PLANEJAMENTO, dia)
    nota = _ler(caminho)
    ini, fim = nota.topo.find(CONS_INI), nota.topo.find(CONS_FIM)
    if not (0 <= ini < fim):
        return []
    abertas = [_SUFIXOS_ACAO.sub("", m.group(1)).strip() for m in _ABERTA_RE.finditer(nota.topo[ini:fim])]
    feitas_fora: list[str] = []
    for alvo in _alvos_promovidos(nota.frontmatter):
        try:
            feitas_fora += [_normalizar(m.group(1)) for m in _FEITA_RE.finditer(alvo.read_text(encoding="utf-8"))]
        except OSError:
            continue
    pendentes, vistos = [], set()
    for acao in abertas:
        chave = _normalizar(acao)
        if not chave or chave in vistos or any(chave in f or f in chave for f in feitas_fora if f):
            continue
        vistos.add(chave)
        pendentes.append(acao)
    return pendentes[:MAX_PENDENTES]
