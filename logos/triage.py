"""Deriva rota e timestamp a partir do nome do arquivo de áudio."""
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Route(str, Enum):
    DIARIO = "Diario"
    PLANEJAMENTO = "Planejamento"
    INBOX = "Inbox"


# "2026-08-23 22-46-52 D.m4a" / "2026-08-23 22-46-52 D4.m4a" / "2026-08-23 22-46-52 Diário.m4a"
# / "2026-08-23 22-46-52.m4a"
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


def triage(filename: str) -> Triage:
    """Nunca lança exceção e nunca descarta: nome não reconhecido cai em Route.INBOX
    com recorded_at = agora, para nunca perder um arquivo silenciosamente."""
    stem = _strip_extension(filename)
    stem = _strip_evr_prefix(stem)

    m = _NAME_RE.match(stem)
    if not m:
        return Triage(route=Route.INBOX, recorded_at=datetime.now(), tag="")

    date_part = m.group("date")
    time_part = m.group("time").replace("-", ":")
    tag = (m.group("tag") or "").strip()

    try:
        recorded_at = datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        recorded_at = datetime.now()

    route = _route_from_tag(tag)
    return Triage(route=route, recorded_at=recorded_at, tag=tag)


def _strip_extension(filename: str) -> str:
    idx = filename.rfind(".")
    return filename[:idx] if idx > 0 else filename


def _strip_evr_prefix(stem: str) -> str:
    """".evr_recently_deleted_(TIMESTAMP)2026-08-23 23-07-27 Diário" -> "2026-08-23 23-07-27 Diário" """
    m = re.match(r"^\.evr_recently_deleted_\(\d+\)(?P<rest>.*)$", stem)
    return m.group("rest") if m else stem


def _route_from_tag(tag: str) -> Route:
    if not tag:
        return Route.INBOX
    first = tag[0].lower()
    if first == "d":
        return Route.DIARIO
    if first == "p":
        return Route.PLANEJAMENTO
    return Route.INBOX

