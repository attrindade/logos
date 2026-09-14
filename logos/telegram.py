"""Bot Telegram do Logos: captura de texto e voz por long polling, e envio pela linha de comando.

Contrato com os jobs do vault (sessões A e C), a partir da raiz do repo com o .venv:

    python -m logos.telegram send --text "<texto>" [--file <caminho>]
    python -m logos.telegram check

Códigos de saída: 0 ok, 1 falha no envio, 2 bot não configurado (sem token ou sem dono).

Decisões:
- `requests` puro, não `python-telegram-bot`. A PTB 22 é só asyncio: numa thread do watcher
  exigiria um event loop próprio, e o `send` montaria uma Application para uma mensagem.
  Long polling é um laço de `getUpdates`; `getFile`, `reply_to_message` e `media_group_id`
  (Etapa 4) são campos simples da API.
- Thread dentro do processo do watcher, não segundo processo: um lock, um log, um relançamento,
  e um único escritor do status.json (o StatusTracker regrava o arquivo inteiro).
- Este módulo não importa `logos.status` no topo: o `send` roda enquanto o watcher está vivo e
  não pode sobrescrever o status.json dele.

O bot só escreve na Inbox, sempre por temporário com ponto + rename; quem gera a nota é o
pipeline. Nome: `YYYY-MM-DD HH-MM-SS tg <message_id>.<ext>`. O sufixo `tg` marca `canal: telegram`
e o `message_id` evita colisão no mesmo segundo e duplicata quando o Telegram reentrega updates.
"""
import argparse
import logging
import mimetypes
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

import requests
import yaml

from . import config
from .triage import TELEGRAM_TAG, _normalize_word

logger = logging.getLogger(__name__)

API = "https://api.telegram.org"
POLL_TIMEOUT_S = 30
MAX_BACKOFF_S = 60
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # limite de download da Bot API
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MESSAGE_LIMIT = 4096
CAPTION_LIMIT = 1024
PHOTO_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

EXIT_OK, EXIT_FAILED, EXIT_NOT_CONFIGURED = 0, 1, 2

# comando -> primeira palavra que a triagem entende
PREFIX_COMMANDS = {"tarefa": "tarefa", "ideia": "ideia", "planejamento": "planejamento", "diario": "diário"}

DIARIO_AVISO = (
    "Lembrete: Diário pelo Telegram passa pela nuvem do Telegram. "
    "O caminho do Diário continua sendo o gravador + Syncthing."
)
AJUDA = (
    "Mande texto ou voz; cada mensagem vira uma nota.\n"
    "/tarefa <texto> · /ideia <texto> · /planejamento <texto> · /diario <texto>\n"
    "/nova fecha a sessão (a partir da Etapa 4)"
)


class TelegramError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Segredos (fora do repo): state/secrets.yaml ou LOGOS_TELEGRAM_TOKEN
# ---------------------------------------------------------------------------

def secrets_path() -> Path:
    return config.STATE_DIR / "secrets.yaml"


def load_secrets() -> dict:
    try:
        data = yaml.safe_load(secrets_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return data if isinstance(data, dict) else {}


def save_secrets(updates: dict) -> None:
    path = secrets_path()
    data = load_secrets()
    data.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)


def get_token() -> str | None:
    return os.getenv("LOGOS_TELEGRAM_TOKEN") or load_secrets().get("telegram_token") or None


def get_owner_chat_id() -> int | None:
    value = load_secrets().get("telegram_chat_id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _redact(text: str, token: str) -> str:
    return text.replace(token, "<token>") if token else text


def call(token: str, method: str, *, http_timeout: float = 15, files: dict | None = None, **params):
    """Chama um método da Bot API. Erros nunca carregam o token (a URL o contém).

    `http_timeout` não se chama `timeout` porque `getUpdates` tem um parâmetro `timeout` próprio."""
    url = f"{API}/bot{token}/{method}"
    try:
        if files:
            resp = requests.post(url, data=params, files=files, timeout=http_timeout)
        else:
            resp = requests.post(url, json=params, timeout=http_timeout)
    except requests.RequestException as e:
        raise TelegramError(f"{method}: {_redact(str(e), token)}") from None
    try:
        payload = resp.json()
    except ValueError:
        raise TelegramError(f"{method}: HTTP {resp.status_code}", code=resp.status_code) from None
    if not payload.get("ok"):
        code = payload.get("error_code")
        raise TelegramError(f"{method}: {code} {payload.get('description', '')}".strip(), code=code)
    return payload["result"]


def download_file(token: str, file_id: str, dest: Path) -> None:
    info = call(token, "getFile", file_id=file_id)
    url = f"{API}/file/bot{token}/{info['file_path']}"
    tmp = dest.with_name(f".{dest.name}.tmp")
    try:
        with requests.get(url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(1 << 16):
                    f.write(chunk)
    except requests.RequestException as e:
        tmp.unlink(missing_ok=True)
        raise TelegramError(f"download: {_redact(str(e), token)}") from None
    os.replace(tmp, dest)


# ---------------------------------------------------------------------------
# Captura
# ---------------------------------------------------------------------------

def capture_name(message: dict, ext: str) -> str:
    stamp = datetime.fromtimestamp(message["date"]).strftime("%Y-%m-%d %H-%M-%S")
    return f"{stamp} {TELEGRAM_TAG} {message['message_id']}{ext}"


def _parse_command(text: str) -> tuple[str, str]:
    """'/tarefa@LogosBot comprar pão' -> ('tarefa', 'comprar pão')."""
    if not text.startswith("/"):
        return "", ""
    parts = text.split(maxsplit=1)
    name = parts[0][1:].split("@", 1)[0].lower()
    return name, parts[1].strip() if len(parts) > 1 else ""


def _starts_with_diario(text: str) -> bool:
    words = text.split()
    return bool(words) and _normalize_word(words[0]) in ("diario", "diarios")


def _audio_ext(message: dict, media: dict) -> str:
    if message.get("voice"):
        return ".oga"
    suffix = Path(media.get("file_name") or "").suffix
    return suffix or mimetypes.guess_extension(media.get("mime_type") or "") or ".audio"


class Bot:
    def __init__(self, token: str):
        self.token = token

    @property
    def inbox(self) -> Path:
        return config.INBOX_DIR

    def reply(self, message: dict, text: str) -> None:
        try:
            call(
                self.token, "sendMessage",
                chat_id=message["chat"]["id"], text=text,
                reply_parameters={"message_id": message["message_id"], "allow_sending_without_reply": True},
            )
        except TelegramError as e:
            logger.warning(f"Telegram: falha ao responder: {e}")

    def handle_update(self, update: dict) -> None:
        message = update.get("message")
        if not message:
            return
        chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()
        command, rest = _parse_command(text)
        owner = get_owner_chat_id()

        if owner is None:
            if command == "start":
                save_secrets({"telegram_chat_id": chat_id})
                logger.info(f"Telegram: dono registrado, chat_id={chat_id}")
                self.reply(message, "Logos conectado a este chat. Mande texto ou voz.\n\n" + AJUDA)
            else:
                logger.warning(f"Telegram: mensagem ignorada, dono ainda não registrado (chat_id={chat_id}). Mande /start.")
            return
        if chat_id != owner:
            logger.warning(f"Telegram: mensagem de chat não autorizado ignorada (chat_id={chat_id})")
            return

        try:
            if command:
                self._handle_command(message, command, rest)
            elif text:
                self._capture_text(message, text)
            elif message.get("voice") or message.get("audio"):
                self._capture_audio(message, message.get("voice") or message.get("audio"))
            else:
                self.reply(message, "Por enquanto só texto e voz. Foto, PDF e documentos chegam na Etapa 4.")
        except Exception:
            logger.exception(f"Telegram: falha ao capturar a mensagem {message.get('message_id')}")
            self.reply(message, "⚠️ Não consegui salvar esta mensagem. Reenvie, por favor.")

    def _handle_command(self, message: dict, command: str, rest: str) -> None:
        if command == "start":
            self.reply(message, "Logos já está conectado a este chat.\n\n" + AJUDA)
        elif command in PREFIX_COMMANDS:
            if not rest:
                self.reply(message, f"Uso: /{command} <texto>")
                return
            self._capture_text(message, f"{PREFIX_COMMANDS[command]} {rest}")
        elif command == "nova":
            self.reply(message, "Anotado. A janela de sessão chega na Etapa 4; hoje cada mensagem já vira uma nota.")
        else:
            self.reply(message, AJUDA)

    def _capture_text(self, message: dict, text: str) -> None:
        name = capture_name(message, ".txt")
        dest = self.inbox / name
        if dest.exists():
            logger.info(f"Telegram: reentrega ignorada, {name} já existe")
        else:
            tmp = dest.with_name(f".{name}.tmp")
            tmp.write_text(text + "\n", encoding="utf-8")
            os.replace(tmp, dest)
            logger.info(f"Telegram: texto salvo em {name}")
        ack = f"📥 {name}"
        if _starts_with_diario(text):
            ack += "\n" + DIARIO_AVISO
        self.reply(message, ack)

    def _capture_audio(self, message: dict, media: dict) -> None:
        size = media.get("file_size") or 0
        if size > MAX_DOWNLOAD_BYTES:
            self.reply(
                message,
                f"Áudio de {size / 1024 / 1024:.1f} MB passa do limite de 20 MB da Bot API. Mande pelo gravador + Syncthing.",
            )
            return
        name = capture_name(message, _audio_ext(message, media))
        dest = self.inbox / name
        if dest.exists():
            logger.info(f"Telegram: reentrega ignorada, {name} já existe")
        else:
            download_file(self.token, media["file_id"], dest)
            logger.info(f"Telegram: voz salva em {name}")
        self.reply(message, f"📥 {name}")

    def run_polling(self, stop: threading.Event) -> None:
        from .status import tracker  # só no processo do watcher

        username = None
        offset = None
        backoff = 1
        while not stop.is_set():
            try:
                if username is None:
                    username = call(self.token, "getMe")["username"]
                    logger.info(f"Telegram: bot @{username} conectado (long polling)")
                params = {"timeout": POLL_TIMEOUT_S, "allowed_updates": ["message"]}
                if offset is not None:
                    params["offset"] = offset
                updates = call(self.token, "getUpdates", http_timeout=POLL_TIMEOUT_S + 5, **params)
                backoff = 1
                tracker.set_bot(
                    "polling", username=username, owner_registered=get_owner_chat_id() is not None,
                    last_poll=datetime.now().isoformat(), last_error=None,
                )
                for update in updates:
                    offset = update["update_id"] + 1  # avança mesmo se falhar: não travar numa mensagem
                    self.handle_update(update)
            except TelegramError as e:
                if e.code == 401:
                    logger.error("Telegram: token recusado (401). Bot desligado até corrigir o secrets.yaml e relançar.")
                    tracker.set_bot("error", username=username, last_error="token recusado (401)")
                    return
                if e.code == 409:
                    logger.error("Telegram: conflito 409, outro processo lê getUpdates com este token.")
                else:
                    logger.warning(f"Telegram: {e}. Nova tentativa em {backoff}s.")
                tracker.set_bot("error", username=username, last_error=str(e))
                stop.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)
            except Exception as e:
                logger.exception("Telegram: erro inesperado no polling")
                tracker.set_bot("error", username=username, last_error=str(e))
                stop.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)


def start_bot_thread(stop: threading.Event) -> threading.Thread | None:
    """Sobe o bot numa thread daemon do watcher. Sem token, registra `off` e segue sem bot."""
    from .status import tracker

    token = get_token()
    if not token:
        logger.info(f"Telegram: sem token ({secrets_path()} ou LOGOS_TELEGRAM_TOKEN), bot desligado.")
        tracker.set_bot("off", last_error=None)
        return None
    thread = threading.Thread(target=Bot(token).run_polling, args=(stop,), name="telegram-bot", daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _chunks(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks or [""]


def send(text: str | None, file: Path | None) -> int:
    token, chat_id = get_token(), get_owner_chat_id()
    if not token or chat_id is None:
        missing = "token" if not token else "dono (mande /start ao bot com o watcher rodando)"
        print(f"Bot não configurado: falta {missing}. Veja {secrets_path()}.", file=sys.stderr)
        return EXIT_NOT_CONFIGURED
    try:
        if file is None:
            for chunk in _chunks(text):
                call(token, "sendMessage", chat_id=chat_id, text=chunk)
            return EXIT_OK

        if not file.is_file():
            print(f"Arquivo não encontrado: {file}", file=sys.stderr)
            return EXIT_FAILED
        size = file.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            print(f"Arquivo de {size / 1024 / 1024:.1f} MB passa do limite de 50 MB da Bot API.", file=sys.stderr)
            return EXIT_FAILED

        caption = text if text and len(text) <= CAPTION_LIMIT else None
        if text and caption is None:
            for chunk in _chunks(text):
                call(token, "sendMessage", chat_id=chat_id, text=chunk)
        is_photo = file.suffix.lower() in PHOTO_EXTS and size <= MAX_PHOTO_BYTES
        method, field = ("sendPhoto", "photo") if is_photo else ("sendDocument", "document")
        params = {"chat_id": chat_id}
        if caption:
            params["caption"] = caption
        with open(file, "rb") as f:
            call(token, method, http_timeout=120, files={field: (file.name, f)}, **params)
        return EXIT_OK
    except TelegramError as e:
        print(f"Falha no envio: {e}", file=sys.stderr)
        return EXIT_FAILED


def check() -> int:
    token = get_token()
    if not token:
        print(f"Sem token: grave telegram_token em {secrets_path()} ou defina LOGOS_TELEGRAM_TOKEN.")
        return EXIT_NOT_CONFIGURED
    try:
        me = call(token, "getMe")
    except TelegramError as e:
        print(f"Token não aceito: {e}")
        return EXIT_FAILED
    print(f"Bot: @{me['username']}")
    owner = get_owner_chat_id()
    if owner is None:
        print("Dono: ainda não registrado. Com o watcher rodando, mande /start ao bot pelo celular.")
        return EXIT_NOT_CONFIGURED
    print(f"Dono: chat_id {owner}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m logos.telegram", description="Bot Telegram do Logos.")
    sub = parser.add_subparsers(dest="command", required=True)
    send_parser = sub.add_parser("send", help="manda texto e/ou arquivo ao dono do bot")
    send_parser.add_argument("--text", help="texto da mensagem (legenda, se couber, quando houver --file)")
    send_parser.add_argument("--file", type=Path, help="imagem vai como foto; o resto como documento")
    sub.add_parser("check", help="confere token e dono registrado")
    args = parser.parse_args(argv)

    if args.command == "send":
        if not args.text and not args.file:
            parser.error("informe --text e/ou --file")
        return send(args.text, args.file)
    return check()


if __name__ == "__main__":
    sys.exit(main())
