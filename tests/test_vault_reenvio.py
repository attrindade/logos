"""LOG 6 (28/10/2026): "Vault no celular: 1 enviada(s)" a cada volta, desde 21/10.

Achado: a comparação de versão está certa (com o vault real, o ensaio só de leitura de 28/10 deu 275
locais, 275 na nuvem, 0 diferentes). A nota reenviada era `inbox/sessoes-<dia>.md`, a trilha que o
hook Stop do Claude Code reescreve a cada resposta: cada envio era uma mudança de verdade. Aqui: a
comparação, o caminho no DEBUG e a chave `vault_celular_fora` (vazia de fábrica).
"""
import logging

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from logos import cofre, config, organiza, vault_celular


def test_versao_so_muda_com_o_conteudo_ou_com_o_celular():
    ids = b"k" * 32
    a, b = cofre.publica_b64(X25519PrivateKey.generate()), cofre.publica_b64(X25519PrivateKey.generate())
    v = vault_celular.versao_de(ids, "texto".encode(), a)
    assert v == vault_celular.versao_de(ids, "texto".encode(), a)       # mesmo conteúdo: igual
    assert v != vault_celular.versao_de(ids, "texto.".encode(), a)      # um ponto a mais: muda
    assert v != vault_celular.versao_de(ids, "texto".encode(), b)       # celular novo: muda


def _ambiente(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    v = tmp_path / "vault"
    for rel in ("brain/Gotchas.md", "inbox/sessoes-2026-10-28.md"):
        (v / rel).parent.mkdir(parents=True, exist_ok=True)
        (v / rel).write_text(f"conteúdo de {rel}", encoding="utf-8")
    cel = X25519PrivateKey.generate()
    monkeypatch.setattr(organiza, "chave_celular", lambda k: cofre.publica_b64(cel))
    nuvem = {}

    def falso(metodo, url, key, corpo=None):
        if metodo == "GET":
            return {"itens": [{"id": i, "versao": ver} for i, ver in nuvem.items()]}
        if metodo == "PUT":
            nuvem[corpo["id"]] = corpo["versao"]
        if metodo == "DELETE":
            nuvem.pop(url.split("id=")[1])
        return {"ok": True}

    monkeypatch.setattr(organiza, "_pedido", falso)
    return v, nuvem


def test_trilha_de_sessoes_reescrita_e_a_unica_reenviada_e_o_debug_diz_qual(tmp_path, monkeypatch, caplog):
    v, _ = _ambiente(tmp_path, monkeypatch)
    assert vault_celular.passo("k", v) == (2, 0)
    for n in range(3):  # o hook reescreve a linha da sessão a cada resposta
        (v / "inbox/sessoes-2026-10-28.md").write_text(f"- 11:{n}4 · sessão", encoding="utf-8")
        with caplog.at_level(logging.DEBUG, logger="logos.vault_celular"):
            assert vault_celular.passo("k", v) == (1, 0)
    assert "enviada inbox/sessoes-2026-10-28.md (mudou)" in caplog.text
    assert "Gotchas" not in caplog.text
    assert vault_celular.passo("k", v) == (0, 0)  # sem mudança, zero reenvio


def test_chave_vault_celular_fora_tira_a_trilha_do_celular(tmp_path, monkeypatch):
    v, nuvem = _ambiente(tmp_path, monkeypatch)
    assert vault_celular.passo("k", v) == (2, 0)
    monkeypatch.setattr(config, "VAULT_CELULAR_FORA", ["inbox/sessoes-"])
    assert set(vault_celular.notas(v)) == {"brain/Gotchas.md"}
    assert vault_celular.passo("k", v) == (0, 1)  # a cópia dela no celular sai
    (v / "inbox/sessoes-2026-10-28.md").write_text("outra", encoding="utf-8")
    assert vault_celular.passo("k", v) == (0, 0)
    assert len(nuvem) == 1
