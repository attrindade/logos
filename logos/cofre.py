"""Cofre do Diário do app Organiza: só o PC abre o que o celular fecha.

O celular cifra cada parte do Diário com a chave pública deste PC (X25519 efêmero → HKDF-SHA256
com info "organiza-cofre-v1" → AES-256-GCM, dados associados = id da parte; ver Cofre.java no
app). A Semana, no meio, guarda só o envelope. A chave privada fica em
state/organiza/cofre.key, fora do git e fora da nuvem; perder o arquivo = os diários ainda na
nuvem não abrem mais (o app avisa e passa a usar a chave nova quando ela for publicada).

    python -m logos.cofre --publicar   cria a chave se faltar e publica a pública na Semana
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import config

INFO = b"organiza-cofre-v1"
MIME = "application/organiza-cofre"


class CofreErro(Exception):
    """O envelope não abre: chave trocada, dado adulterado ou envelope de outra parte."""


def caminho_chave() -> Path:
    return config.STATE_DIR / "organiza" / "cofre.key"


def carregar_ou_criar(path: Path | None = None) -> X25519PrivateKey:
    path = path or caminho_chave()
    if path.exists():
        return X25519PrivateKey.from_private_bytes(path.read_bytes())
    path.parent.mkdir(parents=True, exist_ok=True)
    chave = X25519PrivateKey.generate()
    cru = chave.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                              serialization.NoEncryption())
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(cru)
    tmp.replace(path)
    proteger(path)
    return chave


def proteger(path: Path) -> None:
    """Só o usuário dono lê a chave: tira a herança de permissões da pasta (Windows)."""
    if sys.platform != "win32":
        os.chmod(path, 0o600)
        return
    try:
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:F"],
                       check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        pass  # sem icacls: fica a permissão herdada da pasta do usuário


def publica_b64(chave: X25519PrivateKey) -> str:
    cru = chave.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(cru).decode()


def impressao(pub_b64: str) -> str:
    """Os 8 primeiros bytes do SHA-256 da pública: o app manda junto, para o PC saber a chave."""
    return base64.b64encode(hashlib.sha256(base64.b64decode(pub_b64)).digest()[:8]).decode()


def _chave_simetrica(segredo: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=INFO).derive(segredo)


def abrir(envelope: str | dict, id_parte: str, chave: X25519PrivateKey) -> bytes:
    try:
        env = json.loads(envelope) if isinstance(envelope, str) else envelope
        if not isinstance(env, dict):
            raise ValueError("envelope não é objeto")
    except ValueError as e:
        # Envelope truncado ou vazio é "não abre", nunca "sem conteúdo" (que apagaria os bons).
        raise CofreErro(f"envelope ilegível: {e}") from e
    if env.get("v") != 1:
        raise CofreErro(f"versão de envelope desconhecida: {env.get('v')}")
    minha = impressao(publica_b64(chave))
    if env.get("chave_pc") and env["chave_pc"] != minha:
        raise CofreErro("o envelope foi fechado com outra chave do PC (chave trocada ou de outro PC)")
    try:
        efemera = X25519PublicKey.from_public_bytes(base64.b64decode(env["efemera"]))
        segredo = chave.exchange(efemera)
        return AESGCM(_chave_simetrica(segredo)).decrypt(
            base64.b64decode(env["nonce"]), base64.b64decode(env["ct"]), id_parte.encode("utf-8"))
    except CofreErro:
        raise
    except Exception as e:  # tag errada, base64 ruim, chave ruim: tudo é "não abre"
        raise CofreErro(f"não abre: {type(e).__name__}") from e


def selar(conteudo: bytes, id_parte: str, pub_b64: str) -> dict:
    """O mesmo que o celular faz; usado nos testes para conferir o desenho dos dois lados."""
    pc = X25519PublicKey.from_public_bytes(base64.b64decode(pub_b64))
    efemera = X25519PrivateKey.generate()
    nonce = os.urandom(12)
    ct = AESGCM(_chave_simetrica(efemera.exchange(pc))).encrypt(nonce, conteudo, id_parte.encode("utf-8"))
    return {
        "v": 1,
        "chave_pc": impressao(pub_b64),
        "efemera": publica_b64(efemera),
        "nonce": base64.b64encode(nonce).decode(),
        "ct": base64.b64encode(ct).decode(),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Cofre do Diário do app Organiza.")
    ap.add_argument("--publicar", action="store_true", help="cria a chave se faltar e publica a pública na Semana")
    args = ap.parse_args()
    chave = carregar_ou_criar()
    pub = publica_b64(chave)
    print(f"Chave pública do PC: {pub} (impressão {impressao(pub)})")
    if args.publicar:
        from .organiza import URL, _pedido, chave as chave_semana
        key = chave_semana()
        if not key:
            raise SystemExit("Falta a chave da Semana: SEMANA_KEY ou semana_key em state/secrets.yaml.")
        _pedido("PUT", URL.replace("/capturas", "/linhas"), key, {"chave": "cofre", "dados": {"pub": pub}})
        print("Publicada na Semana (/api/linhas, chave 'cofre').")


if __name__ == "__main__":
    main()
