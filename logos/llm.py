"""Cliente Ollama: num_ctx explícito, chunking para transcritos longos, YAML estrito
."""
import logging
import re
from pathlib import Path

import requests
import yaml

from . import config
from .triage import Route

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_PROMPT_FILES = {
    Route.DIARIO: "diario.txt",
    Route.PLANEJAMENTO: "planejamento.txt",
    Route.INBOX: "inbox.txt",
}

# aproximação grosseira: português tende a ~1.3 chars/token em contagem de whitespace;
# usamos contagem de palavras * 1.4 como proxy conservador de tokens (evita truncamento).
_TOKENS_PER_WORD = 1.4


def _estimate_tokens(text: str) -> int:
    return int(len(text.split()) * _TOKENS_PER_WORD)


def _load_prompt(route: Route) -> str:
    path = _PROMPTS_DIR / _PROMPT_FILES[route]
    return path.read_text(encoding="utf-8")


def _chunk_text(text: str, max_words: int) -> list[str]:
    """Divide em blocos de até max_words palavras, respeitando fronteiras de frase quando
    existirem. O transcrito do Whisper é uma string contínua sem quebras de parágrafo
    (stt.py concatena segmentos com "".join), então não dá para depender de \\n\\n — a
    divisão precisa funcionar mesmo em texto corrido."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    if not sentences:
        sentences = [text]

    chunks, current, current_words = [], [], 0
    for s in sentences:
        w = len(s.split())
        if current and current_words + w > max_words:
            chunks.append(" ".join(current))
            current, current_words = [], 0
        current.append(s)
        current_words += w
    if current:
        chunks.append(" ".join(current))
    return chunks


def _ollama_generate(prompt: str, model: str = None, keep_alive: str = None) -> str:
    model = model or config.LLM_MODEL
    resp = requests.post(
        f"{config.OLLAMA_HOST}/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"num_ctx": config.OLLAMA_NUM_CTX},
            "keep_alive": keep_alive if keep_alive is not None else config.OLLAMA_KEEP_ALIVE,
        },
        timeout=600,
    )
    resp.raise_for_status()
    return resp.json()["response"].strip()


def _strip_yaml_fences(raw: str) -> str:
    """Remove tags de raciocínio (thinking) e cercas ```yaml ... ``` se o modelo emitir."""
    # Remove blocos <|channel>thought ... <channel|> ou <think> ... </think>
    raw = re.sub(r"<\|channel>thought.*?<channel\|>", "", raw, flags=re.DOTALL)
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    raw = raw.strip()
    
    # Remove cercas markdown
    m = re.match(r"^```(?:yaml)?\s*\n(.*)\n```\s*$", raw, re.DOTALL)
    return m.group(1).strip() if m else raw


def enrich(transcript: str, route: Route, model: str = None) -> dict:
    """Retorna um dict com os campos do YAML gerado pela LLM. Nunca lança se o parse
    falhar — devolve um dict de fallback com o texto bruto, para não travar o pipeline."""
    if not transcript.strip():
        return {"titulo": "Sem fala detectada", "tags": [], "resumo": "Transcrição vazia."}

    prompt_template = _load_prompt(route)
    est_tokens = _estimate_tokens(transcript)

    if est_tokens <= config.CHUNK_TOKEN_THRESHOLD:
        raw = _ollama_generate(prompt_template.format(transcript=transcript), model=model)
    else:
        max_words = int(config.CHUNK_TOKEN_THRESHOLD / _TOKENS_PER_WORD)
        chunks = _chunk_text(transcript, max_words)
        logger.info(f"Transcrito longo ({est_tokens} tokens estimados) — dividido em {len(chunks)} blocos.")
        partial_summaries = []
        for i, chunk in enumerate(chunks):
            partial = _ollama_generate(prompt_template.format(transcript=chunk), model=model)
            partial_summaries.append(f"[Bloco {i+1}]\n{partial}")
        consolidation_input = "\n\n".join(partial_summaries)
        raw = _ollama_generate(prompt_template.format(transcript=consolidation_input), model=model)

    return parse_enrichment(raw)


# Campos que o writer trata como lista. A LLM às vezes escreve `- [[PPJ]]` sem aspas, que o
# YAML lê como lista aninhada ([["PPJ"]]) e o writer despejaria como `- - - PPJ`.
_LIST_FIELDS = (
    "tags", "pessoas", "projetos", "acoes", "decisoes", "bloqueios", "vitorias",
    "gratidao", "tensoes", "pontos", "takeaways",
)
_ENTITY_FIELDS = ("pessoas", "projetos")
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|]+)(?:\|[^\[\]]*)?\]\]")


def _known_entities() -> tuple[set[str], dict[str, str]]:
    """Grafias exatas das entidades do Contexto de Referência dos prompts e, por minúsculas,
    a primeira grafia citada. Lidas dos próprios prompts para acompanhar qualquer edição neles."""
    exact: set[str] = set()
    by_lower: dict[str, str] = {}
    for filename in _PROMPT_FILES.values():
        for m in _WIKILINK_RE.finditer((_PROMPTS_DIR / filename).read_text(encoding="utf-8")):
            name = m.group(1).strip()
            exact.add(name)
            by_lower.setdefault(name.lower(), name)
    return exact, by_lower


def _is_yaml_wikilink(value) -> bool:
    """`[[Nome]]` sem aspas vira [["Nome"]] no safe_load."""
    return (
        isinstance(value, list) and len(value) == 1
        and isinstance(value[0], list) and len(value[0]) == 1
        and isinstance(value[0][0], str)
    )


def _flatten(value, as_wikilink: bool) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if as_wikilink and _is_yaml_wikilink(value):
        return [f"[[{value[0][0].strip()}]]"]
    if isinstance(value, (list, tuple)):
        return [item for v in value for item in _flatten(v, as_wikilink)]
    if isinstance(value, dict):
        return [item for v in value.values() for item in _flatten(v, as_wikilink)]
    text = str(value).strip()
    return [text] if text else []


def _normalize_tag(tag: str) -> str:
    tag = tag.strip().strip("[]").strip().lstrip("#").strip().lower()
    return re.sub(r"\s+", "-", tag)  # tag do Obsidian não aceita espaço


def _canonical_entity(item: str, known: tuple[set[str], dict[str, str]]) -> str:
    exact, by_lower = known

    def fix(m: re.Match) -> str:
        name = m.group(1).strip()
        if name in exact:
            return f"[[{name}]]"  # grafia exata existe (ex.: Minimundo e minimundo são entidades distintas)
        return f"[[{by_lower.get(name.lower(), name)}]]"

    return _WIKILINK_RE.sub(fix, item)


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(i for i in items if i))


def normalize_enrichment(data: dict) -> dict:
    """Achata listas aninhadas, limpa tags (minúsculas, sem #) e restaura a grafia canônica dos
    wikilinks de pessoas e projetos. Nomes sem wikilink ficam como estão: o prompt manda deixar
    texto simples quando a entidade é ambígua (ex.: "Vini")."""
    known = _known_entities()
    result = dict(data)
    for field in _LIST_FIELDS:
        if field not in result:
            continue
        items = _flatten(result[field], as_wikilink=field in _ENTITY_FIELDS or field == "tags")
        if field == "tags":
            items = [_normalize_tag(t) for t in items]
        elif field in _ENTITY_FIELDS:
            items = [_canonical_entity(i, known) for i in items]
        result[field] = _dedupe(items)
    return result


def parse_enrichment(raw: str) -> dict:
    raw = _strip_yaml_fences(raw)
    try:
        parsed = yaml.safe_load(raw)
        if isinstance(parsed, dict):
            return normalize_enrichment(parsed)
    except Exception as e:
        logger.warning(f"Falha no safe_load do YAML: {e}. Tentando extração resiliente.")

    # Fallback resiliente campo a campo via regex
    fallback = {}
    m_title = re.search(r"^titulo:\s*[\"']?(.*?)[\"']?$", raw, re.MULTILINE | re.IGNORECASE)
    if m_title:
        fallback["titulo"] = m_title.group(1).strip()
    
    m_resumo = re.search(r"^resumo:\s*[\"']?(.*?)[\"']?$", raw, re.MULTILINE | re.IGNORECASE)
    if m_resumo:
        fallback["resumo"] = m_resumo.group(1).strip()
        
    fallback.setdefault("titulo", "Nota de voz")
    fallback.setdefault("tags", [])
    fallback.setdefault("resumo", raw[:500])
    return normalize_enrichment(fallback)

