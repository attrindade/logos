"""Transcrição headless.
Adaptado para suporte multiplataforma (Windows e Linux), download automático ou uso offline.
"""
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from faster_whisper import WhisperModel

from . import config

logger = logging.getLogger(__name__)

_model: WhisperModel | None = None


def check_ffmpeg() -> bool:
    """Verifica se o ffmpeg está disponível e executável no PATH."""
    try:
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **kwargs,
        )
        return result.returncode == 0
    except FileNotFoundError:
        logger.error("ffmpeg não encontrado no PATH do sistema.")
        return False


def get_model() -> WhisperModel:
    """Carrega o modelo faster-whisper. Se já existir localmente, carrega sem consultar o HF Hub.
    Caso contrário, baixa para config.WHISPER_MODELS_DIR."""
    global _model
    if _model is None:
        models_dir = Path(config.WHISPER_MODELS_DIR)
        models_dir.mkdir(parents=True, exist_ok=True)
        
        # Se houver snapshots/arquivos já baixados no models_dir ou o usuário configurou offline
        has_local_model = any(models_dir.iterdir()) if models_dir.exists() else False
        if has_local_model:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")

        logger.info(f"Carregando modelo Whisper '{config.WHISPER_MODEL_SIZE}' de {models_dir} ...")
        _model = WhisperModel(
            config.WHISPER_MODEL_SIZE,
            device="cpu",
            compute_type=config.WHISPER_COMPUTE_TYPE,
            cpu_threads=config.WHISPER_CPU_THREADS,
            download_root=str(models_dir),
        )
    return _model


def extract_audio_to_wav(input_path: Path) -> Path:
    """Converte qualquer mídia suportada por ffmpeg para WAV 16kHz mono temporário."""
    if not check_ffmpeg():
        raise RuntimeError(
            "ffmpeg não encontrado no PATH. Instale o ffmpeg no sistema antes de prosseguir:\n"
            "  - Windows: winget install Gyan.FFmpeg (ou baixe em ffmpeg.org)\n"
            "  - Linux: sudo apt install ffmpeg (ou equivalente da sua distro)"
        )

    tmp_wav = Path(tempfile.mktemp(suffix=".wav"))
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        str(tmp_wav),
    ]
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou em {input_path}: {result.stderr.decode(errors='replace')}")
    return tmp_wav


def transcribe_file(input_path: Path) -> str:
    """Transcreve um arquivo de áudio/vídeo e retorna o texto concatenado."""
    wav_path = extract_audio_to_wav(input_path)
    try:
        model = get_model()
        segments, info = model.transcribe(
            str(wav_path),
            beam_size=5,
            language=config.WHISPER_LANGUAGE,
            vad_filter=config.WHISPER_VAD_FILTER,
        )
        text = "".join(segment.text for segment in segments)
        return text.strip()
    finally:
        try:
            if wav_path.exists():
                os.remove(wav_path)
        except OSError:
            pass

