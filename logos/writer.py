"""Monta a nota .md final estruturada e enriquecida para o Obsidian, guarda anexos e copia o áudio para Archive.

O transcrito verbatim é sempre preservado no corpo da nota, intocado — a LLM é aditiva, nunca destrutiva.
"""
import re
import shutil
from datetime import datetime
from pathlib import Path

import yaml

from . import config
from .triage import Route, Triage

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"}


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


# ---------------------------------------------------------
# Anexos (pacotes do Telegram)
# ---------------------------------------------------------

def _vault_root() -> Path:
    return config.VAULT_DIR or config.NOTES_DIR.parent


def vault_link(path: Path) -> str:
    """Caminho relativo ao vault para wikilinks e embeds. O caminho completo evita que o Obsidian
    confunda `X.pdf` com o sidecar `X.pdf.md`, cujo nome de nota é o mesmo `X.pdf`."""
    try:
        return Path(path).resolve().relative_to(_vault_root().resolve()).as_posix()
    except ValueError:
        return Path(path).name


def _attachment_folder(recorded_at: datetime, route: Route | None = None) -> Path:
    # Anexo do diário mora dentro de Diario/: herda o .gitignore, a exclusão do MCP e a
    # allowlist do bot. Em capturas/anexos/ o PDF e o sidecar dele iriam para o GitHub.
    base = config.NOTES_DIR / Route.DIARIO.value if route == Route.DIARIO else config.NOTES_DIR
    return base / "anexos" / f"{recorded_at:%Y}" / f"{recorded_at:%m}"


def store_attachment(source: Path, recorded_at: datetime, n: int, route: Route | None = None) -> Path:
    """Copia o anexo para `anexos/YYYY/MM/YYYY-MM-DD_HH-MM-SS_n.<ext>`. Reaproveita a cópia de uma
    tentativa anterior do mesmo pacote (mesmo tamanho); nome ocupado por outro arquivo ganha sufixo."""
    folder = _attachment_folder(recorded_at, route)
    folder.mkdir(parents=True, exist_ok=True)
    stem, suffix = f"{recorded_at:%Y-%m-%d_%H-%M-%S}_{n}", source.suffix.lower()
    dest, k = folder / f"{stem}{suffix}", 2
    while dest.exists() and dest.stat().st_size != source.stat().st_size:
        dest, k = folder / f"{stem}-{k}{suffix}", k + 1
    if not dest.exists():
        shutil.copy2(source, dest)
    return dest


def write_sidecar(attachment: Path, text: str) -> Path:
    sidecar = attachment.with_name(f"{attachment.name}.md")
    header = _yaml_frontmatter({"tipo": "sidecar", "anexo": vault_link(attachment)})
    sidecar.write_text(f"{header}\n# Texto extraído de {attachment.name}\n\n{text.strip()}\n", encoding="utf-8")
    return sidecar


def write_link_sidecar(url: str, title: str, text: str, recorded_at: datetime, n: int, route: Route | None = None) -> Path:
    folder = _attachment_folder(recorded_at, route)
    folder.mkdir(parents=True, exist_ok=True)
    sidecar = folder / f"{recorded_at:%Y-%m-%d_%H-%M-%S}_{n}.link.md"
    header = _yaml_frontmatter({"tipo": "sidecar", "url": url, "titulo": title})
    sidecar.write_text(f"{header}\n# {title or url}\n\n<{url}>\n\n{text.strip()}\n", encoding="utf-8")
    return sidecar


def _map_line(local: dict) -> str:
    lat, lon = local["lat"], local["lon"]
    label = f"{lat}, {lon}" + (f" · {local['titulo']}" if local.get("titulo") else "")
    return f"📍 [{label}](https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=17/{lat}/{lon})"


def _attachment_lines(anexos: list[dict]) -> list[str]:
    lines = []
    for anexo in anexos:
        if anexo.get("url"):
            title = (anexo.get("titulo") or anexo["url"]).replace("[", "(").replace("]", ")")
            line = f"- 🔗 [{title}]({anexo['url']})"
            if anexo.get("sidecar"):
                line += f" · [[{vault_link(anexo['sidecar'])}|artigo]]"
            lines.append(line)
            continue
        link = vault_link(anexo["path"])
        ext = Path(anexo["path"]).suffix.lower()
        if ext in IMAGE_EXTS:
            lines += [f"![[{link}|400]]", ""]
        elif ext == ".pdf":
            lines += [f"![[{link}#page=1]]", ""]
        else:
            lines.append(f"- [[{link}]]")
        if anexo.get("sidecar"):
            lines.append(f"- Texto extraído: [[{vault_link(anexo['sidecar'])}]]")
    return lines


def _attachment_paths(anexos: list[dict]) -> list[str]:
    paths = []
    for anexo in anexos:
        target = anexo.get("sidecar") if anexo.get("url") else anexo.get("path")
        if target:
            paths.append(vault_link(target))
    return paths


def append_to_note(note_path: Path, anexos: list[dict], transcript: str, recorded_at: datetime, local: dict | None = None) -> None:
    """Resposta a uma mensagem que já virou nota: acrescenta uma seção ao fim, sem tocar no
    frontmatter (que pode ter sido editado à mão ou por outro processo)."""
    parts = ["", "", f"## 📎 Resposta de {recorded_at:%Y-%m-%d %H:%M}", ""]
    parts += _attachment_lines(anexos)
    if local:
        parts.append(_map_line(local))
    if transcript.strip():
        parts += ["", transcript.strip()]
    with open(note_path, "a", encoding="utf-8") as f:
        f.write("\n".join(parts) + "\n")


def write_note(
    source_filename: str,
    triage_result: Triage,
    transcript: str,
    enrichment: dict,
    llm_model: str,
    fonte_tipo: str = "audio",
    anexos: list[dict] | None = None,
    local: dict | None = None,
    tipo: str | None = None,
    degradado: str = "",
) -> Path:
    anexos = anexos or []
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
        "tipo": tipo or triage_result.route.value.lower(),
        "titulo": enrichment.get("titulo", ""),
        "tags": _as_list(enrichment.get("tags")),
    }

    # Metadados contextuais adicionais por rota
    if enrichment.get("humor"):
        frontmatter["humor"] = enrichment.get("humor")
    # categoria/prioridade vindas da primeira palavra (regra) prevalecem sobre a LLM
    prioridade = triage_result.prioridade or enrichment.get("prioridade")
    if prioridade:
        frontmatter["prioridade"] = prioridade
    if degradado:
        frontmatter["status"] = "degradado"
    elif enrichment.get("status"):
        frontmatter["status"] = enrichment.get("status")
    categoria = triage_result.categoria or enrichment.get("categoria")
    if categoria:
        frontmatter["categoria"] = categoria

    # Relações de entidades do Second Brain
    pessoas = _as_list(enrichment.get("pessoas"))
    if pessoas:
        frontmatter["pessoas"] = pessoas

    projetos = _as_list(enrichment.get("projetos"))
    if projetos:
        frontmatter["projetos"] = projetos

    if anexos:
        frontmatter["anexos"] = _attachment_paths(anexos)
    if local:
        frontmatter["local"] = f"{local['lat']}, {local['lon']}"

    frontmatter["fonte"] = source_filename
    frontmatter["fonte_tipo"] = fonte_tipo
    frontmatter["canal"] = triage_result.canal
    if fonte_tipo == "audio":
        frontmatter["modelo_stt"] = config.WHISPER_MODEL_SIZE
    frontmatter["modelo_llm"] = llm_model  # vazio (omitido) quando a nota curta pula a LLM

    body_parts = [_yaml_frontmatter(frontmatter), ""]

    # A nota precisa denunciar a própria degradação: em 2026-09-21 três capturas saíram sem
    # enriquecimento (Ollama fora do ar) com aparência de nota normal, e ninguém percebeu.
    if degradado:
        body_parts += [
            "> [!warning] Enriquecimento não aconteceu",
            f"> {degradado}",
            "> A transcrição abaixo está íntegra. Reprocessar com `retry_failed` depois de"
            " resolver a causa, ou promover esta nota à mão.",
            "",
        ]

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

    # Anexos embutidos (pacotes do Telegram)
    if anexos or local:
        body_parts += ["## 📎 Anexos", *_attachment_lines(anexos)]
        if local:
            body_parts.append(_map_line(local))
        body_parts.append("")

    # Transcrição Verbatim Integral Intocada
    headings = {
        "texto": ("## ✍️ Texto Original", "*(vazio)*"),
        "pacote": ("## 💬 Mensagens", "*(sem texto)*"),
    }
    heading, empty = headings.get(fonte_tipo, ("## 🎙️ Transcrição Original", "*(sem fala detectada)*"))
    body_parts += ["---", heading, "", transcript if transcript.strip() else empty]

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
