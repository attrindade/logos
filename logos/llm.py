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

    raw = _strip_yaml_fences(raw)
    try:
        parsed = yaml.safe_load(raw)
        if not isinstance(parsed, dict):
            raise ValueError("YAML não é um dict")
        return parsed
    except Exception as e:
        logger.warning(f"Falha ao parsear YAML da LLM: {e}. Resposta bruta preservada.")
        return {"titulo": "(falha ao estruturar)", "tags": [], "resumo": raw[:500]}

