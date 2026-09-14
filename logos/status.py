"""Gerenciamento de status em tempo real do pipeline do Logos.

Grava estado atômico em JSON (status.json) para que painéis externos (como o DevPortal)
possam acompanhar o progresso de cada etapa em tempo real.
"""
import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from . import config


class PipelineStage:
    IDLE = "idle"
    STABILIZING = "stabilizing"
    TRANSCRIBING = "transcribing"
    TRIAGING = "triaging"
    ENRICHING = "enriching"
    WRITING = "writing"
    COMPLETED = "completed"
    FAILED = "failed"


STAGE_NAMES = {
    PipelineStage.IDLE: "Ocioso (Monitorando Inbox)",
    PipelineStage.STABILIZING: "Detecção & Estabilização",
    PipelineStage.TRANSCRIBING: "Transcrição de Áudio (Whisper)",
    PipelineStage.TRIAGING: "Triagem & Roteamento",
    PipelineStage.ENRICHING: "Enriquecimento com IA (LLM)",
    PipelineStage.WRITING: "Geração de Nota no Obsidian",
    PipelineStage.COMPLETED: "Concluído",
    PipelineStage.FAILED: "Falha no Processamento",
}

STAGE_STEPS = {
    PipelineStage.IDLE: 0,
    PipelineStage.STABILIZING: 1,
    PipelineStage.TRANSCRIBING: 2,
    PipelineStage.TRIAGING: 3,
    PipelineStage.ENRICHING: 4,
    PipelineStage.WRITING: 5,
    PipelineStage.COMPLETED: 5,
    PipelineStage.FAILED: 0,
}

STAGE_PERCENT = {
    PipelineStage.IDLE: 0,
    PipelineStage.STABILIZING: 10,
    PipelineStage.TRANSCRIBING: 35,
    PipelineStage.TRIAGING: 55,
    PipelineStage.ENRICHING: 80,
    PipelineStage.WRITING: 95,
    PipelineStage.COMPLETED: 100,
    PipelineStage.FAILED: 0,
}


@dataclass
class StatusData:
    stage: str = PipelineStage.IDLE
    step: int = 0
    total_steps: int = 5
    step_name: str = STAGE_NAMES[PipelineStage.IDLE]
    progress_pct: int = 0
    filename: str = ""
    file_size: int = 0
    route: str = ""
    message: str = "Aguardando novos arquivos de áudio..."
    details: str = ""
    started_at: str = ""
    updated_at: str = ""
    last_completed: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    pid: int = 0
    heartbeat: str = ""


class StatusTracker:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or config.STATUS_PATH
        self._lock = threading.Lock()
        self._current = StatusData(
            pid=os.getpid(),
            updated_at=datetime.now().isoformat(),
            heartbeat=datetime.now().isoformat(),
        )
        self._load_last_state()
        self._save()


    def _load_last_state(self):
        """Carrega estado persistido anterior (ex: última nota concluída)."""
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if "last_completed" in data and isinstance(data["last_completed"], dict):
                        self._current.last_completed = data["last_completed"]
            except Exception:
                pass

    def _save(self):
        """Salva atomicamente o estado em disco."""
        with self._lock:
            self._current.updated_at = datetime.now().isoformat()
            self._current.heartbeat = datetime.now().isoformat()
            self._current.pid = os.getpid()

            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(asdict(self._current), f, ensure_ascii=False, indent=2)
                tmp.replace(self.path)
            except Exception:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass

    def set_idle(self, message: str = "Aguardando novos arquivos de áudio..."):
        self._current.stage = PipelineStage.IDLE
        self._current.step = 0
        self._current.step_name = STAGE_NAMES[PipelineStage.IDLE]
        self._current.progress_pct = 0
        self._current.filename = ""
        self._current.file_size = 0
        self._current.route = ""
        self._current.message = message
        self._current.details = ""
        self._current.started_at = ""
        self._current.error = None
        self._save()

    def set_stage(
        self,
        stage: str,
        filename: str = "",
        file_size: int = 0,
        route: str = "",
        message: str = "",
        details: str = "",
        progress_pct: Optional[int] = None,
        started_at: Optional[str] = None,
    ):
        self._current.stage = stage
        self._current.step = STAGE_STEPS.get(stage, 0)
        self._current.step_name = STAGE_NAMES.get(stage, stage)
        self._current.progress_pct = progress_pct if progress_pct is not None else STAGE_PERCENT.get(stage, 0)
        if filename:
            self._current.filename = filename
        if file_size:
            self._current.file_size = file_size
        if route:
            self._current.route = route
        self._current.message = message or STAGE_NAMES.get(stage, "")
        self._current.details = details
        if started_at:
            self._current.started_at = started_at
        elif not self._current.started_at and stage != PipelineStage.IDLE:
            self._current.started_at = datetime.now().isoformat()
        self._current.error = None
        self._save()

    def set_completed(
        self,
        filename: str,
        route: str,
        note_path: str,
        archive_path: str = "",
        elapsed_s: float = 0.0,
    ):
        now_iso = datetime.now().isoformat()
        self._current.last_completed = {
            "filename": filename,
            "route": route,
            "note_path": note_path,
            "archive_path": archive_path,
            "completed_at": now_iso,
            "elapsed_s": round(elapsed_s, 2),
        }
        self._current.stage = PipelineStage.COMPLETED
        self._current.step = 5
        self._current.step_name = STAGE_NAMES[PipelineStage.COMPLETED]
        self._current.progress_pct = 100
        self._current.filename = filename
        self._current.route = route
        self._current.message = f"Processamento concluído em {elapsed_s:.1f}s!"
        self._current.details = f"Nota gerada: {Path(note_path).name}"
        self._current.error = None
        self._save()

    def set_failed(self, filename: str, stage: str, error: str):
        self._current.stage = PipelineStage.FAILED
        self._current.step_name = STAGE_NAMES[PipelineStage.FAILED]
        self._current.progress_pct = 0
        self._current.filename = filename
        self._current.message = f"Falha na etapa '{STAGE_NAMES.get(stage, stage)}'"
        self._current.details = error
        self._current.error = error
        self._save()

    def heartbeat(self):
        """Atualiza carimbo de tempo para sinalizar processo ativo."""
        self._save()

    def get_data(self) -> dict:
        return asdict(self._current)


# Instância global do tracker
tracker = StatusTracker()
