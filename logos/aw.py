"""Um evento por captura concluída no ActivityWatch (contrato com a sessão C, aw-tools).

Bucket `aw-watcher-logos_<hostname>`, tipo `logos.capture`, dados
`{rota, canal, tipo, duracao_audio_s, processamento_s}`.

Fala com a API REST por `requests` com timeout curto em vez do `aw-client`: o
`ActivityWatchClient._post` não define timeout, e um servidor que aceita a conexão mas não
responde travaria o pipeline. Se o servidor não responder, a captura segue sem evento.
"""
import logging
import socket
from datetime import datetime, timezone

import requests

from . import config

logger = logging.getLogger(__name__)

EVENT_TYPE = "logos.capture"
TIMEOUT_S = 2


def bucket_id() -> str:
    return f"aw-watcher-logos_{socket.gethostname()}"


def post_capture(
    started_at: datetime,
    *,
    rota: str,
    canal: str,
    tipo: str,
    duracao_audio_s: float,
    processamento_s: float,
) -> bool:
    base = f"http://{config.AW_HOST}:{config.AW_PORT}/api/0/buckets/{bucket_id()}"
    event = {
        "timestamp": started_at.astimezone(timezone.utc).isoformat(),
        "duration": round(processamento_s, 2),
        "data": {
            "rota": rota,
            "canal": canal,
            "tipo": tipo,
            "duracao_audio_s": round(duracao_audio_s, 2),
            "processamento_s": round(processamento_s, 2),
        },
    }
    try:
        # Criar a cada chamada é barato (304 se já existe) e cobre o servidor ter subido depois do Logos.
        requests.post(
            base,
            json={"client": "logos", "type": EVENT_TYPE, "hostname": socket.gethostname()},
            timeout=TIMEOUT_S,
        )
        resp = requests.post(f"{base}/events", json=[event], timeout=TIMEOUT_S)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.debug(f"ActivityWatch indisponível, evento da captura descartado: {e}")
        return False
