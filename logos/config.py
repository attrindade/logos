"""Configuração central do Logos com suporte a arquivo de configuração local e variáveis de ambiente."""
import os
from pathlib import Path
import yaml

# Diretório base do repositório
BASE_DIR = Path(__file__).resolve().parent.parent

# Caminho do arquivo de configuração do usuário (se existir)
USER_CONFIG_PATH = BASE_DIR / "logos_config.yaml"

def _load_user_config() -> dict:
    if USER_CONFIG_PATH.exists():
        try:
            with open(USER_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}

_user_cfg = _load_user_config()

# ---------------------------------------------------------
# Diretórios de Dados
# ---------------------------------------------------------
# Fallback padrão: pasta "Logos" no diretório Home do usuário
_DEFAULT_DATA_ROOT = Path.home() / "Logos"
_raw_data_root = os.getenv("LOGOS_DATA_ROOT") or _user_cfg.get("data_root")
DATA_ROOT = Path(_raw_data_root).expanduser().resolve() if _raw_data_root else _DEFAULT_DATA_ROOT

INBOX_DIR = DATA_ROOT / "Inbox"
ARCHIVE_DIR = DATA_ROOT / "Archive"
TRANSCRIPTS_DIR = DATA_ROOT / "Transcripts"
NOTES_DIR = DATA_ROOT / "Notes"
STATE_DIR = DATA_ROOT / "state"
LOGS_DIR = DATA_ROOT / "logs"
LEDGER_PATH = STATE_DIR / "ledger.json"

# ---------------------------------------------------------
# Modelo Whisper (STT)
# ---------------------------------------------------------
_raw_whisper_dir = os.getenv("WHISPER_MODELS_DIR") or _user_cfg.get("whisper_models_dir")
if _raw_whisper_dir:
    WHISPER_MODELS_DIR = str(Path(_raw_whisper_dir).expanduser().resolve())
else:
    # Padrão: models/ dentro do DATA_ROOT do Logos
    WHISPER_MODELS_DIR = str(DATA_ROOT / "models")

WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE") or _user_cfg.get("whisper_model_size", "large-v3-turbo")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE") or _user_cfg.get("whisper_compute_type", "int8")
WHISPER_CPU_THREADS = int(os.getenv("WHISPER_CPU_THREADS") or _user_cfg.get("whisper_cpu_threads", 6))
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE") or _user_cfg.get("whisper_language", "pt")
WHISPER_VAD_FILTER = bool(os.getenv("WHISPER_VAD_FILTER") or _user_cfg.get("whisper_vad_filter", True))

# ---------------------------------------------------------
# LLM (Ollama Local)
# ---------------------------------------------------------
OLLAMA_HOST = os.getenv("OLLAMA_HOST") or _user_cfg.get("ollama_host", "http://localhost:11434")
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX") or _user_cfg.get("ollama_num_ctx", 8192))
OLLAMA_KEEP_ALIVE = str(os.getenv("OLLAMA_KEEP_ALIVE") or _user_cfg.get("ollama_keep_alive", "0"))
LLM_MODEL = os.getenv("LLM_MODEL") or _user_cfg.get("llm_model", "gemma4:26b")
CHUNK_TOKEN_THRESHOLD = int(os.getenv("CHUNK_TOKEN_THRESHOLD") or _user_cfg.get("chunk_token_threshold", 6000))

# ---------------------------------------------------------
# Retenção e Políticas
# ---------------------------------------------------------
ARCHIVE_RETENTION_DAYS = int(os.getenv("ARCHIVE_RETENTION_DAYS") or _user_cfg.get("archive_retention_days", 60))

# ---------------------------------------------------------
# Watcher / Monitoramento
# ---------------------------------------------------------
STABILITY_CHECK_INTERVAL_S = int(os.getenv("STABILITY_CHECK_INTERVAL_S") or _user_cfg.get("stability_check_interval_s", 5))
STABILITY_CHECK_COUNT = int(os.getenv("STABILITY_CHECK_COUNT") or _user_cfg.get("stability_check_count", 2))
PROCESS_PRIORITY_BELOW_NORMAL = bool(os.getenv("PROCESS_PRIORITY_BELOW_NORMAL") or _user_cfg.get("process_priority_below_normal", True))


def ensure_directories():
    """Garante que todas as pastas essenciais do Logos existam."""
    for d in (DATA_ROOT, INBOX_DIR, ARCHIVE_DIR, TRANSCRIPTS_DIR, NOTES_DIR, STATE_DIR, LOGS_DIR, Path(WHISPER_MODELS_DIR)):
        d.mkdir(parents=True, exist_ok=True)

