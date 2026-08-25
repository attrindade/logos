"""Versão enxuta do benchmark: 4 transcritos representativos, 1 repetição por modelo,
mantendo o modelo carregado (keep_alive="5m") durante a rodada — só recarrega ao trocar
de candidato, evitando o desperdício observado na primeira tentativa."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logos import config
from logos.llm import _load_prompt, _ollama_generate, _strip_yaml_fences
from logos.triage import Route
from bench.run_llm_bench import score_response, route_for_transcript

CANDIDATES = [
    "qwen2.5:7b-instruct-q4_K_M",
    "llama3.1:8b-instruct-q4_K_M",
    "qwen2.5:3b-instruct-q4_K_M",
]

# 4 transcritos cobrindo as 3 rotas e a faixa de tamanho real observada (13 a 1267 palavras)
SELECTED = [
    "2026-08-23_22-46-52_Diario.txt",        # 724 palavras
    "2026-08-23_22-52-10_Planejamento.txt",  # 1267 palavras (maior do corpus)
    "2026-08-23_23-41-40_Inbox.txt",         # 148 palavras
    "2026-08-23_23-13-03_Inbox.txt",         # 28 palavras
]


def main():
    transcript_files = [config.TRANSCRIPTS_DIR / name for name in SELECTED]
    missing = [p for p in transcript_files if not p.exists()]
    if missing:
        print(f"Faltando: {missing}")
        sys.exit(1)

    results = {}

    for model in CANDIDATES:
        print(f"=== {model} ===")
        model_results = {"valid_yaml": 0, "list_fields_ok": 0, "english_leak": 0, "only_block": 0, "total": 0, "time_s": 0.0, "errors": 0}

        for i, tpath in enumerate(transcript_files):
            route = route_for_transcript(tpath)
            transcript = tpath.read_text(encoding="utf-8")
            prompt = _load_prompt(route).format(transcript=transcript)

            # mantém carregado entre chamadas do mesmo modelo; descarrega só no último desta rodada
            keep_alive = "0" if i == len(transcript_files) - 1 else "5m"

            t0 = time.time()
            try:
                raw = _ollama_generate(prompt, model=model, keep_alive=keep_alive)
            except Exception as e:
                print(f"  [ERRO] {tpath.name}: {e}")
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
            print(f"  {tpath.name}: {status} | {elapsed:.1f}s")
            print(f"    -> {_strip_yaml_fences(raw)[:200]!r}")

        results[model] = model_results
        print()

    print("=== RESUMO ===")
    for model, r in results.items():
        n = r["total"] or 1
        print(f"{model}:")
        print(f"  YAML válido: {r['valid_yaml']}/{r['total']}")
        print(f"  campos lista corretos: {r['list_fields_ok']}/{r['total']}")
        print(f"  vazamento de inglês: {r['english_leak']}/{r['total']}")
        print(f"  tempo médio/nota: {r['time_s']/n:.1f}s")
        print(f"  erros: {r['errors']}")
        print()


if __name__ == "__main__":
    main()

