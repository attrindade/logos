"""Eventos do watchdog que devem disparar o processamento."""
from unittest.mock import patch

from watchdog.events import DirCreatedEvent, DirModifiedEvent, FileCreatedEvent, FileMovedEvent

from logos import config, telegram
from logos.ledger import Ledger
from logos.watcher import _InboxHandler, _wait_for_bundle, scan_inbox


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


def test_pasta_de_pacote_criada_na_inbox_e_processada(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path)
    bundle = tmp_path / "2026-09-14 15-02-11 tg 7"
    bundle.mkdir()
    (bundle / "7.jpg").write_bytes(b"x")
    telegram.write_manifest(bundle, {"mensagens": [{"message_id": 7, "tipo": "foto", "arquivo": "7.jpg"}]})
    handler = _InboxHandler(Ledger(tmp_path / "ledger.json"))

    with patch("logos.watcher.process_bundle") as proc:
        handler.on_created(DirCreatedEvent(str(bundle)))
        handler.on_modified(DirModifiedEvent(str(bundle)))  # não reprocessa

    proc.assert_called_once()
    assert proc.call_args.args[0] == bundle


def test_pasta_comum_na_inbox_e_ignorada(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path)
    (tmp_path / "fotos").mkdir()
    handler = _InboxHandler(Ledger(tmp_path / "ledger.json"))
    with patch("logos.watcher.process_bundle") as proc, patch("logos.watcher.process_file") as single:
        handler.on_created(DirCreatedEvent(str(tmp_path / "fotos")))
    proc.assert_not_called()
    single.assert_not_called()


def test_pacote_incompleto_nao_e_processado(tmp_path):
    bundle = tmp_path / "2026-09-14 15-02-11 tg 7"
    bundle.mkdir()
    telegram.write_manifest(bundle, {"mensagens": [{"message_id": 7, "tipo": "foto", "arquivo": "7.jpg"}]})
    assert _wait_for_bundle(bundle, timeout_s=0.2) is False
    (bundle / "7.jpg").write_bytes(b"x")
    assert _wait_for_bundle(bundle, timeout_s=0.2) is True


def test_scan_inbox_processa_pacotes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path)
    bundle = tmp_path / "2026-09-14 15-02-11 tg 7"
    bundle.mkdir()
    telegram.write_manifest(bundle, {"mensagens": [{"message_id": 7, "tipo": "texto", "texto": "oi"}]})
    with patch("logos.watcher.process_bundle") as proc:
        scan_inbox(Ledger(tmp_path / "ledger.json"))
    proc.assert_called_once()


def test_temporario_com_ponto_e_ignorado(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path)
    handler = _InboxHandler(Ledger(tmp_path / "ledger.json"))

    with patch("logos.watcher.process_file") as proc:
        handler.on_created(FileCreatedEvent(str(tmp_path / ".2026-09-14 15-02-11 tg.txt.tmp")))

    proc.assert_not_called()
