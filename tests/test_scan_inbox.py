"""Teste de performance: scan_inbox não deve esperar estabilidade em arquivos já
ENRICHED (ver logos/watcher.py::scan_inbox)."""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logos import config
from logos.ledger import Entry, Ledger, State
from logos.watcher import scan_inbox


def check(cond, msg):
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {msg}")
    return cond


def main():
    results = []
    tmpdir = Path(tempfile.mkdtemp())
    inbox = tmpdir / "Inbox"
    inbox.mkdir()

    f = inbox / "2026-08-23 22-46-52 D.m4a"
    f.write_bytes(b"fake-audio")

    ledger = Ledger(tmpdir / "ledger.json")
    ledger.upsert(Entry(
        filename=f.name, size=f.stat().st_size, state=State.ENRICHED,
        route="Diario", recorded_at="2026-08-23T22:46:52",
    ))

    config.INBOX_DIR = inbox
    config.STABILITY_CHECK_INTERVAL_S = 5
    config.STABILITY_CHECK_COUNT = 2

    t0 = time.time()
    scan_inbox(ledger)
    elapsed = time.time() - t0

    results.append(check(elapsed < 1.0, f"arquivo já ENRICHED não espera estabilidade (levou {elapsed:.2f}s, esperado <1s)"))

    print()
    total = len(results)
    passed = sum(results)
    print(f"{passed}/{total} testes passaram")
    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    main()

