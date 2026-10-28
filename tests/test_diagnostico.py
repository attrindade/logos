"""Rastro das quedas (LOG 2) e logs com limite e acentos (LOG 7), 28/10/2026."""
import faulthandler
import logging
import sys
import threading
from logging.handlers import RotatingFileHandler

import pytest

from logos import config, diagnostico, organiza


@pytest.fixture
def rastro(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", lambda args: None)  # o padrão imprimiria no pytest
    caminho = diagnostico.ligar_rastro("teste", tmp_path)
    yield caminho
    faulthandler.disable()
    f = diagnostico.arquivo_do_rastro()
    diagnostico._RASTRO.update(arquivo=None, processo="")
    if f is not None:
        f.close()


def test_excecao_numa_thread_deixa_linha_no_arquivo(rastro):
    def explode():
        raise RuntimeError("ctranslate2 caiu")

    t = threading.Thread(target=explode, name="transcricao")
    t.start()
    t.join()
    texto = rastro.read_text(encoding="utf-8")
    assert rastro.name == "queda-teste.log"
    assert "teste subiu (PID" in texto
    assert "exceção solta na thread transcricao" in texto and "RuntimeError: ctranslate2 caiu" in texto


def test_excecao_solta_na_principal_tambem(rastro):
    try:
        raise ValueError("sem rede")
    except ValueError:
        sys.excepthook(*sys.exc_info())
    assert "ValueError: sem rede" in rastro.read_text(encoding="utf-8")


def test_faulthandler_escreve_no_mesmo_arquivo(rastro):
    assert faulthandler.is_enabled()
    faulthandler.dump_traceback(file=diagnostico.arquivo_do_rastro())
    diagnostico.arquivo_do_rastro().flush()
    texto = rastro.read_text(encoding="utf-8")
    assert "test_faulthandler_escreve_no_mesmo_arquivo" in texto  # a pilha desta função


def test_log_gira_e_grava_acento(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostico, "MAX_BYTES", 300)
    h = diagnostico.arquivo_de_log(tmp_path / "logs" / "watcher.log")
    assert isinstance(h, RotatingFileHandler) and h.encoding == "utf-8" and h.backupCount == 3
    lg = logging.getLogger("teste-rotacao")
    lg.propagate = False
    lg.addHandler(h)
    try:
        for i in range(30):
            lg.warning(f"entregue à Inbox {i}")
    finally:
        lg.removeHandler(h)
        h.close()
    assert (tmp_path / "logs" / "watcher.log.1").exists()
    assert not (tmp_path / "logs" / "watcher.log.4").exists()
    assert "entregue à Inbox" in (tmp_path / "logs" / "watcher.log").read_text(encoding="utf-8")


def test_drenador_gira_quando_o_stderr_e_outro(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    hs = organiza.handlers_do_log(None)  # pythonw / .vbs novo: stderr não é o organiza.log
    try:
        assert isinstance(hs[0], RotatingFileHandler)
        assert hs[0].baseFilename.endswith("organiza.log") and hs[0].encoding == "utf-8"
    finally:
        for h in hs:
            h.close()


def test_drenador_com_o_vbs_antigo_nao_tenta_girar(tmp_path, monkeypatch):
    # O Organiza.vbs de hoje faz `2>> organiza.log`: o stderr já é o arquivo e girar falharia.
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    with open(tmp_path / "organiza.log", "a", encoding="cp1252") as stderr:
        assert diagnostico.mesmo_arquivo(stderr, tmp_path / "organiza.log")
        hs = organiza.handlers_do_log(stderr)
        assert len(hs) == 1 and type(hs[0]) is logging.StreamHandler
        assert stderr.encoding == "utf-8"  # reconfigurado: acento sai certo
        hs[0].emit(logging.LogRecord("x", logging.INFO, "", 0, "entregue à Inbox", None, None))
    assert "entregue à Inbox" in (tmp_path / "organiza.log").read_text(encoding="utf-8")


def test_vault_no_celular_zero_vai_para_debug(monkeypatch, caplog):
    from logos import vault_celular
    monkeypatch.setattr(vault_celular, "passo", lambda key: (0, 0))
    with caplog.at_level(logging.DEBUG, logger="logos.organiza"):
        organiza.ida_ao_celular("k")
    r = [x for x in caplog.records if "Vault no celular" in x.getMessage()]
    assert r and all(x.levelno == logging.DEBUG for x in r)
    monkeypatch.setattr(vault_celular, "passo", lambda key: (2, 0))
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="logos.organiza"):
        organiza.ida_ao_celular("k")
    assert [x.levelno for x in caplog.records if "Vault no celular" in x.getMessage()] == [logging.INFO]
