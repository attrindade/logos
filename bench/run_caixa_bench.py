"""Conjunto de prova da caixa (spec-captura §6.2; LOG 9 de 28/10/2026). Pronto para quando ele ler as frases.

Gabarito em JSON (lista), um item por frase:
    {"texto": "o que ele disse, certo", "destino": "nota|plano|fim_de_semana|recado",
     "arquivo": "caminho/opcional.m4a", "nomes": ["Osmar"]}
Com `arquivo`, a frase passa antes pelo Whisper (o mesmo do Logos) e mede a taxa de erro por palavra (WER)
do literal e do `texto_limpo`. Sem `arquivo`, o `texto` vai direto ao classificador.

Mede: acerto de destino, erros caros (plano ou recado quando era nota), nomes certos, tempo de cada
chamada (mediana e máximo) e o modelo que de fato respondeu. Não grava nada em state/caixa, não fala
com a Semana e não escreve no vault.

    .venv/Scripts/python.exe bench/run_caixa_bench.py bench/caixa_exemplo.json
    .venv/Scripts/python.exe bench/run_caixa_bench.py gabarito.json --modelo ollama
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logos import caixa  # noqa: E402

CAROS = {("nota", "plano"), ("nota", "recado")}  # (era, saiu)


def _wer(certo: str, saiu: str) -> float | None:
    try:
        import jiwer
    except ImportError:
        return None
    return jiwer.wer(certo.lower(), saiu.lower()) if certo.strip() else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("gabarito", type=Path)
    ap.add_argument("--modelo", choices=["claude", "ollama"], default="claude")
    ap.add_argument("--saida", type=Path, default=None, help="JSON com o resultado de cada frase")
    args = ap.parse_args(argv)
    itens = json.loads(args.gabarito.read_text(encoding="utf-8"))
    nomes = caixa.nomes_do_vault()
    agora = datetime.now()
    chamar = caixa.chamar_claude if args.modelo == "claude" else caixa.chamar_ollama
    linhas, tempos, acertos, caros, nomes_ok, nomes_total, wers = [], [], 0, 0, 0, 0, []
    for n, it in enumerate(itens, 1):
        texto = it.get("texto", "")
        literal = texto
        if it.get("arquivo"):
            from logos.stt import transcribe_file
            literal = transcribe_file(Path(it["arquivo"])).text
        t0 = time.monotonic()
        try:
            bruto, modelo = chamar(caixa.montar_prompt(literal, agora, agora, nomes, []))
            dec = caixa.normalizar(bruto, agora, agora)
        except Exception as e:
            dec, modelo = {"destino": "nota", "texto_limpo": "", "entendi": f"falhou: {e}"}, "nenhum"
        dt = time.monotonic() - t0
        tempos.append(dt)
        certo = dec["destino"] == it["destino"]
        acertos += certo
        caros += (it["destino"], dec["destino"]) in CAROS
        for nome in it.get("nomes") or []:
            nomes_total += 1
            nomes_ok += nome.lower() in (dec.get("texto_limpo") or "").lower()
        if it.get("arquivo"):
            wers.append((_wer(texto, literal), _wer(texto, dec.get("texto_limpo") or "")))
        linhas.append({"n": n, "esperado": it["destino"], "saiu": dec["destino"], "certo": certo,
                       "segundos": round(dt, 1), "modelo": modelo, "entendi": dec.get("entendi")})
        print(f"{n:2}. {dt:5.1f} s {modelo:18} esperado={it['destino']:13} saiu={dec['destino']:13} "
              f"{'ok' if certo else 'ERRO'}")
    total = len(itens) or 1
    print(f"\nacerto de destino: {acertos}/{len(itens)} ({100 * acertos / total:.0f}%) · meta ≥ 85%")
    print(f"erros caros: {caros} ({100 * caros / total:.0f}%) · meta ≤ 5%")
    if nomes_total:
        print(f"nomes no texto_limpo: {nomes_ok}/{nomes_total}")
    if wers:
        lit = [w for w, _ in wers if w is not None]
        lim = [w for _, w in wers if w is not None]
        if lit and lim:
            print(f"WER literal {statistics.mean(lit):.3f} · limpo {statistics.mean(lim):.3f}")
    if tempos:
        print(f"tempo: mediana {statistics.median(tempos):.1f} s · máximo {max(tempos):.1f} s")
    if args.saida:
        args.saida.write_text(json.dumps(linhas, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
