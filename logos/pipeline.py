"""Orquestra as etapas STT -> LLM -> Writer para um único arquivo."""
import logging
import time
from pathlib import Path

from . import config
from .ledger import Entry, Ledger, State
from .llm import enrich
from .status import PipelineStage, tracker
from .stt import transcribe_file
from .triage import is_ignored, triage
from .writer import archive_audio, write_note

logger = logging.getLogger(__name__)


def process_file(path: Path, ledger: Ledger) -> None:
    if is_ignored(path.name):
        return

    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return  # arquivo sumiu entre a detecção e o processamento

    if ledger.is_processed(path.name, size):
        logger.debug(f"Já processado (ledger): {path.name}")
        return

    start_time = time.time()
    existing = ledger.get(path.name, size)

    if existing is not None and existing.state == State.TRANSCRIBED and existing.transcript_path:
        # Retomando após queda entre transcrição e enriquecimento — não retranscreve.
        transcript_path = Path(existing.transcript_path)
        transcript = transcript_path.read_text(encoding="utf-8")
        t = triage(path.name, transcript)
        tracker.set_stage(
            PipelineStage.TRIAGING,
            filename=path.name,
            file_size=size,
            route=t.route.value,
            message=f"Retomando: classificado como '{t.route.value}'",
            details=f"Transcrição reaproveitada de {transcript_path.name}",
        )
    else:
        # Etapa 2: Transcrição STT (Whisper)
        tracker.set_stage(
            PipelineStage.TRANSCRIBING,
            filename=path.name,
            file_size=size,
            message=f"Transcrevendo áudio com Whisper ({config.WHISPER_MODEL_SIZE})...",
            details=f"Arquivo: {path.name} ({round(size / 1024, 1)} KB)",
        )
        try:
            transcript = transcribe_file(path)
        except Exception as e:
            t_initial = triage(path.name)
            logger.error(f"Falha na transcrição de {path.name}: {e}")
            ledger.upsert(Entry(
                filename=path.name, size=size, state=State.FAILED,
                route=t_initial.route.value, recorded_at=t_initial.recorded_at.isoformat(), error=str(e),
            ))
            tracker.set_failed(path.name, PipelineStage.TRANSCRIBING, str(e))
            return

        # Etapa 3: Triagem & Roteamento
        t = triage(path.name, transcript)
        tracker.set_stage(
            PipelineStage.TRIAGING,
            filename=path.name,
            file_size=size,
            route=t.route.value,
            message=f"Triagem concluída: Rota '{t.route.value}'",
            details=f"Data identificada: {t.recorded_at.strftime('%Y-%m-%d %H:%M:%S')}",
        )

        config.TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        transcript_name = f"{t.recorded_at.strftime('%Y-%m-%d_%H-%M-%S')}_{t.route.value}.txt"
        transcript_path = config.TRANSCRIPTS_DIR / transcript_name
        transcript_path.write_text(transcript, encoding="utf-8")

        ledger.upsert(Entry(
            filename=path.name, size=size, state=State.TRANSCRIBED,
            route=t.route.value, recorded_at=t.recorded_at.isoformat(),
            transcript_path=str(transcript_path),
        ))

    # Etapa 4: Enriquecimento com IA (LLM)
    tracker.set_stage(
        PipelineStage.ENRICHING,
        filename=path.name,
        file_size=size,
        route=t.route.value,
        message=f"Enriquecendo com IA via Ollama ({config.LLM_MODEL})...",
        details=f"Gerando resumo estruturado, ações e metadados",
    )

    try:
        enrichment = enrich(transcript, t.route)
    except Exception as e:
        logger.warning(f"Falha na etapa LLM de {path.name}: {e}. Nota será gerada só com o transcrito.")
        enrichment = {"titulo": path.stem, "tags": [], "resumo": ""}

    # Etapa 5: Geração de Nota & Arquivamento
    tracker.set_stage(
        PipelineStage.WRITING,
        filename=path.name,
        file_size=size,
        route=t.route.value,
        message="Salvando nota no Obsidian e arquivando áudio...",
        details=f"Destino: Notes/{t.route.value}",
    )

    note_path = write_note(path.name, t, transcript, enrichment, config.LLM_MODEL)
    archive_path = archive_audio(path, t)

    ledger.upsert(Entry(
        filename=path.name, size=size, state=State.ENRICHED,
        route=t.route.value, recorded_at=t.recorded_at.isoformat(),
        transcript_path=str(transcript_path), note_path=str(note_path),
        archive_path=str(archive_path),
    ))
    logger.info(f"Nota gerada: {note_path}")

    elapsed = time.time() - start_time
    tracker.set_completed(
        filename=path.name,
        route=t.route.value,
        note_path=str(note_path),
        archive_path=str(archive_path),
        elapsed_s=elapsed,
    )


def retry_failed(ledger: Ledger) -> None:
    """Reenfileira entradas com estado FAILED."""
    for entry in ledger.failed_entries():
        path = config.INBOX_DIR / entry.filename
        if path.exists():
            logger.info(f"Reprocessando arquivo que falhou antes: {entry.filename}")
            process_file(path, ledger)


