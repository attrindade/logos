"""Teste de aceite da Fase 6: reinício não reprocessa ENRICHED,
e entradas FAILED são reenfileiradas automaticamente. Totalmente autocontido e isolado."""
import sys
import tempfile
import wave
import struct
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logos import config
from logos.ledger import Entry, Ledger, State
from logos.pipeline import process_file, retry_failed


def check(cond, msg):
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {msg}")
    return cond


def create_dummy_wav(path: Path):
    """Cria um arquivo WAV mínimo válido para não depender de arquivos reais em disco."""
    with wave.open(str(path), "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        # 0.5s de silêncio
        data = struct.pack("<h", 0) * 8000
        f.writeframes(data)


def main():
    results = []
    tmpdir = Path(tempfile.mkdtemp())
    inbox = tmpdir / "Inbox"
    inbox.mkdir()

    config.DATA_ROOT = tmpdir
    config.INBOX_DIR = inbox
    config.TRANSCRIPTS_DIR = tmpdir / "Transcripts"
    config.NOTES_DIR = tmpdir / "Notes"
    config.ARCHIVE_DIR = tmpdir / "Archive"
    config.LLM_MODEL = "qwen2.5:3b-instruct-q4_K_M"

    dst = inbox / "2026-08-23 23-13-03.wav"
    create_dummy_wav(dst)
    size = dst.stat().st_size

    ledger = Ledger(tmpdir / "ledger.json")

    # simula uma falha anterior (ex: Ollama fora do ar durante o processamento original)
    ledger.upsert(Entry(
        filename=dst.name, size=size, state=State.FAILED,
        route="Inbox", recorded_at="2026-08-23T23:13:03", error="simulado: ollama indisponível",
    ))
    results.append(check(not ledger.is_processed(dst.name, size), "FAILED não conta como processado"))

    with patch("logos.pipeline.transcribe_file", return_value="Texto de teste transcrito."), \
         patch("logos.pipeline.enrich", return_value={"titulo": "Teste", "tags": ["teste"], "resumo": "Resumo de teste"}):
        retry_failed(ledger)

    entry = ledger.get(dst.name, size)
    results.append(check(entry is not None and entry.state == State.ENRICHED, "retry_failed() reprocessa e chega a ENRICHED"))
    results.append(check(ledger.is_processed(dst.name, size), "após retry, is_processed() = True"))
    results.append(check(Path(entry.note_path).exists(), "nota foi gerada de fato"))

    # rodar de novo: não deve reprocessar (idempotência pós-sucesso)
    note_mtime_before = Path(entry.note_path).stat().st_mtime
    with patch("logos.pipeline.transcribe_file", return_value="Texto de teste transcrito."), \
         patch("logos.pipeline.enrich", return_value={"titulo": "Teste", "tags": ["teste"], "resumo": "Resumo de teste"}):
        process_file(dst, ledger)
        
    note_mtime_after = Path(entry.note_path).stat().st_mtime
    results.append(check(note_mtime_before == note_mtime_after, "reinício não reprocessa ENRICHED (nota não foi reescrita)"))

    print()
    total = len(results)
    passed = sum(results)
    print(f"{passed}/{total} testes passaram")
    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    main()

