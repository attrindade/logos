"""Monta a nota .md final e copia o áudio para Archive.

O transcrito verbatim é sempre preservado no corpo da nota, intocado — a LLM é aditiva,
nunca substitui."""
import shutil
from pathlib import Path

import yaml

from . import config
from .triage import Route, Triage


def _yaml_frontmatter(fields: dict) -> str:
    return "---\n" + yaml.safe_dump(fields, allow_unicode=True, sort_keys=False) + "---\n"


def _as_list(value) -> list:
    """A LLM às vezes devolve 'a, b, c' em vez de uma lista YAML (observado com
    qwen2.5:7b no campo tags). Normaliza para lista em vez de propagar o tipo errado."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [value]


def write_note(
    source_filename: str,
    triage_result: Triage,
    transcript: str,
    enrichment: dict,
    llm_model: str,
) -> Path:
    route_dir = config.NOTES_DIR / triage_result.route.value
    route_dir.mkdir(parents=True, exist_ok=True)

    frontmatter = {
        "date": triage_result.recorded_at.strftime("%Y-%m-%d"),
        "time": triage_result.recorded_at.strftime("%H:%M:%S"),
        "tipo": triage_result.route.value.lower(),
        "titulo": enrichment.get("titulo", ""),
        "tags": _as_list(enrichment.get("tags")),
        "fonte": source_filename,
        "modelo_stt": config.WHISPER_MODEL_SIZE,
        "modelo_llm": llm_model,
    }

    body_parts = [_yaml_frontmatter(frontmatter), ""]

    resumo = enrichment.get("resumo")
    if resumo:
        body_parts += ["## Resumo", resumo, ""]

    pontos = _as_list(enrichment.get("pontos"))
    if pontos:
        body_parts += ["## Pontos", *(f"- {p}" for p in pontos), ""]

    acoes = _as_list(enrichment.get("acoes"))
    if acoes:
        body_parts += ["## Ações", *(f"- [ ] {a}" for a in acoes), ""]

    body_parts += ["---", "## Transcrição", "", transcript if transcript.strip() else "*(sem fala detectada)*"]

    note_name = f"{triage_result.recorded_at.strftime('%Y-%m-%d_%H-%M-%S')}.md"
    note_path = route_dir / note_name
    note_path.write_text("\n".join(body_parts), encoding="utf-8")
    return note_path


def archive_audio(source_path: Path, triage_result: Triage) -> Path:
    """Copia (nunca move) o áudio original para Archive."""
    config.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    dest_name = f"{triage_result.recorded_at.strftime('%Y-%m-%d_%H-%M-%S')}_{triage_result.route.value}{source_path.suffix}"
    dest_path = config.ARCHIVE_DIR / dest_name
    shutil.copy2(source_path, dest_path)
    return dest_path

