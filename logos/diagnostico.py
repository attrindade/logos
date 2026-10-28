"""Rastro das quedas e logs com limite (28/10/2026).

Em 27/10, o watcher morreu no meio de uma reunião de 1h43 sem deixar uma linha: o `pythonw` não tem
saída de erro, e nada no código registrava uma exceção solta nem uma queda nativa (ctranslate2). O
vigia religou em 5 min, a transcrição recomeçou do zero e ninguém soube por quê.

- `ligar_rastro("watcher")` liga o `faulthandler` (queda nativa: acesso inválido, abort) e os ganchos
  `sys.excepthook` e `threading.excepthook` (exceção que ninguém pegou, na thread principal ou em
  outra), todos gravando em `logs/queda-<processo>.log`. Cada subida deixa uma linha "subiu", para
  separar uma queda da outra.
- `arquivo_de_log(caminho)`: o log do processo com rotação (MAX_BYTES × BACKUPS) e em UTF-8. Antes,
  o `watcher.log` e o `organiza.log` cresciam sem limite.
"""
from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

MAX_BYTES = 5 * 1024 * 1024  # 5 MB × 3 cópias (números sugeridos em 28/10, sem medida)
BACKUPS = 3

_RASTRO = {"arquivo": None, "processo": ""}


def caminho_do_rastro(processo: str, pasta: Path | None = None) -> Path:
    return (pasta or config.LOGS_DIR) / f"queda-{processo}.log"


def arquivo_do_rastro():
    """O arquivo aberto em que o faulthandler escreve (None antes de `ligar_rastro`)."""
    return _RASTRO["arquivo"]


def _escrever(texto: str) -> None:
    f = _RASTRO["arquivo"]
    if f is None:
        return
    try:
        f.write(texto if texto.endswith("\n") else texto + "\n")
        f.flush()
    except (OSError, ValueError):
        pass


def _carimbo() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _excecao(titulo: str, tipo, valor, tb) -> None:
    corpo = "".join(traceback.format_exception(tipo, valor, tb))
    _escrever(f"--- {_carimbo()} {_RASTRO['processo']}: {titulo} ---\n{corpo}")
    try:
        logger.critical(f"{titulo}: {tipo.__name__}: {valor} (detalhes em {caminho_do_rastro(_RASTRO['processo'])})")
    except Exception:
        pass


def ligar_rastro(processo: str, pasta: Path | None = None) -> Path:
    """Liga o rastro de queda do processo. Chamar uma vez, no começo do `main`. Nunca levanta: sem
    o arquivo, o processo segue como antes (só sem rastro)."""
    caminho = caminho_do_rastro(processo, pasta)
    try:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        f = open(caminho, "a", encoding="utf-8")  # fica aberto: o faulthandler escreve pelo descritor
    except OSError as e:
        logger.warning(f"Rastro de queda não ligado ({caminho}): {e}")
        return caminho
    antigo = _RASTRO["arquivo"]
    _RASTRO.update(arquivo=f, processo=processo)
    if antigo is not None and antigo is not f:
        try:
            antigo.close()
        except OSError:
            pass
    _escrever(f"--- {_carimbo()} {processo} subiu (PID {os.getpid()}) ---")
    faulthandler.enable(file=f, all_threads=True)

    hook_antigo = sys.excepthook

    def gancho(tipo, valor, tb):
        if not issubclass(tipo, KeyboardInterrupt):
            _excecao("exceção solta na thread principal", tipo, valor, tb)
        hook_antigo(tipo, valor, tb)

    thread_antigo = threading.excepthook

    def gancho_thread(args):
        if args.exc_type is not SystemExit:
            nome = args.thread.name if args.thread is not None else "?"
            _excecao(f"exceção solta na thread {nome}", args.exc_type, args.exc_value, args.exc_traceback)
        thread_antigo(args)

    sys.excepthook = gancho
    threading.excepthook = gancho_thread
    return caminho


def arquivo_de_log(caminho: Path) -> logging.Handler:
    """Handler do log do processo: gira em MAX_BYTES, guarda BACKUPS cópias, sempre em UTF-8."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    return RotatingFileHandler(caminho, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")


def mesmo_arquivo(stream, caminho: Path) -> bool:
    """O `stream` (ex.: o stderr redirecionado pelo .vbs) já escreve em `caminho`? Dois escritores no
    mesmo arquivo impedem a rotação no Windows (o rename falha com o arquivo aberto pelo cmd)."""
    try:
        if stream is None or not caminho.exists():
            return False
        a, b = os.fstat(stream.fileno()), os.stat(caminho)
        return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino) and a.st_ino != 0
    except (OSError, ValueError, AttributeError):
        return False
