"""Assistente do dia do app Planeja: "Como está indo?" e "Fechar o dia".

O celular põe a fala na Semana (`/api/dia`); o drenador busca as novas a cada volta e, numa
thread própria (uma por vez), pensa com o Claude sem tela: o plano da semana já com os ajustes,
as marcações, o mínimo de cada dia, a ordem de corte, os pedidos de remarcar, a conversa de hoje
e a hora de agora. A resposta volta como texto curto, frases de mudança e operações sobre o plano
(`lib/ajustes.mjs` da Semana). Quem confere se as operações se aplicam é a Semana: se ela recusar
(422), o Claude recebe o erro e tenta mais uma vez; depois disso vira falha.

    novo → pensando → pronta (o celular aceita ou não) | aplicada (fechamento) | falha

No fechamento, "cortar" e "guardar" (Próx. semana) são decisão dele e viram operação aqui, sem
passar pelo Claude; o Claude só acha lugar para o que vai para amanhã ou outro dia e refaz o
resto da semana se precisar.

Dia lógico (desde 25/10/2026): o dia vira às 4h (`dia.dia_logico`, `virada_do_dia_h`), como no
Planeja e no Organiza. À 1h30 de quinta ainda é quarta: "hoje" é quarta, e o "amanhã" de um
fechamento é o `para` que o celular mandou ou, em fala antiga sem ele, o dia seguinte ao da fala.

API antiga e nova: a Semana nova manda `recursos` em `/api/semana`. Sem `recursos` (ou sem um
item dele), o comportamento é o de antes: sem `guardar` ("Próx. semana" fica sem decisão), sem
`resumo` e com a ordem de corte lida do index.html.

Fica de fora: nada do vault entra no prompt, e o Claude roda sem ferramentas (`--tools ""`),
sem as configurações do usuário e com um prompt de sistema próprio, curto.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, organiza
from .dia import dia_logico

logger = logging.getLogger(__name__)

BASE = organiza.URL.rsplit("/api/", 1)[0]
FUSO = ZoneInfo("America/Sao_Paulo")
# Id completo (pedido dele, 28/10/2026): o Claude que roda na hora é o Opus 5.5; alias curto o CLI ignora.
MODELO = "claude-opus-5-5"
LIMITE_CLAUDE_S = 300
INDEX_SEMANA = Path("C:/Workspace/repo-att/semana/site/index.html")
NOMES = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]

OPS_CLAUDE = ["mover", "horario", "novo", "tirar", "cortar"]
RESUMO_MAX = 300  # o limite da op `resumo` na Semana (tema e min, cada um)


def tem_recurso(semana: dict, nome: str) -> bool:
    """A Semana nova anuncia o que sabe fazer em `recursos`; sem a lista, é a API antiga."""
    return nome in (semana.get("recursos") or [])


def ops_do_claude(semana: dict) -> list[str]:
    return OPS_CLAUDE + (["resumo"] if tem_recurso(semana, "resumo") else [])


def schema(semana: dict) -> dict:
    return {
        "type": "object",
        "properties": {
            "resposta": {"type": "string", "description": "2 a 4 frases para ele, em português"},
            "mudancas": {"type": "array", "items": {"type": "string"}, "description": "uma frase curta por mudança"},
            "ops": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {"type": "string", "enum": ops_do_claude(semana)},
                        "tarefa": {"type": "string"}, "dia": {"type": "string"}, "bloco": {"type": "string"},
                        "i": {"type": "string"}, "f": {"type": "string"}, "para": {"type": "string"},
                        "k": {"type": "string"}, "nome": {"type": "string"},
                        "tarefas": {"type": "array", "items": {"type": "string"}},
                        **({"tema": {"type": "string"}, "min": {"type": "string"}}
                           if tem_recurso(semana, "resumo") else {}),
                    },
                    "required": ["op"],
                },
            },
        },
        "required": ["resposta", "mudancas", "ops"],
    }


SCHEMA = schema({})  # o da API antiga

SISTEMA_RESUMO = """- {"op":"resumo","dia":D,"tema":"...","min":"..."} reescreve o resumo (tema) do dia e, se algo do
  mínimo saiu ou entrou, o mínimo de texto; mande ao menos um dos dois, até 300 caracteres cada. Use quando
  suas mudanças deixarem o tema do dia errado (ex.: "Refeito: começa às 10h30; o roteiro foi para quinta.")."""

SISTEMA = """Você é o assistente do dia do André, dentro do app Planeja. Ele planeja a semana em blocos
de horário no fim de semana e, durante o dia, conta como as coisas estão indo. Você devolve UMA
sugestão: o texto para ele e as operações que mudam o plano. Ele aceita com um toque.

Como pensar (Cal Newport: quando o plano quebra, refaça os blocos do resto do dia, não desista dele):
- Mude o mínimo que resolve. Uma fala sobre a tarde não mexe na quinta sem motivo.
- O "mínimo do dia" de cada dia vem antes de tudo. Se não cabe, diga o que sai, pela ordem de corte.
- Não encha o dia: respeite pausas, almoço, o horário de sair e o tempo livre. Prefira cortar ou
  passar para outro dia a espremer.
- Nada no passado: só mexa em blocos que começam depois de agora (hoje) ou em dias que vêm.
- Tarefa marcada [x] já foi feita: não mova.
- Bloco marcado "imprevistos" é folga de propósito: é o primeiro lugar para pôr uma tarefa movida.
- Bloco marcado "hábito" ou "contínua" não é pendência: não mova nem corte por ele.
- Pedido explícito dele manda. Se faltar informação para decidir, pergunte na resposta e mande ops vazia.

Operações (o bloco é achado pelo dia AAAA-MM-DD e pela hora de início HH:MM, no plano como está):
- {"op":"mover","tarefa":ID,"dia":D,"bloco":HH:MM} leva a tarefa (pelo id) para um bloco de trabalho.
- {"op":"horario","dia":D,"bloco":HH:MM,"i":HH:MM,"f":HH:MM,"para":D2?} muda o horário (e o dia, com para).
- {"op":"novo","dia":D,"i":HH:MM,"f":HH:MM,"k":gov|ppj|ren|pesq|leve|pausa|livre,"nome":"...","tarefas":["texto"]?}
- {"op":"tirar","dia":D,"bloco":HH:MM} só bloco sem tarefas (mova ou corte antes).
- {"op":"cortar","tarefa":ID} a tarefa sai da semana; volta na preparação do fim de semana.
As operações valem em ordem: a segunda já vê o plano mudado pela primeira. Pausa e livre não levam tarefas.

Escrita: português do Brasil, "você", frases curtas, sem jargão, sem emoji. "resposta" diz o que
você sugere e por quê em 2 a 4 frases. "mudancas" tem uma frase por mudança, como
"Roteiro das entrevistas vai para quinta, 14h" ou "Sai: contagem de empresas (primeira da ordem de corte)".
"""
_LINHA_CORTAR = '- {"op":"cortar","tarefa":ID} a tarefa sai da semana; volta na preparação do fim de semana.'


def sistema(semana: dict) -> str:
    """O prompt de sistema: a op `resumo` só entra se a Semana souber aplicá-la."""
    if tem_recurso(semana, "resumo"):
        return SISTEMA.replace(_LINHA_CORTAR, _LINHA_CORTAR + "\n" + SISTEMA_RESUMO)
    return SISTEMA


class SemanaRecusou(Exception):
    """A Semana não aceitou as operações (422): o texto do erro volta ao Claude."""


def _http(metodo: str, caminho: str, key: str, corpo: dict | None = None) -> dict:
    dados = json.dumps(corpo, ensure_ascii=False).encode("utf-8") if corpo is not None else None
    req = urllib.request.Request(BASE + caminho, data=dados, method=metodo, headers={
        "X-Semana-Key": key, "User-Agent": organiza.UA, "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=organiza.LIMITE_S) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 422:
            raise SemanaRecusou(json.loads(e.read().decode("utf-8")).get("erro", "não se aplica")) from e
        raise


def ordem_de_corte(index: Path = INDEX_SEMANA) -> list[str]:
    """A lista "Se estourar, sai nesta ordem" do index.html: a reserva para a API antiga."""
    try:
        html = index.read_text(encoding="utf-8")
    except OSError:
        return []
    m = re.search(r'<ol class="corte">(.*?)</ol>', html, re.S)
    if not m:
        return []
    return [re.sub(r"<[^>]+>", "", li).strip() for li in re.findall(r"<li>(.*?)</li>", m.group(1), re.S)]


def corte_da_semana(semana: dict) -> list[str]:
    """A ordem de corte vem de `semana.corte` em `/api/semana` (a Semana nova tira do plano.js);
    sem ela, do index.html, como antes."""
    corte = (semana.get("semana") or {}).get("corte")
    if isinstance(corte, list) and corte:
        return [str(c).strip() for c in corte if str(c).strip()]
    return ordem_de_corte()


def _dia_nome(iso: str) -> str:
    return NOMES[datetime.fromisoformat(iso).weekday()]


def _hoje(agora: datetime) -> str:
    """O dia lógico de agora (vira às 4h): à 1h30 de quinta ainda é quarta."""
    return dia_logico(agora).isoformat()


def minimo_do_dia(d: dict, checks: dict | None = None) -> str:
    """O mínimo calculado: os `min` de blocos e tarefas que estão no dia depois dos ajustes (uma
    tarefa movida leva o `min` junto). Sem nenhum, a reserva é o `min` de texto do dia."""
    checks = checks or {}
    rotulos = []
    for b in d.get("blocos") or []:
        if b.get("min"):
            rotulos.append(str(b["min"]))
        for t in b.get("tarefas") or []:
            if t.get("min"):
                rotulos.append(str(t["min"]) + (" (feito)" if checks.get(t["id"]) else ""))
    return "; ".join(rotulos) if rotulos else str(d.get("min") or "")


def _marcas_do_bloco(b: dict) -> str:
    marcas = [b["tag"]] if b.get("tag") else []
    marcas += [nome for campo, nome in (("dl", "fixo"), ("opt", "se der"), ("folga", "imprevistos"),
                                         ("hab", "hábito"), ("cont", "contínua")) if b.get(campo)]
    if b.get("min"):
        marcas.append(f"mínimo: {b['min']}")
    return "".join(f" · {m}" for m in marcas)


def plano_em_texto(semana: dict, agora: datetime) -> str:
    """O plano como o Claude lê: dias de hoje em diante inteiros; dos que passaram, só o que ficou."""
    hoje = _hoje(agora)
    checks = semana.get("checks") or {}
    linhas = []
    for d in semana.get("dias", []):
        passado = d["id"] < hoje
        blocos = d["blocos"]
        if passado:  # hábito e contínua nunca são "o que ficou"
            blocos = [b for b in blocos if not b.get("hab") and not b.get("cont")
                      and any(not checks.get(t["id"]) for t in b.get("tarefas") or [])]
            if not blocos:
                continue
        cab = f"## {d['id']} {_dia_nome(d['id'])}" + (" (já passou: só o que ficou sem marcar)" if passado else "")
        linhas.append(cab)
        minimo = "" if passado else minimo_do_dia(d, checks)
        if minimo:
            linhas.append(f"Mínimo do dia: {minimo}")
        for b in blocos:
            linhas.append(f"{b['i']}–{b['f']} [{b['k']}] {b['nome']}{_marcas_do_bloco(b)}")
            for t in b.get("tarefas") or []:
                if passado and checks.get(t["id"]):
                    continue
                marca = f" (mínimo: {t['min']})" if t.get("min") else ""
                linhas.append(f"    - [{'x' if checks.get(t['id']) else ' '}] {t['id']} :: {t['t']}{marca}")
        linhas.append("")
    cortadas = semana.get("cortadas") or []
    if cortadas:
        linhas.append("Já cortado esta semana: " + "; ".join(t["t"] for t in cortadas))
    guardadas = semana.get("guardadas") or []
    if guardadas:
        linhas.append("Guardado para o fim de semana (não é pendência): " + "; ".join(t["t"] for t in guardadas))
    return "\n".join(linhas)


def _conversa_de_hoje(conversa: list[dict], hoje: str, atual: str) -> str:
    linhas = []
    for c in conversa:
        if c["dia"] != hoje or c["id"] == atual or c["estado"] in ("novo", "pensando"):
            continue
        linhas.append(f"- {c['criada'][11:16]} UTC ele: {c['texto'] or '(fechamento sem frase)'}")
        if c.get("resposta"):
            linhas.append(f"  você: {c['resposta']} [{c['estado']}]")
    return "\n".join(linhas) or "(nada ainda)"


def _linha_agora(agora: datetime) -> str:
    relogio = agora.strftime("%Y-%m-%d")
    hoje = _hoje(agora)
    base = f"Agora: {_dia_nome(relogio)} {relogio}, {agora.strftime('%H:%M')} (horário de Brasília)."
    if hoje == relogio:
        return base
    return base + f" O dia dele vira às 4h: para o plano, hoje ainda é {_dia_nome(hoje)} {hoje}."


def _amanha_da_fala(fala: dict, agora: datetime) -> str:
    """O "amanhã" de uma fala antiga, sem `para`: o dia seguinte ao da fala, não ao relógio.
    O Planeja antigo manda em `dia` a data do relógio: num fechamento à 1h30 de quinta, `dia` é
    quinta, mas o dia lógico (vira às 4h) ainda é quarta. Vale o menor dos dois (revisão, B7)."""
    try:
        base = date.fromisoformat(str(fala.get("dia") or ""))
    except ValueError:
        base = dia_logico(agora)
    try:
        criada = datetime.fromisoformat(str(fala.get("criada") or "").replace("Z", "+00:00"))
        if criada.tzinfo is not None:
            base = min(base, dia_logico(criada.astimezone(FUSO)))
    except ValueError:
        pass
    return (base + timedelta(days=1)).isoformat()


def _destino_em_texto(x: dict, fala: dict, agora: datetime) -> str:
    para = x.get("para")
    try:
        para = date.fromisoformat(str(para)).isoformat() if para else None
    except ValueError:
        para = None
    if x.get("destino") == "amanha":
        alvo = para or _amanha_da_fala(fala, agora)
        return f"para {alvo} ({_dia_nome(alvo)}, o próximo dia de trabalho)"
    if para:
        return f"para {para} ({_dia_nome(para)}, o dia que ele escolheu)"
    return "outro dia desta semana (você escolhe)"


def montar_prompt(fala: dict, semana: dict, conversa: list[dict], remarcar: list[dict],
                  corte: list[str], agora: datetime) -> str:
    hoje = _hoje(agora)
    partes = [
        _linha_agora(agora),
        "",
        "# Plano da semana (id da tarefa :: texto; [x] = feita)",
        plano_em_texto(semana, agora),
        "# Ordem de corte (se estourar, sai nesta ordem; é a dele)",
        "\n".join(f"{n}. {c}" for n, c in enumerate(corte, 1)) or "(sem lista)",
        "",
        "# Pedidos de remarcar que ele já fez no celular (para a preparação do fim de semana)",
        "\n".join(f"- {r['texto']}" for r in remarcar) or "(nenhum)",
        "",
        "# Conversa de hoje",
        _conversa_de_hoje(conversa, hoje, fala["id"]),
        "",
    ]
    if fala["tipo"] == "fechamento":
        ctx = fala.get("contexto") or {}
        todas = [x for x in ctx.get("decisoes") or [] if isinstance(x, dict) and x.get("tarefa")]
        decisoes = [x for x in todas if x.get("destino") in ("amanha", "outro")]
        guarda = tem_recurso(semana, "guardar")
        saem = [x["tarefa"] for x in todas if x.get("destino") == "cortar" or (guarda and x.get("destino") == "guardar")]
        partes += [
            "# Fechamento do dia",
            "Ele fechou o dia e decidiu o destino de cada pendência. Estas ele cortou ou guardou para a"
            " semana que vem e já saem antes das suas operações (não use estes ids): "
            + (", ".join(saem) or "nenhuma") + ".",
        ]
        ficam = [x["tarefa"] for x in todas if x.get("destino") == "guardar" and not guarda]
        if ficam:  # API antiga: a Semana não guarda, e cortar seria decidir por ele (revisão, B8)
            partes.append("Estas ele quis guardar para a semana que vem, mas a Semana ainda não sabe guardar:"
                          " ficam onde estão, sem decisão (não use estes ids nem diga que foram para a"
                          " semana que vem): " + ", ".join(ficam) + ".")
        partes += [
            "Ache lugar para estas, no dia pedido, e refaça esse dia e o resto da semana só se precisar:",
            "\n".join(f"- {x['tarefa']} → {_destino_em_texto(x, fala, agora)}" for x in decisoes)
            or "(nenhuma para mover)",
        ]
        primeira = str(ctx.get("primeira") or "").strip()[:300]
        if primeira:
            partes.append(f"Ele quer começar o próximo dia de trabalho por: {primeira}; ponha isso no primeiro"
                          " bloco de trabalho desse dia.")
        if tem_recurso(semana, "resumo"):
            partes.append("Se o próximo dia mudou, reescreva o tema dele com a operação resumo.")
        partes += [
            f"O que ele disse sobre o dia: {fala['texto'] or '(nada)'}",
            "A resposta é o que ele lê amanhã cedo: diga como ficou o amanhã em 2 a 3 frases.",
        ]
    else:
        partes += ["# O que ele contou agora", fala["texto"]]
        ctx = fala.get("contexto") or {}
        if ctx.get("agora"):
            partes.append(f"(No celular, o bloco de agora era: {ctx['agora']})")
    return "\n".join(partes)


def _claude_exe() -> str:
    candidatos = [Path.home() / "scoop/apps/nodejs-lts/current/bin/node_modules/@anthropic-ai/claude-code/bin/claude.exe"]
    achado = shutil.which("claude")
    if achado:
        candidatos.append(Path(achado).parent / "node_modules/@anthropic-ai/claude-code/bin/claude.exe")
    for c in candidatos:
        if c.exists():
            return str(c)
    raise RuntimeError("claude.exe não encontrado")


def rodar_claude(prompt: str, sistema_: str = SISTEMA, schema_: dict | None = None, *,
                 modelo: str = MODELO, limite: int = LIMITE_CLAUDE_S, pasta: Path | None = None,
                 inteiro: bool = False) -> dict:
    """Uma chamada sem tela, sem ferramentas e sem as configurações do usuário. Devolve o JSON.
    A caixa (`logos/caixa.py`, 28/10/2026) passa o modelo pelo id completo, o próprio limite e
    `inteiro=True`, que devolve a saída inteira do CLI (com `modelUsage`, o modelo que respondeu)."""
    pasta = pasta or config.STATE_DIR / "planeja"
    pasta.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [_claude_exe(), "-p", "--model", modelo, "--tools", "", "--setting-sources", "",
         "--strict-mcp-config", "--no-session-persistence", "--output-format", "json",
         "--system-prompt", sistema_, "--json-schema", json.dumps(schema_ or SCHEMA)],
        input=prompt, capture_output=True, text=True, encoding="utf-8", cwd=pasta,
        timeout=limite, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        saida = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"Claude saiu com {r.returncode}: {(r.stderr or r.stdout)[-300:]}") from None
    if saida.get("is_error") or not isinstance(saida.get("structured_output"), dict):
        raise RuntimeError(f"Claude não devolveu a sugestão: {str(saida.get('result'))[:300]}")
    return saida if inteiro else saida["structured_output"]


FRASE_CORTAR = "Sai da semana: "
FRASE_GUARDAR = "Vai para a semana que vem: "


def _ops_de_corte(fala: dict, semana: dict | None = None) -> list[tuple[dict, str]]:
    """As decisões dele que viram operação sem o Claude, com a frase de cada uma. "Próx. semana"
    (`destino: guardar`) vira `guardar` se a Semana souber; na API antiga, nada: a tarefa fica onde
    está, sem decisão. Virar `cortar` com a frase "Vai para a semana que vem" prometia o que a API
    antiga não faz, e o corte não volta na preparação (revisão de 25/10, B8)."""
    ctx = fala.get("contexto") or {}
    guarda = tem_recurso(semana or {}, "guardar")
    saida = []
    for x in ctx.get("decisoes") or []:
        if not isinstance(x, dict) or not x.get("tarefa"):
            continue
        if x.get("destino") == "cortar":
            saida.append(({"op": "cortar", "tarefa": x["tarefa"]}, FRASE_CORTAR))
        elif x.get("destino") == "guardar" and guarda:
            saida.append(({"op": "guardar", "tarefa": x["tarefa"]}, FRASE_GUARDAR))
    return saida


def _limpar(s: dict, permitidas: list[str] | None = None) -> tuple[str, list[str], list[dict]]:
    resposta = str(s.get("resposta") or "").strip()[:1500]
    mudancas = [str(m).strip()[:200] for m in s.get("mudancas") or [] if str(m).strip()][:20]
    ops = [{k: v for k, v in o.items() if v not in (None, "", [])} for o in s.get("ops") or [] if isinstance(o, dict)]
    if permitidas is not None:  # a op que a Semana não sabe aplicar não sai daqui (ex.: resumo na API antiga)
        ops = [o for o in ops if o.get("op") in permitidas]
    for o in ops:
        if o.get("op") == "resumo":
            for campo in ("tema", "min"):
                if campo in o:
                    o[campo] = str(o[campo])[:RESUMO_MAX]
    ops = [o for o in ops if o.get("op") != "resumo" or o.get("tema") or o.get("min")]
    return resposta, mudancas, ops


def atender(key: str, fala: dict, claude=None, agora: datetime | None = None) -> str:
    """Pensa uma fala e devolve o estado final que a Semana registrou."""
    fid = fala["id"]
    _http("PUT", "/api/dia", key, {"id": fid, "estado": "pensando"})
    agora = agora or datetime.now(FUSO)
    semana = _http("GET", "/api/semana", key)
    conversa = _http("GET", "/api/dia", key).get("conversa") or []
    remarcar = _http("GET", "/api/remarcar", key).get("remarcar") or []
    if claude is None:
        sis, esq = sistema(semana), schema(semana)
        claude = lambda p: rodar_claude(p, sis, esq)  # noqa: E731
    cortes = _ops_de_corte(fala, semana) if fala["tipo"] == "fechamento" else []
    prompt = montar_prompt(fala, semana, conversa, remarcar, corte_da_semana(semana), agora)
    erro = None
    for tentativa in range(2):
        p = prompt if erro is None else (
            prompt + f"\n\n# A sugestão anterior não se aplicou\nA página recusou as operações: {erro}\n"
                     "Corrija (ids e horários têm de existir no plano acima) e devolva de novo.")
        resposta, mudancas, ops = _limpar(claude(p), ops_do_claude(semana))
        corpo = {"id": fid, "estado": "pronta", "resposta": resposta,
                 "mudancas": [frase + _texto_da(semana, o["tarefa"]) for o, frase in cortes] + mudancas,
                 "ops": [o for o, _ in cortes] + ops}
        try:
            return _http("PUT", "/api/dia", key, corpo).get("estado", "pronta")
        except SemanaRecusou as e:
            erro = str(e)
            logger.info(f"Dia {fid}: a Semana recusou ({erro}); tentativa {tentativa + 1}")
    _http("PUT", "/api/dia", key, {"id": fid, "estado": "falha",
                                   "resposta": f"Não consegui montar uma sugestão que coubesse no plano ({erro})."})
    return "falha"


def _texto_da(semana: dict, tid: str) -> str:
    for d in semana.get("dias", []):
        for b in d["blocos"]:
            for t in b.get("tarefas") or []:
                if t["id"] == tid:
                    return t["t"]
    return tid


_trabalhando = threading.Lock()


def _atender_todas(key: str, falas: list[dict]) -> None:
    try:
        for f in falas:
            try:
                estado = atender(key, f)
                logger.info(f"Dia {f['id']} ({f['tipo']}): {estado}")
            except Exception as e:  # uma fala com problema não segura as outras
                logger.warning(f"Dia {f.get('id')}: {e}")
                try:
                    _http("PUT", "/api/dia", key, {"id": f["id"], "estado": "falha",
                                                   "resposta": "O PC não conseguiu pensar nisso agora. Tente de novo daqui a pouco."})
                except Exception:
                    pass
    finally:
        _trabalhando.release()


INTERVALO_S = 15


def iniciar(key: str) -> threading.Thread:
    """Laço próprio, mais curto que a volta do drenador (60 s mais WhatsApp e vault): a fala
    é de quem está no meio do dia esperando a resposta."""
    def laco() -> None:
        while True:
            try:
                n = passo(key)
                if n:
                    logger.info(f"Dia do celular: {n} fala(s) para pensar")
            except Exception as e:  # rede fora, Semana fora: tenta de novo na próxima volta
                logger.warning(f"Dia do celular: volta falhou: {e}")
            time.sleep(INTERVALO_S)

    t = threading.Thread(target=laco, name="planeja-espera", daemon=True)
    t.start()
    return t


def passo(key: str) -> int:
    """Uma volta do drenador: se não houver outra em curso, pega as falas novas numa thread."""
    if not _trabalhando.acquire(blocking=False):
        return 0
    try:
        falas = _http("GET", "/api/dia?novas=1", key).get("conversa") or []
    except Exception:
        _trabalhando.release()
        raise
    if not falas:
        _trabalhando.release()
        return 0
    threading.Thread(target=_atender_todas, args=(key, falas), name="planeja-dia", daemon=True).start()
    return len(falas)
