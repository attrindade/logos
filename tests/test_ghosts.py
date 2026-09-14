"""Fantasmas de sincronização parcial: `moov atom not found` não vira FAILED, e falhas de
versões parciais já superadas não são reprocessadas nem sobrevivem à limpeza."""
import json
import logging
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from logos import config
from logos.ledger import Entry, Ledger, State
from logos.pipeline import process_file, retry_failed

REAL_LEDGER = Path(r"C:\Users\andre\Logos\state\ledger.json")
GHOST = "2026-09-01 10-38-03.m4a"
PARTIAL_SIZES = [1582838, 2423205, 3259157, 4101798, 4939936, 5775848, 6613929]
FINAL_SIZE = 7053774

MOOV_ERROR = RuntimeError(
    "ffmpeg falhou em x.m4a: [in#0 @ 000001d84df90c00] moov atom not found\n"
    "Error opening input: Invalid data found when processing input"
)


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "Inbox")
    monkeypatch.setattr(config, "TRANSCRIPTS_DIR", tmp_path / "Transcripts")
    monkeypatch.setattr(config, "NOTES_DIR", tmp_path / "Notes")
    monkeypatch.setattr(config, "ARCHIVE_DIR", tmp_path / "Archive")
    config.INBOX_DIR.mkdir()
    return config.INBOX_DIR


def _ghost_ledger(path: Path) -> Ledger:
    ledger = Ledger(path)
    for size in PARTIAL_SIZES:
        ledger.upsert(Entry(filename=GHOST, size=size, state=State.FAILED, route="Diario", error="moov atom not found"))
    ledger.upsert(Entry(filename=GHOST, size=FINAL_SIZE, state=State.ENRICHED, route="Diario"))
    return ledger


def test_moov_atom_not_found_nao_grava_failed(inbox, tmp_path):
    audio = inbox / "2026-09-14 10-00-00.m4a"
    audio.write_bytes(b"parcial")
    ledger = Ledger(tmp_path / "ledger.json")

    with patch("logos.pipeline.transcribe_file", side_effect=MOOV_ERROR):
        process_file(audio, ledger)

    assert ledger.get(audio.name, audio.stat().st_size) is None
    assert ledger.failed_entries() == []


def test_outro_erro_do_ffmpeg_continua_failed(inbox, tmp_path):
    audio = inbox / "2026-09-14 10-00-00.m4a"
    audio.write_bytes(b"corrompido")
    ledger = Ledger(tmp_path / "ledger.json")

    with patch("logos.pipeline.transcribe_file", side_effect=RuntimeError("ffmpeg falhou: Invalid data")):
        process_file(audio, ledger)

    assert ledger.get(audio.name, audio.stat().st_size).state == State.FAILED


def test_retry_failed_pula_superados_sem_log(inbox, tmp_path, caplog):
    (inbox / GHOST).write_bytes(b"x")
    ledger = _ghost_ledger(tmp_path / "ledger.json")

    with patch("logos.pipeline.process_file") as proc, caplog.at_level(logging.INFO):
        retry_failed(ledger)

    proc.assert_not_called()
    assert "Reprocessando" not in caplog.text


def test_retry_failed_ainda_reprocessa_falha_legitima(inbox, tmp_path):
    (inbox / "b.m4a").write_bytes(b"x")
    ledger = Ledger(tmp_path / "ledger.json")
    ledger.upsert(Entry(filename="b.m4a", size=1, state=State.FAILED))
    ledger.upsert(Entry(filename="b.m4a", size=0, state=State.ENRICHED))  # menor: não supera

    with patch("logos.pipeline.process_file") as proc:
        retry_failed(ledger)

    proc.assert_called_once()


def test_remove_superseded_failures(tmp_path):
    ledger = _ghost_ledger(tmp_path / "ledger.json")
    ledger.upsert(Entry(filename="outro.m4a", size=10, state=State.FAILED))

    removed = ledger.remove_superseded_failures()

    assert sorted(removed) == sorted(Ledger.key(GHOST, s) for s in PARTIAL_SIZES)
    reloaded = json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))
    assert Ledger.key(GHOST, FINAL_SIZE) in reloaded
    assert Ledger.key("outro.m4a", 10) in reloaded
    assert ledger.remove_superseded_failures() == []


@pytest.mark.skipif(not REAL_LEDGER.exists(), reason="ledger real não disponível nesta máquina")
def test_ledger_real_nao_gera_reprocessamento(inbox, tmp_path, caplog):
    copy = tmp_path / "ledger.json"
    shutil.copy2(REAL_LEDGER, copy)
    ledger = Ledger(copy)
    for entry in ledger.failed_entries():
        (inbox / entry.filename).write_bytes(b"x")

    with patch("logos.pipeline.process_file") as proc, caplog.at_level(logging.INFO):
        retry_failed(ledger)

    proc.assert_not_called()
    assert "Reprocessando" not in caplog.text
