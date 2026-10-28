"""Três defeitos medidos em 2026-09-21: temporário de sync virando nota duplicada, gravação
longa caindo na consolidação que devolve YAML inválido, e falha da LLM produzindo uma nota
crua com cara de nota normal."""
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from logos import config
from logos.ledger import Ledger
from logos.llm import is_long_recording, recording_enrichment
from logos.pipeline import process_file
from logos.stt import Transcription
from logos.triage import is_ignored

CONN_ERROR = requests.exceptions.ConnectionError(
    "HTTPConnectionPool(host='localhost', port=11434): Max retries exceeded"
)


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "Inbox")
    monkeypatch.setattr(config, "TRANSCRIPTS_DIR", tmp_path / "Transcripts")
    monkeypatch.setattr(config, "NOTES_DIR", tmp_path / "Notes")
    monkeypatch.setattr(config, "ARCHIVE_DIR", tmp_path / "Archive")
    config.INBOX_DIR.mkdir()
    return config.INBOX_DIR


def _note_of(path: Path) -> str:
    notes = list((config.NOTES_DIR).rglob("*.md"))
    assert len(notes) == 1, notes
    return notes[0].read_text(encoding="utf-8")


# --- D3: temporário do Syncthing ------------------------------------------------

def test_temporario_do_syncthing_e_ignorado():
    # Gerou duplicata real: ~syncthing~2026-09-18 20-39-56.m4a.tmp virou a nota de 21/09.
    assert is_ignored("~syncthing~2026-09-18 20-39-56.m4a.tmp")
    assert is_ignored("2026-09-18 20-39-56.m4a.part")
    assert not is_ignored("2026-09-18 20-39-56.m4a")


def test_temporario_nao_gera_nota(inbox, tmp_path):
    audio = inbox / "~syncthing~2026-09-18 20-39-56.m4a.tmp"
    audio.write_bytes(b"x" * 100)
    ledger = Ledger(tmp_path / "ledger.json")

    with patch("logos.pipeline.transcribe_file") as stt:
        process_file(audio, ledger)

    stt.assert_not_called()
    assert not config.NOTES_DIR.exists() or list(config.NOTES_DIR.rglob("*.md")) == []


# --- D2: gravação longa ---------------------------------------------------------

def test_limiar_de_gravacao_acompanha_o_limiar_de_chunk():
    curto = "palavra " * 100
    longo = "palavra " * (int(config.CHUNK_TOKEN_THRESHOLD / 1.4) + 500)
    assert not is_long_recording(curto)
    assert is_long_recording(longo)


def test_gravacao_longa_nao_chama_o_contrato_de_nota_de_voz(inbox, tmp_path):
    audio = inbox / "2026-09-18 20-39-56.m4a"
    audio.write_bytes(b"x" * 100)
    ledger = Ledger(tmp_path / "ledger.json")
    longo = "Na ultima aventura eu estava no bar com voces. " * 3000

    with patch("logos.pipeline.transcribe_file", return_value=Transcription(text=longo, duration_s=9840.0)), \
         patch("logos.pipeline.enrich") as enrich_mock, \
         patch("logos.llm._ollama_generate", return_value="Sessao de RPG no bar"):
        process_file(audio, ledger)

    enrich_mock.assert_not_called()  # a consolicação que devolvia YAML inválido não roda
    nota = _note_of(audio)
    assert "tipo: gravacao" in nota
    assert "Sessao de RPG no bar" in nota
    assert "## " not in nota.split("---", 2)[2].split("## 🎙️")[0]  # nenhuma seção do contrato


def test_titulo_da_gravacao_cai_nas_primeiras_palavras_sem_ollama():
    longo = "Nesse momento antes da treta acontecer o que tu fez na ultima aventura " * 800
    with patch("logos.llm._ollama_generate", side_effect=CONN_ERROR):
        assert recording_enrichment(longo)["titulo"].startswith("Nesse momento")


# --- D1: falha da LLM fica visível ----------------------------------------------

def test_falha_da_llm_marca_a_nota_como_degradada(inbox, tmp_path):
    audio = inbox / "2026-09-18 11-00-00.m4a"
    audio.write_bytes(b"x" * 100)
    ledger = Ledger(tmp_path / "ledger.json")
    fala = "Preciso lembrar de mandar o contrato para o Renato ainda esta semana. " * 20

    with patch("logos.pipeline.transcribe_file", return_value=Transcription(text=fala, duration_s=90.0)), \
         patch("logos.pipeline.enrich", side_effect=CONN_ERROR), \
         patch("logos.pipeline._notify_degraded") as notify:
        process_file(audio, ledger)

    nota = _note_of(audio)
    assert "status: degradado" in nota
    assert "[!warning] Enriquecimento não aconteceu" in nota
    assert "o serviço provavelmente está parado" in nota
    assert fala.strip()[:40] in nota  # transcrição preservada intocada
    notify.assert_called_once()


def test_nota_degradada_nao_se_chama_pelo_timestamp(inbox, tmp_path):
    # Antes: titulo = path.stem, indistinguível de uma nota normal na listagem.
    audio = inbox / "2026-09-18 20-39-56.m4a"
    audio.write_bytes(b"x" * 100)
    ledger = Ledger(tmp_path / "ledger.json")
    fala = "Combinamos de revisar a proposta antes da reuniao de quinta. " * 20

    with patch("logos.pipeline.transcribe_file", return_value=Transcription(text=fala, duration_s=90.0)), \
         patch("logos.pipeline.enrich", side_effect=CONN_ERROR), \
         patch("logos.pipeline._notify_degraded"):
        process_file(audio, ledger)

    assert "2026-09-18_20-39-56 - 2026-09-18 20-39-56.md" not in [p.name for p in config.NOTES_DIR.rglob("*.md")]


def test_aviso_do_telegram_nunca_derruba_o_pipeline():
    from logos import telegram
    with patch("logos.telegram.BOT_LIGADO", True), \
         patch("logos.telegram.get_token", return_value="t"), \
         patch("logos.telegram.get_owner_chat_id", return_value=1), \
         patch("logos.telegram.call", side_effect=RuntimeError("bot fora do ar")):
        assert telegram.notify("teste") is False


def test_modelo_llm_fica_vazio_quando_o_titulo_nao_passou_pela_llm():
    longo = "Na ultima aventura eu estava no bar com voces. " * 3000
    with patch("logos.llm._ollama_generate", side_effect=CONN_ERROR):
        assert recording_enrichment(longo)["_llm_usado"] is False
    with patch("logos.llm._ollama_generate", return_value="Sessao de RPG no bar"):
        assert recording_enrichment(longo)["_llm_usado"] is True


# --- devolutiva da nota pronta ---------------------------------------------------

def test_nota_pronta_volta_no_chat(inbox, tmp_path):
    audio = inbox / "2026-09-21 09-00-00.m4a"
    audio.write_bytes(b"x" * 100)
    ledger = Ledger(tmp_path / "ledger.json")
    fala = "Preciso mandar a proposta para o Renato ate quinta. " * 20
    enriquecido = {"titulo": "Proposta para o Renato", "tags": ["cliente"],
                   "proxima_acao": "Enviar a proposta ate quinta", "acoes": ["Revisar valores"], "resumo": "."}

    with patch("logos.pipeline.transcribe_file", return_value=Transcription(text=fala, duration_s=60.0)), \
         patch("logos.pipeline.enrich", return_value=enriquecido), \
         patch("logos.aviso.notify") as notify:
        process_file(audio, ledger)

    enviado = notify.call_args[0][0]
    assert "Proposta para o Renato" in enviado
    assert "Enviar a proposta ate quinta" in enviado
    assert "Revisar valores" in enviado


def test_diario_devolve_so_a_confirmacao_sem_conteudo(inbox, tmp_path):
    # O diário é 100% local por decisão registrada; tudo que passa pelo bot passa pela nuvem
    # do Telegram. Nem título, nem ação, nem uma linha da fala podem sair daqui.
    audio = inbox / "2026-09-21 23-00-00.m4a"
    audio.write_bytes(b"x" * 100)
    ledger = Ledger(tmp_path / "ledger.json")
    fala = "Diario. Hoje foi um dia dificil e eu briguei com uma pessoa muito proxima. " * 20
    enriquecido = {"titulo": "Briga com pessoa proxima", "tags": [], "humor": "abalado",
                   "proxima_acao": "Pedir desculpas", "acoes": ["Conversar amanha"], "resumo": "."}

    with patch("logos.pipeline.transcribe_file", return_value=Transcription(text=fala, duration_s=60.0)), \
         patch("logos.pipeline.enrich", return_value=enriquecido), \
         patch("logos.aviso.notify") as notify:
        process_file(audio, ledger)

    enviado = notify.call_args[0][0]
    assert enviado == "Logos: diário registrado."
    for vazado in ("Briga", "proxima", "desculpas", "dificil", "abalado", "Conversar"):
        assert vazado not in enviado


def test_devolutiva_quebrada_nao_derruba_o_pipeline(inbox, tmp_path):
    audio = inbox / "2026-09-21 10-00-00.m4a"
    audio.write_bytes(b"x" * 100)
    ledger = Ledger(tmp_path / "ledger.json")
    fala = "Lembrar de conferir o CI do aluguel finder. " * 20

    with patch("logos.pipeline.transcribe_file", return_value=Transcription(text=fala, duration_s=60.0)), \
         patch("logos.pipeline.enrich", return_value={"titulo": "CI", "tags": [], "resumo": "."}), \
         patch("logos.aviso.notify", side_effect=RuntimeError("bot fora")):
        process_file(audio, ledger)

    assert ledger.get(audio.name, audio.stat().st_size).state == "enriched"
