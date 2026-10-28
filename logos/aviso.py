"""Os avisos do Logos e dos jobs do vault: o registro da aba Logos do Organiza (28/10/2026).

Desde 14/10/2026 o bot do Telegram está desligado e o canal dos avisos é o registro de eventos
(`eventos.py`), que o Vigia leva, cifrado, para a aba Logos do app Organiza. Até aqui isso morava em
`logos/telegram.py` com o nome do bot; agora tem nome próprio. `logos.telegram` reexporta tudo e
`python -m logos.telegram send` continua funcionando (os jobs do vault ainda o chamam). Nada aqui
fala com o Telegram nem lê o `secrets.yaml`: o token do bot pode sumir do disco sem quebrar nada.

    python -m logos.aviso send --text "Job promover ok"
    python -m logos.aviso send --file C:/caminho/Briefing.md
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import eventos

EXIT_OK = 0


def registrar(origem: str, texto: str, nivel: str | None = None) -> None:
    """Uma linha no registro da aba Logos. Sem `nivel`, ele sai dos moldes do texto (`nivel_de`)."""
    eventos.registrar(origem, texto, nivel or eventos.nivel_de(texto))


def notify(text: str) -> bool:
    """Aviso do pipeline (nota pronta, nota degradada, "diário registrado"). Nunca levanta: um aviso
    que derruba o pipeline é pior que a falha que ele anuncia. False quando não deu para registrar."""
    try:
        registrar("logos", text)
        return True
    except Exception:
        return False


def resumo_do_envio(text: str | None, file: Path | None) -> str:
    resumo = (text or "").strip() or (f"Pronto: {file.name}" if file is not None else "")
    if file is not None and text:
        resumo = f"{text.strip()}\n{file.name}"
    return resumo


def send(text: str | None, file: Path | None) -> int:
    """O aviso dos jobs do vault (`_run-claude.ps1`, briefing, promover). O arquivo não sai daqui:
    entra só o nome dele. Repetição (os jobs tentam 3 vezes) não duplica (`eventos.registrar`)."""
    registrar("jobs", resumo_do_envio(text, file))
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m logos.aviso",
                                     description="Avisos do Logos para a aba Logos do Organiza.")
    sub = parser.add_subparsers(dest="command", required=True)
    send_parser = sub.add_parser("send", help="grava um aviso no registro da aba Logos")
    send_parser.add_argument("--text", help="texto do aviso")
    send_parser.add_argument("--file", type=Path, help="arquivo pronto (entra só o nome)")
    args = parser.parse_args(argv)
    if not args.text and not args.file:
        parser.error("informe --text e/ou --file")
    return send(args.text, args.file)


if __name__ == "__main__":
    sys.exit(main())
