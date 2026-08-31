"""Entrypoint do Logos: inicia o watcher residente com tratamento robusto de erros."""
import logging
import sys
import traceback
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from logos import config
from logos.watcher import run

if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        config.ensure_directories()
        crash_log = config.LOGS_DIR / "crash.log"
        with open(crash_log, "a", encoding="utf-8") as f:
            f.write(f"\n--- CRASH AT {Path(__file__).name} ---\n")
            traceback.print_exc(file=f)
        sys.exit(1)
