"""Eventos do watchdog que devem disparar o processamento."""
from unittest.mock import patch

from watchdog.events import FileCreatedEvent, FileMovedEvent

from logos import config
from logos.ledger import Ledger
from logos.watcher import _InboxHandler


def test_rename_de_temporario_para_a_inbox_processa_o_destino(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path)
    final = tmp_path / "2026-09-14 15-02-11 tg.txt"
    final.write_text("oi", encoding="utf-8")
    handler = _InboxHandler(Ledger(tmp_path / "ledger.json"))

    with patch("logos.watcher.process_file") as proc:
        handler.on_moved(FileMovedEvent(str(tmp_path / ".2026-09-14 15-02-11 tg.txt.tmp"), str(final)))

    proc.assert_called_once()
    assert proc.call_args.args[0] == final


def test_rename_para_fora_da_inbox_e_ignorado(tmp_path, monkeypatch):
    inbox = tmp_path / "Inbox"
    inbox.mkdir()
    monkeypatch.setattr(config, "INBOX_DIR", inbox)
    handler = _InboxHandler(Ledger(tmp_path / "ledger.json"))

    with patch("logos.watcher.process_file") as proc:
        handler.on_moved(FileMovedEvent(str(inbox / "a.txt"), str(tmp_path / "a.txt")))

    proc.assert_not_called()


def test_temporario_com_ponto_e_ignorado(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path)
    handler = _InboxHandler(Ledger(tmp_path / "ledger.json"))

    with patch("logos.watcher.process_file") as proc:
        handler.on_created(FileCreatedEvent(str(tmp_path / ".2026-09-14 15-02-11 tg.txt.tmp")))

    proc.assert_not_called()
