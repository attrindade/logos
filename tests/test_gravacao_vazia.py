"""Toque sem fala não vira nota (LOG 5, 28/10/2026).

Em 25 e 27/10, três notas "Anexo do Telegram" vazias: o filtro de voz (VAD) tirou 100% do áudio
de um toque no microfone e o pacote virou nota mesmo assim, com o título de reserva da era do bot.
"""
import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from logos import config, eventos, organiza
from logos.ledger import Ledger, State
from logos.organiza import write_manifest
from logos.pipeline import process_bundle, read_manifest
from logos.stt import Transcription

NOME = "2026-10-27 14-31-26 app 0b6abafe"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "Inbox")
    monkeypatch.setattr(config, "TRANSCRIPTS_DIR", tmp_path / "Transcripts")
    monkeypatch.setattr(config, "NOTES_DIR", vault / "capturas")
    monkeypatch.setattr(config, "ARCHIVE_DIR", tmp_path / "Archive")
    monkeypatch.setattr(config, "VAULT_DIR", None)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    config.INBOX_DIR.mkdir()
    return Ledger(tmp_path / "ledger.json")


def _pacote(mensagens, **extra) -> Path:
    d = config.INBOX_DIR / NOME
    d.mkdir()
    (d / "01.m4a").write_bytes(b"\x00" * 9000)
    write_manifest(d, {"origem": "organiza", "captura_id": "0b6abafe-1", "aberto_em": "2026-10-27T14:31:26",
                       "mensagens": mensagens, **extra})
    return d


def _rodar(d, ledger, fala=""):
    with patch("logos.pipeline.transcribe_file", return_value=Transcription(fala, 3.0)), \
         patch("logos.pipeline.enrich", return_value={"titulo": "x", "tags": [], "resumo": "."}) as llm, \
         patch("logos.pipeline.aw.post_capture"):
        process_bundle(d, ledger)
    return llm


def _notas():
    return list(config.NOTES_DIR.rglob("*.md")) if config.NOTES_DIR.exists() else []


def test_voz_de_silencio_nao_vira_nota_e_vira_um_evento(ledger):
    d = _pacote([{"tipo": "voz", "arquivo": "01.m4a", "quando": "2026-10-27T14:31:30"}])
    llm = _rodar(d, ledger)
    assert _notas() == []
    llm.assert_not_called()
    evs = eventos.ultimos()
    assert len(evs) == 1 and evs[0]["nivel"] == "info" and evs[0]["texto"].startswith("Gravação vazia descartada")
    _, digest = read_manifest(d)
    assert ledger.is_processed_bundle(NOME, digest)
    assert ledger.get_bundle(NOME, digest).state == State.DESCARTADA
    _rodar(d, ledger)  # a varredura de 10 min passa de novo: nada muda
    assert len(eventos.ultimos()) == 1 and _notas() == []


def test_ideia_so_com_a_palavra_do_tipo_tambem_e_vazia(ledger):
    d = _pacote([{"tipo": "texto", "texto": "Ideia", "quando": "2026-10-27T14:31:26"},
                 {"tipo": "voz", "arquivo": "01.m4a", "quando": "2026-10-27T14:31:30"}])
    _rodar(d, ledger)
    assert _notas() == []


def test_diario_vazio_nao_leva_conteudo_no_evento(ledger):
    d = _pacote([{"tipo": "voz", "arquivo": "01.m4a", "quando": "2026-10-27T22:31:30"}], rota="diario")
    _rodar(d, ledger)
    assert _notas() == []
    assert eventos.ultimos()[0]["texto"] == f"Diário vazio descartado: {NOME} não tinha fala nem texto."


def test_com_fala_segue_o_caminho_normal(ledger):
    d = _pacote([{"tipo": "voz", "arquivo": "01.m4a", "quando": "2026-10-27T14:31:30"}])
    _rodar(d, ledger, fala="comprar fita amanhã cedo")
    assert len(_notas()) == 1


def test_diario_so_com_escalas_continua_registrando(ledger):
    d = _pacote([], rota="diario", formulario={"humor_nota": 3})
    _rodar(d, ledger)
    assert len(_notas()) == 1  # o check-in do formulário vai para a nota do dia


def test_titulos_do_telegram_sairam_do_codigo():
    raiz = Path(__file__).resolve().parent.parent / "logos"
    achados = [f.name for f in raiz.glob("*.py")
               if re.search(r"(Foto|Fotos|Anexo) do Telegram", f.read_text(encoding="utf-8"))]
    assert achados == []


def test_drenador_fecha_o_pacote_descartado(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(organiza, "caminho_chave_existe", lambda: False)
    organiza._RECURSOS.update(ate=0.0, lista=None)
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"x": {"filename": NOME, "state": "descartada", "note_path": ""}}), encoding="utf-8")
    organiza._gravar_entregues({"cid-0000000001": NOME, "cid-0000000002": "diario:" + NOME})
    pedidos = []
    recursos = {"lista": []}

    def falso(metodo, url, key, corpo=None):
        pedidos.append((metodo, corpo))
        if url.endswith("/semana"):
            return {"recursos": recursos["lista"]}
        return {"capturas": []} if "/pendentes" in url else {"ok": True}

    monkeypatch.setattr(organiza, "_pedido", falso)
    # Semana antiga: "nota" com o aviso no título; do Diário, sem título.
    assert organiza.passo("k", tmp_path / "Inbox", tmp_path / "m", ledger) == (0, 2)
    puts = [c for m, c in pedidos if m == "PUT"]
    assert {"id": "cid-0000000001", "estado": "nota", "titulo": "Gravação vazia: nada a guardar"} in puts
    assert {"id": "cid-0000000002", "estado": "nota"} in puts
    # Semana nova: "descartada".
    organiza._gravar_entregues({"cid-0000000003": NOME})
    recursos["lista"] = ["descartada"]
    organiza._RECURSOS.update(ate=0.0, lista=None)
    pedidos.clear()
    assert organiza.passo("k", tmp_path / "Inbox", tmp_path / "m", ledger) == (0, 1)
    assert ("PUT", {"id": "cid-0000000003", "estado": "descartada"}) in pedidos
