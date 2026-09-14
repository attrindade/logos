"""Testes do StatusTracker em tempo real."""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logos.status import PipelineStage, StatusTracker


def check(cond, msg):
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {msg}")
    return cond


def main():
    results = []
    tmpdir = Path(tempfile.mkdtemp())
    status_file = tmpdir / "status.json"

    tracker = StatusTracker(status_file)
    results.append(check(status_file.exists(), "status.json criado na inicialização"))

    # Teste 1: Estado Inicial Idle
    tracker.set_idle("Pronto para novos áudios")
    data = json.loads(status_file.read_text(encoding="utf-8"))
    results.append(check(data["stage"] == PipelineStage.IDLE, "Estágio inicial IDLE"))
    results.append(check(data["progress_pct"] == 0, "Progresso 0% em IDLE"))

    # Teste 2: Estabilizando
    tracker.set_stage(
        PipelineStage.STABILIZING,
        filename="audio1.m4a",
        file_size=1024,
        message="Aguardando sync...",
    )
    data = json.loads(status_file.read_text(encoding="utf-8"))
    results.append(check(data["stage"] == PipelineStage.STABILIZING, "Estágio STABILIZING"))
    results.append(check(data["step"] == 1, "Passo 1"))
    results.append(check(data["filename"] == "audio1.m4a", "Nome do arquivo correto"))

    # Teste 3: Transcrevendo
    tracker.set_stage(
        PipelineStage.TRANSCRIBING,
        message="Transcrevendo com Whisper",
    )
    data = json.loads(status_file.read_text(encoding="utf-8"))
    results.append(check(data["stage"] == PipelineStage.TRANSCRIBING, "Estágio TRANSCRIBING"))
    results.append(check(data["step"] == 2, "Passo 2"))

    # Teste 4: Concluído
    tracker.set_completed(
        filename="audio1.m4a",
        route="Diario",
        note_path="C:/Workspace/notes/Notes/Diario/2026-08-31.md",
        elapsed_s=12.5,
    )
    data = json.loads(status_file.read_text(encoding="utf-8"))
    results.append(check(data["stage"] == PipelineStage.COMPLETED, "Estágio COMPLETED"))
    results.append(check(data["progress_pct"] == 100, "Progresso 100%"))
    results.append(check(data["last_completed"]["route"] == "Diario", "Última rota gravada"))
    results.append(check(data["last_completed"]["elapsed_s"] == 12.5, "Tempo decorrido gravado"))

    # Teste 5: Falha
    tracker.set_failed("audio2.m4a", PipelineStage.TRANSCRIBING, "FFmpeg crash")
    data = json.loads(status_file.read_text(encoding="utf-8"))
    results.append(check(data["stage"] == PipelineStage.FAILED, "Estágio FAILED"))
    results.append(check(data["error"] == "FFmpeg crash", "Mensagem de erro preservada"))

    print()
    total = len(results)
    passed = sum(results)
    print(f"{passed}/{total} testes de status passaram")
    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    main()
