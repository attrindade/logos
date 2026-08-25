"""Entrypoint do Logos: inicia o watcher residente."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from logos.watcher import run

if __name__ == "__main__":
    run()

