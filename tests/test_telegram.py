"""Etapas 3 e 4: bot Telegram (dono, comandos, pacotes multimodais) e CLI send/check. Sem rede."""
import json
import logging
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from logos import config, telegram
from logos.telegram import Bot, TelegramError
from logos.triage import is_bundle_name, triage

TOKEN = "123456:SEGREDO-NAO-VAZAR"
OWNER = 42
DATE = int(datetime(2026, 9, 14, 15, 2, 11).timestamp())
BUNDLE = "2026-09-14 15-02-11 tg 7"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "Inbox")
    monkeypatch.setattr(config, "TELEGRAM_WINDOW_MIN", 10)
    monkeypatch.delenv("LOGOS_TELEGRAM_TOKEN", raising=False)
    config.INBOX_DIR.mkdir()
    return tmp_path


@pytest.fixture
def owner(env):
    telegram.save_secrets({"telegram_token": TOKEN, "telegram_chat_id": OWNER})


@pytest.fixture
def api():
    with patch.object(telegram, "call") as call:
        yield call


@pytest.fixture
def downloads():
    """download_file falso: grava o file_id como conteúdo do destino."""
    def fake(token, file_id, dest):
        dest.write_bytes(f"conteudo {file_id}".encode())

    with patch.object(telegram, "download_file", side_effect=fake) as download:
        yield download


def _update(text=None, chat=OWNER, message_id=7, date=DATE, **extra):
    message = {"message_id": message_id, "date": date, "chat": {"id": chat}, **extra}
    if text is not None:
        message["text"] = text
    return {"update_id": 1000 + message_id, "message": message}


def _replies(api):
    return [c.kwargs["text"] for c in api.call_args_list if c.args[1] == "sendMessage"]


def _staged():
    root = telegram.staging_dir()
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


def _inbox():
    return sorted(p.name for p in config.INBOX_DIR.iterdir())


def _manifest(name):
    return json.loads((config.INBOX_DIR / name / "manifest.json").read_text(encoding="utf-8"))


# --- dono -----------------------------------------------------------------

def test_start_registra_o_dono(env, api):
    telegram.save_secrets({"telegram_token": TOKEN})
    Bot(TOKEN).handle_update(_update("/start", chat=555))

    assert telegram.get_owner_chat_id() == 555
    assert telegram.get_token() == TOKEN  # preserva as outras chaves
    assert "conectado" in _replies(api)[0]


def test_sem_dono_mensagem_comum_e_ignorada(env, api):
    telegram.save_secrets({"telegram_token": TOKEN})
    Bot(TOKEN).handle_update(_update("oi"))
    assert telegram.get_owner_chat_id() is None
    assert _inbox() == [] and _staged() == []
    api.assert_not_called()


def test_outro_chat_e_ignorado_e_o_conteudo_nao_vai_ao_log(owner, api, caplog):
    with caplog.at_level(logging.INFO):
        Bot(TOKEN).handle_update(_update("segredo do intruso", chat=999))
    assert _inbox() == [] and _staged() == []
    api.assert_not_called()
    assert "999" in caplog.text and "segredo do intruso" not in caplog.text


# --- sessões e pacotes ----------------------------------------------------

def test_texto_abre_sessao_e_nova_entrega_o_pacote(owner, api):
    bot = Bot(TOKEN)
    bot.handle_update(_update("tarefa urgente testar o bot"))

    assert _staged() == [BUNDLE] and _inbox() == []
    assert "sessão aberta" in _replies(api)[0] and "/nova" in _replies(api)[0]

    bot.handle_update(_update("/nova", message_id=8))

    assert _staged() == [] and _inbox() == [BUNDLE]
    manifest = _manifest(BUNDLE)
    assert manifest["motivo_fechamento"] == "nova"
    assert manifest["mensagens"] == [
        {"message_id": 7, "date": "2026-09-14T15:02:11", "tipo": "texto", "texto": "tarefa urgente testar o bot"},
    ]
    assert "Sessão fechada com 1" in _replies(api)[1]
    assert is_bundle_name(BUNDLE)
    t = triage(BUNDLE, "tarefa urgente testar o bot")
    assert (t.canal, t.categoria, t.prioridade) == ("telegram", "tarefa", "alta")
    assert t.recorded_at == datetime(2026, 9, 14, 15, 2, 11)


def test_texto_foto_e_pdf_na_janela_viram_um_pacote(owner, api, downloads):
    bot = Bot(TOKEN)
    bot.handle_update(_update("planejamento da visita técnica", message_id=7))
    bot.handle_update(_update(
        message_id=8, date=DATE + 60, caption="quadro da reunião",
        photo=[{"file_id": "p-pequena", "file_size": 10}, {"file_id": "p-grande", "file_size": 99}],
    ))
    bot.handle_update(_update(
        message_id=9, date=DATE + 120,
        document={"file_id": "d1", "file_size": 500, "file_name": "Relatório.pdf", "mime_type": "application/pdf"},
    ))
    bot.handle_update(_update("/nova", message_id=10))

    assert _inbox() == [BUNDLE]
    assert sorted(p.name for p in (config.INBOX_DIR / BUNDLE).iterdir()) == ["8.jpg", "9.pdf", "manifest.json"]
    mensagens = _manifest(BUNDLE)["mensagens"]
    assert [m["tipo"] for m in mensagens] == ["texto", "foto", "documento"]
    assert mensagens[1]["legenda"] == "quadro da reunião" and mensagens[1]["arquivo"] == "8.jpg"
    assert downloads.call_args_list[0].args[1] == "p-grande"
    assert mensagens[2]["nome_original"] == "Relatório.pdf"
    assert "3 na sessão" in _replies(api)[2]


def test_janela_vencida_fecha_no_tick(owner, api):
    bot = Bot(TOKEN)
    bot.handle_update(_update("ideia um"))
    assert bot.expire_if_idle() is None
    bot.session.last_at -= 11 * 60

    assert bot.expire_if_idle() == BUNDLE
    assert _inbox() == [BUNDLE] and _manifest(BUNDLE)["motivo_fechamento"] == "janela"
    assert bot.session is None


def test_mensagem_depois_da_janela_abre_outro_pacote(owner, api):
    bot = Bot(TOKEN)
    bot.handle_update(_update("ideia um"))
    bot.session.last_at -= 11 * 60
    bot.handle_update(_update("ideia dois", message_id=20))

    assert _inbox() == [BUNDLE]
    assert _staged() == ["2026-09-14 15-02-11 tg 20"]


def test_resposta_a_mensagem_da_sessao_fica_no_pacote(owner, api):
    bot = Bot(TOKEN)
    bot.handle_update(_update("ideia"))
    bot.handle_update(_update("complemento", message_id=8, reply_to_message={"message_id": 7}))

    assert bot.session.size == 2
    assert bot.session.manifest["mensagens"][1]["reply_to"] == 7
    assert "reply_to" not in bot.session.manifest


def test_resposta_a_mensagem_antiga_vira_pacote_fechado_com_reply_to(owner, api, downloads):
    bot = Bot(TOKEN)
    bot.handle_update(_update("ideia aberta", message_id=30))
    bot.handle_update(_update(message_id=31, photo=[{"file_id": "f", "file_size": 5}], reply_to_message={"message_id": 3}))

    assert _inbox() == ["2026-09-14 15-02-11 tg 31"]
    assert _manifest("2026-09-14 15-02-11 tg 31")["reply_to"] == 3
    assert bot.session is not None and bot.session.size == 1  # a sessão aberta segue intacta
    assert "resposta" in _replies(api)[1]


def test_resposta_a_sessao_com_janela_vencida_vai_para_a_nota_original(owner, api):
    bot = Bot(TOKEN)
    bot.handle_update(_update("ideia", message_id=7))
    bot.session.last_at -= 11 * 60  # venceu, mas o tick do polling ainda não passou
    bot.handle_update(_update("complemento", message_id=8, reply_to_message={"message_id": 7}))

    assert bot.session is None
    assert _inbox() == [BUNDLE, "2026-09-14 15-02-11 tg 8"]
    assert _manifest("2026-09-14 15-02-11 tg 8")["reply_to"] == 7


def test_sessao_vazia_por_falha_no_download_nao_vira_pacote(owner, api):
    bot = Bot(TOKEN)
    with patch.object(telegram, "download_file", side_effect=TelegramError("getFile: 400")):
        bot.handle_update(_update(photo=[{"file_id": "p", "file_size": 5}]))
    assert bot.session is not None and bot.session.size == 0

    bot.handle_update(_update("/nova", message_id=8))

    assert _inbox() == [] and _staged() == []
    assert _replies(api)[-1] == "Sessão vazia descartada."


def test_album_localizacao_e_links_vao_ao_manifest(owner, api, downloads):
    bot = Bot(TOKEN)
    texto = "leia https://exemplo.com/artigo e 😀 https://b.org"
    bot.handle_update(_update(texto, message_id=7, entities=[
        {"type": "url", "offset": 5, "length": 26},
        {"type": "url", "offset": 37, "length": 13},  # o emoji ocupa 2 unidades UTF-16
        {"type": "text_link", "offset": 0, "length": 4, "url": "https://c.net"},
    ]))
    for mid in (8, 9):
        bot.handle_update(_update(message_id=mid, media_group_id="album1", photo=[{"file_id": f"f{mid}", "file_size": 5}]))
    bot.handle_update(_update(
        message_id=10,
        location={"latitude": -30.03, "longitude": -51.23},
        venue={"location": {"latitude": -30.03, "longitude": -51.23}, "title": "Escritório", "address": "Rua X"},
    ))

    mensagens = bot.session.manifest["mensagens"]
    assert mensagens[0]["links"] == ["https://exemplo.com/artigo", "https://b.org", "https://c.net"]
    assert [m.get("media_group_id") for m in mensagens[1:3]] == ["album1", "album1"]
    assert mensagens[3]["local"] == {"lat": -30.03, "lon": -51.23, "titulo": "Escritório", "endereco": "Rua X"}


def test_comando_prefixa_a_primeira_palavra(owner, api):
    bot = Bot(TOKEN)
    bot.handle_update(_update("/tarefa@LogosBot comprar pão"))
    assert bot.session.manifest["mensagens"][0]["texto"] == "tarefa comprar pão"


def test_comando_sem_texto_responde_uso(owner, api):
    Bot(TOKEN).handle_update(_update("/ideia"))
    assert _staged() == []
    assert _replies(api) == ["Uso: /ideia <texto>"]


@pytest.mark.parametrize("text", ["/diario hoje foi longo", "Diário: hoje foi longo"])
def test_diario_ganha_lembrete_da_nuvem(owner, api, text):
    bot = Bot(TOKEN)
    bot.handle_update(_update(text))
    assert bot.session.manifest["mensagens"][0]["texto"].lower().startswith("diário")
    assert "nuvem" in _replies(api)[0]


def test_nova_sem_sessao_e_comando_desconhecido(owner, api):
    bot = Bot(TOKEN)
    bot.handle_update(_update("/nova", message_id=1))
    bot.handle_update(_update("/xyz", message_id=2))
    assert _inbox() == [] and _staged() == []
    first, second = _replies(api)
    assert "Nenhuma sessão" in first and "/tarefa" in second


def test_reentrega_dentro_da_sessao_nao_duplica(owner, api):
    bot = Bot(TOKEN)
    bot.handle_update(_update("ideia uma"))
    bot.handle_update(_update("ideia uma"))
    assert bot.session.size == 1


def test_reentrega_de_pacote_ja_entregue_nao_duplica(owner, api):
    bot = Bot(TOKEN)
    bot.handle_update(_update("ideia"))
    bot.handle_update(_update("/nova", message_id=8))
    bot.handle_update(_update("ideia"))  # o Telegram reentrega a mensagem 7
    bot.handle_update(_update("/nova", message_id=8))
    assert _inbox() == [BUNDLE] and _staged() == []


def test_voz_e_baixada_como_oga(owner, api, downloads):
    bot = Bot(TOKEN)
    bot.handle_update(_update(voice={"file_id": "abc", "file_size": 12345, "duration": 4}))
    assert downloads.call_args.args[1] == "abc"
    assert downloads.call_args.args[2] == telegram.staging_dir() / BUNDLE / "7.oga"
    assert bot.session.manifest["mensagens"][0]["tipo"] == "voz"


def test_audio_e_documento_mantem_a_extensao(owner, api, downloads):
    bot = Bot(TOKEN)
    bot.handle_update(_update(audio={"file_id": "a", "file_size": 10, "file_name": "gravacao.M4A"}, message_id=7))
    bot.handle_update(_update(document={"file_id": "d", "file_size": 10, "mime_type": "application/pdf"}, message_id=8))
    assert [m["arquivo"] for m in bot.session.manifest["mensagens"]] == ["7.m4a", "8.pdf"]


@pytest.mark.parametrize("media", [
    {"voice": {"file_id": "v", "file_size": 25 * 1024 * 1024}},
    {"photo": [{"file_id": "p", "file_size": 21 * 1024 * 1024}]},
    {"document": {"file_id": "d", "file_size": 30 * 1024 * 1024, "file_name": "grande.pdf"}},
])
def test_arquivo_acima_de_20mb_e_recusado(owner, api, downloads, media):
    Bot(TOKEN).handle_update(_update(**media))
    downloads.assert_not_called()
    assert _staged() == []
    assert "20 MB" in _replies(api)[0]


def test_tipo_nao_suportado_responde(owner, api):
    Bot(TOKEN).handle_update(_update(sticker={"file_id": "s"}))
    assert _staged() == []
    assert "não suportado" in _replies(api)[0]


def test_falha_ao_baixar_avisa_e_nao_registra_a_mensagem(owner, api):
    bot = Bot(TOKEN)
    with patch.object(telegram, "download_file", side_effect=TelegramError("getFile: 400")):
        bot.handle_update(_update(voice={"file_id": "abc", "file_size": 1}))
    assert "Reenvie" in _replies(api)[-1]
    assert bot.session is None or bot.session.size == 0


def test_recover_entrega_pacotes_deixados_abertos(owner, api):
    Bot(TOKEN).handle_update(_update("texto antes da queda"))

    reiniciado = Bot(TOKEN)  # a sessão em memória se perdeu
    assert reiniciado.recover() == [BUNDLE]
    assert _inbox() == [BUNDLE] and _staged() == []
    assert _manifest(BUNDLE)["motivo_fechamento"] == "reinicio"


def test_recover_descarta_pasta_sem_mensagens(env):
    vazia = telegram.staging_dir() / "2026-09-14 15-02-11 tg 1"
    vazia.mkdir(parents=True)
    assert Bot(TOKEN).recover() == []
    assert not vazia.exists()


def test_manifest_e_deterministico(env, tmp_path):
    d = tmp_path / "pacote"
    d.mkdir()
    telegram.write_manifest(d, {"b": 1, "a": [1, 2]})
    first = (d / "manifest.json").read_bytes()
    telegram.write_manifest(d, {"a": [1, 2], "b": 1})
    assert (d / "manifest.json").read_bytes() == first


# --- API ------------------------------------------------------------------

def test_download_grava_por_temporario(env):
    dest = env / "7.oga"
    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.iter_content.return_value = [b"OggS", b"dados"]
    with patch.object(telegram, "call", return_value={"file_path": "voice/file_1.oga"}), \
         patch.object(telegram.requests, "get", return_value=resp) as get:
        telegram.download_file(TOKEN, "abc", dest)
    assert get.call_args.args[0] == f"https://api.telegram.org/file/bot{TOKEN}/voice/file_1.oga"
    assert dest.read_bytes() == b"OggSdados"
    assert not (env / ".7.oga.tmp").exists()


def test_erros_de_rede_nao_vazam_o_token():
    err = requests.ConnectionError(f"HTTPSConnectionPool: /bot{TOKEN}/getUpdates refused")
    with patch.object(telegram.requests, "post", side_effect=err):
        with pytest.raises(TelegramError) as exc:
            telegram.call(TOKEN, "getUpdates")
    assert TOKEN not in str(exc.value)


def test_timeout_do_getupdates_nao_colide_com_o_timeout_http():
    """Regressão: `timeout` é parâmetro do getUpdates; o timeout HTTP é outro argumento."""
    resp = MagicMock()
    resp.json.return_value = {"ok": True, "result": []}
    with patch.object(telegram.requests, "post", return_value=resp) as post:
        assert telegram.call(TOKEN, "getUpdates", http_timeout=35, timeout=30, allowed_updates=["message"]) == []
    assert post.call_args.kwargs["timeout"] == 35
    assert post.call_args.kwargs["json"] == {"timeout": 30, "allowed_updates": ["message"]}


def test_erro_da_api_carrega_o_codigo():
    resp = MagicMock()
    resp.json.return_value = {"ok": False, "error_code": 409, "description": "Conflict"}
    with patch.object(telegram.requests, "post", return_value=resp):
        with pytest.raises(TelegramError) as exc:
            telegram.call(TOKEN, "getUpdates")
    assert exc.value.code == 409


# --- polling --------------------------------------------------------------

def _fake_api(stop, updates_per_call):
    seen = []

    def fake_call(token, method, **kwargs):
        if method == "getMe":
            return {"username": "LogosBot"}
        if method == "getUpdates":
            seen.append(kwargs)
            if len(seen) > len(updates_per_call):
                stop.set()
                return []
            return updates_per_call[len(seen) - 1]
        return {}

    return fake_call, seen


def test_polling_recupera_avanca_offset_e_publica_status(owner):
    orfao = telegram.staging_dir() / "2026-09-13 10-00-00 tg 1"
    orfao.mkdir(parents=True)
    telegram.write_manifest(orfao, {"mensagens": [{"message_id": 1, "tipo": "texto", "texto": "de ontem"}]})
    stop = threading.Event()
    fake_call, seen = _fake_api(stop, [[_update("ideia testar polling", message_id=9)]])

    with patch.object(telegram, "call", side_effect=fake_call), \
         patch("logos.status.tracker.set_bot") as set_bot:
        Bot(TOKEN).run_polling(stop)

    assert "2026-09-13 10-00-00 tg 1" in _inbox()
    assert "offset" not in seen[0] and seen[1]["offset"] == 1010  # update_id 1009 + 1
    assert seen[0]["allowed_updates"] == ["message"]
    assert _staged() == ["2026-09-14 15-02-11 tg 9"]
    assert set_bot.call_args.args[0] == "polling"
    assert set_bot.call_args.kwargs["session"] == "2026-09-14 15-02-11 tg 9"


def test_polling_fecha_janela_vencida(owner, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_WINDOW_MIN", 0)
    stop = threading.Event()
    fake_call, _ = _fake_api(stop, [[_update("ideia rápida", message_id=9)]])
    with patch.object(telegram, "call", side_effect=fake_call), patch("logos.status.tracker.set_bot"):
        Bot(TOKEN).run_polling(stop)
    assert _inbox() == ["2026-09-14 15-02-11 tg 9"]


def test_polling_para_com_token_recusado(owner):
    with patch.object(telegram, "call", side_effect=TelegramError("getMe: 401 Unauthorized", code=401)), \
         patch("logos.status.tracker.set_bot") as set_bot:
        Bot(TOKEN).run_polling(threading.Event())  # retorna em vez de girar para sempre
    assert set_bot.call_args.args[0] == "error"


def test_sem_token_o_bot_fica_off(env):
    with patch("logos.status.tracker.set_bot") as set_bot:
        assert telegram.start_bot_thread(threading.Event()) is None
    assert set_bot.call_args.args[0] == "off"


def test_variavel_de_ambiente_tem_precedencia(env, monkeypatch):
    telegram.save_secrets({"telegram_token": "do-arquivo"})
    monkeypatch.setenv("LOGOS_TELEGRAM_TOKEN", "da-env")
    assert telegram.get_token() == "da-env"


# --- CLI ------------------------------------------------------------------

def test_send_sem_configuracao_sai_com_2(env, capsys):
    assert telegram.main(["send", "--text", "oi"]) == 2
    telegram.save_secrets({"telegram_token": TOKEN})
    assert telegram.main(["send", "--text", "oi"]) == 2
    assert "dono" in capsys.readouterr().err


def test_send_texto_longo_em_partes(owner, api):
    text = "\n".join(["linha " + "x" * 90] * 100)  # ~9,7 mil caracteres
    assert telegram.main(["send", "--text", text]) == 0
    sent = [c.kwargs["text"] for c in api.call_args_list]
    assert len(sent) == 3 and all(len(s) <= 4096 for s in sent) and "".join(sent) == text
    assert all(c.kwargs["chat_id"] == OWNER and "parse_mode" not in c.kwargs for c in api.call_args_list)


def test_send_imagem_vai_como_foto_com_legenda(owner, api, tmp_path):
    img = tmp_path / "atividade.png"
    img.write_bytes(b"\x89PNG")
    assert telegram.main(["send", "--text", "Resumo do dia", "--file", str(img)]) == 0
    [c] = api.call_args_list
    assert c.args[1] == "sendPhoto" and c.kwargs["caption"] == "Resumo do dia" and "photo" in c.kwargs["files"]


def test_send_documento_sem_legenda(owner, api, tmp_path):
    doc = tmp_path / "briefing.md"
    doc.write_text("# briefing", encoding="utf-8")
    assert telegram.main(["send", "--file", str(doc)]) == 0
    [c] = api.call_args_list
    assert c.args[1] == "sendDocument" and "caption" not in c.kwargs


def test_send_falha_da_api_sai_com_1(owner):
    with patch.object(telegram, "call", side_effect=TelegramError("sendMessage: 400 Bad Request")):
        assert telegram.main(["send", "--text", "oi"]) == 1


def test_send_arquivo_inexistente_sai_com_1(owner, api, tmp_path):
    assert telegram.main(["send", "--file", str(tmp_path / "nao-existe.png")]) == 1
    api.assert_not_called()


def test_check(owner, api, capsys):
    api.return_value = {"username": "LogosBot"}
    assert telegram.main(["check"]) == 0
    assert "@LogosBot" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Consulta (/ver, /buscar, /hoje): despacho, formato e o que ela não pode fazer
# ---------------------------------------------------------------------------

@pytest.fixture
def vault_falso(tmp_path, monkeypatch):
    from logos import consulta

    raiz = tmp_path / "vault"
    (raiz / "work/active").mkdir(parents=True)
    (raiz / "work/active/PPJ.md").write_text(
        "---\nstatus: active\n---\n\n# PPJ\n\nReunião com <Bruna> & equipe.\n", encoding="utf-8",
    )
    (raiz / "capturas/Diario").mkdir(parents=True)
    (raiz / "capturas/Diario/d.md").write_text("# Diário\n\nSENTINELA.\n", encoding="utf-8")
    monkeypatch.setattr(config, "VAULT_DIR", raiz)
    return consulta


def test_ver_responde_em_html_sem_abrir_sessao(owner, api, vault_falso):
    """Consultar não é capturar: nada vai para a Inbox e nenhuma sessão nasce."""
    bot = Bot(TOKEN)
    bot.handle_update(_update("/ver"))
    assert bot.session is None
    assert list(config.INBOX_DIR.iterdir()) == []
    [chamada] = api.call_args_list
    assert chamada.args[1] == "sendMessage"
    assert chamada.kwargs["parse_mode"] == "HTML"
    assert "PPJ" in chamada.kwargs["text"]


def test_consulta_escapa_o_conteudo_do_vault(owner, api, vault_falso):
    Bot(TOKEN).handle_update(_update("/ver ppj"))
    texto = api.call_args_list[0].kwargs["text"]
    assert "&lt;Bruna&gt; &amp; equipe" in texto


def test_consulta_nunca_alcanca_o_diario(owner, api, vault_falso):
    """O termo digitado volta no eco; o que não pode voltar é o arquivo, o caminho ou o texto."""
    Bot(TOKEN).handle_update(_update("/buscar SENTINELA"))
    texto = api.call_args_list[0].kwargs["text"]
    assert "Nada com" in texto
    assert "Diario" not in texto and "d.md" not in texto


def test_erro_de_consulta_vira_resposta_em_texto(owner, api, vault_falso):
    """ConsultaError é recado para o dono, não exceção que derruba o polling."""
    Bot(TOKEN).handle_update(_update("/ver ppj 99"))
    [chamada] = api.call_args_list
    assert "parse_mode" not in chamada.kwargs
    assert "seções" in chamada.kwargs["text"]


def test_falha_inesperada_da_consulta_nao_derruba_o_bot(owner, api, vault_falso, monkeypatch):
    monkeypatch.setattr(vault_falso, "projetos", MagicMock(side_effect=OSError("disco")))
    Bot(TOKEN).handle_update(_update("/ver"))
    assert "não consegui ler o vault" in api.call_args_list[0].kwargs["text"].lower()


def test_html_recusado_reenvia_sem_formatacao(owner, api, vault_falso):
    api.side_effect = [TelegramError("sendMessage: 400 Bad Request", code=400), {}]
    Bot(TOKEN).handle_update(_update("/ver"))
    segunda = api.call_args_list[1]
    assert "parse_mode" not in segunda.kwargs
    assert "<b>" not in segunda.kwargs["text"]


def test_timeout_de_rede_nao_degrada_para_texto_cru(owner, api, vault_falso):
    """Timeout não é recusa de formatação: reenviar sem tags trocaria a resposta boa por uma
    feia sem motivo, e arriscaria mensagem dobrada se a primeira tiver chegado."""
    api.side_effect = TelegramError("sendMessage: Read timed out")
    Bot(TOKEN).handle_update(_update("/ver"))
    assert api.call_count == 1


def test_menu_cobre_todos_os_comandos_que_o_bot_entende(owner, api):
    """O que o bot atende e o menu não anuncia é comando que ele precisa lembrar sozinho.

    Os atalhos de uma letra e o /0 ficam fora do menu de propósito: o menu é para descobrir,
    e nove linhas com sinônimo em metade delas descobre pior. Cada verbo anuncia o seu atalho
    na descrição."""
    nomes = {nome for nome, _ in telegram.MENU}
    verbos = set(telegram.CONSULTA_COMMANDS.values())
    assert set(telegram.PREFIX_COMMANDS) <= nomes
    assert verbos <= nomes
    assert {"nova"} <= nomes
    for atalho in ("/0", "/v", "/b", "/h", "/a"):
        assert any(atalho in descricao for _, descricao in telegram.MENU), atalho
    for nome, descricao in telegram.MENU:
        assert nome.islower() and nome.isascii() and 1 <= len(nome) <= 32
        assert 1 <= len(descricao) <= 256
        assert not any(ord(c) > 0x2500 for c in descricao)


def test_menu_publicado_ao_conectar(owner, api):
    assert telegram.publicar_menu(TOKEN) is True
    [chamada] = api.call_args_list
    assert chamada.args[1] == "setMyCommands"
    assert {c["command"] for c in chamada.kwargs["commands"]} == {n for n, _ in telegram.MENU}


def test_menu_que_falha_nao_derruba_nada(owner, api):
    api.side_effect = TelegramError("setMyCommands: 400 Bad Request", code=400)
    assert telegram.publicar_menu(TOKEN) is False


def test_atalhos_de_uma_letra_valem(owner, api, vault_falso):
    for atalho in ("/v", "/0", "/a"):
        api.reset_mock()
        Bot(TOKEN).handle_update(_update(atalho))
        assert api.call_count >= 1, atalho
        assert api.call_args_list[0].kwargs["parse_mode"] == "HTML"


def test_numero_da_resposta_anterior_abre_a_nota(owner, api, vault_falso):
    """O contrato inteiro do desenho: o /p1 que apareceu na resposta funciona na seguinte."""
    bot = Bot(TOKEN)
    bot.handle_update(_update("/ver"))
    assert "/p1" in api.call_args_list[0].kwargs["text"]
    api.reset_mock()
    bot.handle_update(_update("/p1", message_id=8))
    assert "PPJ" in api.call_args_list[0].kwargs["text"]


def test_indice_sem_contexto_explica_em_vez_de_explodir(owner, api, vault_falso):
    """Depois de um relançamento o bot nasce sem contexto; /p3 não pode virar exceção."""
    Bot(TOKEN).handle_update(_update("/p3"))
    texto = api.call_args_list[0].kwargs["text"]
    assert "/ver" in texto and "parse_mode" not in api.call_args_list[0].kwargs


def test_cada_bot_tem_o_seu_contexto(owner, api, vault_falso):
    assert Bot(TOKEN).contexto is not Bot(TOKEN).contexto


def test_indice_nao_confunde_com_captura(owner, api, vault_falso, downloads):
    """'/p1' é consulta; 'p1' sem barra é texto e tem de virar nota."""
    bot = Bot(TOKEN)
    bot.handle_update(_update("p1"))
    assert bot.session is not None
    assert bot.session.manifest["mensagens"][0]["texto"] == "p1"


def test_comando_parecido_com_indice_mas_invalido_cai_na_ajuda(owner, api, vault_falso):
    Bot(TOKEN).handle_update(_update("/p"))
    texto = api.call_args_list[0].kwargs["text"]
    assert "Capturar" in texto  # _ajuda() de texto puro


def test_ajuda_anuncia_os_comandos_de_consulta(owner, api, vault_falso):
    Bot(TOKEN).handle_update(_update("/ajuda"))
    texto = api.call_args_list[0].kwargs["text"]
    for esperado in ("/0", "/ver", "/buscar", "/hoje", "/p1", "/s1", "/r1", "/tarefa"):
        assert esperado in texto, esperado


def test_acks_de_captura_nao_tem_emoji(owner, api, downloads):
    bot = Bot(TOKEN)
    bot.handle_update(_update("uma ideia solta"))
    texto = api.call_args_list[0].kwargs["text"]
    assert texto.startswith("Texto ")
    assert not any(ord(c) > 0x2500 for c in texto), texto


def test_cli_nao_importa_status_nem_toca_no_status_json(tmp_path):
    """`send` roda com o watcher vivo: importar logos.status regravaria o status.json dele."""
    repo = Path(__file__).resolve().parent.parent
    code = (
        "import sys, logos.telegram as t; "
        "rc = t.main(['send', '--text', 'oi']); "
        "print(rc, 'logos.status' in sys.modules)"
    )
    env = {**__import__("os").environ, "LOGOS_DATA_ROOT": str(tmp_path), "LOGOS_TELEGRAM_TOKEN": ""}
    out = subprocess.run([sys.executable, "-c", code], cwd=repo, env=env, capture_output=True, text=True, timeout=60)
    assert out.stdout.split() == ["2", "False"], out.stderr
    assert not (tmp_path / "state" / "status.json").exists()
