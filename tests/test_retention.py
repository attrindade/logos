"""Testes de expiração de Archive."""
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logos import config
from logos.ledger import Entry, Ledger, State
from logos.watcher import expire_archive


def check(cond, msg):
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {msg}")
    return cond


def main():
    results = []
    tmpdir = Path(tempfile.mkdtemp())
    archive_dir = tmpdir / "Archive"
    archive_dir.mkdir()

    old_audio = archive_dir / "old.m4a"
    old_audio.write_bytes(b"fake-audio-old")
    recent_audio = archive_dir / "recent.m4a"
    recent_audio.write_bytes(b"fake-audio-recent")

    ledger = Ledger(tmpdir / "ledger.json")

    old_date = (datetime.now() - timedelta(days=90)).isoformat()
    recent_date = (datetime.now() - timedelta(days=5)).isoformat()

    ledger.upsert(Entry(
        filename="old.m4a", size=100, state=State.ENRICHED,
        recorded_at=old_date, archive_path=str(old_audio),
        note_path="fake_note_old.md",
    ))
    ledger.upsert(Entry(
        filename="recent.m4a", size=100, state=State.ENRICHED,
        recorded_at=recent_date, archive_path=str(recent_audio),
        note_path="fake_note_recent.md",
    ))

    config.ARCHIVE_RETENTION_DAYS = 60
    expire_archive(ledger)

    results.append(check(not old_audio.exists(), "áudio com 90 dias é apagado (>60d)"))
    results.append(check(recent_audio.exists(), "áudio com 5 dias é preservado (<60d)"))

    old_entry = ledger.get("old.m4a", 100)
    results.append(check(old_entry.state == State.ARCHIVE_EXPIRED, "estado vira ARCHIVE_EXPIRED"))
    results.append(check(old_entry is not None, "entrada do ledger NUNCA é apagada, mesmo expirada"))
    results.append(check(old_entry.note_path == "fake_note_old.md", "nota permanece referenciada (não é tocada)"))

    recent_entry = ledger.get("recent.m4a", 100)
    results.append(check(recent_entry.state == State.ENRICHED, "entrada recente mantém estado ENRICHED"))

    # rodar de novo não deve falhar (arquivo já apagado)
    expire_archive(ledger)
    results.append(check(True, "segunda varredura não lança exceção sobre arquivo já expirado"))

    print()
    total = len(results)
    passed = sum(results)
    print(f"{passed}/{total} testes passaram")
    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    main()

