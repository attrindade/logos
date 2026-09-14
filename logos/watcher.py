"""Observa Inbox continuamente, valida estabilidade de escrita, processa e expira Archive
. NUNCA escreve, move ou apaga nada dentro de Inbox."""
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from . import config
from .ledger import Entry, Ledger, State
from .pipeline import process_file, retry_failed
from .status import PipelineStage, tracker
from .triage import is_ignored, is_text_file

logger = logging.getLogger(__name__)

TEXT_STABILITY_INTERVAL_S = 1


def _set_low_priority():
    if not config.PROCESS_PRIORITY_BELOW_NORMAL:
        return
    try:
        if sys.platform == "win32":
            import psutil
            psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            # Em sistemas POSIX/Linux, nice positivo = menor prioridade
            os.nice(10)
        logger.info("Prioridade do processo ajustada para nível reduzido.")
    except Exception as e:
        logger.warning(f"Não foi possível ajustar prioridade do processo: {e}")


def _wait_for_stable_file(path: Path) -> bool:
    """Confirma que o arquivo parou de ser escrito antes de enfileirar.

    Texto chega completo (Syncthing e o bot gravam em temporário e renomeiam) e é pequeno:
    uma confirmação em 1 s basta. Áudio mantém a espera configurada."""
    if is_text_file(path.name):
        interval, count = TEXT_STABILITY_INTERVAL_S, 1
    else:
        interval, count = config.STABILITY_CHECK_INTERVAL_S, config.STABILITY_CHECK_COUNT

    last_size = -1
    stable_reads = 0
    while True:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        if size == last_size:
            stable_reads += 1
        else:
            stable_reads = 0
        last_size = size

        tracker.set_stage(
            PipelineStage.STABILIZING,
            filename=path.name,
            file_size=size,
            message="Sincronizando / validando estabilidade na Inbox...",
            details=f"Verificação {stable_reads}/{count} ({round(size / 1024, 1)} KB)",
        )
        if stable_reads >= count:
            break
        time.sleep(interval)

    try:
        with open(path, "rb"):
            pass
    except OSError:
        return False
    return True


def expire_archive(ledger: Ledger) -> None:
    """Apaga áudio do Archive com mais de ARCHIVE_RETENTION_DAYS. A nota e o transcrito
    são preservados; a entrada do ledger NUNCA é apagada."""
    cutoff = datetime.now() - timedelta(days=config.ARCHIVE_RETENTION_DAYS)
    for entry in ledger.entries_for_expiry(cutoff):
        archive_path = Path(entry.archive_path)
        if archive_path.exists():
            try:
                archive_path.unlink()
                logger.info(f"Áudio expirado (>{config.ARCHIVE_RETENTION_DAYS}d): {archive_path.name}")
            except OSError as e:
                logger.warning(f"Falha ao expirar {archive_path}: {e}")
                continue
        entry.state = State.ARCHIVE_EXPIRED
        ledger.upsert(entry)


def scan_inbox(ledger: Ledger) -> None:
    """Varre a Inbox inteira — cobre arquivos que chegaram enquanto o watcher estava
    parado, e retoma qualquer TRANSCRIBED pendente de enriquecimento."""
    if not config.INBOX_DIR.exists():
        return
        
    for path in sorted(config.INBOX_DIR.iterdir()):
        if not path.is_file() or is_ignored(path.name):
            continue
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            continue
        if ledger.is_processed(path.name, size):
            continue
        if not _wait_for_stable_file(path):
            continue
        process_file(path, ledger)
        tracker.set_idle()


class _InboxHandler(FileSystemEventHandler):
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def on_created(self, event):
        self._handle(event)

    def on_modified(self, event):
        self._handle(event)

    def on_moved(self, event):
        # Syncthing e o bot gravam num temporário e renomeiam: a chegada é um "moved".
        self._handle(event, Path(event.dest_path))

    def _handle(self, event, path: Path | None = None):
        if event.is_directory:
            return
        path = path or Path(event.src_path)
        if path.parent != config.INBOX_DIR:
            return  # renomeado para fora da Inbox
        if is_ignored(path.name):
            return
        if _wait_for_stable_file(path):
            process_file(path, self.ledger)
            tracker.set_idle()


def run():
    config.ensure_directories()

    handlers = [logging.FileHandler(config.LOGS_DIR / "watcher.log", encoding="utf-8")]
    # rodando via pythonw.exe (sem console) sys.stdout é None — StreamHandler falharia
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )

    _set_low_priority()

    ledger = Ledger(config.LEDGER_PATH)

    logger.info("Varredura inicial da Inbox (cobre arquivos chegados offline e retomadas pendentes)...")
    retry_failed(ledger)
    scan_inbox(ledger)
    expire_archive(ledger)

    tracker.set_idle("Watcher ativo • Monitorando Inbox")

    observer = Observer()
    observer.schedule(_InboxHandler(ledger), str(config.INBOX_DIR), recursive=False)
    observer.start()
    logger.info(f"Observando {config.INBOX_DIR} ...")

    last_daily_check = datetime.now()
    try:
        while True:
            time.sleep(10)
            tracker.heartbeat()
            if datetime.now() - last_daily_check >= timedelta(days=1):
                expire_archive(ledger)
                last_daily_check = datetime.now()
    except KeyboardInterrupt:
        logger.info("Encerrando watcher...")
        tracker.set_idle("Watcher parado.")
        observer.stop()
    observer.join()


if __name__ == "__main__":
    run()


