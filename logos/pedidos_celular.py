"""Pedidos do celular para rodar no PC um job do vault (app Organiza, aba Vault, "Rodar no PC").

O celular põe o pedido na Semana (`/api/pedidos`, só os jobs da allowlist); o drenador busca os
novos a cada volta e dispara pelo mesmo `jobs.disparar` do `/j` do bot, que lê o lock do vault e
avisa quem está rodando. O que o bot mandaria no chat volta para a Semana como estado e resposta:

    novo → rodando (ou ocupado) → ok / falha

Roda no laço do drenador (`python -m logos.organiza`). Fica de fora do que o bot faz: nada além
da allowlist de `jobs.JOBS`, nunca um nome livre vindo da nuvem.
"""
from __future__ import annotations

import html
import logging
import re
import threading

from . import eventos, jobs, organiza

logger = logging.getLogger(__name__)

URL = organiza.URL.replace("/capturas", "/pedidos")


def _texto(html_bot: str) -> str:
    """A resposta do bot é HTML do Telegram (<b>, <i>, <code>); o celular mostra texto."""
    texto = html.unescape(re.sub(r"<[^>]+>", "", html_bot or ""))
    # "Aviso aqui quando terminar." é do chat do bot; no celular o estado muda sozinho.
    return texto.replace("Aviso aqui quando terminar.", "").strip()[:2000]


def _estado(key: str, pid: str, estado: str, resposta: str) -> None:
    organiza._pedido("PUT", URL, key, {"id": pid, "estado": estado, "resposta": resposta})


def _rodar(key: str, pedido: dict) -> None:
    pid = pedido["id"]
    job = next((j for j in jobs.JOBS if j.chave == pedido.get("job")), None)
    if job is None:
        _estado(key, pid, "falha", "Este job não está na lista do PC.")
        return
    fim = threading.Event()

    def notificar(texto: str) -> None:
        # Chega numa thread do job, minutos depois. Sem ✔️ ou com ⚠️ conta como falha.
        ok = jobs.MARCA_OK in texto and jobs.MARCA_FALHA not in texto
        fim.set()
        eventos.registrar("jobs", f"{job.titulo} (pedido pelo celular): " + ("terminou." if ok else _texto(texto)),
                          "info" if ok else "falha")
        try:
            _estado(key, pid, "ok" if ok else "falha", _texto(texto))
        except Exception as e:
            logger.warning(f"Pedido {pid}: não deu para avisar o fim: {e}")

    # "rodando" antes de disparar: um job que termina num instante não pode ter o "ok" coberto
    # pelo "rodando" que viria depois.
    _estado(key, pid, "rodando", f"{job.titulo}: começando.")
    eventos.registrar("jobs", f"{job.titulo}: pedido pelo celular, começando.")
    try:
        resposta = jobs.disparar(job, notificar=notificar)
    except jobs.JobError as e:
        _estado(key, pid, "falha", str(e))
        return
    if fim.is_set():
        return
    ocupado = resposta.startswith(jobs.MARCA_FALHA)
    _estado(key, pid, "ocupado" if ocupado else "rodando", _texto(resposta))


def passo(key: str) -> int:
    """Uma volta: dispara os pedidos novos (no máximo 5). Devolve quantos pegou."""
    pedidos = organiza._pedido("GET", URL + "?novos=1", key).get("pedidos") or []
    for p in pedidos:
        try:
            _rodar(key, p)
        except Exception as e:  # um pedido com problema não segura os outros
            logger.warning(f"Pedido {p.get('id')}: {e}")
    return len(pedidos)
