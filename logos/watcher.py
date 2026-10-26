"""Observa Inbox continuamente, valida estabilidade de escrita, processa e expira Archive.

O watcher NUNCA escreve, move ou apaga nada dentro de Inbox. Só o bot Telegram (thread neste
mesmo processo) entrega arquivos e pacotes novos, sempre por rename de algo já completo."""
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from . import config, grava_drive, sono
from .ledger import Entry, Ledger, State
from .pipeline import process_bundle, process_file, read_manifest, retry_failed
from .status import PipelineStage, tracker
from .telegram import start_bot_thread
from .triage import is_audio_file, is_bundle_name, is_ignored, is_text_file

logger = logging.getLogger(__name__)

TEXT_STABILITY_INTERVAL_S = 1
BUNDLE_READY_TIMEOUT_S = 30

# Rede de segurança para as pastas extras: o Drive File Stream é um sistema de arquivos virtual
# e nada garante que todo evento chegue (nem que a unidade esteja montada na hora em que o
# watcher sobe). A varredura é barata — o ledger descarta o que já foi processado — e é o que
# transforma "perdeu o evento" em "atrasou 10 minutos" em vez de "sumiu em silêncio".
RESCAN_INTERVAL_MIN = 10


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
        except (FileNotFoundError, OSError):
            # O arquivo sumiu em voo (movido, substituído pela sincronização, ou a unidade do
            # Drive saiu do ar). Sem devolver o tracker para ocioso, o status fica preso em
            # "estabilizando" para sempre e o DevPortal mostra um trabalho que não existe.
            tracker.set_idle()
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
        tracker.set_idle()
        return False
    return True


def _wait_for_bundle(path: Path, timeout_s: float | None = None) -> bool:
    """Pacote pronto = manifest legível e todos os arquivos que ele lista presentes. O bot entrega
    por rename (atômico), mas uma cópia entre volumes chega aos poucos."""
    deadline = time.time() + (BUNDLE_READY_TIMEOUT_S if timeout_s is None else timeout_s)
    while True:
        loaded = read_manifest(path)
        if loaded is not None:
            files = [m["arquivo"] for m in loaded[0].get("mensagens", []) if m.get("arquivo")]
            if all((path / f).exists() for f in files):
                return True
        if time.time() >= deadline:
            logger.warning(f"Pacote incompleto, ignorado por ora: {path.name}")
            return False
        time.sleep(0.5)


def expire_archive(ledger: Ledger) -> None:
    """Apaga áudio do Archive com mais de ARCHIVE_RETENTION_DAYS. A nota e o transcrito
    são preservados; a entrada do ledger NUNCA é apagada.

    O original continua na Inbox (pasta só-recebe do Syncthing; este processo nunca mexe nela),
    então a varredura volta a vê-lo depois daqui: é `Ledger.is_processed` aceitar ARCHIVE_EXPIRED
    que impede a nota refeita (laço de 23 a 25/10/2026)."""
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


def is_watched_dir(directory: Path) -> bool:
    return directory in config.inbox_dirs()


def _subscribe_pending(observer, handler, observadas: list[Path]) -> None:
    """Inscreve pasta observada que ainda não existia quando o watcher subiu — o caso é o
    Drive montar depois do boot."""
    for directory in config.inbox_dirs():
        if directory in observadas:
            continue
        try:
            if not directory.exists():
                continue
        except OSError:
            continue
        observer.schedule(handler, str(directory), recursive=False)
        observadas.append(directory)
        logger.info(f"Pasta apareceu e entrou na observação: {directory}")


def accepts(directory: Path, filename: str) -> bool:
    """Numa pasta extra (Drive) só áudio entra; ali convivem PDF, zip, audiobook e atalhos
    .gdoc. Na Inbox própria segue tudo, porque só o gravador e o bot escrevem nela."""
    if is_ignored(filename):
        return False
    if directory == config.INBOX_DIR:
        return True
    return is_audio_file(filename)


def scan_inbox(ledger: Ledger) -> None:
    """Varre as pastas observadas — cobre arquivos e pacotes que chegaram enquanto o watcher
    estava parado, e retoma qualquer TRANSCRIBED pendente de enriquecimento."""
    for directory in config.inbox_dirs():
        if not directory.exists():
            if directory != config.INBOX_DIR:
                logger.warning(f"Pasta extra indisponível, pulando nesta varredura: {directory}")
            continue
        _scan_one(directory, ledger)


def _scan_one(directory: Path, ledger: Ledger) -> None:
    try:
        entries = sorted(directory.iterdir())
    except OSError as e:
        # Unidade do Drive pode sumir no meio da varredura; não derruba o watcher.
        logger.warning(f"Falha ao listar {directory}: {e}")
        return

    for path in entries:
        if is_ignored(path.name):
            continue
        if path.is_dir():
            # Pacote (foto+legenda) só existe na Inbox própria: quem os monta é o bot.
            if directory == config.INBOX_DIR and is_bundle_name(path.name) and _wait_for_bundle(path):
                process_bundle(path, ledger)
                tracker.set_idle()
            continue
        if not path.is_file() or not accepts(directory, path.name):
            continue
        try:
            size = path.stat().st_size
        except (FileNotFoundError, OSError):
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
        if not event.is_directory:  # pasta modificada = arquivo dentro dela; o pacote chega pelo created
            self._handle(event)

    def on_moved(self, event):
        # Syncthing e o bot gravam num temporário e renomeiam: a chegada é um "moved".
        self._handle(event, Path(event.dest_path))

    def _handle(self, event, path: Path | None = None):
        path = path or Path(event.src_path)
        if not is_watched_dir(path.parent):
            return  # renomeado para fora de uma pasta observada
        if event.is_directory:
            # No Windows, uma pasta renomeada para dentro da Inbox chega como DirCreatedEvent.
            if path.parent == config.INBOX_DIR and is_bundle_name(path.name) and _wait_for_bundle(path):
                process_bundle(path, self.ledger)
                tracker.set_idle()
            return
        if not accepts(path.parent, path.name):
            return  # temporário, metadado, ou não-áudio numa pasta extra
        if _wait_for_stable_file(path):
            process_file(path, self.ledger)
            tracker.set_idle()


def _espelhar_grava(ledger: Ledger) -> None:
    """Uma passada do espelho do Grava no Drive, em thread própria (grava_drive.iniciar_passada):
    copiar uma gravação de horas para o G: não pode segurar a transcrição. Nada aqui derruba o watcher.
    O ledger em memória diz ao espelho qual gravação transcrita a fala mandou para o Diário."""
    try:
        grava_drive.iniciar_passada(ledger)
    except Exception:
        logger.exception("Espelho do Grava não começou; tento de novo na próxima varredura.")


def run():
    config.ensure_directories()

    handlers = [logging.FileHandler(config.LOGS_DIR / "watcher.log", encoding="utf-8")]
    # rodando via pythonw.exe (sem console) sys.stdout é None — StreamHandler falharia
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))

    from .telegram import proteger_log

    proteger_log(handlers)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )

    _set_low_priority()

    ledger = Ledger(config.LEDGER_PATH)

    # Observer e bot sobem ANTES da varredura inicial: ela roda nesta thread e pode levar horas
    # (em 2026-10-05 uma gravação de 4h na Inbox deixou o bot desconectado desde o boot). Assim o
    # bot responde na hora e o que ele entrega é processado na thread do watchdog, em paralelo.
    observer = Observer()
    handler = _InboxHandler(ledger)
    observadas = []
    for directory in config.inbox_dirs():
        if not directory.exists():
            # Pasta extra some quando o Drive não está montado. A varredura periódica a
            # reencontra e a inscreve depois; o que não pode é o watcher não subir por isso.
            logger.warning(f"Pasta não encontrada, seguindo sem ela: {directory}")
            continue
        observer.schedule(handler, str(directory), recursive=False)
        observadas.append(directory)
    observer.start()
    logger.info("Observando: " + " | ".join(str(d) for d in observadas))

    # O bot só entrega arquivos e pacotes na Inbox; o observer acima os processa como qualquer chegada.
    stop_bot = threading.Event()
    start_bot_thread(stop_bot)
    # Antes da varredura inicial pelo mesmo motivo do bot: ela pode levar horas.
    _espelhar_grava(ledger)

    logger.info("Varredura inicial da Inbox (cobre arquivos chegados offline e retomadas pendentes)...")
    retry_failed(ledger)
    scan_inbox(ledger)
    expire_archive(ledger)

    tracker.set_idle("Watcher ativo • Monitorando Inbox")

    last_daily_check = datetime.now()
    last_rescan = datetime.now()
    try:
        while True:
            time.sleep(10)
            tracker.heartbeat()
            # Banco novo do relógio: refaz as notas de sono e manda a mensagem da noite, uma vez.
            sono.verificar()
            if datetime.now() - last_rescan >= timedelta(minutes=RESCAN_INTERVAL_MIN):
                _subscribe_pending(observer, handler, observadas)
                _espelhar_grava(ledger)
                scan_inbox(ledger)
                last_rescan = datetime.now()
            if datetime.now() - last_daily_check >= timedelta(days=1):
                expire_archive(ledger)
                last_daily_check = datetime.now()
    except KeyboardInterrupt:
        logger.info("Encerrando watcher...")
        stop_bot.set()
        tracker.set_idle("Watcher parado.")
        observer.stop()
    observer.join()


if __name__ == "__main__":
    run()
