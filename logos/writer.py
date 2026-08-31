"""Monta a nota .md final estruturada e enriquecida para o Obsidian e copia o áudio para Archive.

O transcrito verbatim é sempre preservado no corpo da nota, intocado — a LLM é aditiva, nunca destrutiva.
"""
import re
import shutil
from pathlib import Path

import yaml

from . import config
from .triage import Route, Triage


def _yaml_frontmatter(fields: dict) -> str:
    # Filtra chaves vazias desnecessárias para manter frontmatter limpo
    clean_fields = {k: v for k, v in fields.items() if v not in (None, "", [], {})}
    return "---\n" + yaml.safe_dump(clean_fields, allow_unicode=True, sort_keys=False) + "---\n"


def _as_list(value) -> list:
    """Normaliza para lista caso a LLM devolva string separada por vírgula ou item único."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [value]


def _sanitize_filename(name: str) -> str:
    """Remove caracteres inválidos para nomes de arquivos no Windows e Linux."""
    s = re.sub(r'[\\/*?:"<>|]', "", name)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:80] if s else "Nota"


def write_note(
    source_filename: str,
    triage_result: Triage,
    transcript: str,
    enrichment: dict,
    llm_model: str,
) -> Path:
    route_dir = config.NOTES_DIR / triage_result.route.value
    route_dir.mkdir(parents=True, exist_ok=True)

    date_str = triage_result.recorded_at.strftime("%Y-%m-%d")
    time_str = triage_result.recorded_at.strftime("%H:%M:%S")

    # ---------------------------------------------------------
    # 1. Frontmatter Unificado & Rico para Obsidian / Dataview
    # ---------------------------------------------------------
    frontmatter = {
        "date": date_str,
        "time": time_str,
        "tipo": triage_result.route.value.lower(),
        "titulo": enrichment.get("titulo", ""),
        "tags": _as_list(enrichment.get("tags")),
        "daily": f"[[{date_str}]]",
    }

    # Metadados contextuais adicionais por rota
    if enrichment.get("humor"):
        frontmatter["humor"] = enrichment.get("humor")
    if enrichment.get("prioridade"):
        frontmatter["prioridade"] = enrichment.get("prioridade")
    if enrichment.get("status"):
        frontmatter["status"] = enrichment.get("status")
    if enrichment.get("categoria"):
        frontmatter["categoria"] = enrichment.get("categoria")

    # Relações de entidades do Second Brain
    pessoas = _as_list(enrichment.get("pessoas"))
    if pessoas:
        frontmatter["pessoas"] = pessoas

    projetos = _as_list(enrichment.get("projetos"))
    if projetos:
        frontmatter["projetos"] = projetos

    frontmatter["fonte"] = source_filename
    frontmatter["modelo_stt"] = config.WHISPER_MODEL_SIZE
    frontmatter["modelo_llm"] = llm_model

    body_parts = [_yaml_frontmatter(frontmatter), ""]

    # ---------------------------------------------------------
    # 2. Seções Estruturadas no Corpo da Nota
    # ---------------------------------------------------------
    # Próxima Ação Imediata (GTD - Destaque de execução)
    proxima_acao = enrichment.get("proxima_acao")
    if proxima_acao and str(proxima_acao).strip():
        body_parts += ["## ⚡ Próxima Ação Imediata", f"- [ ] {proxima_acao} ⏫", ""]

    # Ações & Tarefas
    acoes = _as_list(enrichment.get("acoes"))
    if acoes:
        body_parts += ["## 📋 Ações & Tarefas", *(f"- [ ] {a}" for a in acoes), ""]

    # Decisões Tomadas
    decisoes = _as_list(enrichment.get("decisoes"))
    if decisoes:
        body_parts += ["## ⚖️ Decisões Tomadas", *(f"- {d}" for d in decisoes), ""]

    # Bloqueios / Dependências
    bloqueios = _as_list(enrichment.get("bloqueios"))
    if bloqueios:
        body_parts += ["## 🚫 Bloqueios & Dependências", *(f"- {b}" for b in bloqueios), ""]

    # Vitórias / Conquistas (Brag Doc)
    vitorias = _as_list(enrichment.get("vitorias"))
    if vitorias:
        body_parts += ["## 🏆 Vitórias & Progresso (Brag Doc)", *(f"- {v}" for v in vitorias), ""]

    # Gratidão / Apreciação
    gratidao = _as_list(enrichment.get("gratidao"))
    if gratidao:
        body_parts += ["## 🙏 Gratidão & Apreciação", *(f"- {g}" for g in gratidao), ""]

    # Tensões / Preocupações
    tensoes = _as_list(enrichment.get("tensoes"))
    if tensoes:
        body_parts += ["## 🌧️ Tensões & Preocupações", *(f"- {t}" for t in tensoes), ""]

    # Pontos de Atenção / Reflexões
    pontos = _as_list(enrichment.get("pontos"))
    if pontos:
        body_parts += ["## 💡 Pontos & Reflexões", *(f"- {p}" for p in pontos), ""]

    # Takeaways Chave (Inbox / Notas)
    takeaways = _as_list(enrichment.get("takeaways"))
    if takeaways:
        body_parts += ["## 📌 Principais Conclusões", *(f"- {tk}" for tk in takeaways), ""]

    # Resumo Narrativo / Contexto
    resumo = enrichment.get("resumo")
    if resumo:
        body_parts += ["## 📝 Resumo", resumo, ""]

    # Transcrição Verbatim Integral Intocada
    body_parts += ["---", "## 🎙️ Transcrição Original", "", transcript if transcript.strip() else "*(sem fala detectada)*"]

    # ---------------------------------------------------------
    # 3. Nome de Arquivo Human-Readable para o Obsidian
    # ---------------------------------------------------------
    raw_title = enrichment.get("titulo", "").strip()
    clean_title = _sanitize_filename(raw_title) if raw_title else triage_result.route.value
    timestamp_prefix = triage_result.recorded_at.strftime("%Y-%m-%d_%H-%M-%S")
    note_name = f"{timestamp_prefix} - {clean_title}.md"
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
