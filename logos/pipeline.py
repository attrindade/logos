"""Orquestra as etapas STT (ou texto direto) -> LLM -> Writer para um único arquivo."""
import logging
import time
from datetime import datetime
from pathlib import Path

from . import aw, config
from .ledger import Entry, Ledger, State
from .llm import enrich, short_enrichment
from .status import PipelineStage, tracker
from .stt import transcribe_file
from .triage import is_ignored, is_text_file, triage
from .writer import archive_audio, write_note

logger = logging.getLogger(__name__)


def is_incomplete_file_error(error: Exception) -> bool:
    """MP4/M4A sem o átomo moov: o arquivo ainda não terminou de ser escrito/sincronizado."""
    return "moov atom not found" in str(error)


def _mark_failed(path: Path, size: int, mtime: datetime, ledger: Ledger, stage: str, error: Exception) -> None:
    t_initial = triage(path.name, fallback=mtime)
    ledger.upsert(Entry(
        filename=path.name, size=size, state=State.FAILED,
        route=t_initial.route.value, recorded_at=t_initial.recorded_at.isoformat(), error=str(error),
    ))
    tracker.set_failed(path.name, stage, str(error))


def process_file(path: Path, ledger: Ledger) -> None:
    if is_ignored(path.name):
        return

    try:
        stat = path.stat()
    except FileNotFoundError:
        return  # arquivo sumiu entre a detecção e o processamento
    size = stat.st_size
    # Nome fora do padrão "YYYY-MM-DD HH-MM-SS" (ex.: texto salvo à mão) usa o mtime.
    mtime = datetime.fromtimestamp(stat.st_mtime)

    if ledger.is_processed(path.name, size):
        logger.debug(f"Já processado (ledger): {path.name}")
        return

    start_time = time.time()
    started_at = datetime.now().astimezone()
    fonte_tipo = "texto" if is_text_file(path.name) else "audio"
    duracao_audio_s = 0.0
    existing = ledger.get(path.name, size)

    if existing is not None and existing.state == State.TRANSCRIBED and existing.transcript_path:
        # Retomando após queda entre transcrição e enriquecimento — não retranscreve.
        transcript_path = Path(existing.transcript_path)
        transcript = transcript_path.read_text(encoding="utf-8")
        t = triage(path.name, transcript, fallback=mtime)
        tracker.set_stage(
            PipelineStage.TRIAGING,
            filename=path.name,
            file_size=size,
            route=t.route.value,
            message=f"Retomando: classificado como '{t.route.value}'",
            details=f"Transcrição reaproveitada de {transcript_path.name}",
        )
    else:
        if fonte_tipo == "texto":
            try:
                # utf-8-sig: o Bloco de Notas e alguns apps gravam BOM
                transcript = path.read_text(encoding="utf-8-sig").strip()
            except (OSError, UnicodeDecodeError) as e:
                logger.error(f"Falha ao ler texto de {path.name}: {e}")
                _mark_failed(path, size, mtime, ledger, PipelineStage.TRANSCRIBING, e)
                return
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
                transcription = transcribe_file(path)
            except Exception as e:
                if is_incomplete_file_error(e):
                    # Syncthing entregou uma versão parcial enquanto o gravador ainda escrevia.
                    # Não é falha: quando o tamanho mudar, o watcher chama process_file de novo.
                    logger.info(f"Arquivo incompleto (sincronização parcial), aguardando nova versão: {path.name} ({size} bytes)")
                    tracker.set_idle()
                    return
                logger.error(f"Falha na transcrição de {path.name}: {e}")
                _mark_failed(path, size, mtime, ledger, PipelineStage.TRANSCRIBING, e)
                return
            transcript = transcription.text
            duracao_audio_s = transcription.duration_s

        # Etapa 3: Triagem & Roteamento
        t = triage(path.name, transcript, fallback=mtime)
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

    # Etapa 4: Enriquecimento com IA (LLM) — notas curtas pulam a LLM
    if len(transcript.split()) < config.LLM_MIN_WORDS:
        enrichment = short_enrichment(transcript)
        llm_model = ""
    else:
        tracker.set_stage(
            PipelineStage.ENRICHING,
            filename=path.name,
            file_size=size,
            route=t.route.value,
            message=f"Enriquecendo com IA via Ollama ({config.LLM_MODEL})...",
            details=f"Gerando resumo estruturado, ações e metadados",
        )
        llm_model = config.LLM_MODEL
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
        message="Salvando nota no Obsidian e arquivando original...",
        details=f"Destino: Notes/{t.route.value}",
    )

    note_path = write_note(path.name, t, transcript, enrichment, llm_model, fonte_tipo=fonte_tipo)
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
    aw.post_capture(
        started_at,
        rota=t.route.value,
        canal=t.canal,
        tipo=fonte_tipo,
        duracao_audio_s=duracao_audio_s,
        processamento_s=elapsed,
    )


def retry_failed(ledger: Ledger) -> None:
    """Reenfileira entradas com estado FAILED, exceto as já superadas por uma versão maior
    do mesmo arquivo que chegou a ENRICHED (fantasmas de sincronização parcial)."""
    for entry in ledger.failed_entries():
        if ledger.is_superseded(entry):
            logger.debug(f"Ignorando falha obsoleta ({entry.size} bytes): {entry.filename}")
            continue
        path = config.INBOX_DIR / entry.filename
        if path.exists():
            logger.info(f"Reprocessando arquivo que falhou antes: {entry.filename}")
            process_file(path, ledger)
