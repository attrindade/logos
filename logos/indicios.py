"""Coletor de indícios ("parece feito"; spec K §1.3, rodada de 25/10/2026). DESLIGADO de fábrica.

Olha as tarefas de hoje e de ontem ainda sem marca no plano já com os ajustes (`/api/semana`) e
procura indício de que foram feitas: commits e arquivos mudados no repositório do bloco, sessões do
Claude Code nele (ActivityWatch) e mensagem mandada ao contato citado na tarefa (banco do Assistente,
**só leitura**). O que achar vira uma *proposta* (`PUT /api/propostas`): nada é marcado sozinho; só
um toque dele em "Confirmar" grava a marcação (regra de ouro, K §1.1).

Regras (K §1.3):
- força 3: mensagem ao contato da tarefa depois do início do bloco;
- força 2: commit no repositório do bloco durante o bloco ou até 3 h depois; ou ≥ 10 arquivos
  mudados no dia;
- força 1: ≥ 40 min de sessão do Claude Code no repositório. Sozinha não propõe: só reforça;
- hábito e contínua não ganham proposta por aqui; uma proposta por tarefa e fonte; no máximo 12
  abertas;
- a frase de evidência leva só nome de repositório, nome de contato que já está na tarefa,
  contagens e horários (nunca texto de mensagem nem título de janela), até 160 caracteres.

Ligar: `indicios_ligado: true` no `logos_config.yaml` **e** a Semana nova no ar (`recursos` com
"propostas"). Sem os dois, `passo` não faz nada. Nesta rodada ele fica desligado (pergunta 2 do plano).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import time
import unicodedata
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config

logger = logging.getLogger(__name__)

FUSO = ZoneInfo("America/Sao_Paulo")
UA = "Logos-indicios/0.1"  # User-Agent próprio: o urllib padrão leva 403/1010 do Cloudflare (Gotcha)
INTERVALO_S = 30 * 60
JANELA_H = (10, 23)
MAX_ABERTAS = 12
DEPOIS_DO_BLOCO_S = 3 * 3600
MIN_ARQUIVOS = 10
MIN_SESSAO_S = 40 * 60
EVIDENCIA_MAX = 160
WORKSPACE_JSON = Path("C:/Workspace/_cli/workspace.json")
AW = "http://127.0.0.1:5600/api/0"
BUCKET_CLAUDE = "aw-watcher-claude-code_att"
BANCO_ASSISTENTE = Path.home() / "Assistente" / "state" / "assistente.db"
# Tipo do bloco → começo do id do repositório no workspace.json (K §1.3).
REPOS_DO_TIPO = {"gov": ("govclima",), "ren": ("baradv-front",), "pesq": ("pesq-",),
                 "ppj": ("ppj-", "juventude-")}
_IGNORAR = {".git", "node_modules", ".venv", "venv", "__pycache__", ".wrangler", "dist", "build", ".gradle"}
_MAX_ENTRADAS = 20000

_ultima = 0.0


def ligado() -> bool:
    return bool(getattr(config, "INDICIOS_LIGADO", False))


# --------------------------------------------------------------------------- utilidades

def _norm(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))


def _ts(dia: str, hhmm: str) -> float:
    return datetime.fromisoformat(f"{dia}T{hhmm}").replace(tzinfo=FUSO).timestamp()


def _hora(ts: float) -> str:
    d = datetime.fromtimestamp(ts, FUSO)
    return f"{d.hour}h{d.minute:02d}"


def _duracao(s: float) -> str:
    m = int(s // 60)
    return f"{m // 60}h{m % 60:02d}" if m >= 60 else f"{m} min"


# --------------------------------------------------------------------------- o que olhar

def candidatas(semana: dict, hoje: str) -> list[dict]:
    """Tarefas de hoje e de ontem, sem marca, sem proposta aberta, fora de hábito e contínua."""
    ontem = (datetime.fromisoformat(hoje) - timedelta(days=1)).date().isoformat()
    checks = semana.get("checks") or {}
    abertas = {p.get("tarefa") for p in semana.get("propostas") or []}
    saida = []
    for d in semana.get("dias") or []:
        if d.get("id") not in (hoje, ontem):
            continue
        for b in d.get("blocos") or []:
            if b.get("hab") or b.get("cont"):
                continue
            for t in b.get("tarefas") or []:
                if checks.get(t["id"]) or t["id"] in abertas:
                    continue
                saida.append({"tarefa": t["id"], "t": t.get("t", ""), "dia": d["id"], "k": b.get("k", ""),
                              "i": _ts(d["id"], b["i"]), "f": _ts(d["id"], b["f"]), "msg": bool(t.get("msg"))})
    return saida


def repos_do_tipo(k: str, workspace: Path = WORKSPACE_JSON) -> list[tuple[str, Path]]:
    prefixos = REPOS_DO_TIPO.get(k)
    if not prefixos:
        return []
    try:
        dados = json.loads(workspace.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    lista = dados.get("pastas", dados) if isinstance(dados, dict) else dados
    if isinstance(lista, dict):
        lista = next((v for v in lista.values() if isinstance(v, list)), [])
    saida = []
    for p in lista:
        if isinstance(p, dict) and p.get("tipo") == "repo" and str(p.get("id", "")).startswith(prefixos):
            saida.append((p["id"], Path(p["caminho"])))
    return saida


def contatos_da_tarefa(texto: str, pessoas: list[str]) -> list[str]:
    """Os primeiros nomes de `org/people/` que aparecem no texto da tarefa ("Mandar ao Igor …")."""
    palavras = set(re.findall(r"\w+", _norm(texto)))
    vistos = []
    for nome in pessoas:
        primeiro = nome.split()[0]
        if len(primeiro) >= 3 and _norm(primeiro) in palavras and primeiro not in vistos:
            vistos.append(primeiro)
    return vistos


def pessoas_do_vault(vault: Path | None = None) -> list[str]:
    vault = vault or config.VAULT_DIR
    if not vault:
        return []
    try:
        return sorted(p.stem for p in (Path(vault) / "org" / "people").glob("*.md"))
    except OSError:
        return []


# --------------------------------------------------------------------------- as fontes

def commits(repo: Path, desde: float, ate: float) -> list[float]:
    try:
        r = subprocess.run(["git", "-C", str(repo), "log", "--all", "--format=%ct",
                            f"--since={int(desde)}", f"--until={int(ate)}"],
                           capture_output=True, text=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return []
    return sorted(float(x) for x in r.stdout.split() if x.isdigit())


def arquivos_mudados(repo: Path, desde: float, ate: float) -> int:
    n, vistos = 0, 0
    for raiz, pastas, nomes in os.walk(repo):
        pastas[:] = [p for p in pastas if p not in _IGNORAR]
        for nome in nomes:
            vistos += 1
            if vistos > _MAX_ENTRADAS:
                return n
            try:
                m = os.stat(os.path.join(raiz, nome)).st_mtime
            except OSError:
                continue
            if desde <= m < ate:
                n += 1
    return n


def sessao_claude(repo_id: str, desde: float, ate: float, eventos=None) -> float:
    """Segundos de sessão do Claude Code no repositório (bucket do aw-tools), na janela."""
    if eventos is None:
        try:
            q = (f"{AW}/buckets/{BUCKET_CLAUDE}/events?start={datetime.fromtimestamp(desde, FUSO).isoformat()}"
                 f"&end={datetime.fromtimestamp(ate, FUSO).isoformat()}&limit=5000")
            q = q.replace("+", "%2B")
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(q, timeout=5) as r:
                eventos = json.loads(r.read().decode("utf-8"))
        except Exception:
            return 0.0
    return sum(float(e.get("duration") or 0) for e in eventos
               if (e.get("data") or {}).get("repo") == repo_id)


def mensagem_ao_contato(banco: Path, contato: str, desde: float) -> float | None:
    """A hora da primeira mensagem mandada por ele a uma conversa cujo nome tem o contato, desde
    `desde`. Abre o banco do Assistente só para leitura (`mode=ro`) e nunca lê o texto."""
    if not banco.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{banco.as_posix()}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return None
    try:
        alvo = _norm(contato)
        jids = [j for j, nome, apelido in con.execute("SELECT jid, nome, apelido FROM conversas")
                if alvo in re.findall(r"\w+", _norm(f"{nome} {apelido}"))]
        if not jids:
            return None
        marcas = ",".join("?" * len(jids))
        horas = [r[0] for r in con.execute(
            f"SELECT MIN(ts) FROM mensagens WHERE de_mim = 1 AND ts >= ? AND jid IN ({marcas})",
            [int(desde), *jids])] + [r[0] for r in con.execute(
            f"SELECT MIN(ts) FROM enviadas WHERE ts >= ? AND jid IN ({marcas})", [int(desde), *jids])]
        horas = [h for h in horas if h]
        if not horas:
            return None
        h = float(min(horas))
        return h / 1000 if h > 1e11 else h  # o banco guarda em s ou ms conforme a origem
    except sqlite3.Error:
        return None
    finally:
        con.close()


# --------------------------------------------------------------------------- juntar

def avaliar(c: dict, repos: list[tuple[str, Path]], pessoas: list[str], banco: Path, agora: float,
            fontes=None) -> list[dict]:
    """As propostas de uma tarefa: uma por fonte de força ≥ 2, com o reforço de força 1 na frase."""
    f = fontes or {}
    git = f.get("commits", commits)
    arq = f.get("arquivos", arquivos_mudados)
    ses = f.get("sessao", sessao_claude)
    msg = f.get("mensagem", mensagem_ao_contato)
    fim_git = min(agora, c["f"] + DEPOIS_DO_BLOCO_S)
    inicio_dia = _ts(c["dia"], "00:00")
    saida = []
    reforco = []
    for rid, caminho in repos:
        s = ses(rid, c["i"], fim_git)
        if s >= MIN_SESSAO_S:
            reforco.append(f"{_duracao(s)} de sessão do Claude no {rid}")
    for rid, caminho in repos:
        cs = git(caminho, c["i"], fim_git)
        if cs:
            saida.append({"fonte": "git", "forca": 2,
                          "evidencia": f"{len(cs)} commit{'s' if len(cs) > 1 else ''} no {rid} entre "
                                       f"{_hora(cs[0])} e {_hora(cs[-1])}" if len(cs) > 1 else
                                       f"1 commit no {rid} às {_hora(cs[0])}"})
            break
    if not any(p["fonte"] == "git" for p in saida):
        for rid, caminho in repos:
            n = arq(caminho, inicio_dia, min(agora, inicio_dia + 86400))
            if n >= MIN_ARQUIVOS:
                saida.append({"fonte": "arquivos", "forca": 2,
                              "evidencia": f"{n} arquivos mudados no {rid} no dia"})
                break
    for contato in contatos_da_tarefa(c["t"], pessoas):
        h = msg(banco, contato, c["i"])
        if h is not None:
            saida.append({"fonte": "whats", "forca": 3, "evidencia": f"Você mandou mensagem ao {contato} às {_hora(h)}"})
            break
    for p in saida:
        if reforco and p["fonte"] in ("git", "arquivos"):
            p["evidencia"] += " e " + reforco[0]
        p["evidencia"] = p["evidencia"][:EVIDENCIA_MAX]
        p["tarefa"] = c["tarefa"]
    return saida


def coletar(semana: dict, agora: float, workspace: Path = WORKSPACE_JSON, banco: Path = BANCO_ASSISTENTE,
            pessoas: list[str] | None = None, fontes=None, ja: set[str] | None = None) -> list[dict]:
    """O que propor agora (puro, sem rede): respeita as 12 abertas e o que já foi mandado."""
    hoje = datetime.fromtimestamp(agora, FUSO).date().isoformat()
    abertas = len(semana.get("propostas") or [])
    vaga = MAX_ABERTAS - abertas
    if vaga <= 0:
        return []
    pessoas = pessoas_do_vault() if pessoas is None else pessoas
    ja = ja or set()
    saida = []
    for c in candidatas(semana, hoje):
        if c["i"] > agora:
            continue
        for p in avaliar(c, repos_do_tipo(c["k"], workspace), pessoas, banco, agora, fontes):
            if f"{p['fonte']}:{p['tarefa']}" in ja:
                continue
            saida.append(p)
            if len(saida) >= vaga:
                return saida
    return saida


# --------------------------------------------------------------------------- rede

def _estado_path() -> Path:
    return config.STATE_DIR / "indicios.json"


def _http(metodo: str, caminho: str, key: str, corpo: dict | None = None) -> dict:
    from . import organiza
    base = organiza.URL.rsplit("/api/", 1)[0]
    dados = json.dumps(corpo, ensure_ascii=False).encode("utf-8") if corpo is not None else None
    req = urllib.request.Request(base + caminho, data=dados, method=metodo, headers={
        "X-Semana-Key": key, "User-Agent": UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def passo(key: str, agora: float | None = None, http=None) -> int | None:
    """Uma volta do drenador. None se desligado ou se a Semana não tem propostas; senão, quantas
    propostas mandou (0 fora da janela das 10h às 23h ou antes de INTERVALO_S)."""
    global _ultima
    if not ligado():
        return None
    agora = agora or time.time()
    hora = datetime.fromtimestamp(agora, FUSO).hour
    if not (JANELA_H[0] <= hora < JANELA_H[1]) or agora - _ultima < INTERVALO_S:
        return 0
    _ultima = agora
    http = http or _http
    semana = http("GET", "/api/semana", key)
    if "propostas" not in (semana.get("recursos") or []):
        return None
    try:
        ja = set(json.loads(_estado_path().read_text(encoding="utf-8")).get("mandadas", []))
    except (OSError, ValueError):
        ja = set()
    n = 0
    for p in coletar(semana, agora, ja=ja):
        try:
            http("PUT", "/api/propostas", key, p)
        except Exception as e:  # 422 (a tarefa sumiu) ou rede: a próxima volta tenta de novo
            logger.info(f"Indícios: proposta {p['fonte']}:{p['tarefa']} não foi: {e}")
            continue
        ja.add(f"{p['fonte']}:{p['tarefa']}")
        n += 1
    try:
        _estado_path().parent.mkdir(parents=True, exist_ok=True)
        _estado_path().write_text(json.dumps({"mandadas": sorted(ja)[-500:]}), encoding="utf-8")
    except OSError:
        pass
    return n
