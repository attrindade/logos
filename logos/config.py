"""Configuração central do Logos com suporte a arquivo de configuração local e variáveis de ambiente."""
import os
import shutil
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

_raw_inbox_dir = os.getenv("LOGOS_INBOX_DIR") or _user_cfg.get("inbox_dir")
INBOX_DIR = Path(_raw_inbox_dir).expanduser().resolve() if _raw_inbox_dir else DATA_ROOT / "Inbox"

# Pastas extras observadas além da Inbox própria. Caso de uso: a pasta do Google Drive onde o
# gravador de entrevistas deposita — o Drive for Desktop a monta como caminho local (G:\...),
# então não é conector nenhum, é só mais uma pasta.
# INBOX_DIR continua sendo a Inbox canônica: é onde o bot do Telegram grava e a única que o
# ensure_directories cria. As extras NUNCA são criadas — se o Drive não estiver montado, o
# watcher avisa e segue, em vez de fabricar uma pasta órfã no lugar do ponto de montagem.
# Nas extras só entra áudio (triage.AUDIO_EXTENSIONS): numa pasta do Drive convivem PDF, zip,
# audiobook e atalhos .gdoc que não têm o que fazer no pipeline.
# `is not None` e não truthiness: a variável definida como vazia precisa significar "nenhuma
# pasta extra", e é assim que os testes se isolam da pasta real do Drive. Com truthiness, o
# vazio cairia de volta no yaml e a suíte varreria o Drive de verdade.
_raw_extra = os.getenv("LOGOS_EXTRA_INBOX_DIRS")
if _raw_extra is not None:
    _extra_list = [p for p in _raw_extra.split(os.pathsep) if p.strip()]
else:
    _extra_list = _user_cfg.get("extra_inbox_dirs") or []
EXTRA_INBOX_DIRS = [Path(p).expanduser().resolve() for p in _extra_list]


def inbox_dirs() -> list[Path]:
    """Pastas observadas, a própria primeiro. É função e não constante de módulo de propósito:
    os testes trocam INBOX_DIR em tempo de execução, e uma lista montada no import ficaria
    apontando para a pasta antiga."""
    return [INBOX_DIR, *(d for d in EXTRA_INBOX_DIRS if d != INBOX_DIR)]


# Espelho do Grava no Drive (logos/grava_drive.py): copia o que o Syncthing trouxe do celular
# para Recordings/Grava (transcrever/ e guardar/<apelido>/) para "<grava_drive_dir>/<apelido>/".
# O Drive for Desktop monta o Drive em G:, então é cópia entre pastas locais, sem projeto no
# Google Cloud. Vazio desliga, no yaml ou na variável — `is not None` pelo mesmo motivo de
# EXTRA_INBOX_DIRS: os testes desligam pela variável vazia e nunca podem escrever no G: real.
def _opcional(env: str, chave: str, padrao: str) -> str:
    valor = os.getenv(env)
    if valor is None:
        valor = _user_cfg.get(chave, padrao)
    return str(valor or "").strip()


_raw_grava_dir = _opcional("LOGOS_GRAVA_DIR", "grava_dir", str(DATA_ROOT / "Grava"))
GRAVA_DIR = Path(_raw_grava_dir).expanduser().resolve() if _raw_grava_dir else None
# Sem resolve(): com o Drive desmontado a letra nem existe, e o caminho tem de ficar como foi escrito.
_raw_grava_drive = _opcional("LOGOS_GRAVA_DRIVE_DIR", "grava_drive_dir", "G:/Meu Drive/Grava")
GRAVA_DRIVE_DIR = Path(_raw_grava_drive).expanduser() if _raw_grava_drive else None
# Palavras que, no nome do arquivo ou de uma pasta, nunca vão ao Drive (comparação sem acento e
# por começo de palavra: "terapia" pega "Terapias"). O Diário fica de fora sempre, esteja ou não
# na lista; ela serve para acrescentar outros sem mexer em código (gotcha da terapia no vault).
_raw_excluir = _user_cfg.get("grava_drive_excluir")
GRAVA_DRIVE_EXCLUIR = [str(p) for p in _raw_excluir] if isinstance(_raw_excluir, list) else ["terapia"]


_raw_notes_dir = os.getenv("LOGOS_NOTES_DIR") or _user_cfg.get("notes_dir")
NOTES_DIR = Path(_raw_notes_dir).expanduser().resolve() if _raw_notes_dir else DATA_ROOT / "Notes"

ARCHIVE_DIR = DATA_ROOT / "Archive"
TRANSCRIPTS_DIR = DATA_ROOT / "Transcripts"
STATE_DIR = DATA_ROOT / "state"
LOGS_DIR = DATA_ROOT / "logs"
LEDGER_PATH = STATE_DIR / "ledger.json"
STATUS_PATH = STATE_DIR / "status.json"


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
# Rodada de 25/10/2026, desligados de fábrica (ligar só depois de medir no conjunto de prova):
# N5 = filtro de alucinação (hallucination_silence_threshold + frases que o Whisper inventa no
# silêncio); N1 = vocabulário (initial_prompt e hotwords de vocabulario.txt + org/people + work/active).
WHISPER_ANTI_ALUCINACAO = bool(_user_cfg.get("whisper_anti_alucinacao", False))
WHISPER_VOCABULARIO = bool(_user_cfg.get("whisper_vocabulario", False))
# Coletor de indícios "parece feito" (`logos/indicios.py`, spec K §1.3): desligado até ele decidir
# (pergunta 2 do plano de 25/10/2026). Mesmo ligado, só age com a Semana nova (`recursos: propostas`).
INDICIOS_LIGADO = bool(_user_cfg.get("indicios_ligado", False))
# A caixa da captura (`logos/caixa.py`, LOG 9 de 28/10/2026). D1a respondida por ele em 28/10: o Claude
# na hora, e tem de ser o Opus 5.5, pelo id completo (alias curto como "opus" o CLI ignora e roda outro
# modelo). O Ollama local é a reserva quando o Claude falha ou passa do tempo. A variável de ambiente
# LOGOS_CAIXA_LIGADA=0 desliga (a suíte de testes usa isso para nunca chamar o claude.exe).
_env_caixa = os.getenv("LOGOS_CAIXA_LIGADA")
CAIXA_LIGADA = (_env_caixa not in ("0", "false", "")) if _env_caixa is not None else bool(_user_cfg.get("caixa_ligada", True))
CAIXA_MODELO = str(_user_cfg.get("caixa_modelo", "claude-opus-5-5"))
CAIXA_RESERVA = str(_user_cfg.get("caixa_reserva", "gemma4:e4b"))
# Medido em 28/10/2026 (5 frases inventadas, Opus 5.5): ver o feito2-LOG. Passou disso, vai a reserva.
CAIXA_LIMITE_CLAUDE_S = int(_user_cfg.get("caixa_limite_claude_s", 60))
CAIXA_LIMITE_OLLAMA_S = int(_user_cfg.get("caixa_limite_ollama_s", 300))

# ---------------------------------------------------------
# LLM (Ollama Local)
# ---------------------------------------------------------
OLLAMA_HOST = os.getenv("OLLAMA_HOST") or _user_cfg.get("ollama_host", "http://localhost:11434")
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX") or _user_cfg.get("ollama_num_ctx", 8192))
OLLAMA_KEEP_ALIVE = str(os.getenv("OLLAMA_KEEP_ALIVE") or _user_cfg.get("ollama_keep_alive", "10m"))
LLM_MODEL = os.getenv("LLM_MODEL") or _user_cfg.get("llm_model", "gemma4:e4b")
# Descrição de foto sem texto reconhecido pelo OCR. Vazio desliga (a variável vazia também vale).
VISION_MODEL = (
    os.environ["LOGOS_VISION_MODEL"] if "LOGOS_VISION_MODEL" in os.environ
    else _user_cfg.get("vision_model", LLM_MODEL)
)
CHUNK_TOKEN_THRESHOLD = int(os.getenv("CHUNK_TOKEN_THRESHOLD") or _user_cfg.get("chunk_token_threshold", 6500))
# Abaixo disso a nota pula a LLM: título das primeiras palavras e resumo = texto.
LLM_MIN_WORDS = int(os.getenv("LLM_MIN_WORDS") or _user_cfg.get("llm_min_words", 40))

# ---------------------------------------------------------
# ActivityWatch (evento por captura; ignorado se o servidor não responder)
# ---------------------------------------------------------
AW_HOST = os.getenv("LOGOS_AW_HOST") or _user_cfg.get("aw_host", "127.0.0.1")
AW_PORT = int(os.getenv("LOGOS_AW_PORT") or _user_cfg.get("aw_port", 5600))

# ---------------------------------------------------------
# Relógio (Huawei Watch Fit, modelo TIA-B09, via Gadgetbridge)
# ---------------------------------------------------------
# Cópia do banco que a "Exportação automática" do Gadgetbridge grava numa pasta do Syncthing.
# Sem valor, `python -m logos.relogio` pede --db e não faz nada.
_raw_gb_db = os.getenv("LOGOS_GADGETBRIDGE_DB") or _user_cfg.get("gadgetbridge_db")
GADGETBRIDGE_DB = Path(_raw_gb_db).expanduser() if _raw_gb_db else None

# ---------------------------------------------------------
# Vault no celular (`logos/vault_celular.py`)
# ---------------------------------------------------------
# Começos de caminho (relativos ao vault) que NÃO vão para o celular, além das guardas fixas. Vazio
# de fábrica = tudo da lista vai, como desde 14/10. Em 28/10/2026 viu-se que o reenvio "1 enviada(s)"
# a cada 10 min era o `inbox/sessoes-<dia>.md`, que o hook do Claude Code reescreve a cada resposta:
# ["inbox/sessoes-"] corta isso (e apaga as cópias dele no celular). Decisão dele.
# LOGOS_VAULT_CELULAR_FORA (vírgulas; vazia = nenhum) tem precedência: a suíte de testes a zera para
# não herdar a chave do logos_config.yaml (ligada em 28/10/2026, onda 4).
_FORA_ENV = os.getenv("LOGOS_VAULT_CELULAR_FORA")
VAULT_CELULAR_FORA = ([x.strip() for x in _FORA_ENV.split(",") if x.strip()] if _FORA_ENV is not None
                      else [str(x) for x in (_user_cfg.get("vault_celular_fora") or [])])

# ---------------------------------------------------------
# Vigia: checagens novas de 28/10/2026 (`logos/vigia.py`)
# ---------------------------------------------------------
# Relógio sem amostra nova há mais de 48 h vira "atenção" na aba Logos. Ligado de fábrica (D3,
# padrão seguro (c) do plano de 28/10): se ele parou de usar o relógio, `vigia_relogio: false`.
VIGIA_RELOGIO = bool(_user_cfg.get("vigia_relogio", True))
# Coletores do ActivityWatch que devem ter evento novo (início do nome do bucket). Sem evento há
# mais de 48 h de PC ligado, ou nunca, vira "atenção". A janela tem checagem própria (chk_aw).
# Fora da lista de fábrica (revisão de 28/10/2026, achado do pacote AW): o `input`, tirado de
# propósito do aw-qt.toml em 08/10, e o `obsidian`, que só registra com o Obsidian de tela aberto
# (não é superfície, plano v2). Com eles, a "atenção" seria eterna. O `web-chrome_` com o "_" pula
# o bucket órfão `aw-watcher-web-chrome` (sem `_att`, parado desde 14/09).
VIGIA_AW_ESPERADOS = [str(x) for x in (_user_cfg.get("vigia_aw_esperados") or (
    "aw-watcher-afk_", "aw-watcher-web-chrome_"))]

# ---------------------------------------------------------
# Telegram, anexos e OCR
# ---------------------------------------------------------
# Mensagens com menos que isso entre si formam um pacote (uma nota).
TELEGRAM_WINDOW_MIN = float(os.getenv("LOGOS_TELEGRAM_WINDOW_MIN") or _user_cfg.get("telegram_window_min", 10))
# Diário e plano feitos antes desta hora contam para o dia anterior: o diário da madrugada é do
# dia que acabou de passar, não do que começa.
VIRADA_DO_DIA_H = int(os.getenv("LOGOS_VIRADA_DO_DIA_H") or _user_cfg.get("virada_do_dia_h", 4))

# Raiz do vault: embeds com caminho completo (writer) e base da allowlist de pastas que o
# bot pode ler (consulta). Padrão: pasta-mãe de notes_dir.
# `is not None` pelo mesmo motivo de EXTRA_INBOX_DIRS: a variável definida como vazia precisa
# significar "sem vault", senão a suíte cairia de volta no yaml e leria o vault de verdade.
_env_vault_dir = os.getenv("LOGOS_VAULT_DIR")
_raw_vault_dir = _env_vault_dir if _env_vault_dir is not None else _user_cfg.get("vault_dir")
VAULT_DIR = Path(_raw_vault_dir).expanduser().resolve() if _raw_vault_dir else None


def _find_tesseract() -> str:
    configured = os.getenv("LOGOS_TESSERACT_CMD") or _user_cfg.get("tesseract_cmd")
    if configured:
        return str(configured)
    found = shutil.which("tesseract")
    if found:
        return found
    default = Path(os.getenv("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR" / "tesseract.exe"
    return str(default) if default.exists() else ""


TESSERACT_CMD = _find_tesseract()
_raw_tessdata_dir = os.getenv("LOGOS_TESSDATA_DIR") or _user_cfg.get("tessdata_dir")
TESSDATA_DIR = Path(_raw_tessdata_dir).expanduser().resolve() if _raw_tessdata_dir else DATA_ROOT / "models" / "tessdata"
OCR_LANGS = os.getenv("LOGOS_OCR_LANGS") or _user_cfg.get("ocr_langs", "por+eng")

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


def secrets_path() -> Path:
    """Os segredos ficam fora do repo, em state/secrets.yaml (saiu de `logos/telegram.py` em 28/10/2026)."""
    return STATE_DIR / "secrets.yaml"


def load_secrets() -> dict:
    try:
        data = yaml.safe_load(secrets_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return data if isinstance(data, dict) else {}


def save_secrets(updates: dict) -> None:
    path = secrets_path()
    data = load_secrets()
    data.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)


def ensure_directories():
    """Garante que todas as pastas essenciais do Logos existam."""
    for d in (DATA_ROOT, INBOX_DIR, ARCHIVE_DIR, TRANSCRIPTS_DIR, NOTES_DIR, STATE_DIR, LOGS_DIR, Path(WHISPER_MODELS_DIR)):
        d.mkdir(parents=True, exist_ok=True)

