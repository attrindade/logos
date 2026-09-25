"""Nota por dia do Diário e do Planejamento: entradas que se somam, consolidado, integrações."""
import os
import time
from datetime import date, datetime
from unittest.mock import patch

import pytest
import yaml

from logos import config, dia, telegram
from logos.ledger import Ledger
from logos.pipeline import process_bundle, process_file
from logos.stt import Transcription
from logos.triage import Route

HOJE = date(2026, 9, 24)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    raiz = tmp_path / "vault"
    monkeypatch.setattr(config, "VAULT_DIR", raiz)
    monkeypatch.setattr(config, "NOTES_DIR", raiz / "capturas")
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "Inbox")
    monkeypatch.setattr(config, "TRANSCRIPTS_DIR", tmp_path / "Transcripts")
    monkeypatch.setattr(config, "ARCHIVE_DIR", tmp_path / "Archive")
    monkeypatch.setattr(config, "LLM_MIN_WORDS", 40)
    monkeypatch.setattr(config, "VIRADA_DO_DIA_H", 4)
    config.INBOX_DIR.mkdir()
    return raiz


def _entrada(fonte, hora="21:10", formulario=None, **enr):
    return {"fonte": fonte, "hora": hora, "momento": f"2026-09-24T{hora}:00", "origem": "telegram",
            "formulario": formulario or {}, "enriquecimento": enr}


def _ler(caminho):
    texto = caminho.read_text(encoding="utf-8")
    return texto, yaml.safe_load(texto.split("---\n")[1])


# --- dia.py ---------------------------------------------------------------------

def test_duas_entradas_no_mesmo_dia_viram_uma_nota(vault):
    r1 = dia.registrar_entrada(Route.PLANEJAMENTO, HOJE, _entrada("a", "08:10", {"foco": "[[PPJ]]"},
                               titulo="Manhã", acoes=["Revisar SIOP"], proxima_acao="Abrir o SIOP"), "fala 1")
    r2 = dia.registrar_entrada(Route.PLANEJAMENTO, HOJE, _entrada("b", "13:05", {"energia": 4},
                               titulo="Tarde", acoes=["revisar siop", "Ligar para a Lorena"]), "fala 2")

    assert r1.caminho == r2.caminho and r1.caminho.name == "Plano — 2026-09-24.md"
    assert (r1.numero, r2.numero, r2.total) == (1, 2, 2)
    texto, fm = _ler(r2.caminho)
    assert fm["entradas"] == 2 and fm["horas"] == ["08:10", "13:05"]
    assert fm["foco"] == "[[PPJ]]" and fm["energia"] == 4 and fm["para"] == "2026-09-24"
    assert "## 08:10 — entrada 1 · Manhã" in texto and "## 13:05 — entrada 2 · Tarde" in texto
    consolidado = texto.split(dia.CONS_INI)[1].split(dia.CONS_FIM)[0]
    assert consolidado.count("- [ ] Revisar SIOP") == 1 and "revisar siop" not in consolidado  # sem repetir
    assert "- [ ] Ligar para a Lorena" in consolidado and "- [ ] Abrir o SIOP ⏫" in consolidado
    assert "fala 1" in texto and "fala 2" in texto


def test_regravar_nao_acumula_linhas_em_branco(vault):
    for i in range(4):
        r = dia.registrar_entrada(Route.DIARIO, HOJE, _entrada(f"f{i}", titulo=f"t{i}"), "x")
    texto = r.caminho.read_text(encoding="utf-8")
    assert "---\n\n# Diário de qui 24/09" in texto and "\n\n\n" not in texto


def test_mesma_fonte_substitui_em_vez_de_somar(vault):
    dia.registrar_entrada(Route.DIARIO, HOJE, _entrada("pacote", titulo="Primeira versão"), "x")
    r = dia.registrar_entrada(Route.DIARIO, HOJE, _entrada("pacote", titulo="Reprocessada"), "x")
    texto, fm = _ler(r.caminho)
    assert r.numero == 1 and fm["entradas"] == 1
    assert "Reprocessada" in texto and "Primeira versão" not in texto


def test_madrugada_conta_para_o_dia_anterior(vault):
    assert dia.dia_logico(datetime(2026, 9, 25, 1, 30)) == HOJE
    assert dia.dia_logico(datetime(2026, 9, 25, 4, 0)) == date(2026, 9, 25)


def test_edicao_a_mao_sobrevive_a_entrada_nova(vault):
    r = dia.registrar_entrada(Route.PLANEJAMENTO, HOJE, _entrada("a", acoes=["Enviar proposta", "Revisar"],
                              pessoas=["[[Vini]]", "[[Lorena]]"]), "x")
    texto = r.caminho.read_text(encoding="utf-8")
    texto = texto.replace("- [ ] Enviar proposta", "- [x] Enviar proposta")
    texto = texto.replace("- '[[Vini]]'\n", "")  # correção à mão: não era o Vini
    texto = texto.replace("---\n\n", "promoted:\n- work/active/PPJ.md\ncorrecoes:\n- tirei o Vini\n---\n\n", 1)
    r.caminho.write_text(texto, encoding="utf-8")

    dia.registrar_entrada(Route.PLANEJAMENTO, HOJE, _entrada("b", "22:00", acoes=["Dormir cedo"]), "y")

    texto, fm = _ler(r.caminho)
    assert fm["promoted"] == ["work/active/PPJ.md"] and fm["correcoes"] == ["tirei o Vini"]
    assert fm["pessoas"] == ["[[Lorena]]"]
    assert "- [x] Enviar proposta" in texto and "- [ ] Dormir cedo" in texto


def test_entrada_nova_acrescenta_pessoa_sem_desfazer_as_outras(vault):
    dia.registrar_entrada(Route.DIARIO, HOJE, _entrada("a", pessoas=["[[Lorena]]"]), "x")
    r = dia.registrar_entrada(Route.DIARIO, HOJE, _entrada("b", pessoas=["[[Eduardo]]", "[[Lorena]]"]), "y")
    assert _ler(r.caminho)[1]["pessoas"] == ["[[Lorena]]", "[[Eduardo]]"]


def test_comentario_apagado_nao_refaz_o_consolidado(vault):
    r = dia.registrar_entrada(Route.PLANEJAMENTO, HOJE, _entrada("a", acoes=["Ação do dia"]), "x")
    dia.registrar_entrada(Route.PLANEJAMENTO, HOJE, _entrada("b", acoes=["Outra"]), "y")
    texto = r.caminho.read_text(encoding="utf-8")
    linhas = [l for l in texto.splitlines() if not (l.startswith("<!-- logos:entrada") and '"fonte": "a"' in l)]
    r.caminho.write_text("\n".join(linhas) + "\n", encoding="utf-8")

    r3 = dia.registrar_entrada(Route.PLANEJAMENTO, HOJE, _entrada("c", acoes=["Terceira"]), "z")

    texto, fm = _ler(r.caminho)
    assert r3.aviso and fm["entradas"] == 3 and r3.numero == 3
    consolidado = texto.split(dia.CONS_INI)[1].split(dia.CONS_FIM)[0]
    assert "Ação do dia" in consolidado and "Terceira" not in consolidado


def test_lock_segura_a_segunda_escrita_e_vencido_e_retomado(vault, monkeypatch):
    lock = config.STATE_DIR / "dias" / f"Diario-{HOJE.isoformat()}.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("123")
    monkeypatch.setattr(dia, "LOCK_ESPERA_S", 0.3)
    with pytest.raises(dia.DiaError):
        dia.registrar_entrada(Route.DIARIO, HOJE, _entrada("a"), "x")

    velho = time.time() - dia.LOCK_VENCE_S - 10
    os.utime(lock, (velho, velho))
    r = dia.registrar_entrada(Route.DIARIO, HOJE, _entrada("a"), "x")
    assert r.caminho.exists() and not lock.exists()


def test_acao_marcada_na_nota_de_projeto_nao_volta_como_pendente(vault):
    projeto = vault / "work" / "active" / "PPJ.md"
    projeto.parent.mkdir(parents=True)
    projeto.write_text("# PPJ\n\n- [x] Revisar o SIOP\n- [ ] Ligar para a Julia\n", encoding="utf-8")
    r = dia.registrar_entrada(Route.PLANEJAMENTO, HOJE, _entrada(
        "a", acoes=["Revisar o SIOP", "Ligar para a Julia", "Comprar café"]), "x")
    texto = r.caminho.read_text(encoding="utf-8").replace("---\n\n", "promoted:\n- '[[PPJ]]'\n---\n\n", 1)
    texto = texto.replace("- [ ] Comprar café", "- [x] Comprar café")
    r.caminho.write_text(texto, encoding="utf-8")

    assert dia.acoes_pendentes(HOJE) == ["Ligar para a Julia"]


def test_diario_so_devolve_o_veredito_para_o_plano(vault):
    dia.registrar_entrada(Route.PLANEJAMENTO, HOJE, _entrada("p", acoes=["x"], intencao="entregar"), "")
    dia.marcar_cumprido(HOJE, "parcial")
    texto, fm = _ler(dia.caminho_nota(Route.PLANEJAMENTO, HOJE))
    assert fm["cumprido"] == "parcial" and "em parte (conferido no diário)" in texto
    assert dia.plano_de(HOJE)["intencao"] == "entregar"


def test_amanha_do_diario(vault):
    dia.registrar_entrada(Route.DIARIO, HOJE, _entrada("a", amanha="começar cedo"), "")
    dia.registrar_entrada(Route.DIARIO, HOJE, _entrada("b", "23:00", amanha="sem celular na cama"), "")
    assert dia.amanha_do_diario(HOJE) == "começar cedo · sem celular na cama"


def test_comentario_nao_quebra_com_seta_no_texto(vault):
    r = dia.registrar_entrada(Route.DIARIO, HOJE, _entrada("a", pontos=["a --> b"]), "")
    assert dia.ler_entradas(Route.DIARIO, HOJE)[0]["enriquecimento"]["pontos"] == ["a --> b"]
    assert "a --> b" in r.caminho.read_text(encoding="utf-8")


# --- pipeline -------------------------------------------------------------------

def _pacote(nome, mensagens, files=None, **extra):
    pasta = config.INBOX_DIR / nome
    pasta.mkdir()
    for arquivo, dados in (files or {}).items():
        (pasta / arquivo).write_bytes(dados)
    telegram.write_manifest(pasta, {"versao": 2, "pacote": nome, "chat_id": 42, "aberto_em": "2026-09-24T21:40:00",
                                    "mensagens": mensagens, **extra})
    return pasta


def _rodar(pasta, ledger, enrichment=None, notify=None):
    with patch("logos.pipeline.enrich", return_value=enrichment or {"titulo": "Entrada", "tags": []}) as llm, \
         patch("logos.pipeline.transcribe_file", return_value=Transcription("fala", 3.0)), \
         patch("logos.extractors.document_text", return_value="texto do pdf"), \
         patch("logos.pipeline.aw.post_capture"), \
         patch("logos.telegram.notify") as avisos:
        process_bundle(pasta, ledger)
    return llm, avisos


def test_rota_do_manifest_vence_a_primeira_palavra_e_agrupa_por_pergunta(vault, tmp_path):
    ledger = Ledger(tmp_path / "ledger.json")
    longo = "palavra " * 45
    pasta = _pacote("2026-09-24 21-40-00 tg 7", [
        {"message_id": 8, "tipo": "texto", "texto": "ideia solta antes"},
        {"message_id": 9, "tipo": "texto", "texto": "o deploy travou " + longo, "pergunta": 2},
    ], rota="diario", dia="2026-09-24", formulario={"humor_nota": 4, "energia": 2})

    llm, avisos = _rodar(pasta, ledger, {"titulo": "Dia puxado", "tags": [], "humor": "cansado",
                                         "tensoes": ["deploy travado"], "amanha": "pedir ajuda"})

    texto_llm = llm.call_args.args[0]
    assert texto_llm.startswith("Check-in informado pelo autor: humor 4/5 · energia 2/5")
    assert "### O que pesou ou travou?" in texto_llm
    nota = dia.caminho_nota(Route.DIARIO, HOJE)
    texto, fm = _ler(nota)
    assert fm["humor_nota"] == 4 and fm["humor_notas"] == [4] and fm["energia"] == 2 and fm["humor"] == "cansado"
    assert "#### O que pesou ou travou?" in texto and "ideia solta antes" in texto
    assert "📊 humor 4/5 · energia 2/5 · via telegram" in texto
    assert not list((config.NOTES_DIR / "Inbox").glob("*.md")) if (config.NOTES_DIR / "Inbox").exists() else True
    assert avisos.call_args.args[0] == "Logos: diário registrado."


def test_pacote_so_com_formulario_vira_entrada_sem_llm(vault, tmp_path):
    ledger = Ledger(tmp_path / "ledger.json")
    pasta = _pacote("2026-09-24 21-40-00 tg 7", [], rota="diario", dia="2026-09-24",
                    formulario={"humor_nota": 3, "sono": "7–8h"})
    llm, _ = _rodar(pasta, ledger)
    llm.assert_not_called()
    texto, fm = _ler(dia.caminho_nota(Route.DIARIO, HOJE))
    assert fm["entradas"] == 1 and fm["sono"] == "7–8h"
    assert "*(só o check-in)*" in texto


def test_anexo_do_diario_fica_dentro_de_diario(vault, tmp_path):
    ledger = Ledger(tmp_path / "ledger.json")
    pasta = _pacote("2026-09-24 21-40-00 tg 7", [
        {"message_id": 8, "tipo": "documento", "arquivo": "8.pdf", "nome_original": "exame.pdf"},
    ], files={"8.pdf": b"%PDF"}, rota="diario", dia="2026-09-24")
    _rodar(pasta, ledger)

    dentro = config.NOTES_DIR / "Diario" / "anexos" / "2026" / "09"
    assert sorted(p.name for p in dentro.iterdir()) == ["2026-09-24_21-40-00_1.pdf", "2026-09-24_21-40-00_1.pdf.md"]
    assert not (config.NOTES_DIR / "anexos").exists()
    assert "capturas/Diario/anexos/2026/09/2026-09-24_21-40-00_1.pdf" in dia.caminho_nota(Route.DIARIO, HOJE).read_text(encoding="utf-8")


def test_diario_que_confere_o_plano_marca_o_plano(vault, tmp_path):
    ledger = Ledger(tmp_path / "ledger.json")
    dia.registrar_entrada(Route.PLANEJAMENTO, HOJE, _entrada("p", intencao="entregar"), "")
    pasta = _pacote("2026-09-24 21-40-00 tg 7", [], rota="diario", dia="2026-09-24",
                    formulario={"plano_cumprido": "sim"})
    _rodar(pasta, ledger)
    assert _ler(dia.caminho_nota(Route.PLANEJAMENTO, HOJE))[1]["cumprido"] == "sim"


def test_plano_do_formulario_vai_para_o_dia_escolhido_com_trazidas(vault, tmp_path):
    ledger = Ledger(tmp_path / "ledger.json")
    pasta = _pacote("2026-09-24 21-40-00 tg 7", [
        {"message_id": 8, "tipo": "texto", "texto": "amanhã é o relatório", "pergunta": 1},
    ], rota="planejamento", para="2026-09-25", formulario={"horizonte": "amanha", "foco": "[[Climatizando]]"},
        carregar={"trazidas": ["Ligar para a Lorena"], "de": "qua 24/09"})
    _, avisos = _rodar(pasta, ledger)

    nota = dia.caminho_nota(Route.PLANEJAMENTO, date(2026, 9, 25))
    texto, fm = _ler(nota)
    assert fm["para"] == "2026-09-25" and fm["foco"] == "[[Climatizando]]"
    assert "- [ ] Ligar para a Lorena (trazida de qua 24/09)" in texto
    assert "Plano — 2026-09-25.md" in avisos.call_args.args[0] and "Foco: [[Climatizando]]" in avisos.call_args.args[0]


def test_audio_do_gravador_soma_na_nota_do_dia_e_planejamento_amanha(vault, tmp_path):
    ledger = Ledger(tmp_path / "ledger.json")
    for nome, fala in (("2026-09-24 20-00-00.m4a", "diário hoje foi bom"),
                       ("2026-09-24 21-00-00.m4a", "diário mais uma coisa"),
                       ("2026-09-24 21-30-00.m4a", "planejamento amanhã cedo o relatório")):
        audio = config.INBOX_DIR / nome
        audio.write_bytes(b"audio" + nome.encode())
        with patch("logos.pipeline.transcribe_file", return_value=Transcription(fala, 5.0)), \
             patch("logos.pipeline.aw.post_capture"), patch("logos.telegram.notify") as avisos:
            process_file(audio, ledger)

    texto, fm = _ler(dia.caminho_nota(Route.DIARIO, HOJE))
    assert fm["entradas"] == 2 and fm["horas"] == ["20:00", "21:00"] and "via gravador" in texto
    assert dia.caminho_nota(Route.PLANEJAMENTO, date(2026, 9, 25)).exists()
    assert avisos.call_args.args[0].startswith("Logos: plano para sex 25/09, entrada 1")
