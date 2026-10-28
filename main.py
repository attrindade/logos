"""Entrypoint do Logos: inicia o watcher residente com tratamento robusto de erros."""
import argparse
import json
import logging
import shutil
import sys
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

# Só config aqui: importar watcher/pipeline carrega logos.status, que grava o status.json no
# import. Os subcomandos de manutenção não podem tocar no status do watcher em execução.
from logos import config

LOCK_PATH = config.STATE_DIR / "logos.lock"


def limpar_fantasmas() -> int:
    """Remove do ledger as entradas FAILED de sincronizações parciais já superadas."""
    from logos.ledger import Ledger

    if not config.LEDGER_PATH.exists():
        print(f"Ledger não encontrado: {config.LEDGER_PATH}")
        return 1

    backup = config.LEDGER_PATH.with_name(f"ledger.{datetime.now():%Y%m%d-%H%M%S}.bak.json")
    shutil.copy2(config.LEDGER_PATH, backup)

    def states() -> Counter:
        data = json.loads(config.LEDGER_PATH.read_text(encoding="utf-8"))
        return Counter(v["state"] for v in data.values())

    before = states()
    removed = Ledger(config.LEDGER_PATH).remove_superseded_failures()
    after = states()

    print(f"Backup: {backup}")
    print(f"Antes:  {sum(before.values())} entradas {dict(before)}")
    for key in removed:
        print(f"  removida: {key}")
    print(f"Depois: {sum(after.values())} entradas {dict(after)}")
    return 0


def run_watcher() -> int:
    from logos import lock

    config.ensure_directories()
    holder = lock.acquire(LOCK_PATH)
    if holder is not None:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
            handlers=[logging.FileHandler(config.LOGS_DIR / "watcher.log", encoding="utf-8")],
        )
        logging.getLogger(__name__).warning(
            f"Outra instância do Logos já está rodando (PID {holder}, lock {LOCK_PATH}). Saindo."
        )
        return 0

    # Queda nativa ou exceção solta deixam rastro em logs/queda-watcher.log (28/10/2026: o watcher
    # morreu no meio de uma reunião em 27/10 sem uma linha). Só a instância que ficou com a trava.
    from logos.diagnostico import ligar_rastro

    ligar_rastro("watcher")
    from logos.watcher import run

    run()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Logos: watcher de voz e texto para o vault.")
    parser.add_argument(
        "--limpar-fantasmas",
        action="store_true",
        help="remove do ledger falhas de versões parciais já concluídas e sai",
    )
    args = parser.parse_args(argv)

    if args.limpar_fantasmas:
        return limpar_fantasmas()
    return run_watcher()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        config.ensure_directories()
        crash_log = config.LOGS_DIR / "crash.log"
        with open(crash_log, "a", encoding="utf-8") as f:
            f.write(f"\n--- CRASH AT {Path(__file__).name} ---\n")
            traceback.print_exc(file=f)
        sys.exit(1)
