"""Fase 6: bench de setembro de 2026 (LLM, keep_alive, num_ctx, Whisper, visão).

Uso: python bench/run_bench_2026_09.py <llm|keepalive|numctx|whisper|vision> [modelos...]
Cada medição grava uma linha JSON em bench/tmp/<fase>.jsonl assim que termina, para não
perder horas de CPU se algo cair no meio. `summary` lê os JSONL e imprime as tabelas.

Diferenças em relação a run_llm_bench.py:
- chama o Ollama direto para ler load_duration, prompt_eval_count e eval_count/eval_duration
  e para passar num_ctx e keep_alive explícitos (o config não é tocado);
- o detector de inglês olha só os valores do YAML (o antigo casava a chave `tags:` e dava 4/4);
- entidades são conferidas contra um gabarito por transcrição.
"""
import base64
import json
import re
import statistics
import sys
import time
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logos import config
from logos.llm import _WIKILINK_RE, _known_entities, _load_prompt, _strip_yaml_fences, parse_enrichment
from logos.triage import Route

TMP = Path(__file__).resolve().parent / "tmp"
TIMEOUT_S = 2400
REPEATS = 2

MODELS = ["qwen2.5:3b-instruct-q4_K_M", "gemma4:e2b", "gemma4:e4b", "gemma4:26b"]

SELECTED = [
    "2026-08-23_22-46-52_Diario.txt",        # 724 palavras
    "2026-08-23_22-52-10_Planejamento.txt",  # 1267 palavras
    "2026-08-23_23-41-40_Inbox.txt",         # 148 palavras
    "2026-08-23_23-13-03_Inbox.txt",         # 28 palavras (produção pula a LLM abaixo de 40)
]
LONGEST = "2026-08-23_22-52-10_Planejamento.txt"

# Gabarito. required: tem de aparecer em pessoas/projetos. accepted: aparecer não é erro
# (inferência pedida pelo prompt ou grafia alternativa). forbidden: aparecer é erro grave.
GABARITO = {
    "2026-08-23_22-46-52_Diario.txt": {
        "required": ["Governa Clima", "Lorena", "Eduardo", "Jonas Papaleo",
                     "Rodrigo Bercini Martins", "Vinicius Corbellini"],
        "accepted": ["pesq-bonan", "pesq-manifestacao", "Euzeneia", "attrindade"],
        "forbidden": ["Vini"],  # o Vini do diário é o do clube do livro (ver Gotchas)
    },
    "2026-08-23_22-52-10_Planejamento.txt": {
        "required": ["Eduardo", "Celso", "Lorena", "Bruna", "Vini",
                     "Governa Clima", "PPJ", "pesq-manifestacao", "minimundo"],
        "accepted": ["Renato Barcelos", "Renato Barcelos Advogados", "Instituto Zen",
                     "Instituto Cíclica", "baradv-front", "Euzeneia", "attrindade"],
        "forbidden": ["Vinicius Corbellini"],
    },
    "2026-08-23_23-41-40_Inbox.txt": {
        "required": [],
        "accepted": ["Logos"],
        "forbidden": [],
    },
    "2026-08-23_23-13-03_Inbox.txt": {
        "required": [],
        "accepted": [],
        "forbidden": [],
    },
}

LIST_FIELDS = {
    Route.DIARIO: ("tags", "vitorias", "gratidao", "tensoes", "pontos", "pessoas", "projetos"),
    Route.PLANEJAMENTO: ("tags", "acoes", "decisoes", "bloqueios", "vitorias", "pessoas", "projetos"),
    Route.INBOX: ("tags", "takeaways", "acoes", "pessoas", "projetos"),
}

# palavras funcionais do inglês sem homógrafo comum em pt-BR ("for", "to" e "a" ficam de fora)
_ENGLISH = re.compile(
    r"\b(the|and|of|with|this|that|is|are|was|were|however|therefore|summary|meeting|"
    r"here is|task|tasks|action items?|next step)\b",
    re.IGNORECASE,
)


def route_for(name: str) -> Route:
    stem = Path(name).stem
    if stem.endswith("_Diario"):
        return Route.DIARIO
    if stem.endswith("_Planejamento"):
        return Route.PLANEJAMENTO
    return Route.INBOX


def ollama(prompt: str, model: str, keep_alive: str, num_ctx: int = 8192, images=None, think=None) -> dict:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_ctx": num_ctx},
        "keep_alive": keep_alive,
    }
    if think is not None:  # produção não envia; None reproduz o padrão do modelo
        payload["think"] = think
    if images:
        payload["images"] = images
    t0 = time.time()
    resp = requests.post(f"{config.OLLAMA_HOST}/api/generate", json=payload, timeout=TIMEOUT_S)
    resp.raise_for_status()
    data = resp.json()
    data["_wall_s"] = time.time() - t0
    return data


def unload_all():
    ps = requests.get(f"{config.OLLAMA_HOST}/api/ps", timeout=30).json().get("models", [])
    for m in ps:
        requests.post(f"{config.OLLAMA_HOST}/api/generate",
                      json={"model": m["name"], "keep_alive": 0}, timeout=120)
    for _ in range(60):
        if not requests.get(f"{config.OLLAMA_HOST}/api/ps", timeout=30).json().get("models"):
            return
        time.sleep(1)
    raise RuntimeError("Ollama não descarregou os modelos em 60 s")


def _values_text(obj) -> str:
    if isinstance(obj, dict):
        return " ".join(_values_text(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return " ".join(_values_text(v) for v in obj)
    return "" if obj is None else str(obj)


def _has_nested(value) -> bool:
    return isinstance(value, list) and any(isinstance(v, (list, dict)) for v in value)


def score(raw: str, route: Route, transcript_name: str) -> dict:
    cleaned = _strip_yaml_fences(raw)
    out = {"yaml_ok": False, "list_ok": False, "nested_lists": False, "english": False,
           "english_hits": [], "thought_tags": bool(re.search(r"<\|channel>thought|<think>", raw))}
    try:
        parsed = yaml.safe_load(cleaned)
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        out["yaml_ok"] = True
        fields = LIST_FIELDS[route]
        out["list_ok"] = all(isinstance(parsed.get(f), list) or parsed.get(f) is None for f in fields)
        out["nested_lists"] = any(_has_nested(parsed.get(f)) for f in ("pessoas", "projetos", "tags"))
        text = _values_text(parsed)
    else:
        text = cleaned
    # wikilinks e nomes próprios em inglês (The Movie Chooser) não contam
    text = _WIKILINK_RE.sub(" ", text).replace("The Movie Chooser", " ")
    hits = sorted({m.group(0).lower() for m in _ENGLISH.finditer(text)})
    out["english"] = bool(hits)
    out["english_hits"] = hits

    # entidades avaliadas no caminho de produção (parse_enrichment normaliza grafias)
    enriched = parse_enrichment(raw)
    links = set()
    for f in ("pessoas", "projetos"):
        for item in enriched.get(f, []) or []:
            links.update(m.group(1).strip() for m in _WIKILINK_RE.finditer(str(item)))
    exact, _ = _known_entities()
    known_lower = {k.lower() for k in exact}
    g = GABARITO[transcript_name]
    low = {l.lower() for l in links}
    req = [r for r in g["required"] if r.lower() in low]
    allowed = {x.lower() for x in g["required"] + g["accepted"]}
    out["entities_found"] = sorted(links)
    out["req_hit"] = len(req)
    out["req_total"] = len(g["required"])
    out["forbidden"] = sorted(l for l in links if l.lower() in {x.lower() for x in g["forbidden"]})
    out["unknown"] = sorted(l for l in links if l.lower() not in known_lower)  # fora da whitelist
    out["false_pos"] = sorted(l for l in links if l.lower() in known_lower and l.lower() not in allowed)
    return out


def metrics(data: dict) -> dict:
    ns = 1e9
    ec, ed = data.get("eval_count", 0), data.get("eval_duration", 0)
    pc, pd = data.get("prompt_eval_count", 0), data.get("prompt_eval_duration", 0)
    return {
        "wall_s": round(data["_wall_s"], 2),
        "load_s": round(data.get("load_duration", 0) / ns, 2),
        "total_s": round(data.get("total_duration", 0) / ns, 2),
        "prompt_tokens": pc,
        "prompt_tok_s": round(pc / (pd / ns), 1) if pd else None,
        "eval_tokens": ec,
        "eval_tok_s": round(ec / (ed / ns), 2) if ed else None,
        "thinking_chars": len(data.get("thinking") or ""),
    }


def append(phase: str, row: dict):
    TMP.mkdir(exist_ok=True)
    with open(TMP / f"{phase}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    brief = {k: row[k] for k in row if k not in ("raw", "english_hits", "entities_found")}
    print(json.dumps(brief, ensure_ascii=False), flush=True)


def build_prompt(name: str) -> str:
    text = (config.TRANSCRIPTS_DIR / name).read_text(encoding="utf-8")
    return _load_prompt(route_for(name)).format(transcript=text)


def run_llm(models):
    for model in models:
        unload_all()
        print(f"=== {model} ===", flush=True)
        first = True
        prev = {}
        calls = [(rep, name) for rep in range(REPEATS) for name in SELECTED]
        for i, (rep, name) in enumerate(calls):
            keep = "0" if i == len(calls) - 1 else "10m"
            data = ollama(build_prompt(name), model, keep)
            row = {"model": model, "transcript": name, "rep": rep, "cold": first, **metrics(data)}
            row.update(score(data["response"], route_for(name), name))
            row["raw"] = data["response"]
            append("llm", row)
            first = False
            if name in prev and prev[name] > 0 and row["eval_tok_s"] and row["eval_tok_s"] * 3 < prev[name]:
                print(f"PARADA: {model} ficou 3x mais lento em {name}", flush=True)
                unload_all()
                sys.exit(3)
            prev[name] = row["eval_tok_s"] or 0


def run_think(models):
    """Mesmas 4 notas com think=false: o gemma4 gasta tokens de raciocínio que não aparecem
    na resposta nem no campo `thinking` quando o parâmetro não é enviado."""
    for model in models:
        unload_all()
        calls = [(rep, name) for rep in range(REPEATS) for name in SELECTED]
        for i, (rep, name) in enumerate(calls):
            keep = "0" if i == len(calls) - 1 else "10m"
            data = ollama(build_prompt(name), model, keep, think=False)
            row = {"model": model, "think": False, "transcript": name, "rep": rep, "cold": i == 0,
                   **metrics(data)}
            row.update(score(data["response"], route_for(name), name))
            row["raw"] = data["response"]
            append("think", row)


def run_keepalive():
    model = "gemma4:e4b"
    for keep in ("0", "10m"):
        unload_all()
        t0 = time.time()
        for i, name in enumerate(SELECTED):
            data = ollama(build_prompt(name), model, keep)
            append("keepalive", {"model": model, "keep_alive": keep, "seq": i, "transcript": name,
                                 **metrics(data)})
        append("keepalive", {"model": model, "keep_alive": keep, "seq": "total",
                             "wall_s": round(time.time() - t0, 2)})
    unload_all()


def run_numctx():
    model = "gemma4:e4b"
    prompt = build_prompt(LONGEST)
    for ctx in (4096, 8192):
        unload_all()
        for rep in range(REPEATS + 1):  # rep 0 inclui a carga do runner com o novo contexto
            data = ollama(prompt, model, "10m", num_ctx=ctx)
            row = {"model": model, "num_ctx": ctx, "rep": rep, "warmup": rep == 0, **metrics(data)}
            row.update(score(data["response"], route_for(LONGEST), LONGEST))
            row["done_reason"] = data.get("done_reason")
            row["raw"] = data["response"]
            append("numctx", row)
    unload_all()


def run_whisper(models):
    import jiwer
    from faster_whisper import WhisperModel
    from logos.stt import extract_audio_to_wav

    unload_all()
    norm = jiwer.Compose([jiwer.ToLowerCase(), jiwer.RemovePunctuation(),
                          jiwer.RemoveMultipleSpaces(), jiwer.Strip(), jiwer.ReduceToListOfListOfWords()])
    for size in models:
        t0 = time.time()
        model = WhisperModel(size, device="cpu", compute_type="int8",
                             cpu_threads=config.WHISPER_CPU_THREADS, download_root=config.WHISPER_MODELS_DIR)
        load_s = time.time() - t0
        for i, name in enumerate(SELECTED):
            audio = config.ARCHIVE_DIR / (Path(name).stem + ".m4a")
            wav = extract_audio_to_wav(audio)
            try:
                t1 = time.time()
                segments, info = model.transcribe(str(wav), beam_size=5, language="pt", vad_filter=True)
                text = "".join(s.text for s in segments).strip()
                elapsed = time.time() - t1
            finally:
                wav.unlink(missing_ok=True)
            ref = (config.TRANSCRIPTS_DIR / name).read_text(encoding="utf-8")
            wer = jiwer.wer(ref, text, reference_transform=norm, hypothesis_transform=norm)
            (TMP / f"whisper_{size}_{Path(name).stem}.txt").write_text(text, encoding="utf-8")
            append("whisper", {"model": size, "audio": audio.name, "duration_s": round(info.duration, 1),
                               "load_s": round(load_s, 1) if i == 0 else None,
                               "transcribe_s": round(elapsed, 1), "rtf": round(elapsed / info.duration, 3),
                               "words": len(text.split()), "wer_vs_saved": round(wer, 4)})
        del model


def run_vision(image_path: str):
    model = "gemma4:e4b"
    img = base64.b64encode(Path(image_path).read_bytes()).decode()
    prompt = "Descreva esta captura de tela em português do Brasil, em até 3 frases. Transcreva o título visível, se houver."
    unload_all()
    for rep in range(REPEATS + 1):
        keep = "0" if rep == REPEATS else "10m"
        try:
            data = ollama(prompt, model, keep, images=[img])
        except requests.HTTPError as e:
            append("vision", {"model": model, "rep": rep, "accepted": False, "error": str(e)})
            return
        append("vision", {"model": model, "rep": rep, "accepted": True, **metrics(data),
                          "response_chars": len(data["response"]), "raw": data["response"]})


def _med(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 2) if values else None


def summary():
    rows = [json.loads(l) for l in (TMP / "llm.jsonl").read_text(encoding="utf-8").splitlines()] \
        if (TMP / "llm.jsonl").exists() else []
    by_model = {}
    for r in rows:
        by_model.setdefault(r["model"], []).append(r)
    print("## LLM")
    for model, rs in by_model.items():
        cold = [r for r in rs if r["cold"]]
        warm = [r for r in rs if not r["cold"]]
        print(f"\n{model}: carga={cold[0]['load_s'] if cold else None}s "
              f"tok/s(mediana)={_med([r['eval_tok_s'] for r in rs])} "
              f"yaml={sum(r['yaml_ok'] for r in rs)}/{len(rs)} listas={sum(r['list_ok'] for r in rs)}/{len(rs)} "
              f"aninhadas={sum(r['nested_lists'] for r in rs)} ingles={sum(r['english'] for r in rs)}/{len(rs)} "
              f"thinking_chars={sum(r['thinking_chars'] for r in rs)} tags_thought={sum(r['thought_tags'] for r in rs)}")
        for name in SELECTED:
            rr = [r for r in rs if r["transcript"] == name]
            ww = [r for r in warm if r["transcript"] == name]
            if not rr:
                continue
            print(f"  {name}: wall_quente(med)={_med([r['wall_s'] for r in ww])}s "
                  f"prompt_tok={rr[0]['prompt_tokens']} eval_tok(med)={_med([r['eval_tokens'] for r in rr])} "
                  f"req={[r['req_hit'] for r in rr]}/{rr[0]['req_total']} "
                  f"proibidas={[r['forbidden'] for r in rr]} fora_lista={[r['unknown'] for r in rr]} "
                  f"falso_pos={[r['false_pos'] for r in rr]} ingles={[r['english_hits'] for r in rr]}")
    for phase in ("keepalive", "numctx", "whisper", "vision"):
        p = TMP / f"{phase}.jsonl"
        if p.exists():
            print(f"\n## {phase}")
            for l in p.read_text(encoding="utf-8").splitlines():
                r = json.loads(l)
                r.pop("raw", None)
                r.pop("entities_found", None)
                print(" ", json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "llm":
        run_llm(args or MODELS)
    elif cmd == "think":
        run_think(args or ["gemma4:e2b", "gemma4:e4b", "gemma4:26b"])
    elif cmd == "keepalive":
        run_keepalive()
    elif cmd == "numctx":
        run_numctx()
    elif cmd == "whisper":
        run_whisper(args or ["large-v3-turbo", "medium"])
    elif cmd == "vision":
        run_vision(args[0])
    elif cmd == "summary":
        summary()
    else:
        sys.exit(f"fase desconhecida: {cmd}")
