"""Formulário guiado do /diario e do /planejamento, por botões, dentro da janela de pacote.

Tudo o que o formulário sabe mora no manifest do pacote aberto, e é por isso que sobrevive a um
reinício. `manifest["form"]` guarda o estado da tela: etapa atual, listas mostradas e o contexto
lido na abertura. `manifest["formulario"]` guarda as respostas (o pipeline as grava no
frontmatter), e `manifest["carregar"]` o que veio do plano anterior ou do diário.

O dado de cada botão é `f:<sid>:<passo>:<ação>`, sempre com índice e nunca com nome, para caber
nos 64 bytes da Bot API. `sid` é o message_id do comando que abriu o formulário, o mesmo que
nomeia o pacote.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol

from . import dia as dia_mod
from .consulta import Tela, esc

PERGUNTAS = {
    "diario": (
        "O que foi bom hoje? (uma vitória, uma gratidão, alguém que ajudou)",
        "O que pesou ou travou?",
        "O que você leva do dia, e o que muda amanhã?",
    ),
    "planejamento": (
        "Qual é a UMA coisa que, feita, faz o dia valer?",
        "Como ficam manhã, tarde e noite?",
        "O que fica de fora, ou o que pode travar?",
    ),
}


@dataclass(frozen=True)
class Escala:
    campo: str
    pergunta: str
    opcoes: tuple[tuple[str, object], ...]
    dica: str = ""


_UM_A_CINCO = tuple((str(n), n) for n in range(1, 6))
ESCALAS = {
    "humor": Escala("humor_nota", "Como está o humor?", _UM_A_CINCO, "1 = péssimo · 5 = ótimo"),
    "energia": Escala("energia", "E a energia?", _UM_A_CINCO, "1 = esgotado · 5 = cheio"),
    "sono": Escala("sono", "Quanto você dormiu?", (
        ("menos de 5h", "<5h"), ("5 a 6h", "5–6h"), ("7 a 8h", "7–8h"), ("mais de 8h", ">8h"))),
    "estresse": Escala("estresse", "Nível de estresse?", (("baixo", "baixo"), ("médio", "médio"), ("alto", "alto"))),
    "dia": Escala("dia_foi", "O dia foi...", (
        ("produtivo", "produtivo"), ("leve", "leve"), ("pesado", "pesado"),
        ("caótico", "caótico"), ("parado", "parado"))),
    "energia_plano": Escala("energia", "Energia prevista para o dia?", _UM_A_CINCO, "1 = esgotado · 5 = cheio"),
    "tempo": Escala("tempo_livre", "Quanto tempo livre para trabalho focado?", (
        ("menos de 2h", "<2h"), ("2 a 4h", "2–4h"), ("4 a 6h", "4–6h"), ("dia cheio", "dia cheio"))),
}
_CUMPRIDO = (("Cumpri", "sim"), ("Em parte", "parcial"), ("Não", "nao"))
_ETAPAS_DIARIO = ("humor", "energia", "sono", "estresse", "dia", "p1", "p2", "p3")
_ETAPAS_PLANO = ("energia_plano", "tempo", "foco", "p1", "p2", "p3")
MAX_PROJETOS = 8


class FormError(Exception):
    """Toque que não vale mais. A mensagem vira aviso passageiro no topo da tela."""


class FonteContexto(Protocol):
    def plano(self, dia: date) -> dict | None: ...
    def entradas_diario(self, dia: date) -> int: ...
    def amanha(self, dia: date) -> str: ...
    def pendentes(self, dia: date) -> list[str]: ...
    def projetos(self) -> list[str]: ...


class VaultContexto:
    """Contexto lido do vault de verdade: as notas do dia e os projetos ativos da consulta."""

    def plano(self, dia: date) -> dict | None:
        return dia_mod.plano_de(dia)

    def entradas_diario(self, dia: date) -> int:
        return dia_mod.entradas_do_diario(dia)

    def amanha(self, dia: date) -> str:
        return dia_mod.amanha_do_diario(dia)

    def pendentes(self, dia: date) -> list[str]:
        return dia_mod.acoes_pendentes(dia)

    def projetos(self) -> list[str]:
        from . import consulta
        return [p.stem for p in consulta.projetos()[:MAX_PROJETOS]]


# ---------------------------------------------------------------------------
# Abertura
# ---------------------------------------------------------------------------

def iniciar(manifest: dict, rota: str, sid: int, agora: datetime, fonte: FonteContexto) -> None:
    hoje = dia_mod.dia_logico(agora)
    manifest["rota"] = rota
    manifest["formulario"] = {}
    form = {"sid": sid, "passo": 0, "listas": {}, "contexto": {}, "marcadas": []}
    if rota == "diario":
        manifest["dia"] = hoje.isoformat()
        plano = fonte.plano(hoje)
        etapas = (["plano"] if plano else []) + list(_ETAPAS_DIARIO)
        form["contexto"] = {"plano": plano, "entradas": fonte.entradas_diario(hoje)}
    else:
        etapas = ["para"]
        form["contexto"] = {"hoje": hoje.isoformat()}
    form["etapas"] = etapas
    manifest["form"] = form


def _preparar_plano(manifest: dict, para: date, fonte: FonteContexto) -> None:
    """Depois de escolher o dia: o que o plano já tem, o que o diário disse e o que ficou aberto."""
    form = manifest["form"]
    anterior = para - timedelta(days=1)
    plano = fonte.plano(para)
    amanha = fonte.amanha(anterior)
    ja_no_plano = {dia_mod._normalizar(a) for a in (plano or {}).get("acoes", [])}
    pendentes = [a for a in fonte.pendentes(anterior) if dia_mod._normalizar(a) not in ja_no_plano]
    projetos = fonte.projetos()
    form["contexto"].update({"plano": plano, "amanha": amanha, "de": dia_mod.rotulo_dia(anterior)})
    form["listas"] = {"pendentes": pendentes, "foco": projetos}
    etapas = ["para"]
    if amanha:
        etapas.append("diario")
    if pendentes:
        etapas.append("pendentes")
    form["etapas"] = etapas + list(_ETAPAS_PLANO)


# ---------------------------------------------------------------------------
# Estado
# ---------------------------------------------------------------------------

def ativo(manifest: dict) -> bool:
    return bool(manifest.get("form"))


def etapa_atual(manifest: dict) -> str | None:
    form = manifest.get("form") or {}
    etapas, passo = form.get("etapas") or [], form.get("passo", 0)
    return etapas[passo] if 0 <= passo < len(etapas) else None


def pergunta_atual(manifest: dict) -> int | None:
    etapa = etapa_atual(manifest)
    return int(etapa[1:]) if etapa and etapa.startswith("p") and etapa[1:].isdigit() else None


def tem_respostas(manifest: dict) -> bool:
    return bool(manifest.get("formulario")) or bool((manifest.get("carregar") or {}))


def _dia_do_form(manifest: dict) -> date | None:
    valor = manifest.get("para") or manifest.get("dia")
    try:
        return date.fromisoformat(valor) if valor else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Telas
# ---------------------------------------------------------------------------

def _dado(manifest: dict, acao: str) -> str:
    form = manifest["form"]
    return f"f:{form['sid']}:{form['passo']}:{acao}"


def _cabecalho(manifest: dict) -> str:
    form = manifest["form"]
    contexto = form.get("contexto") or {}
    dia = _dia_do_form(manifest)
    if manifest["rota"] == "diario":
        titulo = f"<b>Diário · {dia_mod.rotulo_dia(dia)}</b>" if dia else "<b>Diário</b>"
        anteriores = contexto.get("entradas") or 0
        if anteriores:
            titulo += f" · entrada {anteriores + 1} do dia"
    else:
        titulo = f"<b>Plano para {dia_mod.rotulo_dia(dia)}</b>" if dia else "<b>Planejamento</b>"
        plano = contexto.get("plano")
        if plano and dia:
            titulo += f" · somando à entrada {plano['entradas']}"
    total = len(form["etapas"]) if not (manifest["rota"] == "planejamento" and form["passo"] == 0) else None
    progresso = f"Etapa {form['passo'] + 1} de {total}" if total else ""
    nuvem = ("<i>Lembrete: o diário pelo Telegram passa pela nuvem do Telegram.</i>"
             if manifest["rota"] == "diario" and form["passo"] == 0 else "")
    return "\n".join(filter(None, [titulo, progresso, nuvem]))


def _rodape_livre() -> str:
    return "<i>Texto, voz, foto ou arquivo entram nesta entrada a qualquer momento.</i>"


def tela(manifest: dict) -> Tela:
    form = manifest["form"]
    etapa = etapa_atual(manifest)
    contexto = form.get("contexto") or {}
    linhas = [_cabecalho(manifest), ""]
    botoes: list[list[tuple[str, str]]] = []
    concluir = ("Concluir", _dado(manifest, "c"))

    if etapa in ESCALAS:
        escala = ESCALAS[etapa]
        linhas.append(esc(escala.pergunta))
        if escala.dica:
            linhas.append(f"<i>{esc(escala.dica)}</i>")
        opcoes = [(rotulo, _dado(manifest, str(i))) for i, (rotulo, _) in enumerate(escala.opcoes)]
        botoes += [opcoes] if len(opcoes) <= 5 and all(len(r) <= 2 for r, _ in opcoes) else [
            opcoes[i:i + 2] for i in range(0, len(opcoes), 2)]
        botoes.append([("Pular", _dado(manifest, "p")), concluir])
        linhas += ["", _rodape_livre()]
    elif etapa == "plano":
        plano = contexto.get("plano") or {}
        linhas.append("<b>Plano de hoje</b>")
        if plano.get("foco"):
            linhas.append(f"Foco: {esc(plano['foco'])}")
        if plano.get("intencao"):
            linhas.append(f"A UMA coisa: {esc(plano['intencao'])}")
        elif plano.get("proxima_acao"):
            linhas.append(f"Próxima ação: {esc(plano['proxima_acao'])}")
        linhas += ["", "Você cumpriu?"]
        botoes.append([(rotulo, _dado(manifest, str(i))) for i, (rotulo, _) in enumerate(_CUMPRIDO)])
        botoes.append([("Pular", _dado(manifest, "p")), concluir])
    elif etapa == "para":
        hoje = date.fromisoformat(contexto["hoje"])
        linhas.append("O plano é para qual dia?")
        botoes.append([
            (f"Hoje · {dia_mod.rotulo_dia(hoje)}", _dado(manifest, "0")),
            (f"Amanhã · {dia_mod.rotulo_dia(hoje + timedelta(days=1))}", _dado(manifest, "1")),
        ])
        botoes.append([("Cancelar", _dado(manifest, "c"))])
    elif etapa == "diario":
        linhas += [
            f"<b>Do diário de {esc(contexto.get('de', 'ontem'))}</b>, o que muda amanhã:",
            esc(contexto.get("amanha", "")),
            "",
            "<i>Levar copia este trecho para a nota do plano, que vai para o git e fica visível "
            "na consulta. Só ver deixa o diário onde está.</i>",
        ]
        botoes.append([("Levar para o plano", _dado(manifest, "l")), ("Só ver", _dado(manifest, "v"))])
        botoes.append([concluir])
    elif etapa == "pendentes":
        pendentes = form["listas"].get("pendentes") or []
        marcadas = set(form.get("marcadas") or [])
        linhas += [f"<b>Ficou aberto no plano de {esc(contexto.get('de', 'ontem'))}.</b>",
                   "Toque nas ações que vêm para este plano."]
        for i, acao in enumerate(pendentes):
            marca = "[x]" if i in marcadas else "[ ]"
            botoes.append([(f"{marca} {acao}"[:60], _dado(manifest, f"t{i}"))])
        botoes.append([("Trazer marcadas", _dado(manifest, "ok")), ("Nenhuma", _dado(manifest, "p"))])
    elif etapa == "foco":
        projetos = form["listas"].get("foco") or []
        linhas.append("Qual é o foco principal do dia?")
        opcoes = [(nome[:30], _dado(manifest, str(i))) for i, nome in enumerate(projetos)]
        botoes += [opcoes[i:i + 2] for i in range(0, len(opcoes), 2)]
        botoes.append([("Outro", _dado(manifest, "o")), ("Nenhum", _dado(manifest, "p"))])
        botoes.append([concluir])
        linhas += ["", _rodape_livre()]
    elif etapa and etapa.startswith("p"):
        n = int(etapa[1:])
        linhas += [
            f"<b>Pergunta {n} de 3</b>",
            esc(PERGUNTAS[manifest["rota"]][n - 1]),
            "",
            "<i>Responda com texto, voz, foto ou arquivo, quantas mensagens quiser. "
            "Não precisa usar o responder.</i>",
        ]
        botoes.append([("Próxima", _dado(manifest, "n")), concluir] if n < 3 else [concluir])
    return Tela("\n".join(linhas).strip(), botoes)


def resumo(manifest: dict, mensagens: int) -> Tela:
    """A tela que fica no lugar do formulário quando ele fecha, sem botões."""
    dia = _dia_do_form(manifest)
    rotulo = dia_mod.rotulo_dia(dia) if dia else ""
    if manifest.get("rota") == "diario":
        linhas = [f"<b>Diário de {rotulo} fechado.</b>" if rotulo else "<b>Diário fechado.</b>"]
    else:
        linhas = [f"<b>Plano para {rotulo} fechado.</b>" if rotulo else "<b>Planejamento cancelado.</b>"]
    checkin = dia_mod.linha_checkin(manifest.get("formulario") or {})
    if checkin:
        linhas.append(esc(checkin))
    carregar = manifest.get("carregar") or {}
    if carregar.get("trazidas"):
        linhas.append(f"{len(carregar['trazidas'])} ação(ões) trazida(s) do plano anterior.")
    if carregar.get("do_diario"):
        linhas.append("O trecho do diário foi para o plano.")
    if mensagens:
        linhas.append(f"{mensagens} mensagem(ns). A nota sai em seguida.")
    elif checkin or carregar:
        linhas.append("Só o check-in. A nota sai em seguida.")
    else:
        linhas.append("Nada foi registrado.")
    return Tela("\n".join(linhas))


# ---------------------------------------------------------------------------
# Toque
# ---------------------------------------------------------------------------

def ler_dado(dado: str) -> tuple[int, int, str]:
    partes = dado.split(":")
    if len(partes) != 4 or partes[0] != "f" or not partes[1].isdigit() or not partes[2].isdigit():
        raise FormError("Botão com defeito.")
    return int(partes[1]), int(partes[2]), partes[3]


def _opcao(acao: str, quantas: int) -> int:
    if not acao.isdigit() or not 0 <= int(acao) < quantas:
        raise FormError("Botão com defeito.")
    return int(acao)


def aplicar(manifest: dict, passo: int, acao: str, fonte: FonteContexto) -> bool:
    """Aplica o toque e avança. Devolve True quando o formulário acabou (Concluir ou fim)."""
    form = manifest["form"]
    if passo != form["passo"]:
        raise FormError("Essa tela já passou. Use a mensagem mais recente do formulário.")
    etapa = etapa_atual(manifest)
    if acao == "c":
        return True
    respostas = manifest.setdefault("formulario", {})

    if etapa in ESCALAS:
        if acao != "p":
            escala = ESCALAS[etapa]
            respostas[escala.campo] = escala.opcoes[_opcao(acao, len(escala.opcoes))][1]
    elif etapa == "plano":
        if acao != "p":
            respostas["plano_cumprido"] = _CUMPRIDO[_opcao(acao, len(_CUMPRIDO))][1]
    elif etapa == "para":
        hoje = date.fromisoformat(form["contexto"]["hoje"])
        escolha = _opcao(acao, 2)
        para = hoje + timedelta(days=escolha)
        manifest["para"] = para.isoformat()
        respostas["horizonte"] = "amanha" if escolha else "hoje"
        _preparar_plano(manifest, para, fonte)
    elif etapa == "diario":
        if acao == "l":
            manifest.setdefault("carregar", {})["do_diario"] = form["contexto"].get("amanha", "")
        elif acao != "v":
            raise FormError("Botão com defeito.")
    elif etapa == "pendentes":
        pendentes = form["listas"].get("pendentes") or []
        if acao.startswith("t"):
            i = _opcao(acao[1:], len(pendentes))
            marcadas = set(form.get("marcadas") or [])
            marcadas ^= {i}
            form["marcadas"] = sorted(marcadas)
            return False
        if acao == "ok":
            trazidas = [pendentes[i] for i in form.get("marcadas") or [] if i < len(pendentes)]
            if trazidas:
                carregar = manifest.setdefault("carregar", {})
                carregar["trazidas"] = trazidas
                carregar["de"] = form["contexto"].get("de", "")
        elif acao != "p":
            raise FormError("Botão com defeito.")
    elif etapa == "foco":
        projetos = form["listas"].get("foco") or []
        if acao == "o":
            respostas["foco"] = "outro"
        elif acao != "p":
            respostas["foco"] = f"[[{projetos[_opcao(acao, len(projetos))]}]]"
    elif etapa and etapa.startswith("p"):
        if acao not in ("n", "p"):
            raise FormError("Botão com defeito.")
    else:
        return True

    form["passo"] += 1
    return form["passo"] >= len(form["etapas"])


def contexto_llm(manifest: dict) -> str:
    """O que o formulário diz, em texto, para a LLM tratar como fato informado pelo autor."""
    linhas = []
    checkin = dia_mod.linha_checkin(manifest.get("formulario") or {})
    if checkin:
        linhas.append(f"Check-in informado pelo autor: {checkin}.")
    carregar = manifest.get("carregar") or {}
    if carregar.get("trazidas"):
        linhas.append("Ações trazidas do plano anterior: " + "; ".join(carregar["trazidas"]) + ".")
    if carregar.get("do_diario"):
        linhas.append(f"Nota do diário sobre o que muda: {carregar['do_diario']}")
    return "\n".join(linhas)
