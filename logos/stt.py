"""Transcrição headless.
Adaptado para suporte multiplataforma (Windows e Linux), download automático ou uso offline.
"""
import json
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


def extract_audio_to_wav(input_path: Path, inicio_s: float = 0.0) -> Path:
    """Converte qualquer mídia suportada por ffmpeg para WAV 16kHz mono temporário. Com `inicio_s`,
    só o trecho dali até o fim (a retomada de uma transcrição que caiu no meio)."""
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
        # depois do -i: o corte é exato na amostra (antes do -i, o ffmpeg pula para o quadro-chave)
        *(["-ss", f"{inicio_s:.3f}"] if inicio_s > 0 else []),
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


# ---------------------------------------------------------------------------------------------
# Transcrição retomável (LOG 7 da onda 4, 28/10/2026)
#
# Em 27/10, a reunião de 1h43 levou 3,9 h: o watcher caiu no meio e a transcrição recomeçou do zero.
# Agora cada segmento que o Whisper devolve vai para `state/stt/<hash do áudio>.jsonl` assim que sai.
# Se o mesmo áudio (mesmo conteúdo, qualquer nome) for transcrito de novo, o PC continua do fim do
# último segmento gravado: corta o áudio dali (`ffmpeg -ss`) e transcreve só o resto, com o mesmo
# modelo e os mesmos parâmetros (o VAD continua valendo; `clip_timestamps` desligaria o VAD). Ao
# terminar, o arquivo parcial é apagado.
#
# Limites conhecidos da emenda: o Whisper perde o contexto do texto anterior no ponto do corte
# (`condition_on_previous_text`), e uma palavra pode sair partida ali. Só um ponto por queda.
# ---------------------------------------------------------------------------------------------

PARCIAL_MIN_S = 60          # áudio mais curto que isto não grava progresso (recomeçar é barato)
PARCIAL_GUARDA_S = 7 * 24 * 3600
FIM_FOLGA_S = 0.5           # se o último segmento já chega ao fim do áudio, não há resto a transcrever
RETOMADA_CONTEXTO = 400     # caracteres do texto já pronto que vão de contexto na retomada (~120 tokens)


def _hash_arquivo(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()[:24]


def _assinatura() -> dict:
    """O que, se mudar, invalida um parcial: o modelo e as chaves que mudam a transcrição. O conteúdo
    do vocabulário não entra (mudaria a cada nota nova do vault), só se ele está ligado."""
    return {"modelo": config.WHISPER_MODEL_SIZE, "compute": config.WHISPER_COMPUTE_TYPE,
            "lingua": config.WHISPER_LANGUAGE, "vad": bool(config.WHISPER_VAD_FILTER),
            "anti_alucinacao": bool(config.WHISPER_ANTI_ALUCINACAO), "vocabulario": bool(config.WHISPER_VOCABULARIO)}


class Parcial:
    """O progresso de uma transcrição: uma linha de cabeçalho e uma linha por segmento."""

    def __init__(self, caminho: Path):
        self.caminho = caminho
        self.duracao: float | None = None
        self.segmentos: list[dict] = []
        self._f = None

    @classmethod
    def de(cls, input_path: Path) -> "Parcial | None":
        try:
            p = cls(config.STATE_DIR / "stt" / f"{_hash_arquivo(Path(input_path))}.jsonl")
        except OSError:
            return None
        p._ler()
        return p

    def _ler(self) -> None:
        try:
            linhas = self.caminho.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        try:
            cab = json.loads(linhas[0]) if linhas else {}
        except ValueError:
            cab = {}
        if cab.get("assinatura") != _assinatura() or not cab.get("duracao_s"):
            self.apagar()  # outro modelo ou outra chave: não se mistura
            return
        self.duracao = float(cab["duracao_s"])
        for linha in linhas[1:]:
            try:
                s = json.loads(linha)
                self.segmentos.append({"inicio": float(s["inicio"]), "fim": float(s["fim"]), "texto": str(s["texto"])})
            except (ValueError, KeyError, TypeError):
                break  # a última linha pela metade (queda no meio da escrita): fica o que veio antes

    @property
    def retomar_de(self) -> float:
        return self.segmentos[-1]["fim"] if self.segmentos and self.duracao else 0.0

    def comecar(self, duracao: float) -> None:
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        if self.retomar_de:
            # Reescreve o que vale (some uma linha pela metade) e continua dali.
            self._f = open(self.caminho, "w", encoding="utf-8")
            self._escrever({"v": 1, "assinatura": _assinatura(), "duracao_s": self.duracao})
            for s in self.segmentos:
                self._escrever(s)
        else:
            self.duracao = duracao
            self._f = open(self.caminho, "w", encoding="utf-8")
            self._escrever({"v": 1, "assinatura": _assinatura(), "duracao_s": duracao})

    def _escrever(self, d: dict) -> None:
        self._f.write(json.dumps(d, ensure_ascii=False) + "\n")
        self._f.flush()
        os.fsync(self._f.fileno())

    def gravar(self, seg: dict) -> None:
        if self._f is not None:
            try:
                self._escrever(seg)
            except OSError as e:
                logger.warning(f"Transcrição: o progresso não foi gravado ({e}); segue sem")
                self.fechar()

    def fechar(self) -> None:
        if self._f is not None:
            try:
                self._f.close()
            except OSError:
                pass
            self._f = None

    def apagar(self) -> None:
        self.fechar()
        try:
            self.caminho.unlink(missing_ok=True)
        except OSError:
            pass


def _limpar_parciais_velhos(agora: float | None = None) -> None:
    """Um parcial de um áudio que nunca mais voltou (apagado, trocado) sai depois de 7 dias."""
    import time
    agora = agora or time.time()
    try:
        for p in (config.STATE_DIR / "stt").glob("*.jsonl"):
            if agora - p.stat().st_mtime > PARCIAL_GUARDA_S:
                p.unlink(missing_ok=True)
    except OSError:
        pass


def transcribe_file(input_path: Path) -> Transcription:
    """Transcreve um arquivo de áudio/vídeo e retorna o texto concatenado e a duração. Grava o
    progresso por segmento e, se o mesmo áudio já tinha começado e caído, continua de onde parou."""
    _limpar_parciais_velhos()
    parcial = Parcial.de(input_path)
    inicio = parcial.retomar_de if parcial else 0.0
    ja = list(parcial.segmentos) if inicio else []
    if inicio:
        logger.info(f"Transcrição de {Path(input_path).name}: retomando de {inicio:.1f} s "
                    f"de {parcial.duracao:.1f} s ({len(ja)} segmentos já prontos)")
    novos: list[dict] = []
    if inicio and inicio >= (parcial.duracao or 0) - FIM_FOLGA_S:
        duracao = float(parcial.duracao)  # caiu depois do último segmento: não há resto
    else:
        wav_path = extract_audio_to_wav(input_path, inicio_s=inicio) if inicio else extract_audio_to_wav(input_path)
        try:
            model = get_model()
            op = opcoes_transcribe()
            if ja and "initial_prompt" not in op:
                # Na retomada, o fim do texto já pronto vai como contexto da primeira janela: é o que o
                # `condition_on_previous_text` (ligado de fábrica) daria sem a queda. Sem isso, medido
                # em 28/10, o resto saía sem pontuação. Com o vocabulário ligado, ele fica no lugar.
                op["initial_prompt"] = "".join(s["texto"] for s in ja)[-RETOMADA_CONTEXTO:].strip()
            segments, info = model.transcribe(
                str(wav_path),
                beam_size=5,
                language=config.WHISPER_LANGUAGE,
                vad_filter=config.WHISPER_VAD_FILTER,
                **op,
            )
            duracao = float(parcial.duracao) if inicio else float(info.duration)
            if parcial is not None and duracao >= PARCIAL_MIN_S:
                try:
                    parcial.comecar(duracao)
                except OSError as e:
                    logger.warning(f"Transcrição: sem o arquivo de progresso ({e}); segue sem")
            try:
                for s in segments:  # o Whisper devolve um por vez: cada um vai para o parcial na hora
                    seg = {"inicio": round(inicio + float(getattr(s, "start", 0.0) or 0.0), 3),
                           "fim": round(inicio + float(getattr(s, "end", 0.0) or 0.0), 3), "texto": s.text}
                    novos.append(seg)
                    if parcial is not None:
                        parcial.gravar(seg)
            finally:
                if parcial is not None:
                    parcial.fechar()
        finally:
            try:
                if wav_path.exists():
                    os.remove(wav_path)
            except OSError:
                pass
    todos = ja + novos
    if config.WHISPER_ANTI_ALUCINACAO:
        todos = [s for s in todos if not inventada(s["texto"])]
    text = "".join(s["texto"] for s in todos)
    if parcial is not None:
        parcial.apagar()
    return Transcription(text=text.strip(), duration_s=round(duracao, 2))

