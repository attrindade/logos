"""Etapa 4: process_bundle — pacote do Telegram vira uma nota com anexos, sidecars e ledger por hash."""
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from logos import config, telegram
from logos.ledger import Entry, Ledger, State
from logos.pipeline import process_bundle, process_path, read_manifest
from logos.stt import Transcription

NAME = "2026-09-14 15-02-11 tg 7"
ANEXOS = "capturas/anexos/2026/09"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "Inbox")
    monkeypatch.setattr(config, "TRANSCRIPTS_DIR", tmp_path / "Transcripts")
    monkeypatch.setattr(config, "NOTES_DIR", vault / "capturas")
    monkeypatch.setattr(config, "ARCHIVE_DIR", tmp_path / "Archive")
    monkeypatch.setattr(config, "VAULT_DIR", None)
    monkeypatch.setattr(config, "LLM_MIN_WORDS", 40)
    config.INBOX_DIR.mkdir()
    return Ledger(tmp_path / "ledger.json")


def _bundle(mensagens, name=NAME, files=None, **extra) -> Path:
    directory = config.INBOX_DIR / name
    directory.mkdir()
    for filename, data in (files or {}).items():
        (directory / filename).write_bytes(data)
    telegram.write_manifest(directory, {
        "versao": 1, "pacote": name, "chat_id": 42, "aberto_em": "2026-09-14T15:02:11",
        "fechado_em": "2026-09-14T15:15:00", "motivo_fechamento": "nova", "mensagens": mensagens, **extra,
    })
    return directory


def _pdf(text: str) -> bytes:
    fpdf = pytest.importorskip("fpdf")
    pdf = fpdf.FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=11)
    pdf.multi_cell(0, 7, text)
    return bytes(pdf.output())


def _run(path, ledger, enrichment=None, image_text="", link=("", "")):
    with patch("logos.pipeline.enrich", return_value=enrichment or {"titulo": "Visita técnica", "tags": ["climatizando"], "resumo": "r"}) as llm, \
         patch("logos.pipeline.transcribe_file", return_value=Transcription("diário gravado na rua", 7.5)) as stt, \
         patch("logos.extractors.image_text", return_value=image_text), \
         patch("logos.extractors.link_text", return_value=link), \
         patch("logos.pipeline.aw.post_capture") as aw:
        process_bundle(path, ledger)
    return llm, stt, aw


def _note(route):
    [note] = (config.NOTES_DIR / route).glob("*.md")
    text = note.read_text(encoding="utf-8")
    return note, text, yaml.safe_load(text.split("---\n")[1])


def test_texto_foto_e_pdf_viram_uma_nota_com_anexos_e_sidecar(ledger):
    pdf_text = "Relatorio tecnico do Climatizando com medicoes de temperatura. " * 12
    d = _bundle([
        {"message_id": 7, "tipo": "texto", "texto": "planejamento da visita técnica amanhã"},
        {"message_id": 8, "tipo": "foto", "arquivo": "8.jpg", "legenda": "quadro da reunião"},
        {"message_id": 9, "tipo": "documento", "arquivo": "9.pdf", "nome_original": "Relatório.pdf"},
    ], files={"8.jpg": b"\xff\xd8jpeg", "9.pdf": _pdf(pdf_text)})

    llm, stt, aw = _run(d, ledger, image_text="Pauta: orçamento")

    llm.assert_called_once()
    stt.assert_not_called()
    assert "Relatorio tecnico" in llm.call_args.args[0]  # a LLM recebe o texto do PDF
    note, text, fm = _note("Planejamento")
    assert note.name == "Plano — 2026-09-14.md"  # planejamento soma na nota do dia
    assert fm["entradas"] == 1 and fm["horas"] == ["15:02"]
    assert "## 15:02 — entrada 1 · Visita técnica" in text and "via telegram" in text

    anexos_dir = config.NOTES_DIR / "anexos" / "2026" / "09"
    assert (anexos_dir / "2026-09-14_15-02-11_1.jpg").read_bytes() == b"\xff\xd8jpeg"
    sidecar = anexos_dir / "2026-09-14_15-02-11_2.pdf.md"
    assert "Relatorio tecnico do Climatizando" in sidecar.read_text(encoding="utf-8")
    assert not (anexos_dir / "2026-09-14_15-02-11_1.jpg.md").exists()  # OCR curto fica só na nota

    assert f"![[{ANEXOS}/2026-09-14_15-02-11_1.jpg|400]]" in text
    assert f"![[{ANEXOS}/2026-09-14_15-02-11_2.pdf#page=1]]" in text
    assert f"[[{ANEXOS}/2026-09-14_15-02-11_2.pdf.md]]" in text
    assert "[foto 1: Pauta: orçamento] quadro da reunião" in text
    assert "[pdf: Relatório.pdf]" in text

    _, digest = read_manifest(d)
    entry = ledger.get_bundle(NAME, digest)
    assert entry.state == State.ENRICHED and entry.message_ids == [7, 8, 9] and entry.archive_path == ""
    assert aw.call_args.kwargs["tipo"] == "pacote" and aw.call_args.kwargs["canal"] == "telegram"

    llm_again, _, _ = _run(d, ledger)
    llm_again.assert_not_called()  # idempotente pelo hash do manifest
    assert sorted(p.name for p in d.iterdir()) == ["8.jpg", "9.pdf", "manifest.json"]  # Inbox intocada


def test_legenda_e_o_texto_da_triagem_e_nota_curta_pula_a_llm(ledger):
    d = _bundle([{"message_id": 7, "tipo": "foto", "arquivo": "7.jpg", "legenda": "tarefa urgente trocar o filtro"}],
                files={"7.jpg": b"x"})
    llm, _, _ = _run(d, ledger)
    llm.assert_not_called()
    _, _, fm = _note("Inbox")
    assert (fm["categoria"], fm["prioridade"], fm["titulo"]) == ("tarefa", "alta", "tarefa urgente trocar o filtro")


def test_foto_sozinha_ganha_titulo_do_ocr_e_sem_texto_titulo_generico(ledger):
    d = _bundle([{"message_id": 7, "tipo": "foto", "arquivo": "7.jpg"}], files={"7.jpg": b"x"})
    _run(d, ledger, image_text="Nota fiscal 1234 Loja Exemplo")
    _, _, fm = _note("Inbox")
    assert fm["titulo"] == "Nota fiscal 1234 Loja Exemplo"

    d2 = _bundle([{"message_id": 50, "tipo": "foto", "arquivo": "50.jpg"}], name="2026-09-14 16-00-00 tg 50", files={"50.jpg": b"y"})
    _run(d2, ledger, image_text="")
    titles = sorted(yaml.safe_load(p.read_text(encoding="utf-8").split("---\n")[1])["titulo"]
                    for p in (config.NOTES_DIR / "Inbox").glob("*.md"))
    assert "Foto do celular" in titles


def test_voz_no_pacote_e_transcrita_e_decide_a_rota(ledger):
    d = _bundle([{"message_id": 7, "tipo": "voz", "arquivo": "7.oga"}], files={"7.oga": b"OggS"})
    _, stt, aw = _run(d, ledger)
    stt.assert_called_once()
    _, text, fm = _note("Diario")
    assert "[voz 0:07] diário gravado na rua" in text
    assert aw.call_args.kwargs["duracao_audio_s"] == 7.5


def test_localizacao_e_link_com_sidecar(ledger):
    d = _bundle([
        {"message_id": 7, "tipo": "texto", "texto": "ideia ler https://exemplo.com/a", "links": ["https://exemplo.com/a"]},
        {"message_id": 8, "tipo": "localizacao", "local": {"lat": -30.03, "lon": -51.23, "titulo": "Escritório"}},
    ])
    _run(d, ledger, link=("Título do artigo", "Corpo do artigo limpo"))
    _, text, fm = _note("Inbox")
    assert fm["local"] == "-30.03, -51.23" and fm["categoria"] == "ideia"
    assert "openstreetmap.org/?mlat=-30.03&mlon=-51.23" in text
    assert "- 🔗 [Título do artigo](https://exemplo.com/a)" in text
    sidecar = config.NOTES_DIR / "anexos" / "2026" / "09" / "2026-09-14_15-02-11_1.link.md"
    assert "Corpo do artigo limpo" in sidecar.read_text(encoding="utf-8")
    assert fm["anexos"] == [f"{ANEXOS}/2026-09-14_15-02-11_1.link.md"]


def test_md_enviado_como_documento_e_o_texto_da_nota(ledger):
    d = _bundle([{"message_id": 7, "tipo": "documento", "arquivo": "7.md", "nome_original": "pauta.md"}],
                files={"7.md": "reunião com Eduardo sobre a pesquisa".encode("utf-8")})
    _run(d, ledger)
    _, text, fm = _note("Inbox")
    assert fm["categoria"] == "reuniao"
    assert "anexos" not in fm
    assert "reunião com Eduardo sobre a pesquisa" in text


def test_resposta_complementa_a_nota_original(ledger):
    original = config.NOTES_DIR / "Inbox" / "2026-09-10_10-00-00 - Original.md"
    original.parent.mkdir(parents=True)
    original.write_text("---\ntitulo: Original\npromoted:\n  - \"work/x.md\"\n---\n\ncorpo\n", encoding="utf-8")
    ledger.upsert(Entry(filename="2026-09-10 10-00-00 tg 3", size=1, state=State.ENRICHED, route="Inbox",
                        note_path=str(original), manifest_hash="abc", message_ids=[3]))
    d = _bundle([{"message_id": 31, "tipo": "foto", "arquivo": "31.jpg", "reply_to": 3, "legenda": "o recibo"}],
                name="2026-09-14 15-02-11 tg 31", files={"31.jpg": b"img"}, reply_to=3)

    llm, _, _ = _run(d, ledger)

    llm.assert_not_called()
    assert list((config.NOTES_DIR / "Inbox").glob("*.md")) == [original]
    text = original.read_text(encoding="utf-8")
    assert text.startswith("---\ntitulo: Original\npromoted:\n  - \"work/x.md\"\n---\n\ncorpo\n")  # frontmatter intacto
    assert "## 📎 Resposta de 2026-09-14 15:02" in text
    assert f"![[{ANEXOS}/2026-09-14_15-02-11_1.jpg|400]]" in text and "o recibo" in text
    _, digest = read_manifest(d)
    entry = ledger.get_bundle("2026-09-14 15-02-11 tg 31", digest)
    assert entry.note_path == str(original) and entry.message_ids == [31]


def test_resposta_sem_nota_original_vira_nota_nova(ledger):
    d = _bundle([{"message_id": 31, "tipo": "texto", "texto": "ideia solta", "reply_to": 999}], reply_to=999)
    _run(d, ledger)
    _, _, fm = _note("Inbox")
    assert fm["titulo"] == "ideia solta"


def test_falha_no_meio_grava_failed_com_hash_e_retry_reprocessa(ledger):
    d = _bundle([{"message_id": 7, "tipo": "texto", "texto": "ideia"}])
    with patch("logos.pipeline.write_note", side_effect=OSError("disco cheio")), patch("logos.pipeline.aw.post_capture"):
        process_bundle(d, ledger)
    [failed] = ledger.failed_entries()
    assert failed.manifest_hash and failed.message_ids == [7] and "disco cheio" in failed.error
    assert not ledger.is_superseded(failed)

    from logos.pipeline import retry_failed
    with patch.object(config, "INBOX_DIR", d.parent), patch("logos.pipeline.aw.post_capture"):
        retry_failed(ledger)
    _, digest = read_manifest(d)
    assert ledger.is_processed_bundle(NAME, digest)


def test_manifest_ilegivel_nao_toca_no_ledger(ledger):
    d = config.INBOX_DIR / NAME
    d.mkdir()
    (d / "manifest.json").write_text("{ quebrado", encoding="utf-8")
    process_bundle(d, ledger)
    assert ledger.failed_entries() == []


def test_process_path_despacha_por_tipo(ledger):
    comum = config.INBOX_DIR / "fotos"
    comum.mkdir()
    arquivo = config.INBOX_DIR / "2026-09-14 10-00-00.txt"
    arquivo.write_text("oi", encoding="utf-8")
    with patch("logos.pipeline.process_bundle") as bundle, patch("logos.pipeline.process_file") as single:
        process_path(comum, ledger)
        process_path(arquivo, ledger)
    bundle.assert_not_called()
    single.assert_called_once_with(arquivo, ledger)


def test_entrada_antiga_do_ledger_continua_valida(tmp_path):
    ledger = Ledger(tmp_path / "ledger.json")
    old = Entry(**{"filename": "a.m4a", "size": 10, "state": "enriched", "route": "Inbox"})
    assert old.manifest_hash == "" and old.message_ids == []
    ledger.upsert(old)
    assert Ledger.key_for(old) == "a.m4a::10" and ledger.is_processed("a.m4a", 10)
