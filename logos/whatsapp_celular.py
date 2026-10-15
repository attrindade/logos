"""A fila do WhatsApp do Assistente no app Organiza, fechada para o celular (decisão dele em 14/10).

Lê a API local do Assistente (`http://127.0.0.1:8765`, só GET: `/api/fila`, `/api/conversa` e
`/api/historico`, que não mudam nada lá), monta a fila com o resumo, até 3 sugestões de resposta e
as últimas mensagens (áudio pela transcrição) de cada conversa, mais as respondidas hoje e as
adiadas, fecha
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
MAX_HISTORICO = 10    # respondidas hoje e adiadas, cada
MAX_MENSAGENS = 25    # por conversa
MAX_TEXTO = 2000      # por mensagem; o envelope inteiro cabe em 1,5 MB na Semana
MAX_ENV = 1_450_000   # abaixo do limite da Semana
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


def _mensagens(conversa: dict) -> list[dict]:
    """As últimas mensagens da conversa, para ele ver o que foi dito antes de responder (pedido
    dele em 14/10). Áudio vai pela transcrição; foto, vídeo e documento só pelo tipo e a legenda,
    sem o arquivo ("os arquivos não precisa ver por ali")."""
    saida = []
    for m in (conversa.get("mensagens") or [])[-MAX_MENSAGENS:]:
        if not isinstance(m, dict):
            continue
        tipo = m.get("tipo") or "texto"
        transcricao = m.get("transcricao") or ""
        texto = transcricao if tipo == "audio" and transcricao else (m.get("texto") or "")
        saida.append({
            "de_mim": bool(m.get("de_mim")),
            "autor": m.get("autor") or "",
            "ts": m.get("ts") or 0,
            "tipo": tipo,
            "texto": texto[:MAX_TEXTO],
            "duracao": m.get("duracao_s") or 0,
            # Áudio ainda sem transcrição: o app diz que ela está a caminho em vez de mostrar vazio.
            "transcrito": tipo != "audio" or bool(transcricao),
        })
    return saida


def _conversa(jid: str) -> dict:
    # Erro de rede aqui sobe (OSError): fila pela metade não substitui a boa na Semana.
    return _get("/api/conversa?jid=" + urllib.parse.quote(jid))


def montar() -> dict:
    fila = _get("/api/fila")
    itens = []
    for secao in ("voce", "rapida", "grupo"):
        lista = fila.get(secao) or []
        for it in lista[:MAX_GRUPOS] if secao == "grupo" else lista:
            item = {
                "secao": secao,
                "jid": it.get("jid") or "",
                "nome": it.get("nome") or "",
                "novas": it.get("novas") or 0,
                "desde": it.get("desde") or it.get("ultima_ts") or 0,
                "previa": it.get("previa") or "",
                "numero": _numero(it.get("jid", "")),
            }
            c = _conversa(it["jid"])
            s = c.get("sugestao") if c.get("sugestao_fresca") else None
            if isinstance(s, dict):
                item["resumo"] = s.get("resumo") or ""
                item["sugestoes"] = [{"rotulo": x.get("rotulo", ""), "texto": x.get("texto", "")}
                                     for x in (s.get("sugestoes") or [])[:3] if isinstance(x, dict)]
                item["falta_saber"] = [str(x) for x in (s.get("falta_saber") or [])[:4]]
            item["mensagens"] = _mensagens(c)
            itens.append(item)
    # O que já saiu da fila hoje (respondidas) e o que ele adiou: some da fila ao responder, e sem
    # isto não dava para conferir o que foi dito nem ver a adiada no celular.
    hist = _get("/api/historico")
    vistos = {it.get("jid") for s in ("voce", "rapida", "grupo") for it in (fila.get(s) or [])}
    for secao, chave, campo_ts in (("respondida", "respondidas", "ts"), ("adiada", "adiadas", "ate")):
        n = 0
        for h in sorted(hist.get(chave) or [], key=lambda x: -(x.get(campo_ts) or 0)):
            jid = h.get("jid") or ""
            if not jid or jid in vistos or n >= MAX_HISTORICO:
                continue
            vistos.add(jid)
            n += 1
            itens.append({
                "secao": secao,
                "jid": jid,
                "nome": h.get("nome") or "",
                "novas": 0,
                "desde": h.get(campo_ts) or 0,
                "previa": (h.get("texto") or "")[:200],
                "numero": _numero(jid),
                "mensagens": _mensagens(_conversa(jid)),
            })
    doc = {"itens": itens, "grupos_total": len(fila.get("grupo") or [])}
    doc["pessoas"] = pessoas()
    doc["contatos"] = situacao_contatos(doc["pessoas"])
    return doc


# ---- manter contato (pedido dele em 14/10: "quem eu estou falando, quem eu não estou falando") ----

PESSOAS_S = 600
MAX_PESSOAS = 400
_pessoas_cache: tuple[float, list[dict]] = (0.0, [])


def pessoas() -> list[dict]:
    """Conversas individuais e contatos da agenda que o Assistente acha, para o app escolher quem
    acompanhar: a busca vazia (as recentes) e uma por letra (até 40 cada). Refeita a cada 10 min."""
    global _pessoas_cache
    if time.time() - _pessoas_cache[0] < PESSOAS_S and _pessoas_cache[1]:
        return _pessoas_cache[1]
    vistos: dict[str, dict] = {}
    for termo in [""] + list("abcdefghijklmnopqrstuvwxyz"):
        for x in _get("/api/buscar?q=" + urllib.parse.quote(termo)) or []:
            jid = x.get("jid") or ""
            if not jid or x.get("grupo") or jid.endswith("@g.us") or jid in vistos:
                continue
            vistos[jid] = {"jid": jid, "nome": x.get("nome") or "", "ultima": x.get("ultima") or 0}
    lista = sorted(vistos.values(), key=lambda p: (-(p["ultima"] or 0), p["nome"].lower()))[:MAX_PESSOAS]
    _pessoas_cache = (time.time(), lista)
    return lista


def situacao_contatos(conhecidas: list[dict]) -> list[dict]:
    """Para cada pessoa da lista "manter contato": a última mensagem dele e a última da pessoa. O
    app ordena pelo atraso (dias desde a última dele menos a frequência escolhida)."""
    from . import acoes_celular
    ultima_conv = {p["jid"]: p.get("ultima") or 0 for p in conhecidas}
    saida = []
    for c in acoes_celular.contatos():
        jid = c["jid"]
        minha = dela = 0
        try:
            for m in _conversa(jid).get("mensagens") or []:
                ts = m.get("ts") or 0
                if m.get("de_mim"):
                    minha = max(minha, ts)
                else:
                    dela = max(dela, ts)
        except (OSError, ValueError):
            pass
        if not minha and not dela:
            dela = ultima_conv.get(jid, 0)
        saida.append({"jid": jid, "nome": c.get("nome") or "", "dias": c["dias"],
                      "minha": minha, "dela": dela, "numero": _numero(jid)})
    return saida


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
    if len(json.dumps(env)) > MAX_ENV:
        # Grande demais para a Semana (1,5 MB): menos mensagens por conversa e menos pessoas.
        for it in doc["itens"]:
            it["mensagens"] = it.get("mensagens", [])[-8:]
        doc["pessoas"] = doc.get("pessoas", [])[:150]
        env = cofre.selar_para_celular(json.dumps(doc, ensure_ascii=False).encode("utf-8"), ID, pub)
        logger.warning("WhatsApp no celular: fila grande demais, mandada encurtada")
    organiza._pedido("PUT", URL, key, {"chave": "whatsapp", "env": json.dumps(env)})
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(marca, encoding="utf-8")
    return True
