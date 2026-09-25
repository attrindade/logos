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
import urllib3

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


def test_diario_digitado_sem_comando_ganha_lembrete_da_nuvem(owner, api):
    bot = Bot(TOKEN)
    bot.handle_update(_update("Diário: hoje foi longo"))
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
         patch.object(telegram._sessao, "get", return_value=resp) as get:
        telegram.download_file(TOKEN, "abc", dest)
    assert get.call_args.args[0] == f"https://api.telegram.org/file/bot{TOKEN}/voice/file_1.oga"
    assert dest.read_bytes() == b"OggSdados"
    assert not (env / ".7.oga.tmp").exists()


def test_erros_de_rede_nao_vazam_o_token():
    err = requests.ConnectionError(f"HTTPSConnectionPool: /bot{TOKEN}/getUpdates refused")
    with patch.object(telegram._sessao, "post", side_effect=err):
        with pytest.raises(TelegramError) as exc:
            telegram.call(TOKEN, "getUpdates")
    assert TOKEN not in str(exc.value)


def test_timeout_do_getupdates_nao_colide_com_o_timeout_http():
    """Regressão: `timeout` é parâmetro do getUpdates; o timeout HTTP é outro argumento."""
    resp = MagicMock()
    resp.json.return_value = {"ok": True, "result": []}
    with patch.object(telegram._sessao, "post", return_value=resp) as post:
        assert telegram.call(TOKEN, "getUpdates", http_timeout=35, timeout=30, allowed_updates=["message"]) == []
    assert post.call_args.kwargs["timeout"] == 35
    assert post.call_args.kwargs["json"] == {"timeout": 30, "allowed_updates": ["message"]}


def test_erro_da_api_carrega_o_codigo():
    resp = MagicMock()
    resp.json.return_value = {"ok": False, "error_code": 409, "description": "Conflict"}
    with patch.object(telegram._sessao, "post", return_value=resp):
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
    assert seen[0]["allowed_updates"] == ["message", "callback_query"]
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
    assert "PPJ" in str(chamada.kwargs["reply_markup"])


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
    e nove linhas com sinônimo em metade delas descobre pior."""
    nomes = {nome for nome, _ in telegram.MENU}
    verbos = set(telegram.CONSULTA_COMMANDS.values())
    assert set(telegram.PREFIX_COMMANDS) <= nomes
    assert set(telegram.GUIDED_COMMANDS) <= nomes
    assert verbos <= nomes
    assert {"nova"} <= nomes
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


def _toque(dado, chat=OWNER, message_id=50):
    return {"update_id": 5000, "callback_query": {
        "id": "cb", "data": dado, "from": {"id": chat},
        "message": {"message_id": message_id, "chat": {"id": chat}, "date": DATE},
    }}


def _dados(kwargs):
    return {b["text"]: b["callback_data"]
            for linha in kwargs["reply_markup"]["inline_keyboard"] for b in linha}


def _chamadas(api, metodo):
    return [c.kwargs for c in api.call_args_list if c.args[1] == metodo]


def test_toque_troca_a_propria_mensagem(owner, api, vault_falso):
    """O contrato do desenho: tocar num botão troca a mensagem, não manda outra."""
    bot = Bot(TOKEN)
    bot.handle_update(_update("/inicio"))
    dados = _dados(api.call_args_list[0].kwargs)
    [dado] = [d for rotulo, d in dados.items() if "PPJ" in rotulo]
    api.reset_mock()
    bot.handle_update(_toque(dado))
    [edit] = _chamadas(api, "editMessageText")
    assert edit["message_id"] == 50 and "PPJ" in edit["text"] and edit["parse_mode"] == "HTML"
    assert _chamadas(api, "answerCallbackQuery") and not _chamadas(api, "sendMessage")
    assert bot.session is None


def test_toque_de_outro_chat_e_ignorado(owner, api, vault_falso):
    Bot(TOKEN).handle_update(_toque("i", chat=999))
    assert api.call_count == 0


def test_botao_vencido_vira_aviso_passageiro(owner, api, vault_falso):
    """Depois de um relançamento o botão velho não casa com nada: avisa, não explode, e não
    troca a tela por uma mensagem de erro."""
    Bot(TOKEN).handle_update(_toque("o:dead.1"))
    [aviso] = _chamadas(api, "answerCallbackQuery")
    assert "reinício" in aviso["text"]
    assert not _chamadas(api, "editMessageText")


def test_tela_igual_nao_e_erro(owner, api, vault_falso):
    """Tocar em Início estando no Início: a API responde 400 "not modified", e isso é normal."""
    def fake(token, method, **kw):
        if method == "editMessageText":
            raise TelegramError("editMessageText: 400 Bad Request: message is not modified", code=400)
        return {}
    api.side_effect = fake
    Bot(TOKEN).handle_update(_toque("i"))
    assert len(_chamadas(api, "editMessageText")) == 1   # não degradou para texto cru


def test_tela_longa_vai_como_mensagens_novas(owner, api, vault_falso, monkeypatch):
    monkeypatch.setattr(vault_falso, "cmd_botao",
                        lambda dado, ctx: vault_falso.Tela("linha\n" * 3000, [[vault_falso.INICIO]]))
    Bot(TOKEN).handle_update(_toque("i"))
    enviadas = _chamadas(api, "sendMessage")
    assert len(enviadas) > 1 and not _chamadas(api, "editMessageText")
    assert "reply_markup" in enviadas[-1] and "reply_markup" not in enviadas[0]


def test_buscar_pelo_botao_pergunta_e_a_resposta_vira_busca(owner, api, vault_falso):
    """A resposta ao "O que procurar?" é busca, não captura — sem isto o termo viraria nota."""
    api.return_value = {"message_id": 77}
    bot = Bot(TOKEN)
    bot.handle_update(_toque("b"))
    [pergunta] = _chamadas(api, "sendMessage")
    assert pergunta["reply_markup"]["force_reply"] is True
    api.reset_mock()
    bot.handle_update(_update("reunião", message_id=78, reply_to_message={"message_id": 77}))
    assert bot.session is None and list(config.INBOX_DIR.iterdir()) == []
    assert "PPJ" in _chamadas(api, "sendMessage")[0]["text"]
    # Respondida uma vez, a pergunta deixa de valer: responder de novo é captura, como sempre.
    assert 77 not in bot.pedidos_busca


def test_botao_fixo_abre_o_inicio_e_nao_vira_captura(owner, api, vault_falso):
    """O botão fixo manda o próprio rótulo como texto; sem reconhecê-lo, cada toque viraria
    uma nota com o texto "🧭 Início"."""
    bot = Bot(TOKEN)
    bot.handle_update(_update(telegram.BOTAO_INICIO))
    assert bot.session is None and list(config.INBOX_DIR.iterdir()) == []
    [envio] = _chamadas(api, "sendMessage")
    assert "Para onde" in envio["text"] and "inline_keyboard" in envio["reply_markup"]


def test_botao_fixo_de_tipos_lista_os_tipos(owner, api, vault_falso):
    bot = Bot(TOKEN)
    bot.handle_update(_update(telegram.BOTAO_TIPOS))
    assert bot.session is None and list(config.INBOX_DIR.iterdir()) == []
    [envio] = _chamadas(api, "sendMessage")
    assert "Tipos de nota" in envio["text"] and "Projetos" in str(envio["reply_markup"])


def test_so_o_rotulo_exato_e_consulta(owner, api, vault_falso, downloads):
    """"início da reunião" é captura; só o rótulo do botão abre o Início."""
    bot = Bot(TOKEN)
    bot.handle_update(_update("Início da reunião com a Bruna"))
    assert bot.session is not None


def test_respostas_em_texto_instalam_o_botao_fixo(owner, api, vault_falso, downloads):
    """O /start e todo recibo de captura levam o teclado fixo: depois do primeiro, ele fica."""
    bot = Bot(TOKEN)
    for texto in ("/start", "uma ideia solta"):
        api.reset_mock()
        bot.handle_update(_update(texto))
        teclado = _chamadas(api, "sendMessage")[0]["reply_markup"]
        assert teclado["keyboard"] == [[{"text": telegram.BOTAO_INICIO}, {"text": telegram.BOTAO_TIPOS}]]
        assert teclado["is_persistent"] is True


def test_barra_buscar_sem_termo_pergunta(owner, api, vault_falso):
    api.return_value = {"message_id": 77}
    bot = Bot(TOKEN)
    bot.handle_update(_update("/buscar"))
    assert _chamadas(api, "sendMessage")[0]["reply_markup"]["force_reply"] is True
    assert 77 in bot.pedidos_busca


def test_cada_bot_tem_o_seu_contexto(owner, api, vault_falso):
    assert Bot(TOKEN).contexto is not Bot(TOKEN).contexto


def test_texto_parecido_com_comando_e_captura(owner, api, vault_falso, downloads):
    """'p1' sem barra é texto e tem de virar nota."""
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
    for esperado in ("/inicio", "/ver", "/buscar", "/hoje", "/tarefa"):
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


# ---------------------------------------------------------------------------
# Rede instável: o que não pode custar latência nem mensagem
# ---------------------------------------------------------------------------

def test_timeout_do_long_poll_nao_e_erro(owner, monkeypatch):
    """getUpdates fica pendurado de propósito. Contar isso como falha dobrava a espera a cada
    oscilação, e o bot passava mais tempo dormindo do que ouvindo."""
    from logos import status

    estados = []
    monkeypatch.setattr(status.tracker, "set_bot", lambda estado, **kw: estados.append((estado, kw)))

    respostas = [
        {"username": "LogosBot"},                       # getMe
        None,                                           # publicar_menu
        TelegramError("getUpdates: Read timed out", timeout=True),
        TelegramError("getUpdates: Read timed out", timeout=True),
        TelegramError("getUpdates: Read timed out", timeout=True),
    ]
    stop = threading.Event()
    esperas = []

    def fake_call(token, method, **kwargs):
        resposta = respostas.pop(0) if respostas else stop.set() or []
        if isinstance(resposta, TelegramError):
            raise resposta
        return resposta if resposta is not None else {}

    monkeypatch.setattr(stop, "wait", lambda s: esperas.append(s))
    with patch.object(telegram, "call", side_effect=fake_call):
        Bot(TOKEN).run_polling(stop)

    assert esperas == [], f"não deveria esperar entre polls: {esperas}"
    assert all(estado == "polling" for estado, _ in estados), estados
    assert all(kw.get("last_error") is None for _, kw in estados)


def test_erro_de_verdade_continua_com_backoff(owner, monkeypatch):
    from logos import status

    monkeypatch.setattr(status.tracker, "set_bot", lambda estado, **kw: None)
    respostas = [
        {"username": "LogosBot"},
        None,
        TelegramError("getUpdates: 500 Internal Server Error", code=500),
        TelegramError("getUpdates: 500 Internal Server Error", code=500),
    ]
    stop = threading.Event()
    esperas = []

    def fake_call(token, method, **kwargs):
        resposta = respostas.pop(0) if respostas else stop.set() or []
        if isinstance(resposta, TelegramError):
            raise resposta
        return resposta if resposta is not None else {}

    monkeypatch.setattr(stop, "wait", lambda s: esperas.append(s))
    with patch.object(telegram, "call", side_effect=fake_call):
        Bot(TOKEN).run_polling(stop)

    assert esperas == [1, 2], esperas


def test_download_tenta_de_novo_antes_de_desistir(owner, tmp_path, monkeypatch):
    """Uma conexão derrubada no meio não pode custar a nota de voz inteira."""
    monkeypatch.setattr(telegram.time, "sleep", lambda s: None)
    tentativas = []

    class FakeResp:
        def __init__(self, ok):
            self.ok = ok

        def __enter__(self):
            if not self.ok:
                raise requests.ConnectionError("Connection broken")
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, n):
            yield b"audio"

    def fake_get(url, **kwargs):
        tentativas.append(url)
        return FakeResp(len(tentativas) >= 3)

    destino = tmp_path / "98.oga"
    with patch.object(telegram, "call", return_value={"file_path": "voice/98.oga"}):
        with patch.object(telegram._sessao, "get", side_effect=fake_get):
            telegram.download_file(TOKEN, "f-98", destino)

    assert len(tentativas) == 3
    assert destino.read_bytes() == b"audio"


def test_download_desiste_depois_do_limite(owner, tmp_path, monkeypatch):
    monkeypatch.setattr(telegram.time, "sleep", lambda s: None)

    def sempre_falha(url, **kwargs):
        raise requests.ConnectionError("Connection broken")

    with patch.object(telegram, "call", return_value={"file_path": "voice/98.oga"}):
        with patch.object(telegram._sessao, "get", side_effect=sempre_falha):
            with pytest.raises(TelegramError):
                telegram.download_file(TOKEN, "f-98", tmp_path / "98.oga")
    # Nem o destino nem o temporário ficam para trás.
    assert not (tmp_path / "98.oga").exists()
    assert not any(f.name.startswith(".98.oga") for f in tmp_path.iterdir())


def test_timeout_marca_a_excecao(owner):
    with patch.object(telegram._sessao, "post", side_effect=requests.Timeout("Read timed out")):
        with pytest.raises(TelegramError) as erro:
            telegram.call(TOKEN, "getUpdates")
    assert erro.value.timeout is True


def test_timeout_embrulhado_pelo_retry_tambem_marca(owner):
    """Read timeout que passou pelo `Retry` chega como `ConnectionError`, não como `Timeout`.

    Era isto que fazia o long poll expirado recuar exponencialmente (1, 2, 4... 60 s) depois
    das duas correções anteriores no recuo: elas só pegavam o `requests.Timeout` cru. O sinal
    no log é o prefixo `Max retries exceeded ... Caused by ReadTimeoutError`.
    """
    embrulhado = requests.exceptions.ConnectionError(
        urllib3.exceptions.MaxRetryError(
            pool=None, url="/bot<token>/getUpdates",
            reason=urllib3.exceptions.ReadTimeoutError(None, "/", "Read timed out. (read timeout=35)"),
        )
    )
    with patch.object(telegram._sessao, "post", side_effect=embrulhado):
        with pytest.raises(TelegramError) as erro:
            telegram.call(TOKEN, "getUpdates", http_timeout=35)
    assert erro.value.timeout is True


def test_conexao_recusada_nao_e_timeout(owner):
    """Contraprova: queda de conexão continua recuando exponencialmente, de propósito."""
    embrulhado = requests.exceptions.ConnectionError(
        urllib3.exceptions.MaxRetryError(
            pool=None, url="/bot<token>/getUpdates",
            reason=urllib3.exceptions.NewConnectionError(None, "conexão recusada"),
        )
    )
    with patch.object(telegram._sessao, "post", side_effect=embrulhado):
        with pytest.raises(TelegramError) as erro:
            telegram.call(TOKEN, "getUpdates")
    assert erro.value.timeout is False


def test_maratona_de_timeouts_deixa_rastro_no_log(owner, monkeypatch, caplog):
    """Timeout não vai para o log um a um, mas uma queda longa não pode ficar invisível."""
    from logos import status

    monkeypatch.setattr(status.tracker, "set_bot", lambda estado, **kw: None)
    respostas = [{"username": "LogosBot"}, None] + [
        TelegramError("getUpdates: Read timed out", timeout=True)
    ] * telegram.TIMEOUTS_ATE_AVISAR
    stop = threading.Event()

    def fake_call(token, method, **kwargs):
        resposta = respostas.pop(0) if respostas else stop.set() or []
        if isinstance(resposta, TelegramError):
            raise resposta
        return resposta if resposta is not None else {}

    monkeypatch.setattr(stop, "wait", lambda s: None)
    with caplog.at_level(logging.WARNING):
        with patch.object(telegram, "call", side_effect=fake_call):
            Bot(TOKEN).run_polling(stop)

    avisos = [r for r in caplog.records if "timeouts seguidos" in r.message]
    assert len(avisos) == 1, [r.message for r in caplog.records]


def test_timeout_na_subida_nao_recua_exponencialmente(owner, monkeypatch):
    """getMe que expira também não pode dobrar a espera: a espera já foi paga pelo timeout,
    e dobrá-la deixava o bot minutos fora do ar depois de um relançamento."""
    from logos import status

    monkeypatch.setattr(status.tracker, "set_bot", lambda estado, **kw: None)
    respostas = [TelegramError("getMe: Read timed out", timeout=True)] * 4 + [
        {"username": "LogosBot"}, None,
    ]
    stop = threading.Event()
    esperas = []

    def fake_call(token, method, **kwargs):
        resposta = respostas.pop(0) if respostas else stop.set() or []
        if isinstance(resposta, TelegramError):
            raise resposta
        return resposta if resposta is not None else {}

    monkeypatch.setattr(stop, "wait", lambda s: esperas.append(s))
    with patch.object(telegram, "call", side_effect=fake_call):
        Bot(TOKEN).run_polling(stop)

    assert esperas == [telegram.TIMEOUT_RETENTATIVA_S] * 4, esperas


def test_toda_chamada_passa_pela_sessao():
    """Abrir conexão nova por chamada era o que fazia o bot demorar nesta rede: o aperto de mão
    com api.telegram.org custa mais que a chamada. Voltar a `requests.post` solto refaria o
    problema — e, de quebra, os testes passariam a bater na rede de verdade."""
    fonte = Path(telegram.__file__).read_text(encoding="utf-8")
    corpo = fonte.split("_sessao.mount(", 1)[1]
    for chamada in ("requests.post(", "requests.get("):
        assert chamada not in corpo, f"{chamada} direto, fora da sessão"
    assert "_sessao.post(" in corpo and "_sessao.get(" in corpo


def test_log_mascara_token_de_qualquer_origem():
    """Em 22/09 o aviso de nova tentativa do urllib3 gravou a URL com o token no watcher.log."""
    token = "8123456789:AAHfake-Token_que_nao_pode_vazar_1234567"
    registro = logging.LogRecord(
        "urllib3.connectionpool", logging.WARNING, __file__, 1,
        "Retrying after connection broken: /bot%s/getUpdates", (token,), None,
    )
    telegram.FiltroDeToken().filter(registro)
    assert token not in registro.getMessage() and "/bot<token>/getUpdates" in registro.getMessage()


# ---------------------------------------------------------------------------
# /diario e /planejamento guiados
# ---------------------------------------------------------------------------

FORM_MSG = 77


class _Contexto:
    """Contexto falso do formulário: nada de vault nos testes do bot."""

    def __init__(self, plano=None, amanha="", pendentes=(), projetos=("Climatizando", "PPJ"), entradas=0):
        self._plano, self._amanha, self._pendentes = plano, amanha, list(pendentes)
        self._projetos, self._entradas = list(projetos), entradas

    def plano(self, dia):
        return self._plano

    def entradas_diario(self, dia):
        return self._entradas

    def amanha(self, dia):
        return self._amanha

    def pendentes(self, dia):
        return self._pendentes

    def projetos(self):
        return self._projetos


def _bot_guiado(api, contexto=None):
    api.return_value = {"message_id": FORM_MSG}
    bot = Bot(TOKEN)
    bot.fonte_contexto = contexto or _Contexto()
    return bot


def _form_atual(api):
    """A tela mais recente do formulário: a enviada ou a última trocada."""
    chamadas = [c.kwargs for c in api.call_args_list if c.args[1] in ("sendMessage", "editMessageText")
                and "inline_keyboard" in (c.kwargs.get("reply_markup") or {})]
    return chamadas[-1]


def _tocar(bot, api, rotulo):
    dados = _dados(_form_atual(api))
    [dado] = [d for r, d in dados.items() if r == rotulo or r.startswith(rotulo)]
    bot.handle_update(_toque(dado, message_id=FORM_MSG))


def _avisos(api):
    return [c.get("text") for c in _chamadas(api, "answerCallbackQuery") if c.get("text")]


def test_diario_abre_sessao_com_rota_e_formulario(owner, api):
    bot = _bot_guiado(api)
    bot.handle_update(_update("/diario"))

    manifest = bot.session.manifest
    assert manifest["rota"] == "diario" and manifest["form"]["msg_id"] == FORM_MSG
    assert manifest["form"]["etapas"][0] == "humor"  # sem plano do dia, não pergunta se cumpriu
    assert manifest["mensagens"] == [] and _staged() == [BUNDLE]
    [envio] = _chamadas(api, "sendMessage")
    assert "nuvem" in envio["text"] and "Etapa 1 de 8" in envio["text"]
    assert set(_dados(envio)) >= {"1", "5", "Pular", "Concluir"}


def test_diario_com_texto_entra_como_primeira_mensagem_sem_prefixo(owner, api):
    bot = _bot_guiado(api)
    bot.handle_update(_update("/diario hoje foi longo"))
    assert bot.session.manifest["mensagens"][0]["texto"] == "hoje foi longo"


def test_toque_grava_troca_a_mensagem_e_renova_a_janela(owner, api):
    bot = _bot_guiado(api)
    bot.handle_update(_update("/diario"))
    bot.session.last_at -= 9 * 60
    _tocar(bot, api, "4")

    assert bot.session.manifest["formulario"] == {"humor_nota": 4}
    assert bot.session.manifest["form"]["passo"] == 1
    [edit] = _chamadas(api, "editMessageText")
    assert edit["message_id"] == FORM_MSG and "energia" in edit["text"]
    assert bot.expire_if_idle(now=bot.session.last_at + 5 * 60) is None  # o toque renovou a janela
    salvo = json.loads((telegram.staging_dir() / BUNDLE / "manifest.json").read_text(encoding="utf-8"))
    assert salvo["formulario"] == {"humor_nota": 4}


def test_mensagem_durante_a_pergunta_ganha_o_numero_dela(owner, api):
    bot = _bot_guiado(api)
    bot.handle_update(_update("/diario"))
    bot.handle_update(_update("antes das perguntas", message_id=8))
    for _ in range(5):
        _tocar(bot, api, "Pular")
    _tocar(bot, api, "Próxima")  # pergunta 1 -> 2
    bot.handle_update(_update("travou o deploy", message_id=9))

    mensagens = bot.session.manifest["mensagens"]
    assert "pergunta" not in mensagens[0]
    assert mensagens[1]["pergunta"] == 2
    assert _replies(api)[-1] == "Texto · na pergunta 2"


def test_concluir_so_com_escalas_entrega_o_pacote(owner, api):
    bot = _bot_guiado(api)
    bot.handle_update(_update("/diario"))
    _tocar(bot, api, "3")
    _tocar(bot, api, "Concluir")

    assert bot.session is None and _inbox() == [BUNDLE]
    manifest = _manifest(BUNDLE)
    assert manifest["motivo_fechamento"] == "concluido" and manifest["formulario"] == {"humor_nota": 3}
    ultimo = _chamadas(api, "editMessageText")[-1]
    assert "fechado" in ultimo["text"] and ultimo["reply_markup"] == {"inline_keyboard": []}


def test_concluir_sem_resposta_nenhuma_descarta(owner, api):
    bot = _bot_guiado(api)
    bot.handle_update(_update("/diario"))
    _tocar(bot, api, "Concluir")
    assert _inbox() == [] and _staged() == []


def test_janela_vencida_no_meio_do_formulario_entrega_e_fecha_a_tela(owner, api):
    bot = _bot_guiado(api)
    bot.handle_update(_update("/diario"))
    _tocar(bot, api, "5")
    bot.session.last_at -= 11 * 60

    assert bot.expire_if_idle() == BUNDLE
    assert _manifest(BUNDLE)["motivo_fechamento"] == "janela"
    assert "fechado" in _chamadas(api, "editMessageText")[-1]["text"]


def test_botao_de_formulario_fechado_vira_aviso(owner, api):
    bot = _bot_guiado(api)
    bot.handle_update(_update("/diario"))
    dados = _dados(_form_atual(api))
    bot.handle_update(_update("/nova", message_id=8))
    api.reset_mock()
    bot.handle_update(_toque(dados["2"], message_id=FORM_MSG))

    assert _avisos(api) == ["Esse formulário já fechou."]
    assert not _chamadas(api, "editMessageText")


def test_tela_antiga_do_mesmo_formulario_nao_aplica_duas_vezes(owner, api):
    bot = _bot_guiado(api)
    bot.handle_update(_update("/diario"))
    dados = _dados(_form_atual(api))
    bot.handle_update(_toque(dados["2"], message_id=FORM_MSG))
    bot.handle_update(_toque(dados["5"], message_id=FORM_MSG))  # toque duplo na mesma tela

    assert bot.session.manifest["formulario"] == {"humor_nota": 2}
    assert "já passou" in _avisos(api)[-1]


def test_planejamento_fecha_a_sessao_comum_aberta(owner, api):
    bot = _bot_guiado(api)
    bot.handle_update(_update("ideia solta", message_id=5))
    bot.handle_update(_update("/planejamento", message_id=6))

    assert _inbox() == ["2026-09-14 15-02-11 tg 5"]
    assert bot.session.name == "2026-09-14 15-02-11 tg 6" and bot.session.manifest["rota"] == "planejamento"


def test_nova_com_formulario_so_de_botoes(owner, api):
    bot = _bot_guiado(api)
    bot.handle_update(_update("/diario"))
    _tocar(bot, api, "4")
    bot.handle_update(_update("/nova", message_id=8))
    assert _inbox() == [BUNDLE]
    assert "só com o check-in" in _replies(api)[-1]


def test_recover_entrega_pacote_so_com_formulario(owner, api):
    bot = _bot_guiado(api)
    bot.handle_update(_update("/diario"))
    _tocar(bot, api, "4")

    novo = Bot(TOKEN)  # o processo caiu e voltou
    assert novo.recover() == [BUNDLE]
    assert _manifest(BUNDLE)["formulario"] == {"humor_nota": 4}


def test_responder_a_mensagem_do_formulario_fica_na_sessao(owner, api):
    bot = _bot_guiado(api)
    bot.handle_update(_update("/diario"))
    bot.handle_update(_update("resposta", message_id=8, reply_to_message={"message_id": FORM_MSG}))
    assert bot.session.size == 1 and _inbox() == []


def test_diario_confere_o_plano_do_dia(owner, api):
    contexto = _Contexto(plano={"entradas": 1, "foco": "[[PPJ]]", "intencao": "entregar a nota técnica",
                                "proxima_acao": "", "acoes": []})
    bot = _bot_guiado(api, contexto)
    bot.handle_update(_update("/diario"))
    envio = _form_atual(api)
    assert "entregar a nota técnica" in envio["text"] and "Etapa 1 de 9" in envio["text"]
    _tocar(bot, api, "Em parte")
    assert bot.session.manifest["formulario"] == {"plano_cumprido": "parcial"}


def test_planejamento_para_amanha_traz_diario_pendentes_e_foco(owner, api):
    from logos import formulario

    contexto = _Contexto(amanha="começar pelo relatório", pendentes=["Revisar orçamento", "Ligar para a Lorena"])
    bot = _bot_guiado(api, contexto)
    bot.handle_update(_update("/planejamento"))

    rotulos = list(_dados(_form_atual(api)))
    assert rotulos[0].startswith("Hoje · ") and rotulos[1].startswith("Amanhã · ")
    _tocar(bot, api, "Amanhã")
    assert bot.session.manifest["form"]["etapas"][:3] == ["para", "diario", "pendentes"]
    tela = _form_atual(api)["text"]
    assert "começar pelo relatório" in tela and "git" in tela

    _tocar(bot, api, "Levar para o plano")
    _tocar(bot, api, "[ ] Ligar")
    assert "[x] Ligar para a Lorena" in _dados(_form_atual(api))
    _tocar(bot, api, "Trazer marcadas")
    _tocar(bot, api, "4")             # energia
    _tocar(bot, api, "2 a 4h")        # tempo livre
    _tocar(bot, api, "Climatizando")  # foco

    manifest = bot.session.manifest
    assert manifest["para"] == "2026-09-15"
    assert manifest["formulario"] == {"horizonte": "amanha", "energia": 4, "tempo_livre": "2–4h",
                                      "foco": "[[Climatizando]]"}
    assert manifest["carregar"]["do_diario"] == "começar pelo relatório"
    assert manifest["carregar"]["trazidas"] == ["Ligar para a Lorena"]
    assert formulario.pergunta_atual(manifest) == 1


def test_telas_do_formulario_nao_tem_emoji(owner, api):
    bot = _bot_guiado(api, _Contexto(amanha="x", pendentes=["a"]))
    bot.handle_update(_update("/planejamento"))
    for rotulo in ("Amanhã", "Só ver", "Nenhuma", "Pular", "Pular", "Nenhum", "Próxima"):
        _tocar(bot, api, rotulo)
    for chamada in _chamadas(api, "sendMessage") + _chamadas(api, "editMessageText"):
        textos = [chamada["text"]]
        if "inline_keyboard" in (chamada.get("reply_markup") or {}):
            textos += list(_dados(chamada))
        for texto in textos:
            assert not any(ord(c) > 0x2500 for c in texto), texto
