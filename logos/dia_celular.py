"""Assistente do dia do app Planeja: "Como está indo?" e "Fechar o dia".

O celular põe a fala na Semana (`/api/dia`); o drenador busca as novas a cada volta e, numa
thread própria (uma por vez), pensa com o Claude sem tela: o plano da semana já com os ajustes,
as marcações, o mínimo de cada dia, a ordem de corte, os pedidos de remarcar, a conversa de hoje
e a hora de agora. A resposta volta como texto curto, frases de mudança e operações sobre o plano
(`lib/ajustes.mjs` da Semana). Quem confere se as operações se aplicam é a Semana: se ela recusar
(422), o Claude recebe o erro e tenta mais uma vez; depois disso vira falha.

    novo → pensando → pronta (o celular aceita ou não) | aplicada (fechamento) | falha

No fechamento, "cortar" é decisão dele e vira operação aqui, sem passar pelo Claude; o Claude
só acha lugar para o que vai para amanhã ou outro dia e refaz o resto da semana se precisar.

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
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, organiza

logger = logging.getLogger(__name__)

BASE = organiza.URL.rsplit("/api/", 1)[0]
FUSO = ZoneInfo("America/Sao_Paulo")
MODELO = "sonnet"
LIMITE_CLAUDE_S = 300
INDEX_SEMANA = Path("C:/Workspace/repo-att/semana/site/index.html")
NOMES = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]

SCHEMA = {
    "type": "object",
    "properties": {
        "resposta": {"type": "string", "description": "2 a 4 frases para ele, em português"},
        "mudancas": {"type": "array", "items": {"type": "string"}, "description": "uma frase curta por mudança"},
        "ops": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["mover", "horario", "novo", "tirar", "cortar"]},
                    "tarefa": {"type": "string"}, "dia": {"type": "string"}, "bloco": {"type": "string"},
                    "i": {"type": "string"}, "f": {"type": "string"}, "para": {"type": "string"},
                    "k": {"type": "string"}, "nome": {"type": "string"},
                    "tarefas": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["op"],
            },
        },
    },
    "required": ["resposta", "mudancas", "ops"],
}

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
    """A lista "Se estourar, sai nesta ordem" da página da semana (ela vive no index.html)."""
    try:
        html = index.read_text(encoding="utf-8")
    except OSError:
        return []
    m = re.search(r'<ol class="corte">(.*?)</ol>', html, re.S)
    if not m:
        return []
    return [re.sub(r"<[^>]+>", "", li).strip() for li in re.findall(r"<li>(.*?)</li>", m.group(1), re.S)]


def _dia_nome(iso: str) -> str:
    return NOMES[datetime.fromisoformat(iso).weekday()]


def plano_em_texto(semana: dict, agora: datetime) -> str:
    """O plano como o Claude lê: dias de hoje em diante inteiros; dos que passaram, só o que ficou."""
    hoje = agora.strftime("%Y-%m-%d")
    checks = semana.get("checks") or {}
    linhas = []
    for d in semana.get("dias", []):
        passado = d["id"] < hoje
        blocos = d["blocos"]
        if passado:
            blocos = [b for b in blocos if any(not checks.get(t["id"]) for t in b.get("tarefas") or [])]
            if not blocos:
                continue
        cab = f"## {d['id']} {_dia_nome(d['id'])}" + (" (já passou: só o que ficou sem marcar)" if passado else "")
        linhas.append(cab)
        if not passado and d.get("min"):
            linhas.append(f"Mínimo do dia: {d['min']}")
        for b in blocos:
            extra = "".join([f" · {b['tag']}" if b.get("tag") else "", " · se der" if b.get("opt") else ""])
            linhas.append(f"{b['i']}–{b['f']} [{b['k']}] {b['nome']}{extra}")
            for t in b.get("tarefas") or []:
                if passado and checks.get(t["id"]):
                    continue
                linhas.append(f"    - [{'x' if checks.get(t['id']) else ' '}] {t['id']} :: {t['t']}")
        linhas.append("")
    cortadas = semana.get("cortadas") or []
    if cortadas:
        linhas.append("Já cortado esta semana: " + "; ".join(t["t"] for t in cortadas))
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


def montar_prompt(fala: dict, semana: dict, conversa: list[dict], remarcar: list[dict],
                  corte: list[str], agora: datetime) -> str:
    hoje = agora.strftime("%Y-%m-%d")
    partes = [
        f"Agora: {_dia_nome(hoje)} {hoje}, {agora.strftime('%H:%M')} (horário de Brasília).",
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
        decisoes = [x for x in ctx.get("decisoes") or [] if x.get("destino") in ("amanha", "outro")]
        cortadas = [x["tarefa"] for x in ctx.get("decisoes") or [] if x.get("destino") == "cortar"]
        partes += [
            "# Fechamento do dia",
            "Ele fechou o dia e decidiu o destino de cada pendência. Estas ele cortou e já saem antes das"
            " suas operações (não use estes ids): " + (", ".join(cortadas) or "nenhuma") + ".",
            "Ache lugar para estas, no dia pedido, e refaça o amanhã e o resto da semana só se precisar:",
            "\n".join(f"- {x['tarefa']} → {'amanhã' if x['destino'] == 'amanha' else 'outro dia desta semana (você escolhe)'}"
                      for x in decisoes) or "(nenhuma para mover)",
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


def rodar_claude(prompt: str) -> dict:
    """Uma chamada sem tela, sem ferramentas e sem as configurações do usuário. Devolve o JSON."""
    pasta = config.STATE_DIR / "planeja"
    pasta.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [_claude_exe(), "-p", "--model", MODELO, "--tools", "", "--setting-sources", "",
         "--strict-mcp-config", "--no-session-persistence", "--output-format", "json",
         "--system-prompt", SISTEMA, "--json-schema", json.dumps(SCHEMA)],
        input=prompt, capture_output=True, text=True, encoding="utf-8", cwd=pasta,
        timeout=LIMITE_CLAUDE_S, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        saida = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"Claude saiu com {r.returncode}: {(r.stderr or r.stdout)[-300:]}") from None
    if saida.get("is_error") or not isinstance(saida.get("structured_output"), dict):
        raise RuntimeError(f"Claude não devolveu a sugestão: {str(saida.get('result'))[:300]}")
    return saida["structured_output"]


def _ops_de_corte(fala: dict) -> list[dict]:
    ctx = fala.get("contexto") or {}
    return [{"op": "cortar", "tarefa": x["tarefa"]} for x in ctx.get("decisoes") or [] if x.get("destino") == "cortar"]


def _limpar(s: dict) -> tuple[str, list[str], list[dict]]:
    resposta = str(s.get("resposta") or "").strip()[:1500]
    mudancas = [str(m).strip()[:200] for m in s.get("mudancas") or [] if str(m).strip()][:20]
    ops = [{k: v for k, v in o.items() if v not in (None, "", [])} for o in s.get("ops") or [] if isinstance(o, dict)]
    return resposta, mudancas, ops


def atender(key: str, fala: dict, claude=rodar_claude, agora: datetime | None = None) -> str:
    """Pensa uma fala e devolve o estado final que a Semana registrou."""
    fid = fala["id"]
    _http("PUT", "/api/dia", key, {"id": fid, "estado": "pensando"})
    agora = agora or datetime.now(FUSO)
    semana = _http("GET", "/api/semana", key)
    conversa = _http("GET", "/api/dia", key).get("conversa") or []
    remarcar = _http("GET", "/api/remarcar", key).get("remarcar") or []
    cortes = _ops_de_corte(fala) if fala["tipo"] == "fechamento" else []
    prompt = montar_prompt(fala, semana, conversa, remarcar, ordem_de_corte(), agora)
    erro = None
    for tentativa in range(2):
        p = prompt if erro is None else (
            prompt + f"\n\n# A sugestão anterior não se aplicou\nA página recusou as operações: {erro}\n"
                     "Corrija (ids e horários têm de existir no plano acima) e devolva de novo.")
        resposta, mudancas, ops = _limpar(claude(p))
        corpo = {"id": fid, "estado": "pronta", "resposta": resposta,
                 "mudancas": [f"Sai da semana: {_texto_da(semana, o['tarefa'])}" for o in cortes] + mudancas,
                 "ops": cortes + ops}
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
