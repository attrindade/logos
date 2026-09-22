"""Deriva rota e timestamp a partir da fala (primeira palavra) e do arquivo de áudio."""
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path


class Route(str, Enum):
    DIARIO = "Diario"
    PLANEJAMENTO = "Planejamento"
    INBOX = "Inbox"


# Padrão de nome gerado por apps de gravação: "2026-08-23 22-46-52.m4a" ou com sufixos
_NAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})\s(?P<time>\d{2}-\d{2}-\d{2})(?:\s(?P<tag>.*))?$"
)


TEXT_EXTENSIONS = {".txt", ".md"}

# Só usado nas pastas extras (Drive): lá convivem audiobook, PDF, zip e atalhos .gdoc, e o
# Whisper mastigaria tudo. Na Inbox própria segue valendo o filtro largo do is_ignored, porque
# só o gravador e o bot escrevem nela.
AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".ogg", ".opus", ".aac", ".flac", ".mp4", ".m4b", ".wma"}

# Primeira palavra que mantém a rota Inbox mas fixa a categoria por regra, sem depender da LLM.
_CATEGORIAS = {
    "tarefa": "tarefa", "tarefas": "tarefa",
    "ideia": "ideia", "ideias": "ideia",
    "reuniao": "reuniao", "reunioes": "reuniao",
}

# Sufixo no nome dos arquivos gravados pelo bot: "2026-09-14 15-02-11 tg 123.txt" (123 = message_id).
TELEGRAM_TAG = "tg"


@dataclass(frozen=True)
class Triage:
    route: Route
    recorded_at: datetime
    tag: str
    categoria: str = ""
    prioridade: str = ""
    canal: str = "syncthing"


def is_text_file(filename) -> bool:
    return Path(str(filename)).suffix.lower() in TEXT_EXTENSIONS


def is_audio_file(filename) -> bool:
    return Path(str(filename)).suffix.lower() in AUDIO_EXTENSIONS


# Temporários que o Syncthing cria enquanto ainda escreve o arquivo final. Passavam pelo
# filtro do "." e viravam uma segunda nota do mesmo áudio (a chave do ledger é nome::tamanho,
# então o .tmp nunca colide com o original). Visto em 2026-09-21: ~syncthing~2026-09-18
# 20-39-56.m4a.tmp gerou duplicata da captura de 18/09.
_SYNC_TEMP_RE = re.compile(r"^(~syncthing~|\.syncthing\.)|\.(tmp|part|partial|crdownload)$", re.IGNORECASE)


def is_ignored(filename: str) -> bool:
    """Arquivos que começam com '.' são metadados do Syncthing/app e nunca viram nota.
    Inclui .evr_recently_deleted_*: é a lixeira do Easy Voice Recorder, áudio que o dono apagou.
    Inclui também os temporários de sincronização (~syncthing~*, *.tmp)."""
    return filename.startswith(".") or bool(_SYNC_TEMP_RE.search(filename))


def _strip_extension(filename: str) -> str:
    filename_str = str(filename)
    idx = filename_str.rfind(".")
    return filename_str[:idx] if idx > 0 else filename_str


def _strip_evr_prefix(stem: str) -> str:
    """.evr_recently_deleted_(TIMESTAMP)2026-08-23 23-07-27 -> 2026-08-23 23-07-27"""
    m = re.match(r"^\.evr_recently_deleted_\(\d+\)(?P<rest>.*)$", stem)
    return m.group("rest") if m else stem


def _name_match(filename) -> re.Match | None:
    name = Path(filename).name if isinstance(filename, Path) else str(filename)
    return _NAME_RE.match(_strip_evr_prefix(_strip_extension(name)))


def triage_from_filename(filename: str, fallback: datetime | None = None) -> datetime:
    """Extrai o timestamp de gravação a partir do nome do arquivo. Se o nome não seguir o
    padrão, usa `fallback` (o pipeline passa o mtime do arquivo) ou a hora atual."""
    m = _name_match(filename)
    if not m:
        return fallback or datetime.now()

    date_part = m.group("date")
    time_part = m.group("time").replace("-", ":")

    try:
        return datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return fallback or datetime.now()


def is_bundle_name(name: str) -> bool:
    """Pasta de pacote do bot: 'YYYY-MM-DD HH-MM-SS tg <message_id>'."""
    return (
        not is_ignored(name)
        and Path(name).suffix == ""
        and _NAME_RE.match(name) is not None
        and canal_from_filename(name) == "telegram"
    )


def canal_from_filename(filename) -> str:
    m = _name_match(filename)
    words = (m.group("tag") or "").lower().split() if m else []
    return "telegram" if words and words[0] == TELEGRAM_TAG else "syncthing"


def _normalize_word(word: str) -> str:
    """Remove pontuação e converte para minúsculas sem acento para comparação limpa."""
    w = word.strip().lower()
    w = re.sub(r"^[^\w]+|[^\w]+$", "", w)
    nfkd = unicodedata.normalize("NFKD", w)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def triage_route_from_text(transcript: str) -> Route:
    """Classifica a rota pela primeira palavra dita no áudio:
    - 'diario' / 'diarios' -> Route.DIARIO
    - 'planejamento' / 'planejamentos' -> Route.PLANEJAMENTO
    - qualquer outra palavra -> Route.INBOX (nota avulsa)
    """
    if not transcript or not transcript.strip():
        return Route.INBOX

    words = transcript.strip().split()
    if not words:
        return Route.INBOX

    first_word = _normalize_word(words[0])

    if first_word in ("diario", "diarios"):
        return Route.DIARIO
    elif first_word in ("planejamento", "planejamentos"):
        return Route.PLANEJAMENTO
    return Route.INBOX


def triage_category_from_text(transcript: str) -> tuple[str, str]:
    """(categoria, prioridade) pela primeira palavra: 'tarefa', 'ideia' ou 'reunião'.
    'tarefa urgente ...' ganha prioridade alta."""
    words = (transcript or "").split()
    if not words:
        return "", ""
    categoria = _CATEGORIAS.get(_normalize_word(words[0]), "")
    prioridade = ""
    if categoria == "tarefa" and len(words) > 1 and _normalize_word(words[1]) == "urgente":
        prioridade = "alta"
    return categoria, prioridade


def triage(filename: str, transcript: str = "", fallback: datetime | None = None) -> Triage:
    """Realiza a triagem combinando o timestamp do arquivo e a rota pelo conteúdo falado."""
    recorded_at = triage_from_filename(filename, fallback)
    route = triage_route_from_text(transcript)
    categoria, prioridade = triage_category_from_text(transcript) if route == Route.INBOX else ("", "")
    return Triage(
        route=route, recorded_at=recorded_at, tag=route.value,
        categoria=categoria, prioridade=prioridade, canal=canal_from_filename(filename),
    )
