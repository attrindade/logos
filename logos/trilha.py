"""A trilha de cada captura do celular até a nota (LOG 9 da onda 4, 28/10/2026).

Em 27/10, dois pacotes falados às 14h34 só chegaram ao PC às 16h08, e não se soube onde ficaram
(o celular sem rede, o PC dormindo, a Semana lenta ou o drenador parado). Este módulo só registra;
nada aqui muda o caminho da captura. Para cada captura entregue, quatro horários:

    celular  `fechada_em` (o "Guardar" no app; `aberta_em` vai junto)
    vista    a 1ª volta do drenador em que ela apareceu em /api/capturas/pendentes. A Semana não
             devolve a hora em que recebeu (`pendentes.js` não manda o `atualizado`), então "vista na
             Semana" é "vista pelo PC na Semana"
    no PC    o pacote entrou na Inbox (`organiza.entregar`)
    nota     o ledger diz que ela virou nota

Só horários e o id curto (8 caracteres), nunca o conteúdo, em `state/organiza/trilha.json`, e uma
linha INFO no `organiza.log` quando a nota fica pronta.

"Com o PC ligado": o drenador marca o começo de cada volta e o fim (antes de dormir 60 s). Se entre
o fim de uma e o começo da outra passaram mais de `PARADO_S`, o PC estava dormindo, desligado ou o
drenador estava fora: o PC conta como ligado só a partir dali (`ligado_desde`). Uma volta longa (o
drenador ocupado ou travado num pedido) não zera a conta: o PC estava ligado. O Vigia (`chk_trilha`)
dá "atenção" quando uma captura passa de `LIMITE_S` entre o celular e o PC, contados só com o PC
ligado.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

LIMITE_S = 30 * 60        # do celular ao PC, com o PC ligado
PARADO_S = 5 * 60         # o intervalo normal entre voltas é 60 s
VOLTA_LONGA_S = 10 * 60   # uma volta (com o vigia e a ida ao celular) acima disto vai para o log
GUARDA_S = 3 * 24 * 3600  # quanto tempo a trilha de uma captura fica no arquivo
ATENCAO_S = 6 * 3600      # sugestão: quanto tempo um atraso já resolvido segue como atenção


def caminho() -> Path:
    return config.STATE_DIR / "organiza" / "trilha.json"


def ler() -> dict:
    try:
        d = json.loads(caminho().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _gravar(d: dict) -> None:
    p = caminho()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def curto(cid: str) -> str:
    return "".join(ch for ch in str(cid or "") if ch.isalnum())[:8]


def _ts(iso) -> float | None:
    """Hora do celular ("2026-10-27T14:34:10", hora local, como em `nome_pacote`) → epoch."""
    try:
        return datetime.fromisoformat(str(iso)[:19]).timestamp()
    except (TypeError, ValueError):
        return None


def _hora(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "?"


def _min(s: float) -> str:
    return f"{max(0, round(s / 60))} min"


def _seguro(f):
    """A trilha nunca derruba o drenador: qualquer erro aqui vira um DEBUG."""
    def g(*a, **k):
        try:
            return f(*a, **k)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Trilha: {f.__name__} falhou: {e}")
            return None
    g.__name__ = f.__name__
    return g


# ---- as voltas do drenador: o PC estava ligado? ----

@_seguro
def volta(agora: float | None = None) -> None:
    """Começo de uma volta (`organiza.passo`)."""
    agora = agora or time.time()
    d = ler()
    v = d.setdefault("voltas", {})
    inicio, fim = float(v.get("inicio") or 0), float(v.get("fim") or 0)
    if inicio:
        parado = agora - (fim if fim >= inicio else inicio)
        if parado > PARADO_S:
            logger.info(f"Organiza: {_min(parado)} sem volta (PC dormindo, desligado ou drenador fora); "
                        f"o PC conta como ligado a partir de agora")
            v["ligado_desde"] = agora
        elif agora - inicio > VOLTA_LONGA_S:
            logger.info(f"Organiza: a volta anterior levou {_min(agora - inicio)} (começou às {_hora(inicio)})")
    if not v.get("ligado_desde"):
        v["ligado_desde"] = agora
    v["inicio"] = agora
    _gravar(d)


@_seguro
def descanso(agora: float | None = None) -> None:
    """Fim de uma volta, antes de dormir (`organiza.main`)."""
    d = ler()
    d.setdefault("voltas", {})["fim"] = agora or time.time()
    _gravar(d)


def ligado_desde(d: dict | None = None) -> float:
    d = ler() if d is None else d
    return float((d.get("voltas") or {}).get("ligado_desde") or 0)


# ---- as capturas ----

@_seguro
def vistas(capturas: list[dict], agora: float | None = None) -> None:
    """As capturas que vieram em /pendentes nesta volta: marca a 1ª vez que cada uma apareceu."""
    agora = agora or time.time()
    d = ler()
    cs = d.setdefault("capturas", {})
    mudou = False
    for c in capturas or []:
        k = curto(c.get("id"))
        if not k:
            continue
        if k not in cs:
            cs[k] = {"aberta": _ts(c.get("aberta_em")),
                     "celular": _ts(c.get("fechada_em")) or _ts(c.get("aberta_em")), "vista": agora}
        # Ainda na lista nesta volta. A que sai da lista sem chegar ao PC (vazia, falha definitiva,
        # Diário que não abre: o `?pular=`) não conta mais como esperando (ver `atrasadas`).
        cs[k]["ultima"] = agora
        mudou = True
    if mudou:
        _podar(d, agora)
        _gravar(d)


@_seguro
def no_pc(cid: str, agora: float | None = None) -> None:
    agora = agora or time.time()
    d = ler()
    t = d.setdefault("capturas", {}).setdefault(curto(cid), {"vista": agora})
    if t.get("no_pc"):
        return
    t["no_pc"] = agora
    t["ligado_desde"] = ligado_desde(d) or agora
    _gravar(d)


@_seguro
def nota(cid: str, agora: float | None = None) -> None:
    """A nota ficou pronta: grava a hora e põe a trilha inteira numa linha do log (só horários)."""
    agora = agora or time.time()
    d = ler()
    t = (d.get("capturas") or {}).get(curto(cid))
    if t is None or t.get("nota"):
        return
    t["nota"] = agora
    _gravar(d)
    cel = t.get("celular")
    total = f"{_min(agora - cel)} do celular à nota" if cel else "sem a hora do celular"
    logger.info(f"Trilha {curto(cid)}: celular {_hora(cel)} → vista {_hora(t.get('vista'))} → "
                f"no PC {_hora(t.get('no_pc'))} → nota {_hora(agora)} ({total}; "
                f"PC ligado desde {_hora(t.get('ligado_desde'))})")


def _podar(d: dict, agora: float) -> None:
    cs = d.get("capturas") or {}
    for k in [k for k, t in cs.items() if agora - float(t.get("vista") or 0) > GUARDA_S]:
        del cs[k]


# ---- para o Vigia ----

def atraso_com_pc_ligado(t: dict, agora: float, ligado_agora: float) -> float | None:
    """Segundos entre o celular e o PC contados só com o PC ligado. Na captura que ainda não chegou
    ao PC (vista na Semana, não baixada), conta até agora."""
    cel = t.get("celular") or t.get("vista")
    if not cel:
        return None
    chegou = t.get("no_pc")
    ref = float(t.get("ligado_desde") or 0) if chegou else ligado_agora
    return float(chegou or agora) - max(float(cel), ref)


def atrasadas(agora: float | None = None, d: dict | None = None) -> list[dict]:
    """As capturas recentes que passaram de LIMITE_S do celular ao PC com o PC ligado: as que ainda
    esperam e as que chegaram nas últimas ATENCAO_S horas."""
    agora = agora or time.time()
    d = ler() if d is None else d
    ligado = ligado_desde(d)
    inicio = float((d.get("voltas") or {}).get("inicio") or 0)
    saida = []
    for k, t in (d.get("capturas") or {}).items():
        if t.get("no_pc") and agora - float(t["no_pc"]) > ATENCAO_S:
            continue
        if not t.get("no_pc") and float(t.get("ultima") or t.get("vista") or 0) < inicio:
            continue  # saiu da lista da Semana sem vir (vazia, falha definitiva, pulada)
        s = atraso_com_pc_ligado(t, agora, ligado)
        if s is not None and s > LIMITE_S:
            saida.append({"id": k, "segundos": s, "chegou": bool(t.get("no_pc")),
                          "celular": t.get("celular"), "vista": t.get("vista"), "no_pc": t.get("no_pc")})
    return sorted(saida, key=lambda x: -x["segundos"])
