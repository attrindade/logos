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
    """PID vivo rodando main.py. Checar a linha de comando evita confundir com PID reciclado.

    Serviço do sistema nega a linha de comando mas entrega o nome. Em 22/09/2026, depois de
    uma reinicialização, o PID do lock caiu no MpDefenderCoreService.exe e o "na dúvida, não
    roubar" deixou o Logos fora do ar: processo que não é Python não é o Logos."""
    try:
        proc = psutil.Process(pid)
        try:
            return any("main.py" in part for part in proc.cmdline())
        except psutil.AccessDenied:
            return proc.name().lower().startswith("python")
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except psutil.AccessDenied:
        return True  # nem o nome dá para ler: na dúvida, não roubar o lock


def _anterior_ao_boot(path: Path) -> bool:
    """Lock gravado antes de o PC ligar é órfão por definição: desligar não roda o atexit."""
    try:
        return path.stat().st_mtime < psutil.boot_time()
    except OSError:
        return False


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
            if (
                pid is not None
                and pid != os.getpid()
                and not _anterior_ao_boot(path)
                and _is_live_logos(pid)
            ):
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
