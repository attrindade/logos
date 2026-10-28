"""O manifest de um pacote na Inbox (`<pasta>/manifest.json`): quem o monta (o drenador do app
Organiza e, até 14/10/2026, o bot) e quem o lê (o pipeline). Saiu de `logos/telegram.py` em
28/10/2026 para o drenador não depender do módulo do bot; `logos.telegram` reexporta os nomes."""
from __future__ import annotations

import json
import os
from pathlib import Path

MANIFEST = "manifest.json"


def load_manifest(directory: Path) -> dict | None:
    try:
        data = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_manifest(directory: Path, manifest: dict) -> None:
    """Serialização determinística: o hash do manifest compõe a chave do pacote no ledger."""
    tmp = directory / f".{MANIFEST}.tmp"
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, directory / MANIFEST)
