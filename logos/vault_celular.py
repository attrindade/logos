"""Cópia cifrada do vault para o app Organiza, no celular (decisão dele em 14/10/2026).

O que vai: as pastas do bot (`consulta.ALLOWED_SUBDIRS`) mais `reference/`, `perf/`, `inbox/` e os
arquivos soltos direto em `work/` (decisões e o Index); `work/archive/` fica de fora, e
`capturas/Diario/` nunca vai, nem que alguém mude as listas (guarda final em `_permitida`).

Cada nota sai fechada com a chave pública do celular (`cofre.selar_para_celular`, só ele abre) e
fica na Semana com um id opaco: HMAC do caminho com uma chave derivada da chave do PC. A nuvem não
vê nem o nome da nota. Só reenvia o que mudou; o que saiu da lista sai da Semana.

Roda junto com o drenador (`python -m logos.organiza`, a cada 10 min) ou sozinho:
`python -m logos.vault_celular --uma-vez`.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import cofre, config, consulta, organiza

logger = logging.getLogger(__name__)

RECURSIVAS = tuple(consulta.ALLOWED_SUBDIRS) + ("reference", "perf", "inbox")
RASAS = ("work",)          # só os .md direto na pasta
NUNCA = ("capturas/Diario", "work/archive")
MAX_NOTA = 1_000_000       # bytes: envelope + base64 cabe no limite da Semana (1,5 MB)
URL = organiza.URL.replace("/capturas", "/vault")


def _permitida(raiz: Path, f: Path) -> bool:
    """Guarda final, por caminho resolvido: dentro do vault, fora do Diário e do arquivo morto."""
    try:
        rel = f.resolve().relative_to(raiz.resolve()).as_posix()
    except (OSError, ValueError):
        return False
    if any(rel == n or rel.startswith(n + "/") for n in NUNCA):
        return False
    if "/diario/" in f"/{rel.lower()}/":
        return False
    return f.suffix == ".md" and f.is_file()


def notas(raiz: Path) -> dict[str, Path]:
    """caminho relativo → arquivo, só o que a lista deixa sair."""
    achadas: dict[str, Path] = {}
    for sub in RECURSIVAS:
        for f in (raiz / sub).rglob("*.md"):
            if _permitida(raiz, f):
                achadas[f.relative_to(raiz).as_posix()] = f
    for sub in RASAS:
        for f in (raiz / sub).glob("*.md"):
            if _permitida(raiz, f):
                achadas[f.relative_to(raiz).as_posix()] = f
    return achadas


def _chave_ids(chave_pc) -> bytes:
    cru = chave_pc.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                 serialization.NoEncryption())
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"organiza-vault-id").derive(cru)


def id_de(chave_ids: bytes, rel: str) -> str:
    return hmac.new(chave_ids, rel.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def versao_de(chave_ids: bytes, conteudo: bytes) -> str:
    return hmac.new(chave_ids, conteudo, hashlib.sha256).hexdigest()[:32]


def passo(key: str, raiz: Path | None = None) -> tuple[int, int] | None:
    """Uma volta: (enviadas, apagadas), ou None se não há chave do celular fixada para usar."""
    pub = organiza.chave_celular(key)
    if not pub:
        return None
    raiz = raiz or consulta.vault_root()
    chave_ids = _chave_ids(cofre.carregar_ou_criar())
    na_nuvem = {i["id"]: i["versao"] for i in organiza._pedido("GET", URL, key).get("itens", [])}
    vistos, enviadas = set(), 0
    for rel, f in sorted(notas(raiz).items()):
        conteudo = f.read_bytes()
        if len(conteudo) > MAX_NOTA:
            logger.warning(f"Vault no celular: {rel} passa de {MAX_NOTA // 1000} KB e ficou de fora")
            continue
        i, v = id_de(chave_ids, rel), versao_de(chave_ids, conteudo)
        vistos.add(i)
        if na_nuvem.get(i) == v:
            continue
        dentro = json.dumps({"caminho": rel, "mtime": int(f.stat().st_mtime),
                             "texto": conteudo.decode("utf-8", errors="replace")}, ensure_ascii=False).encode("utf-8")
        env = cofre.selar_para_celular(dentro, i, pub)
        organiza._pedido("PUT", URL, key, {"id": i, "versao": v, "env": json.dumps(env)})
        enviadas += 1
    apagadas = 0
    for i in set(na_nuvem) - vistos:
        organiza._pedido("DELETE", f"{URL}?id={i}", key)
        apagadas += 1
    return enviadas, apagadas


def main() -> None:
    ap = argparse.ArgumentParser(description="Cópia cifrada do vault para o app Organiza.")
    ap.add_argument("--uma-vez", action="store_true", help="uma volta só")
    ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    key = organiza.chave()
    if not key:
        raise SystemExit("Falta a chave da Semana: SEMANA_KEY ou semana_key em state/secrets.yaml.")
    r = passo(key)
    logger.info("Vault no celular: sem chave do celular fixada" if r is None else f"Vault no celular: {r[0]} enviada(s), {r[1]} apagada(s)")


if __name__ == "__main__":
    main()
