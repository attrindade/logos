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
import re
import unicodedata
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import cofre, config, consulta, organiza

logger = logging.getLogger(__name__)

RECURSIVAS = tuple(consulta.ALLOWED_SUBDIRS) + ("reference", "perf", "inbox")
RASAS = ("work",)          # só os .md direto na pasta
NUNCA = ("capturas/Diario", "work/archive")
MAX_NOTA = 1_000_000       # bytes de .md
MAX_ENV = 1_450_000        # o envelope pronto, abaixo do limite da Semana (1,5 MB)
URL = organiza.URL.replace("/capturas", "/vault")


def _sem_acento(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii").lower()


def _permitida(raiz: Path, f: Path) -> bool:
    """Guarda final, por caminho RESOLVIDO (um atalho para fora da lista não passa): dentro de uma
    pasta da lista, fora do Diário e do arquivo morto, e nada com "diario" no caminho."""
    try:
        rel = f.resolve().relative_to(raiz.resolve()).as_posix()
    except (OSError, ValueError):
        return False
    if any(_sem_acento(rel).startswith(_sem_acento(n) + "/") for n in NUNCA):
        return False
    partes = _sem_acento(rel).split("/")
    # Pasta chamada Diário em qualquer nível, ou a nota do dia ("Diário — 2026-10-14.md"); uma
    # decisão que só fala do Diário no nome ("Decisão - Exposição do Diário ao MCP") pode ir.
    if "diario" in partes[:-1] or re.match(r"diario\W*\d{4}", partes[-1]):
        return False
    na_lista = any(rel.startswith(r + "/") for r in RECURSIVAS) or any(
        rel.startswith(r + "/") and "/" not in rel[len(r) + 1:] for r in RASAS)
    return na_lista and f.suffix == ".md" and f.is_file()


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


def versao_de(chave_ids: bytes, conteudo: bytes, pub: str = "") -> str:
    """Muda com o conteúdo e com a chave do celular: celular novo recebe tudo de novo."""
    return hmac.new(chave_ids, pub.encode("ascii") + b"\0" + conteudo, hashlib.sha256).hexdigest()[:32]


def passo(key: str, raiz: Path | None = None) -> tuple[int, int] | None:
    """Uma volta: (enviadas, apagadas), ou None se não há chave do celular fixada para usar."""
    pub = organiza.chave_celular(key)
    if not pub:
        return None
    raiz = raiz or consulta.vault_root()
    if not raiz.is_dir():
        logger.warning(f"Vault no celular: {raiz} não está acessível; nada enviado nem apagado")
        return None
    chave_ids = _chave_ids(cofre.carregar_ou_criar())
    na_nuvem = {i["id"]: i["versao"] for i in organiza._pedido("GET", URL, key).get("itens", [])}
    locais = notas(raiz)
    if not locais and na_nuvem:
        # Vault vazio de repente (pasta renomeada, disco fora): não apaga a cópia do celular.
        logger.warning("Vault no celular: nenhuma nota achada; a cópia na Semana fica como está")
        return None
    vistos, enviadas = set(), 0
    for rel, f in sorted(locais.items()):
        i = id_de(chave_ids, rel)
        vistos.add(i)
        try:
            conteudo = f.read_bytes()
            if len(conteudo) > MAX_NOTA:
                logger.warning(f"Vault no celular: uma nota passa de {MAX_NOTA // 1000} KB e ficou de fora")
                continue
            v = versao_de(chave_ids, conteudo, pub)
            if na_nuvem.get(i) == v:
                continue
            texto = conteudo.decode("utf-8")  # nota que não é UTF-8 fica de fora (UnicodeError abaixo)
            dentro = json.dumps({"caminho": rel, "mtime": int(f.stat().st_mtime), "texto": texto},
                                ensure_ascii=False).encode("utf-8")
            env = json.dumps(cofre.selar_para_celular(dentro, i, pub))
            if len(env) > MAX_ENV:
                logger.warning("Vault no celular: uma nota passa do limite da Semana depois de fechada e ficou de fora")
                continue
            organiza._pedido("PUT", URL, key, {"id": i, "versao": v, "env": env})
            enviadas += 1
        except (OSError, UnicodeError, ValueError) as e:  # uma nota ruim não segura as outras
            logger.warning(f"Vault no celular: uma nota ficou de fora ({type(e).__name__})")
    sobra = set(na_nuvem) - vistos
    if len(sobra) > max(20, len(na_nuvem) // 2):
        logger.warning(f"Vault no celular: {len(sobra)} notas sumiriam de uma vez; nada apagado (confira o vault)")
        return enviadas, 0
    apagadas = 0
    for i in sobra:
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
