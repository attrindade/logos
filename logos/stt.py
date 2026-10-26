"""Transcrição headless.
Adaptado para suporte multiplataforma (Windows e Linux), download automático ou uso offline.
"""
import logging
import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from faster_whisper import WhisperModel

from . import config

logger = logging.getLogger(__name__)

_model: WhisperModel | None = None
# A varredura inicial (thread principal) e o observer (thread do watchdog) transcrevem ao mesmo
# tempo quando uma gravação longa está na Inbox ao subir. Sem a trava, os dois veem _model None
# e carregam o modelo duas vezes (~1,8 GB cada).
_model_lock = threading.Lock()


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
    with _model_lock:
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


@dataclass(frozen=True)
class Transcription:
    text: str
    duration_s: float


# N5 (rodada de 25/10/2026): frases que o Whisper inventa em pt-BR no silêncio e no ruído (vêm das
# legendas com que ele foi treinado). Um segmento que é só uma delas sai. Comparação sem acento,
# caixa e pontuação.
FRASES_INVENTADAS = (
    "legendas pela comunidade amara.org", "legenda pela comunidade amara.org", "legendas por amara.org",
    "obrigado por assistir", "obrigada por assistir", "obrigado pela atenção", "inscreva-se no canal",
    "inscreva se no canal", "inscreva-se", "não se esqueça de se inscrever no canal", "deixe seu like",
    "legendas em português", "transcrição e legendas", "se inscreva no canal",
    "legenda adriana zanotto", "legendas adriana zanotto",  # medido no ruído sem VAD, 25/10/2026
)
SILENCIO_ALUCINA_S = 2.0
VOCAB_MAX_CARACTERES = 600  # ~200 tokens: o initial_prompt do Whisper corta em 224


def _norm(s: str) -> str:
    import re
    import unicodedata
    s = "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9.]+", " ", s).strip(" .")


_INVENTADAS = {_norm(f) for f in FRASES_INVENTADAS}


def inventada(texto: str) -> bool:
    return _norm(texto) in _INVENTADAS


def vocabulario(vault: Path | None = None, extra: Path | None = None) -> list[str]:
    """N1: os nomes que o Whisper deve ouvir certo: `vocabulario.txt` (à mão, em DATA_ROOT) primeiro,
    depois as notas de `org/people/` e `work/active/` (só o nome do arquivo, nunca o conteúdo)."""
    nomes: list[str] = []
    extra = extra if extra is not None else config.DATA_ROOT / "vocabulario.txt"
    try:
        nomes += [l.strip() for l in extra.read_text(encoding="utf-8").splitlines()
                  if l.strip() and not l.lstrip().startswith("#")]
    except OSError:
        pass
    vault = vault if vault is not None else config.VAULT_DIR
    if vault:
        for pasta in ("org/people", "work/active"):
            try:
                # "Governa Clima — Reuniões Log" → "Governa Clima": o satélite não é nome que se fala
                # (medido: no silêncio, o prompt com "Reuniões Log" fez o Whisper repetir isso).
                nomes += sorted({p.stem.split(" — ")[0].strip() for p in (Path(vault) / pasta).glob("*.md")})
            except OSError:
                continue
    vistos, saida, total = set(), [], 0
    for n in nomes:
        if n.lower() in vistos:
            continue
        if total + len(n) + 2 > VOCAB_MAX_CARACTERES:
            break
        vistos.add(n.lower())
        saida.append(n)
        total += len(n) + 2
    return saida


def opcoes_transcribe() -> dict:
    """Os argumentos a mais da chamada `transcribe`, conforme as chaves (desligadas de fábrica)."""
    op: dict = {}
    if config.WHISPER_ANTI_ALUCINACAO:
        op["hallucination_silence_threshold"] = SILENCIO_ALUCINA_S
        op["word_timestamps"] = True  # o hallucination_silence_threshold só age com ele
    if config.WHISPER_VOCABULARIO:
        nomes = vocabulario()
        if nomes:
            op["initial_prompt"] = "Nomes: " + ", ".join(nomes) + "."
            op["hotwords"] = " ".join(nomes)
    return op


def transcribe_file(input_path: Path) -> Transcription:
    """Transcreve um arquivo de áudio/vídeo e retorna o texto concatenado e a duração."""
    wav_path = extract_audio_to_wav(input_path)
    try:
        model = get_model()
        segments, info = model.transcribe(
            str(wav_path),
            beam_size=5,
            language=config.WHISPER_LANGUAGE,
            vad_filter=config.WHISPER_VAD_FILTER,
            **opcoes_transcribe(),
        )
        if config.WHISPER_ANTI_ALUCINACAO:
            segments = [s for s in segments if not inventada(s.text)]
        text = "".join(segment.text for segment in segments)
        return Transcription(text=text.strip(), duration_s=round(float(info.duration), 2))
    finally:
        try:
            if wav_path.exists():
                os.remove(wav_path)
        except OSError:
            pass

