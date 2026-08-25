"""Fase 3: benchmark dos candidatos de LLM sobre os transcritos já salvos.

Roda cada candidato 3x sobre cada transcrito e pontua:
(a) YAML válido? (b) português sem vazar inglês? (c) só o bloco pedido? (d) segundos/nota.
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logos import config
from logos.llm import _ollama_generate, _strip_yaml_fences, _load_prompt
from logos.triage import Route
import yaml

CANDIDATES = [
    "qwen2.5:7b-instruct-q4_K_M",
    "llama3.1:8b-instruct-q4_K_M",
    "qwen2.5:3b-instruct-q4_K_M",
]
REPEATS = 3

# heurística simples para detectar vazamento de inglês: palavras funcionais comuns em inglês
_ENGLISH_MARKERS = re.compile(
    r"\b(the|and|summary|title|tags|here is|note:|however|therefore)\b", re.IGNORECASE
)


def route_for_transcript(path: Path) -> Route:
    name = path.stem
    if name.endswith("_Diario"):
        return Route.DIARIO
    if name.endswith("_Planejamento"):
        return Route.PLANEJAMENTO
    return Route.INBOX


def score_response(raw: str) -> dict:
    cleaned = _strip_yaml_fences(raw)
    valid_yaml = False
    is_dict = False
    list_fields_ok = False
    try:
        parsed = yaml.safe_load(cleaned)
        valid_yaml = True
        is_dict = isinstance(parsed, dict)
        if is_dict:
            # campos que deveriam ser lista (tags/pontos/acoes) mas o modelo pode devolver
            # como string "a, b, c" — falha de aderência a formato, não de YAML em si.
            list_fields_ok = all(
                isinstance(parsed.get(f), list) or parsed.get(f) is None
                for f in ("tags", "pontos", "acoes")
            )
    except Exception:
        pass

    english_leak = bool(_ENGLISH_MARKERS.search(cleaned))
    only_block = cleaned.strip() == raw.strip()  # não adicionou texto fora do bloco (fences removidas = ok)

    return {
        "valid_yaml": valid_yaml and is_dict,
        "list_fields_ok": list_fields_ok,
        "english_leak": english_leak,
        "only_block": True if cleaned != raw else only_block,
    }


def main():
    transcript_files = sorted(config.TRANSCRIPTS_DIR.glob("*.txt"))
    transcript_files = [p for p in transcript_files if p.read_text(encoding="utf-8").strip()]
    print(f"{len(transcript_files)} transcritos não vazios para o benchmark.\n")

    results = {}

    for model in CANDIDATES:
        print(f"=== {model} ===")
        model_results = {"valid_yaml": 0, "list_fields_ok": 0, "english_leak": 0, "only_block": 0, "total": 0, "time_s": 0.0, "errors": 0}

        for tpath in transcript_files:
            route = route_for_transcript(tpath)
            transcript = tpath.read_text(encoding="utf-8")
            prompt_template = _load_prompt(route)
            prompt = prompt_template.format(transcript=transcript)

            for rep in range(REPEATS):
                t0 = time.time()
                try:
                    raw = _ollama_generate(prompt, model=model)
                except Exception as e:
                    print(f"  [ERRO] {tpath.name} rep{rep}: {e}")
                    model_results["errors"] += 1
                    continue
                elapsed = time.time() - t0

                scores = score_response(raw)
                model_results["total"] += 1
                model_results["time_s"] += elapsed
                model_results["valid_yaml"] += int(scores["valid_yaml"])
                model_results["list_fields_ok"] += int(scores["list_fields_ok"])
                model_results["english_leak"] += int(scores["english_leak"])
                model_results["only_block"] += int(scores["only_block"])

                status = "OK" if scores["valid_yaml"] else "YAML-INVALIDO"
                print(f"  {tpath.name} rep{rep}: {status} | {elapsed:.1f}s")

        results[model] = model_results
        print()

    print("=== RESUMO ===")
    for model, r in results.items():
        n = r["total"] or 1
        avg_time = r["time_s"] / n
        print(f"{model}:")
        print(f"  YAML válido: {r['valid_yaml']}/{r['total']}")
        print(f"  campos lista corretos (tags/pontos/acoes): {r['list_fields_ok']}/{r['total']}")
        print(f"  vazamento de inglês: {r['english_leak']}/{r['total']}")
        print(f"  só o bloco pedido: {r['only_block']}/{r['total']}")
        print(f"  tempo médio/nota: {avg_time:.1f}s")
        print(f"  erros: {r['errors']}")
        print()


if __name__ == "__main__":
    main()

