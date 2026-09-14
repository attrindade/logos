"""Etapa 4: extração de texto de anexos. Os testes com Tesseract real são pulados sem ele."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from logos import config, extractors

REAL_TESSDATA = Path(r"C:\Users\andre\Logos\models\tessdata")
FONT = Path(r"C:\Windows\Fonts\arial.ttf")
has_tesseract = pytest.mark.skipif(
    not (config.TESSERACT_CMD and (REAL_TESSDATA / "por.traineddata").exists() and FONT.exists()),
    reason="Tesseract com por.traineddata e fonte Arial necessários",
)


@pytest.fixture
def tessdata(monkeypatch):
    monkeypatch.setattr(config, "TESSDATA_DIR", REAL_TESSDATA)


def _text_image(path: Path, text: str) -> Path:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1400, 220), "white")
    ImageDraw.Draw(img).text((40, 60), text, fill="black", font=ImageFont.truetype(str(FONT), 64))
    img.save(path)
    return path


def test_pdf_com_camada_de_texto(tmp_path):
    fpdf = pytest.importorskip("fpdf")
    pdf = fpdf.FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=12)
    pdf.multi_cell(0, 8, "Proposta tecnica do Climatizando para o segundo semestre.")
    path = tmp_path / "a.pdf"
    pdf.output(str(path))
    with patch.object(extractors, "_ocrmypdf_text") as ocr:
        assert "Proposta tecnica do Climatizando" in extractors.pdf_text(path)
    ocr.assert_not_called()


def test_docx_com_paragrafos_e_tabela(tmp_path):
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph("Ata da reunião de orçamento")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text, table.rows[0].cells[1].text = "Item", "R$ 1.200"
    path = tmp_path / "ata.docx"
    document.save(path)
    text = extractors.document_text(path)
    assert "Ata da reunião de orçamento" in text and "Item | R$ 1.200" in text


def test_texto_simples_com_bom(tmp_path):
    path = tmp_path / "n.md"
    path.write_bytes("\ufeff# Título\ncorpo".encode("utf-8"))
    assert extractors.plain_text(path) == "# Título\ncorpo"


def test_extratores_nunca_lancam(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TESSERACT_CMD", "")
    lixo = tmp_path / "lixo.pdf"
    lixo.write_bytes(b"isto nao e um pdf")
    assert extractors.pdf_text(lixo) == ""
    assert extractors.docx_text(tmp_path / "nao-existe.docx") == ""
    assert extractors.ocr_image(tmp_path / "x.jpg") == ""
    assert extractors.document_text(tmp_path / "x.xyz") == ""


def test_link_com_trafilatura(monkeypatch):
    pytest.importorskip("trafilatura")
    monkeypatch.setattr(extractors.shutil, "which", lambda name: None)  # sem defuddle
    html = (
        "<html><head><title>Clima em Porto Alegre</title></head><body><article>"
        "<h1>Clima em Porto Alegre</h1>"
        + "<p>A frente fria chega na quinta-feira com chuva forte e queda de temperatura em toda a região metropolitana.</p>" * 6
        + "</article></body></html>"
    )
    resp = MagicMock(text=html)
    with patch.object(extractors.requests, "get", return_value=resp) as get:
        title, text = extractors.link_text("https://exemplo.com/clima")
    assert get.call_args.kwargs["timeout"] == extractors.LINK_TIMEOUT_S
    assert title == "Clima em Porto Alegre"
    assert "frente fria" in text


def test_link_que_falha_devolve_vazio(monkeypatch):
    monkeypatch.setattr(extractors.shutil, "which", lambda name: None)
    with patch.object(extractors.requests, "get", side_effect=extractors.requests.ConnectionError("offline")):
        assert extractors.link_text("https://exemplo.com") == ("", "")


def test_foto_sem_texto_cai_para_descricao_por_visao(tmp_path):
    with patch.object(extractors, "ocr_image", return_value="  "), \
         patch.object(extractors, "describe_image", return_value="Um quadro branco com um diagrama."):
        assert extractors.image_text(tmp_path / "f.jpg") == "(descrição automática) Um quadro branco com um diagrama."


def test_visao_desligada_nao_chama_o_ollama(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VISION_MODEL", "")
    with patch("logos.llm._ollama_generate") as gen:
        assert extractors.describe_image(tmp_path / "f.jpg") == ""
    gen.assert_not_called()


def test_visao_manda_a_imagem_em_base64(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VISION_MODEL", "gemma4:e4b")
    img = tmp_path / "f.png"
    img.write_bytes(b"\x89PNG")
    with patch("logos.llm._ollama_generate", return_value=" descrição ") as gen:
        assert extractors.describe_image(img) == "descrição"
    assert gen.call_args.kwargs == {"model": "gemma4:e4b", "images": ["iVBORw=="]}


@has_tesseract
def test_ocr_real_em_portugues(tmp_path, tessdata):
    img = _text_image(tmp_path / "recibo.png", "Reunião de orçamento às 14h")
    text = extractors.ocr_image(img)
    assert "orçamento" in text.lower() and "reunião" in text.lower()


@has_tesseract
def test_pdf_escaneado_passa_pelo_ocrmypdf(tmp_path, tessdata):
    from PIL import Image

    png = _text_image(tmp_path / "scan.png", "Contrato de locacao assinado")
    pdf = tmp_path / "scan.pdf"
    Image.open(png).convert("RGB").save(pdf, "PDF", resolution=150)
    text = extractors.pdf_text(pdf)
    assert "contrato" in text.lower() and "locacao" in text.lower()
