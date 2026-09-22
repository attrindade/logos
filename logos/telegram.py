"""Bot Telegram do Logos: captura multimodal por long polling, e envio pela linha de comando.

Contrato com os jobs do vault (sessões A e C), a partir da raiz do repo com o .venv:

    python -m logos.telegram send --text "<texto>" [--file <caminho>]
    python -m logos.telegram check

Códigos de saída: 0 ok, 1 falha no envio, 2 bot não configurado (sem token ou sem dono).

Decisões:
- `requests` puro, não `python-telegram-bot`. A PTB 22 é só asyncio: numa thread do watcher
  exigiria um event loop próprio, e o `send` montaria uma Application para uma mensagem.
  Long polling é um laço de `getUpdates`; `getFile`, `reply_to_message` e `media_group_id`
  são campos simples da API.
- Thread dentro do processo do watcher, não segundo processo: um lock, um log, um relançamento,
  e um único escritor do status.json (o StatusTracker regrava o arquivo inteiro).
- Este módulo não importa `logos.status` no topo: o `send` roda enquanto o watcher está vivo e
  não pode sobrescrever o status.json dele.

Pacotes (Etapa 4): mensagens com menos de `telegram_window_min` entre si formam uma captura. O
pacote é montado em `state/telegram/aberto/<nome>/` (manifest.json regravado a cada mensagem) e,
ao fechar, a pasta inteira é renomeada para a Inbox: o watcher só vê pacotes completos e nada é
movido dentro da Inbox. Fecha com /nova, com a janela vencida (checada a cada volta do polling,
no máximo 30 s de atraso) ou no início do bot (pacotes que uma queda ou reinício deixou abertos).
Nome: `YYYY-MM-DD HH-MM-SS tg <message_id da primeira mensagem>`; o sufixo `tg` marca
`canal: telegram` e o id evita colisão no mesmo segundo. Uma resposta a mensagem fora do pacote
aberto vira pacote próprio, fechado na hora, com `reply_to`: o pipeline anexa o conteúdo à nota
original, achada pelo message_id no ledger.
"""
import argparse
import json
import logging
import mimetypes
import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests
import urllib3
import yaml

from . import config, consulta
from .triage import TELEGRAM_TAG, _normalize_word

logger = logging.getLogger(__name__)

API = "https://api.telegram.org"
POLL_TIMEOUT_S = 30
MAX_BACKOFF_S = 60
# Timeout de long poll é rotina e não vai para o log — mas silêncio absoluto esconderia uma
# queda de rede prolongada, que é o tipo de falha silenciosa que já custou caro aqui. A cada
# tanto de timeouts seguidos sai uma linha, para a ausência de mensagens ter explicação.
TIMEOUTS_ATE_AVISAR = 10
# Espera depois de um timeout, em qualquer fase. Curta de propósito: o tempo de espera já foi
# pago pelo próprio timeout, e recuar mais só adia a volta numa rede que oscila.
TIMEOUT_RETENTATIVA_S = 1
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # limite de download da Bot API
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MESSAGE_LIMIT = 4096
CAPTION_LIMIT = 1024
PHOTO_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
MANIFEST = "manifest.json"
MANIFEST_VERSION = 1

EXIT_OK, EXIT_FAILED, EXIT_NOT_CONFIGURED = 0, 1, 2

# comando -> primeira palavra que a triagem entende
PREFIX_COMMANDS = {"tarefa": "tarefa", "ideia": "ideia", "planejamento": "planejamento", "diario": "diário"}
# Comandos de leitura do vault. Somente leitura: não abrem, não alimentam e não fecham sessão.
# Um atalho de uma letra por verbo, e nada além disso: sinônimo demais é vocabulário para
# decorar, que é o oposto do pedido. `0` é a tela inicial — o comando para quem não lembra
# de nenhum outro.
CONSULTA_COMMANDS = {
    "0": "inicio", "inicio": "inicio", "menu": "inicio",
    "ver": "ver", "v": "ver",
    "buscar": "buscar", "b": "buscar",
    "hoje": "hoje", "h": "hoje",
    "ajuda": "ajuda", "a": "ajuda",
}

# `/p3`, `/s2`, `/r1`: o número que apareceu na resposta anterior vira o comando seguinte.
# É o que dá a curva de aprendizado curta — nada para memorizar, o próximo passo está na tela.
INDICE_RE = re.compile(r"^([psr])(\d{1,3})$")
KIND_LABELS = {
    "texto": "texto", "voz": "voz", "audio": "áudio", "foto": "foto",
    "documento": "documento", "localizacao": "localização",
}

# Menu que o Telegram mostra quando ele digita "/". Sem `setMyCommands` o app não sugere nada,
# e um comando que só existe no texto da ajuda é um comando que ele precisa lembrar — que é
# justamente o que o sistema dele não pode exigir. Captura primeiro, consulta depois.
MENU = [
    ("inicio", "para onde ir  (atalho: /0)"),
    ("ver", "projetos ativos, ou /ver ppj  (atalho: /v)"),
    ("buscar", "procura um termo nas notas  (atalho: /b)"),
    ("hoje", "o briefing mais recente  (atalho: /h)"),
    ("ajuda", "como mexer aqui  (atalho: /a)"),
    ("tarefa", "registra uma tarefa"),
    ("ideia", "registra uma ideia"),
    ("planejamento", "registra um planejamento"),
    ("diario", "registra no diário (passa pela nuvem)"),
    ("nova", "fecha a sessão de captura agora"),
]

DIARIO_AVISO = (
    "Lembrete: Diário pelo Telegram passa pela nuvem do Telegram. "
    "O caminho do Diário continua sendo o gravador + Syncthing."
)


def _fmt_min(value: float) -> str:
    return f"{value:g}"


def _ajuda() -> str:
    return (
        "Capturar: mande texto, voz, foto, PDF, documento, link ou localização. Mensagens "
        f"com até {_fmt_min(config.TELEGRAM_WINDOW_MIN)} min de intervalo viram uma nota só.\n"
        "/tarefa <texto> · /ideia <texto> · /planejamento <texto> · /diario <texto>\n"
        "/nova fecha a sessão agora. Responder a uma mensagem antiga complementa a nota dela.\n"
        "\n"
        "Consultar: /0 mostra para onde ir. /ajuda explica o resto."
    )


class TelegramError(Exception):
    def __init__(self, message: str, code: int | None = None, timeout: bool = False):
        super().__init__(message)
        self.code = code
        # `timeout` separa "a conexão não respondeu a tempo" de "a API recusou". No long poll do
        # getUpdates o primeiro é rotina, não falha: ver o laço em run_polling.
        self.timeout = timeout


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


# Uma conexão reaproveitada para todas as chamadas, em vez de uma nova por chamada.
#
# Medido em 22/09/2026 nesta máquina: com a conexão quente, o `getMe` responde em ~1 s; a
# primeira chamada, que precisa abrir TCP + TLS, falhou por timeout aos 10 s. O caro aqui não é
# a Bot API, é o aperto de mão com ela — e o `requests.post` solto abria um a cada chamada.
# Isso explicava a fila inteira de sintomas: `getMe` falhando na subida, `getUpdates` expirando
# sem nem devolver a lista vazia, e a resposta de uma consulta demorando quase um minuto para
# sair, porque o envio pagava outro aperto de mão.
#
# `read=0` é deliberado: repetir uma leitura que expirou num POST arriscaria mandar a mesma
# mensagem duas vezes. Erro de conexão pode ser repetido em paz, porque o pedido não chegou a
# sair. O long poll segura uma conexão por POLL_TIMEOUT_S, então o pool precisa de folga para o
# `notify` e as respostas do pipeline, que saem de outra thread.
_RETENTATIVA = urllib3.util.Retry(
    total=2, connect=2, read=0, status=0, other=0, backoff_factor=0.5, allowed_methods=None,
)
_sessao = requests.Session()
_sessao.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=4, pool_maxsize=8, max_retries=_RETENTATIVA,
))


def call(token: str, method: str, *, http_timeout: float = 15, files: dict | None = None, **params):
    """Chama um método da Bot API. Erros nunca carregam o token (a URL o contém).

    `http_timeout` não se chama `timeout` porque `getUpdates` tem um parâmetro `timeout` próprio."""
    url = f"{API}/bot{token}/{method}"
    try:
        if files:
            resp = _sessao.post(url, data=params, files=files, timeout=http_timeout)
        else:
            resp = _sessao.post(url, json=params, timeout=http_timeout)
    except requests.Timeout as e:
        raise TelegramError(f"{method}: {_redact(str(e), token)}", timeout=True) from None
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


DOWNLOAD_TENTATIVAS = 3


def download_file(token: str, file_id: str, dest: Path) -> None:
    """Baixa o anexo, com nova tentativa.

    Sem isso, uma conexão derrubada no meio do download custa a mensagem inteira: o `_add`
    levanta, a sessão fica vazia e é descartada, e o dono só recebe "reenvie, por favor".
    Aconteceu em 22/09/2026 com uma nota de voz. Um áudio de 2 MB numa rede instável merece
    três tentativas antes de virar pedido de reenvio."""
    info = call(token, "getFile", file_id=file_id)
    url = f"{API}/file/bot{token}/{info['file_path']}"
    tmp = dest.with_name(f".{dest.name}.tmp")
    for tentativa in range(1, DOWNLOAD_TENTATIVAS + 1):
        try:
            with _sessao.get(url, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_content(1 << 16):
                        f.write(chunk)
            os.replace(tmp, dest)
            return
        except requests.RequestException as e:
            tmp.unlink(missing_ok=True)
            if tentativa == DOWNLOAD_TENTATIVAS:
                raise TelegramError(f"download: {_redact(str(e), token)}") from None
            logger.warning(f"Telegram: download falhou ({tentativa}/{DOWNLOAD_TENTATIVAS}), tentando de novo.")
            time.sleep(tentativa)


# ---------------------------------------------------------------------------
# Pacotes
# ---------------------------------------------------------------------------

def staging_dir() -> Path:
    return config.STATE_DIR / "telegram" / "aberto"


def bundle_name(message: dict) -> str:
    stamp = datetime.fromtimestamp(message["date"]).strftime("%Y-%m-%d %H-%M-%S")
    return f"{stamp} {TELEGRAM_TAG} {message['message_id']}"


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")


def load_manifest(directory: Path) -> dict | None:
    try:
        data = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_manifest(directory: Path, manifest: dict) -> None:
    """Serialização determinística: o hash do manifest compõe a chave do pacote no ledger."""
    tmp = directory / f".{MANIFEST}.tmp"
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, directory / MANIFEST)


def finalize_bundle(directory: Path, manifest: dict, reason: str) -> Path:
    """Fecha o pacote e o entrega à Inbox com um rename da pasta inteira."""
    manifest["fechado_em"] = datetime.now().isoformat(timespec="seconds")
    manifest["motivo_fechamento"] = reason
    write_manifest(directory, manifest)

    dest = config.INBOX_DIR / directory.name
    if dest.exists():
        delivered = load_manifest(dest) or {}
        delivered_ids = {m.get("message_id") for m in delivered.get("mensagens", [])}
        if {m["message_id"] for m in manifest["mensagens"]} <= delivered_ids:
            logger.info(f"Telegram: {directory.name} já tinha sido entregue; cópia do staging descartada")
            shutil.rmtree(directory, ignore_errors=True)
            return dest
        k = 2
        while (config.INBOX_DIR / f"{directory.name} {k}").exists():
            k += 1
        dest = config.INBOX_DIR / f"{directory.name} {k}"

    try:
        os.replace(directory, dest)
    except OSError:
        shutil.move(str(directory), str(dest))  # outro volume: o watcher espera o pacote ficar completo
    logger.info(f"Telegram: pacote {dest.name} entregue à Inbox ({len(manifest['mensagens'])} mensagens, {reason})")
    return dest


def _utf16_slice(text: str, offset: int, length: int) -> str:
    """Offsets de entidades do Telegram são em unidades UTF-16."""
    data = text.encode("utf-16-le")
    return data[2 * offset: 2 * (offset + length)].decode("utf-16-le", errors="ignore")


def _links(message: dict) -> list[str]:
    text = message.get("text") or message.get("caption") or ""
    entities = message.get("entities") or message.get("caption_entities") or []
    urls = []
    for entity in entities:
        if entity.get("type") == "url":
            urls.append(_utf16_slice(text, entity["offset"], entity["length"]))
        elif entity.get("type") == "text_link" and entity.get("url"):
            urls.append(entity["url"])
    return list(dict.fromkeys(urls))


def _media_ext(media: dict, default: str) -> str:
    suffix = Path(media.get("file_name") or "").suffix.lower()
    return suffix or mimetypes.guess_extension(media.get("mime_type") or "") or default


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


@dataclass
class Session:
    directory: Path
    manifest: dict
    last_at: float

    @property
    def name(self) -> str:
        return self.directory.name

    @property
    def ids(self) -> set[int]:
        return {m["message_id"] for m in self.manifest["mensagens"]}

    @property
    def size(self) -> int:
        return len(self.manifest["mensagens"])


class Bot:
    def __init__(self, token: str):
        self.token = token
        self.session: Session | None = None
        # Memória curta da consulta, para `/p3` e `/s2` significarem alguma coisa. Morre com
        # o processo de propósito: ver consulta.Contexto.
        self.contexto = consulta.Contexto()

    def reply(self, message: dict, text: str) -> None:
        try:
            call(
                self.token, "sendMessage",
                chat_id=message["chat"]["id"], text=text,
                reply_parameters={"message_id": message["message_id"], "allow_sending_without_reply": True},
            )
        except TelegramError as e:
            logger.warning(f"Telegram: falha ao responder: {e}")

    def reply_html(self, message: dict, text: str) -> None:
        """Resposta formatada da consulta, quebrada em mensagens por linha inteira.

        `parse_mode` vive só aqui: `send()` e `notify()` mandam texto puro (briefing, avisos de
        job) com `<` e `&` sem escapar, e ligar HTML neles devolveria 400. Se ainda assim a API
        recusar a formatação, reenvia sem tags: consulta entregue feia é melhor que não entregue.

        Só o 400 degrada. Timeout e queda de conexão não são recusa de formatação, e reenviar
        sem tags nesse caso trocaria a resposta boa por uma feia sem motivo — além de arriscar
        duas mensagens se a primeira tiver chegado e só a resposta ter se perdido.
        """
        for indice, parte in enumerate(consulta.partir(text)):
            params = {
                "chat_id": message["chat"]["id"],
                "text": parte,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            }
            if indice == 0:
                params["reply_parameters"] = {
                    "message_id": message["message_id"], "allow_sending_without_reply": True,
                }
            try:
                call(self.token, "sendMessage", **params)
            except TelegramError as e:
                if e.code != 400:
                    logger.warning(f"Telegram: falha ao responder consulta: {e}")
                    return
                logger.warning(f"Telegram: HTML recusado ({e}); reenviando sem formatação.")
                params.pop("parse_mode")
                params["text"] = consulta.sem_tags(parte)
                try:
                    call(self.token, "sendMessage", **params)
                except TelegramError as e2:
                    logger.warning(f"Telegram: falha ao responder consulta: {e2}")
                    return

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
                self.reply(message, "Logos conectado a este chat.\n\n" + _ajuda())
            else:
                logger.warning(f"Telegram: mensagem ignorada, dono ainda não registrado (chat_id={chat_id}). Mande /start.")
            return
        if chat_id != owner:
            logger.warning(f"Telegram: mensagem de chat não autorizado ignorada (chat_id={chat_id})")
            return

        try:
            if command:
                self._handle_command(message, command, rest)
            else:
                self._capture(message)
        except Exception:
            logger.exception(f"Telegram: falha ao capturar a mensagem {message.get('message_id')}")
            self.reply(message, "Não consegui salvar esta mensagem. Reenvie, por favor.")

    def _handle_command(self, message: dict, command: str, rest: str) -> None:
        if command == "start":
            self.reply(message, "Logos já está conectado a este chat.\n\n" + _ajuda())
        elif command in CONSULTA_COMMANDS or INDICE_RE.match(command):
            self._consultar(message, command, rest)
        elif command in PREFIX_COMMANDS:
            if not rest:
                self.reply(message, f"Uso: /{command} <texto>")
                return
            self._capture(message, text=f"{PREFIX_COMMANDS[command]} {rest}")
        elif command == "nova":
            if self.session is None:
                self.reply(message, "Nenhuma sessão aberta.")
                return
            name, size = self.session.name, self.session.size
            self._close(self.session, "nova")
            if size == 0:
                self.reply(message, "Sessão vazia descartada.")
            else:
                self.reply(message, f"Sessão fechada com {size} mensagem(ns): {name}. A nota sai em seguida.")
        else:
            self.reply(message, _ajuda())

    def _consultar(self, message: dict, command: str, rest: str) -> None:
        """Leitura do vault. Não toca a sessão de captura aberta: perguntar não é capturar.

        Registra o tempo de resposta: sem isso, uma consulta bem-sucedida não deixa rastro no
        log, e "o bot demorou" não se distingue de "a mensagem não chegou". Só o nome do
        comando vai para o log — o termo buscado, não."""
        inicio = time.monotonic()
        try:
            indice = INDICE_RE.match(command)
            if indice:
                texto = consulta.cmd_indice(indice.group(1), int(indice.group(2)), self.contexto)
            else:
                verbo = CONSULTA_COMMANDS[command]
                if verbo == "ver":
                    texto = consulta.cmd_ver(rest, self.contexto)
                elif verbo == "buscar":
                    texto = consulta.cmd_buscar(rest, self.contexto)
                elif verbo == "hoje":
                    texto = consulta.cmd_hoje(self.contexto)
                elif verbo == "ajuda":
                    texto = consulta.cmd_ajuda()
                else:
                    texto = consulta.cmd_inicio(self.contexto)
        except consulta.ConsultaError as e:
            self.reply(message, str(e))
            return
        except Exception:
            logger.exception(f"Telegram: falha na consulta /{command}")
            self.reply(message, "Não consegui ler o vault agora. O log do Logos tem o motivo.")
            return
        self.reply_html(message, texto)
        logger.info(f"Telegram: /{command} respondido em {time.monotonic() - inicio:.1f}s")

    @staticmethod
    def _classify(message: dict, text: str) -> tuple[str | None, dict | None, str]:
        if text:
            return "texto", None, ""
        if message.get("voice"):
            return "voz", message["voice"], ".oga"
        if message.get("audio"):
            return "audio", message["audio"], _media_ext(message["audio"], ".audio")
        if message.get("photo"):
            return "foto", message["photo"][-1], ".jpg"  # último tamanho = maior resolução
        if message.get("document"):
            return "documento", message["document"], _media_ext(message["document"], ".bin")
        if message.get("location"):
            return "localizacao", None, ""
        return None, None, ""

    def _capture(self, message: dict, text: str | None = None) -> None:
        # Antes de tudo: uma sessão vencida que o tick do polling ainda não fechou não pode
        # absorver uma resposta a uma mensagem dela (a resposta tem de ir para a nota original).
        self.expire_if_idle()
        text = text if text is not None else (message.get("text") or "").strip()
        kind, media, ext = self._classify(message, text)
        if kind is None:
            self.reply(message, "Tipo de mensagem não suportado. Mande texto, voz, foto, documento ou localização.")
            return
        size = (media or {}).get("file_size") or 0
        if size > MAX_DOWNLOAD_BYTES:
            self.reply(
                message,
                f"Arquivo de {size / 1024 / 1024:.1f} MB passa do limite de 20 MB da Bot API. "
                "Mande pelo Syncthing ou por link do Drive.",
            )
            return
        label = KIND_LABELS[kind]
        aviso = "\n" + DIARIO_AVISO if _starts_with_diario(text or message.get("caption") or "") else ""

        reply_to = (message.get("reply_to_message") or {}).get("message_id")
        if reply_to is not None and not (self.session and reply_to in self.session.ids):
            session = self._open(message, reply_to=reply_to)
            self._add(session, message, kind, media, ext, text)
            self._close(session, "resposta")
            self.reply(message, f"{label.capitalize()} registrado como resposta ({session.name}). Complementa a nota da mensagem original." + aviso)
            return

        opened = self.session is None
        if opened:
            self.session = self._open(message)
        self._add(self.session, message, kind, media, ext, text)
        if opened:
            ack = (
                f"{label.capitalize()} · sessão aberta ({self.session.name}). "
                f"Fecha após {_fmt_min(config.TELEGRAM_WINDOW_MIN)} min sem mensagens ou com /nova."
            )
        else:
            ack = f"{label.capitalize()} · {self.session.size} na sessão"
        self.reply(message, ack + aviso)

    def _open(self, message: dict, reply_to: int | None = None) -> Session:
        directory = staging_dir() / bundle_name(message)
        directory.mkdir(parents=True, exist_ok=True)
        manifest = load_manifest(directory) or {
            "versao": MANIFEST_VERSION,
            "pacote": directory.name,
            "chat_id": message["chat"]["id"],
            "aberto_em": _iso(message["date"]),
            "mensagens": [],
        }
        if reply_to is not None:
            manifest["reply_to"] = reply_to
        return Session(directory, manifest, time.time())

    def _add(self, session: Session, message: dict, kind: str, media: dict | None, ext: str, text: str) -> bool:
        message_id = message["message_id"]
        if message_id in session.ids:
            logger.info(f"Telegram: reentrega ignorada, mensagem {message_id} já está em {session.name}")
            return False
        entry = {"message_id": message_id, "date": _iso(message["date"]), "tipo": kind}
        if text:
            entry["texto"] = text
        caption = (message.get("caption") or "").strip()
        if caption:
            entry["legenda"] = caption
        if media is not None:
            filename = f"{message_id}{ext}"
            download_file(self.token, media["file_id"], session.directory / filename)
            entry["arquivo"] = filename
            if media.get("file_name"):
                entry["nome_original"] = media["file_name"]
            if media.get("mime_type"):
                entry["mime"] = media["mime_type"]
        if kind == "localizacao":
            location = message["location"]
            local = {"lat": location["latitude"], "lon": location["longitude"]}
            venue = message.get("venue") or {}
            if venue.get("title"):
                local["titulo"] = venue["title"]
            if venue.get("address"):
                local["endereco"] = venue["address"]
            entry["local"] = local
        if message.get("media_group_id"):
            entry["media_group_id"] = message["media_group_id"]
        reply_to = (message.get("reply_to_message") or {}).get("message_id")
        if reply_to is not None:
            entry["reply_to"] = reply_to
        links = _links(message)
        if links:
            entry["links"] = links

        session.manifest["mensagens"].append(entry)
        session.last_at = time.time()
        write_manifest(session.directory, session.manifest)
        logger.info(f"Telegram: {kind} adicionado a {session.name}")
        return True

    def _close(self, session: Session, reason: str) -> None:
        if session is self.session:
            self.session = None
        if not session.manifest["mensagens"]:
            # sessão aberta cuja primeira mensagem falhou: não há o que virar nota
            logger.info(f"Telegram: sessão vazia descartada: {session.name}")
            shutil.rmtree(session.directory, ignore_errors=True)
            return
        finalize_bundle(session.directory, session.manifest, reason)

    def expire_if_idle(self, now: float | None = None) -> str | None:
        """Fecha a sessão aberta se a janela venceu. Devolve o nome do pacote fechado."""
        if self.session is None:
            return None
        now = time.time() if now is None else now
        if now - self.session.last_at < config.TELEGRAM_WINDOW_MIN * 60:
            return None
        name = self.session.name
        self._close(self.session, "janela")
        return name

    def recover(self) -> list[str]:
        """Entrega à Inbox os pacotes que uma queda ou reinício deixou abertos no staging."""
        root = staging_dir()
        if not root.is_dir():
            return []
        closed = []
        for directory in sorted(p for p in root.iterdir() if p.is_dir()):
            manifest = load_manifest(directory)
            if not manifest or not manifest.get("mensagens"):
                logger.warning(f"Telegram: pacote vazio descartado do staging: {directory.name}")
                shutil.rmtree(directory, ignore_errors=True)
                continue
            finalize_bundle(directory, manifest, "reinicio")
            closed.append(directory.name)
        return closed

    def run_polling(self, stop: threading.Event) -> None:
        from .status import tracker  # só no processo do watcher

        try:
            recovered = self.recover()
            if recovered:
                logger.info(f"Telegram: {len(recovered)} pacote(s) aberto(s) antes do reinício entregue(s) à Inbox")
        except Exception:
            logger.exception("Telegram: falha ao recuperar pacotes do staging")

        username = None
        offset = None
        backoff = 1
        timeouts = 0
        while not stop.is_set():
            try:
                if username is None:
                    username = call(self.token, "getMe")["username"]
                    logger.info(f"Telegram: bot @{username} conectado (long polling)")
                    publicar_menu(self.token)
                params = {"timeout": POLL_TIMEOUT_S, "allowed_updates": ["message"]}
                if offset is not None:
                    params["offset"] = offset
                updates = call(self.token, "getUpdates", http_timeout=POLL_TIMEOUT_S + 5, **params)
                if timeouts:
                    logger.info(f"Telegram: rede voltou depois de {timeouts} timeout(s) seguidos.")
                backoff = 1
                timeouts = 0
                for update in updates:
                    offset = update["update_id"] + 1  # avança mesmo se falhar: não travar numa mensagem
                    self.handle_update(update)
                self.expire_if_idle()
                tracker.set_bot(
                    "polling", username=username, owner_registered=get_owner_chat_id() is not None,
                    session=self.session.name if self.session else None,
                    last_poll=datetime.now().isoformat(), last_error=None,
                )
            except TelegramError as e:
                if e.timeout and username is not None:
                    # Long poll que expirou é rotina: o getUpdates fica pendurado por
                    # POLL_TIMEOUT_S de propósito, e a rede desta máquina o derruba com
                    # frequência. Tratar isso como falha dobrava a espera a cada oscilação
                    # (1, 2, 4, ... até 60 s), e o bot passava mais tempo dormindo do que
                    # ouvindo — foi por isso que ele demorou a responder em 22/09/2026.
                    # Não há risco de laço apertado: cada tentativa já custa POLL_TIMEOUT_S.
                    backoff = 1
                    timeouts += 1
                    if timeouts % TIMEOUTS_ATE_AVISAR == 0:
                        logger.warning(
                            f"Telegram: {timeouts} timeouts seguidos no getUpdates. O bot segue "
                            "tentando, mas a rede para api.telegram.org está ruim."
                        )
                    self.expire_if_idle()
                    tracker.set_bot(
                        "polling", username=username,
                        owner_registered=get_owner_chat_id() is not None,
                        session=self.session.name if self.session else None,
                        last_poll=datetime.now().isoformat(), last_error=None,
                    )
                    continue
                if e.code == 401:
                    logger.error("Telegram: token recusado (401). Bot desligado até corrigir o secrets.yaml e relançar.")
                    tracker.set_bot("error", username=username, last_error="token recusado (401)")
                    return
                if e.timeout:
                    # Timeout na conexão (o getMe da subida). Recuar exponencialmente aqui era o
                    # mesmo erro do long poll com outra roupa: a espera já foi paga pelo próprio
                    # timeout, então dobrá-la só atrasa a subida. Em 22/09/2026 isso deixou o bot
                    # dois minutos fora do ar depois de um relançamento, numa rede em que a
                    # quarta tentativa seguida costuma passar.
                    espera = TIMEOUT_RETENTATIVA_S
                else:
                    espera = backoff
                    backoff = min(backoff * 2, MAX_BACKOFF_S)
                if e.code == 409:
                    logger.error("Telegram: conflito 409, outro processo lê getUpdates com este token.")
                else:
                    logger.warning(f"Telegram: {e}. Nova tentativa em {espera}s.")
                tracker.set_bot("error", username=username, last_error=str(e))
                stop.wait(espera)
            except Exception as e:
                logger.exception("Telegram: erro inesperado no polling")
                tracker.set_bot("error", username=username, last_error=str(e))
                stop.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)


def publicar_menu(token: str) -> bool:
    """Registra o MENU no Telegram, para o app sugerir os comandos ao digitar "/".

    Idempotente e sem consequência: é estado do lado do servidor, e republicar o mesmo menu não
    muda nada. Nunca levanta — menu que falha não pode derrubar o polling que traz as capturas.
    """
    comandos = [{"command": nome, "description": texto} for nome, texto in MENU]
    try:
        call(token, "setMyCommands", commands=comandos)
        return True
    except Exception as e:
        logger.warning(f"Telegram: menu de comandos não publicado: {e}")
        return False


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


def notify(text: str) -> bool:
    """Aviso de uma linha para o dono. Nunca levanta: um alerta que derruba o pipeline é
    pior que a falha que ele anuncia. Devolve False quando não deu para avisar."""
    token, chat_id = get_token(), get_owner_chat_id()
    if not token or chat_id is None:
        logger.warning("Aviso não enviado: bot do Telegram sem token ou sem dono registrado.")
        return False
    try:
        call(token, "sendMessage", chat_id=chat_id, text=text[:MESSAGE_LIMIT])
        return True
    except Exception as e:
        logger.warning(f"Aviso não enviado pelo Telegram: {e}")
        return False


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
