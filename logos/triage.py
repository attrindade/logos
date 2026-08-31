"""Deriva rota e timestamp a partir da fala (primeira palavra) e do arquivo de áudio."""
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Route(str, Enum):
    DIARIO = "Diario"
    PLANEJAMENTO = "Planejamento"
    INBOX = "Inbox"


# Padrão de nome gerado por apps de gravação: "2026-08-23 22-46-52.m4a" ou com sufixos
_NAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})\s(?P<time>\d{2}-\d{2}-\d{2})(?:\s(?P<tag>.*))?$"
)


@dataclass(frozen=True)
class Triage:
    route: Route
    recorded_at: datetime
    tag: str


def is_ignored(filename: str) -> bool:
    """Arquivos que começam com '.' são metadados do Syncthing/app, nunca áudio a processar
    — exceto os .evr_recently_deleted_*, que SÃO áudio real."""
    if filename.startswith(".evr_recently_deleted_"):
        return False
    return filename.startswith(".")


def _strip_extension(filename: str) -> str:
    idx = filename.rfind(".")
    return filename[:idx] if idx > 0 else filename


def _strip_evr_prefix(stem: str) -> str:
    """.evr_recently_deleted_(TIMESTAMP)2026-08-23 23-07-27 -> 2026-08-23 23-07-27"""
    m = re.match(r"^\.evr_recently_deleted_\(\d+\)(?P<rest>.*)$", stem)
    return m.group("rest") if m else stem


def triage_from_filename(filename: str) -> datetime:
    """Extrai o timestamp de gravação a partir do nome do arquivo."""
    stem = _strip_extension(filename)
    stem = _strip_evr_prefix(stem)

    m = _NAME_RE.match(stem)
    if not m:
        return datetime.now()

    date_part = m.group("date")
    time_part = m.group("time").replace("-", ":")

    try:
        return datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return datetime.now()


def _normalize_word(word: str) -> str:
    """Remove pontuação e converte para minúsculas sem acento para comparação limpa."""
    w = word.strip().lower()
    w = re.sub(r"^[^\w]+|[^\w]+$", "", w)
    nfkd = unicodedata.normalize("NFKD", w)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def triage_route_from_text(transcript: str) -> Route:
    """Classifica a rota pela primeira palavra dita no áudio:
    - 'diario' / 'diários' -> Route.DIARIO
    - 'planejamento' / 'planejar' -> Route.PLANEJAMENTO
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
    elif first_word in ("planejamento", "planejar", "planejamentos"):
        return Route.PLANEJAMENTO
    return Route.INBOX


def triage(filename: str, transcript: str = "") -> Triage:
    """Realiza a triagem combinando o timestamp do arquivo e a rota pelo conteúdo falado."""
    recorded_at = triage_from_filename(filename)
    route = triage_route_from_text(transcript)
    return Triage(route=route, recorded_at=recorded_at, tag=route.value)
