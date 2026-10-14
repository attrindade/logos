"""A fila do WhatsApp do Assistente no app Organiza, fechada para o celular (decisão dele em 14/10).

Lê a API local do Assistente (`http://127.0.0.1:8765`, só GET: `/api/fila` e `/api/conversa`, que
não mudam nada lá), monta a fila com o resumo e até 3 sugestões de resposta de cada conversa, fecha
com a chave do celular e põe na Semana (`/api/cifrados`, chave "whatsapp"). O Assistente não muda:
ele continua só com a biblioteca padrão do Python. Responder continua sendo no WhatsApp; o app
copia a sugestão e abre a conversa.

Roda no laço do drenador (`python -m logos.organiza`), a cada volta, e só manda quando a fila mudou.
Assistente fora do ar: nada é mandado, e o que está na Semana fica (com a hora em que foi gerado).
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.parse
import urllib.request

from . import cofre, config, organiza

logger = logging.getLogger(__name__)

ASSISTENTE = "http://127.0.0.1:8765"
URL = organiza.URL.replace("/capturas", "/cifrados")
MAX_GRUPOS = 10
ID = "whatsapp"  # dados associados do envelope: só serve como a fila do WhatsApp


# Chamada local, nunca por proxy do sistema: a fila em claro não sai desta máquina.
_SEM_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(caminho: str) -> dict:
    req = urllib.request.Request(ASSISTENTE + caminho, headers={"User-Agent": organiza.UA})
    with _SEM_PROXY.open(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def _numero(jid: str) -> str | None:
    """Só conversa individual com número de verdade (não grupo, não LID) abre no WhatsApp pelo número."""
    if jid.endswith("@s.whatsapp.net"):
        n = jid.split("@")[0].split(":")[0]
        return n if n.isdigit() else None
    return None


def montar() -> dict:
    fila = _get("/api/fila")
    itens = []
    for secao in ("voce", "rapida", "grupo"):
        lista = fila.get(secao) or []
        for it in lista[:MAX_GRUPOS] if secao == "grupo" else lista:
            item = {
                "secao": secao,
                "nome": it.get("nome") or "",
                "novas": it.get("novas") or 0,
                "desde": it.get("desde") or it.get("ultima_ts") or 0,
                "previa": it.get("previa") or "",
                "numero": _numero(it.get("jid", "")),
            }
            # Erro de rede aqui sobe (OSError): fila pela metade não substitui a boa na Semana.
            c = _get("/api/conversa?jid=" + urllib.parse.quote(it["jid"]))
            s = c.get("sugestao") if c.get("sugestao_fresca") else None
            if isinstance(s, dict):
                item["resumo"] = s.get("resumo") or ""
                item["sugestoes"] = [{"rotulo": x.get("rotulo", ""), "texto": x.get("texto", "")}
                                     for x in (s.get("sugestoes") or [])[:3] if isinstance(x, dict)]
                item["falta_saber"] = [str(x) for x in (s.get("falta_saber") or [])[:4]]
            itens.append(item)
    return {"itens": itens, "grupos_total": len(fila.get("grupo") or [])}


def _ultimo_path():
    return config.STATE_DIR / "organiza" / "whatsapp.hash"


def passo(key: str) -> bool | None:
    """Uma volta: True se mandou, False se nada mudou, None se não deu (sem chave ou sem Assistente)."""
    try:
        doc = montar()  # primeiro o Assistente: fora do ar, nem pergunta à Semana
    except (OSError, ValueError):
        return None  # Assistente fora do ar ou respondeu pela metade
    pub = organiza.chave_celular(key)
    if not pub:
        return None
    # A chave do celular entra na marca: celular novo recebe a fila de novo.
    marca = hashlib.sha256((pub + json.dumps(doc, sort_keys=True, ensure_ascii=False)).encode("utf-8")).hexdigest()
    p = _ultimo_path()
    try:
        if p.read_text(encoding="utf-8").strip() == marca:
            return False
    except OSError:
        pass
    doc["gerado_em"] = int(time.time())
    env = cofre.selar_para_celular(json.dumps(doc, ensure_ascii=False).encode("utf-8"), ID, pub)
    organiza._pedido("PUT", URL, key, {"chave": "whatsapp", "env": json.dumps(env)})
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(marca, encoding="utf-8")
    return True
