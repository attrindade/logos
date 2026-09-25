"""Etapa 2: texto na Inbox, frontmatter fonte_tipo/canal, notas curtas sem LLM e evento no AW."""
import os
import time
from datetime import datetime
from unittest.mock import patch

import pytest
import yaml

from logos import config
from logos.ledger import Ledger, State
from logos.pipeline import process_file
from logos.stt import Transcription
from logos.watcher import _wait_for_stable_file

LONG_TEXT = " ".join(["palavra"] * 45)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in ("INBOX_DIR", "TRANSCRIPTS_DIR", "NOTES_DIR", "ARCHIVE_DIR"):
        monkeypatch.setattr(config, name, tmp_path / name)
    config.INBOX_DIR.mkdir()
    monkeypatch.setattr(config, "LLM_MIN_WORDS", 40)
    return Ledger(tmp_path / "ledger.json")


def _frontmatter(note_path):
    text = note_path.read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---\n")[1])


def _run(path, ledger, enrichment=None):
    with patch("logos.pipeline.transcribe_file") as stt, \
         patch("logos.pipeline.enrich", return_value=enrichment or {"titulo": "LLM", "tags": []}) as llm, \
         patch("logos.pipeline.aw.post_capture") as aw:
        process_file(path, ledger)
    return stt, llm, aw


def test_texto_curto_do_telegram_vira_nota_sem_whisper_nem_llm(env):
    path = config.INBOX_DIR / "2026-09-14 15-02-11 tg.txt"
    path.write_text("tarefa urgente testar o bot", encoding="utf-8")

    stt, llm, aw = _run(path, env)

    stt.assert_not_called()
    llm.assert_not_called()
    entry = env.get(path.name, path.stat().st_size)
    assert entry.state == State.ENRICHED
    note = config.NOTES_DIR / "Inbox" / "2026-09-14_15-02-11 - tarefa urgente testar o bot.md"
    assert entry.note_path == str(note)
    fm = _frontmatter(note)
    assert fm["fonte_tipo"] == "texto"
    assert fm["canal"] == "telegram"
    assert fm["categoria"] == "tarefa"
    assert fm["prioridade"] == "alta"
    assert fm["titulo"] == "tarefa urgente testar o bot"
    assert "modelo_stt" not in fm and "modelo_llm" not in fm
    assert "## ✍️ Texto Original" in note.read_text(encoding="utf-8")

    kwargs = aw.call_args.kwargs
    assert kwargs["rota"] == "Inbox" and kwargs["canal"] == "telegram" and kwargs["tipo"] == "texto"
    assert kwargs["duracao_audio_s"] == 0.0


def test_texto_longo_passa_pela_llm(env):
    path = config.INBOX_DIR / "2026-09-14 16-00-00.md"
    path.write_text("planejamento " + LONG_TEXT, encoding="utf-8")

    _, llm, _ = _run(path, env, {"titulo": "Plano", "tags": ["x"], "categoria": "insight"})

    llm.assert_called_once()
    nota = config.NOTES_DIR / "Planejamento" / "Plano — 2026-09-14.md"
    fm = _frontmatter(nota)
    assert fm["modelo_llm"] == config.LLM_MODEL
    assert fm["entradas"] == 1 and fm["para"] == "2026-09-14"
    assert "via gravador" in nota.read_text(encoding="utf-8")


def test_categoria_da_regra_prevalece_sobre_a_llm(env):
    path = config.INBOX_DIR / "2026-09-14 16-10-00.txt"
    path.write_text("ideia " + LONG_TEXT, encoding="utf-8")

    _run(path, env, {"titulo": "Algo", "tags": [], "categoria": "insight"})

    fm = _frontmatter(config.NOTES_DIR / "Inbox" / "2026-09-14_16-10-00 - Algo.md")
    assert fm["categoria"] == "ideia"


def test_nome_fora_do_padrao_usa_mtime_e_bom_utf8(env):
    path = config.INBOX_DIR / "nota solta.txt"
    path.write_bytes("﻿reunião com a equipe amanhã".encode("utf-8"))
    ts = datetime(2026, 9, 10, 8, 30, 0).timestamp()
    os.utime(path, (ts, ts))

    _run(path, env)

    note = config.NOTES_DIR / "Inbox" / "2026-09-10_08-30-00 - reunião com a equipe amanhã.md"
    fm = _frontmatter(note)
    assert fm["categoria"] == "reuniao"
    assert "﻿" not in note.read_text(encoding="utf-8")


def test_audio_curto_leva_duracao_ao_aw_e_modelo_stt(env):
    path = config.INBOX_DIR / "2026-09-14 17-00-00.m4a"
    path.write_bytes(b"audio")

    with patch("logos.pipeline.transcribe_file", return_value=Transcription("diário hoje foi bom", 12.3)), \
         patch("logos.pipeline.enrich") as llm, \
         patch("logos.pipeline.aw.post_capture") as aw:
        process_file(path, env)

    llm.assert_not_called()
    nota = config.NOTES_DIR / "Diario" / "Diário — 2026-09-14.md"
    fm = _frontmatter(nota)
    assert fm["tipo"] == "diario" and fm["horas"] == ["17:00"]
    assert "categoria" not in fm
    assert "diário hoje foi bom" in nota.read_text(encoding="utf-8")
    assert aw.call_args.kwargs["duracao_audio_s"] == 12.3


def test_texto_estabiliza_rapido(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STABILITY_CHECK_INTERVAL_S", 5)
    monkeypatch.setattr(config, "STABILITY_CHECK_COUNT", 2)
    path = tmp_path / "2026-09-14 18-00-00.txt"
    path.write_text("oi", encoding="utf-8")

    t0 = time.time()
    assert _wait_for_stable_file(path)
    assert time.time() - t0 < 2
