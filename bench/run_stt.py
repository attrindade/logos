"""Fase 2: transcreve todos os arquivos válidos da Inbox, grava em Transcripts/ e mede
o wall-clock total."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logos import config
from logos.ledger import Entry, Ledger, State
from logos.stt import transcribe_file
from logos.triage import is_ignored, triage


def main():
    config.TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(config.LEDGER_PATH)

    files = [f for f in sorted(config.INBOX_DIR.iterdir()) if f.is_file() and not is_ignored(f.name)]
    print(f"{len(files)} arquivos a transcrever.\n")

    total_start = time.time()
    total_audio_s = 0.0

    for f in files:
        size = f.stat().st_size
        if ledger.is_processed(f.name, size):
            print(f"[skip] {f.name} (já no ledger)")
            continue

        t = triage(f.name)
        t0 = time.time()
        try:
            text = transcribe_file(f)
        except Exception as e:
            print(f"[FAIL] {f.name}: {e}")
            ledger.upsert(Entry(
                filename=f.name, size=size, state=State.FAILED,
                route=t.route.value, recorded_at=t.recorded_at.isoformat(), error=str(e),
            ))
            continue
        elapsed = time.time() - t0

        transcript_name = f"{t.recorded_at.strftime('%Y-%m-%d_%H-%M-%S')}_{t.route.value}.txt"
        transcript_path = config.TRANSCRIPTS_DIR / transcript_name
        transcript_path.write_text(text, encoding="utf-8")

        ledger.upsert(Entry(
            filename=f.name, size=size, state=State.TRANSCRIBED,
            route=t.route.value, recorded_at=t.recorded_at.isoformat(),
            transcript_path=str(transcript_path),
        ))

        n_words = len(text.split())
        print(f"[OK] {f.name} -> {t.route.value} | {elapsed:.1f}s | {n_words} palavras -> {transcript_path.name}")

    total_elapsed = time.time() - total_start
    print(f"\nTempo total de transcrição: {total_elapsed:.1f}s")


if __name__ == "__main__":
    main()

