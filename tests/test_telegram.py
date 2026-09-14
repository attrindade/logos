"""Etapa 3: bot Telegram (captura, dono, comandos, voz) e CLI send/check. Sem rede."""
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
from logos.triage import triage

TOKEN = "123456:SEGREDO-NAO-VAZAR"
OWNER = 42
DATE = int(datetime(2026, 9, 14, 15, 2, 11).timestamp())


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "Inbox")
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


def _update(text=None, chat=OWNER, message_id=7, **extra):
    message = {"message_id": message_id, "date": DATE, "chat": {"id": chat}, **extra}
    if text is not None:
        message["text"] = text
    return {"update_id": 1000 + message_id, "message": message}


def _replies(api):
    return [c.kwargs["text"] for c in api.call_args_list if c.args[1] == "sendMessage"]


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
    assert list(config.INBOX_DIR.iterdir()) == []
    api.assert_not_called()


def test_outro_chat_e_ignorado_e_o_conteudo_nao_vai_ao_log(owner, api, caplog):
    with caplog.at_level(logging.INFO):
        Bot(TOKEN).handle_update(_update("segredo do intruso", chat=999))
    assert list(config.INBOX_DIR.iterdir()) == []
    api.assert_not_called()
    assert "999" in caplog.text and "segredo do intruso" not in caplog.text


def test_texto_vira_arquivo_da_inbox_com_confirmacao(owner, api):
    Bot(TOKEN).handle_update(_update("tarefa urgente testar o bot"))

    name = "2026-09-14 15-02-11 tg 7.txt"
    assert [p.name for p in config.INBOX_DIR.iterdir()] == [name]
    assert (config.INBOX_DIR / name).read_text(encoding="utf-8") == "tarefa urgente testar o bot\n"
    assert _replies(api) == [f"📥 {name}"]
    reply_call = api.call_args_list[0]
    assert reply_call.kwargs["reply_parameters"]["message_id"] == 7

    t = triage(name, "tarefa urgente testar o bot")
    assert (t.canal, t.categoria, t.prioridade) == ("telegram", "tarefa", "alta")
    assert t.recorded_at == datetime(2026, 9, 14, 15, 2, 11)


def test_comando_prefixa_a_primeira_palavra(owner, api):
    Bot(TOKEN).handle_update(_update("/tarefa@LogosBot comprar pão"))
    [path] = config.INBOX_DIR.iterdir()
    assert path.read_text(encoding="utf-8") == "tarefa comprar pão\n"


def test_comando_sem_texto_responde_uso(owner, api):
    Bot(TOKEN).handle_update(_update("/ideia"))
    assert list(config.INBOX_DIR.iterdir()) == []
    assert _replies(api) == ["Uso: /ideia <texto>"]


@pytest.mark.parametrize("text", ["/diario hoje foi longo", "Diário: hoje foi longo"])
def test_diario_ganha_lembrete_da_nuvem(owner, api, text):
    Bot(TOKEN).handle_update(_update(text))
    [path] = config.INBOX_DIR.iterdir()
    assert path.read_text(encoding="utf-8").lower().startswith("diário")
    assert "nuvem" in _replies(api)[0]


def test_nova_e_comando_desconhecido_respondem_sem_gravar(owner, api):
    bot = Bot(TOKEN)
    bot.handle_update(_update("/nova", message_id=1))
    bot.handle_update(_update("/xyz", message_id=2))
    assert list(config.INBOX_DIR.iterdir()) == []
    first, second = _replies(api)
    assert "Etapa 4" in first and "/tarefa" in second


def test_reentrega_nao_duplica(owner, api):
    bot = Bot(TOKEN)
    bot.handle_update(_update("ideia uma"))
    bot.handle_update(_update("ideia uma"))
    assert len(list(config.INBOX_DIR.iterdir())) == 1


def test_mensagens_no_mesmo_segundo_nao_colidem(owner, api):
    bot = Bot(TOKEN)
    bot.handle_update(_update("um", message_id=1))
    bot.handle_update(_update("dois", message_id=2))
    assert len(list(config.INBOX_DIR.iterdir())) == 2


def test_voz_e_baixada_como_oga(owner, api):
    voice = {"file_id": "abc", "file_size": 12345, "duration": 4}
    with patch.object(telegram, "download_file") as download:
        Bot(TOKEN).handle_update(_update(voice=voice))
    download.assert_called_once()
    assert download.call_args.args[1] == "abc"
    assert download.call_args.args[2] == config.INBOX_DIR / "2026-09-14 15-02-11 tg 7.oga"
    assert _replies(api) == ["📥 2026-09-14 15-02-11 tg 7.oga"]


def test_voz_acima_de_20mb_e_recusada(owner, api):
    voice = {"file_id": "abc", "file_size": 25 * 1024 * 1024}
    with patch.object(telegram, "download_file") as download:
        Bot(TOKEN).handle_update(_update(voice=voice))
    download.assert_not_called()
    assert "20 MB" in _replies(api)[0]


def test_audio_mantem_a_extensao(owner, api):
    audio = {"file_id": "abc", "file_size": 10, "file_name": "gravacao.m4a", "mime_type": "audio/mp4"}
    with patch.object(telegram, "download_file") as download:
        Bot(TOKEN).handle_update(_update(audio=audio))
    assert download.call_args.args[2].suffix == ".m4a"


def test_foto_responde_que_chega_na_etapa_4(owner, api):
    Bot(TOKEN).handle_update(_update(photo=[{"file_id": "x"}], caption="quadro"))
    assert list(config.INBOX_DIR.iterdir()) == []
    assert "Etapa 4" in _replies(api)[0]


def test_falha_ao_salvar_avisa_o_usuario(owner, api):
    with patch.object(telegram, "download_file", side_effect=TelegramError("getFile: 400")):
        Bot(TOKEN).handle_update(_update(voice={"file_id": "abc", "file_size": 1}))
    assert "Reenvie" in _replies(api)[0]


def test_download_grava_por_temporario(env):
    dest = config.INBOX_DIR / "2026-09-14 15-02-11 tg 7.oga"
    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.iter_content.return_value = [b"OggS", b"dados"]
    with patch.object(telegram, "call", return_value={"file_path": "voice/file_1.oga"}), \
         patch.object(telegram.requests, "get", return_value=resp) as get:
        telegram.download_file(TOKEN, "abc", dest)
    assert get.call_args.args[0] == f"https://api.telegram.org/file/bot{TOKEN}/voice/file_1.oga"
    assert dest.read_bytes() == b"OggSdados"
    assert [p.name for p in config.INBOX_DIR.iterdir()] == [dest.name]


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

def test_polling_avanca_offset_e_publica_status(owner):
    stop = threading.Event()
    seen_params = []

    def fake_call(token, method, **kwargs):
        if method == "getMe":
            return {"username": "LogosBot"}
        if method == "getUpdates":
            seen_params.append(kwargs)
            if len(seen_params) == 1:
                return [_update("ideia testar polling", message_id=9)]
            stop.set()
            return []
        return {}

    with patch.object(telegram, "call", side_effect=fake_call), \
         patch("logos.status.tracker.set_bot") as set_bot:
        Bot(TOKEN).run_polling(stop)

    assert "offset" not in seen_params[0]
    assert seen_params[1]["offset"] == 1010  # update_id 1009 + 1
    assert seen_params[0]["allowed_updates"] == ["message"]
    assert (config.INBOX_DIR / "2026-09-14 15-02-11 tg 9.txt").exists()
    assert set_bot.call_args.args[0] == "polling"
    assert set_bot.call_args.kwargs["username"] == "LogosBot"


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


def test_send_pdf_vai_como_documento(owner, api, tmp_path):
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
