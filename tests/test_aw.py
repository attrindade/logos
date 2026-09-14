"""Evento de captura no ActivityWatch."""
import time
from datetime import datetime, timezone
from unittest.mock import patch

import requests

from logos import aw, config


def test_servidor_fora_do_ar_nao_lanca_e_e_rapido(monkeypatch):
    monkeypatch.setattr(config, "AW_PORT", 1)
    t0 = time.time()
    ok = aw.post_capture(datetime.now().astimezone(), rota="Inbox", canal="syncthing", tipo="audio",
                         duracao_audio_s=1, processamento_s=2)
    assert ok is False
    assert time.time() - t0 < 5


def test_payload_do_contrato(monkeypatch):
    monkeypatch.setattr(config, "AW_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "AW_PORT", 5600)
    started = datetime(2026, 9, 14, 15, 0, 0).astimezone()

    with patch.object(aw.requests, "post") as post, patch.object(aw.socket, "gethostname", return_value="att"):
        ok = aw.post_capture(started, rota="Inbox", canal="telegram", tipo="texto",
                             duracao_audio_s=0, processamento_s=1.234)

    assert ok is True
    bucket_call, event_call = post.call_args_list
    assert bucket_call.args[0] == "http://127.0.0.1:5600/api/0/buckets/aw-watcher-logos_att"
    assert bucket_call.kwargs["json"]["type"] == "logos.capture"
    assert event_call.args[0].endswith("/aw-watcher-logos_att/events")
    [event] = event_call.kwargs["json"]
    assert datetime.fromisoformat(event["timestamp"]).tzinfo == timezone.utc
    assert event["data"] == {"rota": "Inbox", "canal": "telegram", "tipo": "texto",
                             "duracao_audio_s": 0, "processamento_s": 1.23}
    assert all(c.kwargs["timeout"] == aw.TIMEOUT_S for c in post.call_args_list)


def test_erro_http_vira_false():
    with patch.object(aw.requests, "post") as post:
        post.return_value.raise_for_status.side_effect = requests.HTTPError("500")
        assert aw.post_capture(datetime.now().astimezone(), rota="Inbox", canal="syncthing", tipo="audio",
                               duracao_audio_s=0, processamento_s=0) is False
