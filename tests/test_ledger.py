"""Testes de idempotência do ledger."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logos.ledger import Entry, Ledger, State


def check(cond, msg):
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {msg}")
    return cond


def main():
    results = []
    tmpdir = Path(tempfile.mkdtemp())
    ledger_path = tmpdir / "ledger.json"

    ledger = Ledger(ledger_path)
    results.append(check(not ledger.is_processed("a.m4a", 100), "arquivo novo não é 'processado'"))

    ledger.upsert(Entry(filename="a.m4a", size=100, state=State.TRANSCRIBED))
    results.append(check(
        not ledger.is_processed("a.m4a", 100),
        "TRANSCRIBED não conta como concluído (deve retomar para LLM, não pular)",
    ))

    ledger.upsert(Entry(filename="a.m4a", size=100, state=State.ENRICHED))
    results.append(check(ledger.is_processed("a.m4a", 100), "ENRICHED conta como concluído"))

    # Revert do Syncthing reintroduzindo arquivo antigo com mesmo nome+tamanho -> reconhecido
    ledger2 = Ledger(ledger_path)  # simula reload após restart
    results.append(check(ledger2.is_processed("a.m4a", 100), "sobrevive a reload (persistência em disco)"))

    # Mesmo nome, tamanho diferente (arquivo reescrito) -> não é o mesmo, deve reprocessar
    results.append(check(not ledger2.is_processed("a.m4a", 999), "tamanho diferente = arquivo diferente"))

    ledger.upsert(Entry(filename="b.m4a", size=50, state=State.FAILED, error="boom"))
    failed = ledger.failed_entries()
    results.append(check(len(failed) == 1 and failed[0].filename == "b.m4a", "failed_entries() encontra falhas"))

    print()
    total = len(results)
    passed = sum(results)
    print(f"{passed}/{total} testes passaram")
    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    main()

