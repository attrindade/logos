"""Estado persistente e idempotência do pipeline.

Chave: nome_do_arquivo + tamanho_em_bytes. Isso garante que um "Revert local changes"
do Syncthing, que reintroduz arquivos antigos, seja reconhecido como já processado.

Entradas nunca são apagadas, nem quando o áudio expira do Archive (§2.3) — remover a
entrada permitiria retranscrição e nota duplicada caso o arquivo reapareça na Inbox.
"""
import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


class State(str):
    TRANSCRIBED = "transcribed"
    ENRICHED = "enriched"
    FAILED = "failed"
    ARCHIVE_EXPIRED = "archive_expired"


@dataclass
class Entry:
    filename: str
    size: int
    state: str
    route: str = ""
    recorded_at: str = ""
    transcript_path: str = ""
    note_path: str = ""
    archive_path: str = ""
    error: str = ""
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self):
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._data = {}

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    @staticmethod
    def key(filename: str, size: int) -> str:
        return f"{filename}::{size}"

    def get(self, filename: str, size: int) -> Optional[Entry]:
        raw = self._data.get(self.key(filename, size))
        return Entry(**raw) if raw else None

    def is_processed(self, filename: str, size: int) -> bool:
        """Só ENRICHED conta como totalmente concluído. TRANSCRIBED é intermediário
        (pode ter caído antes da etapa LLM) e deve ser retomado, não pulado."""
        entry = self.get(filename, size)
        return entry is not None and entry.state == State.ENRICHED

    def upsert(self, entry: Entry):
        with self._lock:
            entry.updated_at = datetime.now().isoformat()
            self._data[self.key(entry.filename, entry.size)] = asdict(entry)
            self._save()

    def failed_entries(self) -> list[Entry]:
        return [Entry(**v) for v in self._data.values() if v["state"] == State.FAILED]

    def entries_for_expiry(self, before: datetime) -> list[Entry]:
        result = []
        for v in self._data.values():
            if v["state"] == State.ARCHIVE_EXPIRED or not v.get("archive_path"):
                continue
            if not v.get("recorded_at"):
                continue
            recorded_at = datetime.fromisoformat(v["recorded_at"])
            if recorded_at < before:
                result.append(Entry(**v))
        return result

