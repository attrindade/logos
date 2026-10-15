"""O registro do que aconteceu no ecossistema, para a aba Logos do app Organiza (pedido dele em 14/10).

Qualquer processo do PC acrescenta uma linha (`registrar`) em `state/eventos/AAAA-MM-DD.jsonl`, um
arquivo por dia: ninguém reescreve arquivo que outro processo esteja acrescentando, e a limpeza é
apagar os dias velhos. O drenador (`logos/vigia.py`) lê os últimos dias e manda, fechado com a chave
do celular, junto com o estado das peças.

Do Diário entra só o que o bot já mandava ("diário registrado"): nem título, nem uma linha da fala.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

NIVEIS = ("info", "atencao", "falha")
MAX_TEXTO = 4000  # o resumo diário e os avisos dos jobs vinham inteiros pelo Telegram (até 4096)
GUARDA_DIAS = 30


def pasta() -> Path:
    return config.STATE_DIR / "eventos"


REPETIDO_S = 120  # o mesmo texto de novo nesse intervalo é a mesma coisa (os jobs tentam 3 vezes)


def nivel_de(texto: str) -> str:
    """Nível de um aviso que já vinha pronto para o Telegram, pelo que ele diz. Só pelos moldes
    fixos dos avisos: título de nota ("Logos: Erro de cálculo…") não pode virar falha."""
    t = texto.lower()
    if t.startswith("logos:"):
        return "atencao" if "sem enriquecimento" in t.split("\n", 1)[0] else "info"
    if "falhou" in t or t.startswith("falha"):
        return "falha"
    return "info"


def _repetido(p: Path, texto: str, em: float) -> bool:
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4000))
            fim = f.read().decode("utf-8", "ignore").splitlines()
    except OSError:
        return False
    for linha in reversed(fim[-10:]):
        try:
            e = json.loads(linha)
        except ValueError:
            continue
        if em - e.get("em", 0) > REPETIDO_S:
            return False
        if e.get("texto") == texto:
            return True
    return False


def registrar(origem: str, texto: str, nivel: str = "info", quando: float | None = None) -> None:
    """Uma linha no registro. Nunca levanta: o registro não pode derrubar quem o chama."""
    try:
        em = time.time() if quando is None else quando
        texto = str(texto).strip()[:MAX_TEXTO]
        if not texto:
            return
        linha = {
            "em": int(em),
            "origem": str(origem)[:40],
            "texto": texto,
            "nivel": nivel if nivel in NIVEIS else "info",
        }
        p = pasta() / f"{datetime.fromtimestamp(em):%Y-%m-%d}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        if _repetido(p, texto, em):
            return
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(linha, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"Registro de evento falhou: {e}")


def ultimos(dias: int = 3, limite: int = 200, hoje: date | None = None) -> list[dict]:
    """Os eventos dos últimos `dias` (hoje incluído), do mais novo ao mais velho."""
    hoje = hoje or date.today()
    saida: list[dict] = []
    for n in range(dias):
        p = pasta() / f"{hoje - timedelta(days=n):%Y-%m-%d}.jsonl"
        try:
            linhas = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for linha in linhas:
            try:
                e = json.loads(linha)
            except ValueError:
                continue  # linha cortada no meio por uma queda: some
            if isinstance(e, dict) and isinstance(e.get("em"), int) and e.get("texto"):
                saida.append(e)
    saida.sort(key=lambda e: e["em"], reverse=True)
    return saida[:limite]


def limpar(hoje: date | None = None) -> int:
    """Apaga os dias mais velhos que GUARDA_DIAS. Devolve quantos arquivos saíram."""
    limite = (hoje or date.today()) - timedelta(days=GUARDA_DIAS)
    n = 0
    for p in pasta().glob("*.jsonl"):
        try:
            if date.fromisoformat(p.stem) < limite:
                p.unlink()
                n += 1
        except (ValueError, OSError):
            continue
    return n


def main(argv: list[str] | None = None) -> int:
    """`python -m logos.eventos registrar --origem assistente --texto "..."`: o que antes ia pelo
    `python -m logos.telegram send` (o Assistente e o aw-tools avisavam por ali até 14/10)."""
    import argparse
    p = argparse.ArgumentParser(prog="python -m logos.eventos")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("registrar", help="acrescenta uma linha ao registro da aba Logos")
    r.add_argument("--origem", required=True)
    r.add_argument("--texto", required=True)
    r.add_argument("--nivel", choices=NIVEIS, default=None)
    args = p.parse_args(argv)
    registrar(args.origem, args.texto, args.nivel or nivel_de(args.texto))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
