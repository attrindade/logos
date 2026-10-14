"""Cofre do Diário: o que o celular fecha só abre aqui, e só na parte certa.
A interoperabilidade com o Java (Cofre.java no app) foi conferida no A14 em 14/10/2026."""
import json

import pytest

from logos import cofre


def test_abre_o_que_foi_fechado_para_esta_chave(tmp_path):
    chave = cofre.carregar_ou_criar(tmp_path / "cofre.key")
    env = cofre.selar("o que pesou hoje".encode(), "parte-1", cofre.publica_b64(chave))
    assert "pesou" not in json.dumps(env)
    assert cofre.abrir(json.dumps(env), "parte-1", chave) == "o que pesou hoje".encode()


def test_chave_persiste_e_nao_e_recriada(tmp_path):
    a = cofre.carregar_ou_criar(tmp_path / "cofre.key")
    b = cofre.carregar_ou_criar(tmp_path / "cofre.key")
    assert cofre.publica_b64(a) == cofre.publica_b64(b)
    assert (tmp_path / "cofre.key").stat().st_size == 32


@pytest.mark.parametrize("estrago", ["outra_parte", "outra_chave", "ct", "nonce", "versao"])
def test_recusa_envelope_trocado_ou_adulterado(tmp_path, estrago):
    chave = cofre.carregar_ou_criar(tmp_path / "cofre.key")
    env = cofre.selar(b"x", "parte-1", cofre.publica_b64(chave))
    parte, usar = "parte-1", chave
    if estrago == "outra_parte":
        parte = "parte-2"
    elif estrago == "outra_chave":
        usar = cofre.X25519PrivateKey.generate()
    elif estrago == "ct":
        env["ct"] = ("B" if env["ct"][0] != "B" else "C") + env["ct"][1:]
    elif estrago == "nonce":
        env["nonce"] = ("B" if env["nonce"][0] != "B" else "C") + env["nonce"][1:]
    else:
        env["v"] = 2
    with pytest.raises(cofre.CofreErro):
        cofre.abrir(env, parte, usar)


@pytest.mark.parametrize("ruim", ["", "{trunc", "[]", "null"])
def test_envelope_ilegivel_e_nao_abre_nunca_sem_conteudo(tmp_path, ruim):
    chave = cofre.carregar_ou_criar(tmp_path / "cofre.key")
    with pytest.raises(cofre.CofreErro):
        cofre.abrir(ruim, "parte-1", chave)


def test_para_o_celular_abre_so_com_a_chave_do_celular_e_nunca_como_diario(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    cel = X25519PrivateKey.generate()
    pub_cel = cofre.publica_b64(cel)
    env = cofre.selar_para_celular(b"fila do whatsapp", "w1", pub_cel)
    assert cofre.abrir_do_pc(env, "w1", cel) == b"fila do whatsapp"
    assert env["chave_cel"] == cofre.impressao(pub_cel) and "chave_pc" not in env
    with pytest.raises(Exception):
        cofre.abrir_do_pc(env, "w2", cel)  # outro id
    with pytest.raises(cofre.CofreErro):
        cofre.abrir(env, "w1", cel)  # rótulo do Diário: um sentido não abre no outro
