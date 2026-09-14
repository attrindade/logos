"""Instância única do watcher: lock file com o PID do processo dono.

No Windows, o pythonw.exe do .venv é um lançador que cria o interpretador real como processo
filho com a mesma linha de comando. Só o filho executa este código, então o PID gravado é o
dele; o lançador nunca disputa o lock.
"""
import atexit
import os
from pathlib import Path

import psutil


def _is_live_logos(pid: int) -> bool:
    """PID vivo rodando main.py. Checar a linha de comando evita confundir com PID reciclado."""
    try:
        return any("main.py" in part for part in psutil.Process(pid).cmdline())
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except psutil.AccessDenied:
        return True  # existe e não dá para inspecionar: na dúvida, não roubar o lock


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def acquire(path: Path) -> int | None:
    """Tenta criar o lock. Devolve None se conseguiu, ou o PID da instância viva que o detém."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pid = _read_pid(path)
            if pid is not None and pid != os.getpid() and _is_live_logos(pid):
                return pid
            path.unlink(missing_ok=True)  # lock órfão de uma queda anterior
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        atexit.register(release, path)
        return None
    return _read_pid(path) or -1


def release(path: Path) -> None:
    if _read_pid(path) == os.getpid():
        path.unlink(missing_ok=True)
