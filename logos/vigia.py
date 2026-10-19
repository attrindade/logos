"""O Vigia: confere se as peças do ecossistema estão vivas e manda o estado, com o registro de
eventos (`eventos.py`), para a aba Logos do app Organiza (pedido dele em 14/10).

Roda no laço do drenador (`python -m logos.organiza`): confere a cada CHECAGEM_S e manda, fechado
com a chave do celular, para a Semana (`/api/cifrados`, chave "logos"), quando algo mudou e no
mínimo a cada PUBLICA_S. Quem vigia o vigia é o celular: a hora do envio é o sinal de vida do PC, e
a aba avisa quando ela fica velha (PC desligado, drenador parado ou sem rede). Desde 19/10 a tarefa
agendada \attrindade\Servicos vigia (`_cli/servicos.ps1 vigiar`) religa watcher, drenador e Assistente parados.

Só leitura: nenhuma checagem reinicia, mata ou muda nada. O `status.json` do watcher tem escritor
único (o StatusTracker): aqui ele só é lido.

Teste: `python -m logos.vigia` imprime as checagens (não manda nada).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import psutil

from . import cofre, config, eventos, organiza

logger = logging.getLogger(__name__)

URL = organiza.URL.replace("/capturas", "/cifrados")
ID = "logos"  # dados associados do envelope: só serve como o estado da aba Logos
CHECAGEM_S = 300
PUBLICA_S = 600
EVENTOS_DIAS = 3
EVENTOS_MAX = 200

HEARTBEAT_MAX_S = 300      # o watcher bate a cada 10 s
# Processando, ele fica mudo: a varredura roda na mesma thread do heartbeat e uma gravação longa do
# Grava (até 5 h) leva muitos minutos para transcrever. Nesse caso o limite é este.
OCUPADO_MAX_S = 3 * 3600
OCUPADO = {"stabilizing", "transcribing", "triaging", "enriching", "writing"}
TAREFA_FREQUENTE_S = 3600  # tarefa que roda mais que de hora em hora só entra no registro se falhar
AW_MAX_S = 20 * 60         # o observador de janelas manda a cada poucos segundos com o PC ligado
SYNC_CELULAR_MAX_S = 24 * 3600
DISCO_MIN_GB = 10

# Resultados de tarefa agendada que não são erro: rodando, nunca rodou, já estava rodando (o AHK
# relança a cada 10 min e encontra a instância de pé: 0x800710E0, "pedido recusado"), tarefa sem
# próxima execução.
TAREFA_OK = {0, 0x41301, 0x41303, 0x80070420, 0x800710E0, 0x41300, 0x41302}
ATRASO_TAREFA_S = 30 * 60

_SEM_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_SEM_JANELA = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_ultima_checagem = 0.0
_ultimo_doc: dict | None = None


# --------------------------------------------------------------------------- utilidades

def _item(id_: str, nome: str, estado: str, detalhe: str = "") -> dict:
    return {"id": id_, "nome": nome, "estado": estado, "detalhe": detalhe}


def _get_json(url: str, headers: dict | None = None, timeout: float = 4):
    req = urllib.request.Request(url, headers=headers or {})
    with _SEM_PROXY.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _idade(segundos: float) -> str:
    s = int(segundos)
    if s < 90:
        return f"{s} s"
    if s < 90 * 60:
        return f"{s // 60} min"
    if s < 48 * 3600:
        return f"{s // 3600} h"
    return f"{s // 86400} dias"


def _iso_para_ts(valor: str) -> float | None:
    try:
        d = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if d.tzinfo is None:
        d = d.astimezone()  # hora local sem fuso
    return d.timestamp()


def instancias(padrao: str, procs: list[tuple[int, int, str]]) -> int:
    """Quantas instâncias de verdade casam `padrao` (regex na linha de comando). No Windows o
    python.exe de um venv é um lançador que abre o Python de verdade como filho, com a mesma linha
    de comando: o par conta como uma (ver Gotchas: duas instâncias do watcher)."""
    rx = re.compile(padrao, re.I)
    casam = {pid: ppid for pid, ppid, cmd in procs if rx.search(cmd)}
    return sum(1 for pid, ppid in casam.items() if ppid not in casam)


def processos() -> list[tuple[int, int, str]]:
    saida = []
    for p in psutil.process_iter(["pid", "ppid", "cmdline"]):
        try:
            cmd = " ".join(p.info.get("cmdline") or [])
        except (psutil.Error, OSError):
            continue
        if cmd:
            saida.append((p.info["pid"], p.info.get("ppid") or 0, cmd))
    return saida


# --------------------------------------------------------------------------- checagens

def chk_watcher(procs, agora: float) -> dict:
    nome = "Watcher do Logos"
    n = instancias(r"logos[\\/]main\.py", procs)
    if n == 0:
        return _item("watcher", nome, "falha", "Não está rodando: áudio e anotações não viram nota.")
    if n > 1:
        return _item("watcher", nome, "falha", f"{n} cópias rodando: a mesma captura pode virar duas notas.")
    try:
        st = json.loads(config.STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _item("watcher", nome, "atencao", "Rodando, mas sem o status.json para conferir.")
    hb = _iso_para_ts(st.get("heartbeat") or "")
    ocupado = st.get("stage") in OCUPADO
    if hb is not None and ocupado and agora - hb <= OCUPADO_MAX_S:
        arq = st.get("filename") or "uma captura"
        return _item("watcher", nome, "ok", f"Processando {arq}.")
    if hb is None or agora - hb > HEARTBEAT_MAX_S:
        quanto = f" há {_idade(agora - hb)}" if hb else ""
        return _item("watcher", nome, "falha", f"Rodando, mas parou de dar sinal{quanto}.")
    if st.get("stage") == "failed":
        arq = st.get("filename") or "uma captura"
        return _item("watcher", nome, "atencao", f"A última captura falhou: {arq}.")
    return _item("watcher", nome, "ok", st.get("message") or "")


def chk_drenador(procs) -> dict:
    nome = "Drenador (celular ↔ PC)"
    n = instancias(r"-m\s+logos\.organiza", procs)
    if n > 1:
        return _item("drenador", nome, "falha", f"{n} cópias rodando: a mesma anotação pode entrar duas vezes.")
    return _item("drenador", nome, "ok", "")


def chk_http(id_: str, nome: str, url: str, falta: str) -> dict:
    try:
        _get_json(url)
        return _item(id_, nome, "ok", "")
    except Exception:
        return _item(id_, nome, "falha", falta)


def chk_aw(agora: float) -> dict:
    nome = "ActivityWatch"
    try:
        buckets = _get_json("http://127.0.0.1:5600/api/0/buckets/")
    except Exception:
        return _item("aw", nome, "falha", "O servidor não responde: o dia do PC não está sendo registrado.")
    janelas = [b for k, b in buckets.items() if k.startswith("aw-watcher-window_")]
    ultimos = [t for t in (_iso_para_ts(b.get("last_updated") or "") for b in janelas) if t]
    if not ultimos:
        return _item("aw", nome, "atencao", "Sem o observador de janelas.")
    idade = agora - max(ultimos)
    if idade > AW_MAX_S:
        return _item("aw", nome, "atencao", f"O observador de janelas parou há {_idade(idade)}.")
    return _item("aw", nome, "ok", "")


def _syncthing_api() -> tuple[str, str, dict[str, str]] | None:
    """(endereço, chave da API, {id do aparelho: nome}) lidos da config local do Syncthing."""
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Syncthing" / "config.xml"
    try:
        raiz = ET.parse(base).getroot()
    except (OSError, ET.ParseError):
        return None
    gui = raiz.find("gui")
    if gui is None:
        return None
    chave = (gui.findtext("apikey") or "").strip()
    endereco = (gui.findtext("address") or "127.0.0.1:8384").strip()
    esquema = "https" if gui.get("tls") == "true" else "http"
    aparelhos = {d.get("id"): d.get("name") or d.get("id", "")[:7] for d in raiz.findall("device")}
    return f"{esquema}://{endereco}", chave, aparelhos


def chk_syncthing(procs, agora: float) -> list[dict]:
    nome = "Syncthing"
    if not instancias(r"syncthing\.exe", procs):
        return [_item("syncthing", nome, "falha", "Não está rodando: o áudio do celular não chega ao PC.")]
    api = _syncthing_api()
    if not api or not api[1]:
        return [_item("syncthing", nome, "ok", "Rodando (sem a API para conferir os aparelhos).")]
    url, chave, aparelhos = api
    h = {"X-API-Key": chave}
    try:
        eu = _get_json(url + "/rest/system/status", h).get("myID")
        conexoes = _get_json(url + "/rest/system/connections", h).get("connections", {})
        stats = _get_json(url + "/rest/stats/device", h)
        erros = _get_json(url + "/rest/system/error", h).get("errors") or []
    except Exception:
        return [_item("syncthing", nome, "atencao", "Rodando, mas a API não respondeu.")]
    itens = [_item("syncthing", nome, "atencao" if erros else "ok",
                   f"{len(erros)} erro(s) recente(s) no Syncthing." if erros else "")]
    for dev, rotulo in aparelhos.items():
        if dev == eu:
            continue
        if (conexoes.get(dev) or {}).get("connected"):
            itens.append(_item(f"sync:{rotulo}", f"Syncthing: {rotulo}", "ok", "Conectado."))
            continue
        visto = _iso_para_ts((stats.get(dev) or {}).get("lastSeen") or "")
        if visto is None or visto < 86400 * 365:  # "1970-01-01": nunca visto
            itens.append(_item(f"sync:{rotulo}", f"Syncthing: {rotulo}", "atencao", "Nunca conectou."))
        elif agora - visto > SYNC_CELULAR_MAX_S:
            itens.append(_item(f"sync:{rotulo}", f"Syncthing: {rotulo}", "atencao",
                               f"Sem conectar há {_idade(agora - visto)}."))
        else:
            itens.append(_item(f"sync:{rotulo}", f"Syncthing: {rotulo}", "ok", f"Visto há {_idade(agora - visto)}."))
    return itens


def tarefas() -> list[dict] | None:
    """As tarefas agendadas em \\attrindade\\ (nome, ligada, última e próxima execução, resultado).
    None quando não deu para ler (vira um item de atenção, não uma lista vazia calada)."""
    exe = shutil.which("pwsh") or shutil.which("powershell")
    if not exe:
        return None
    script = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        "Get-ScheduledTask -TaskPath '\\attrindade\\' | ForEach-Object { $i = $_ | Get-ScheduledTaskInfo; "
        "[pscustomobject]@{nome=$_.TaskName; estado=[string]$_.State; "
        "ultima=$(if ($i.LastRunTime) { $i.LastRunTime.ToString('o') }); "
        "proxima=$(if ($i.NextRunTime) { $i.NextRunTime.ToString('o') }); resultado=[int64]$i.LastTaskResult} } "
        "| ConvertTo-Json -Compress"
    )
    try:
        r = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                           text=True, encoding="utf-8", timeout=60, creationflags=_SEM_JANELA)
        dados = json.loads(r.stdout or "null")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if dados is None:
        return None
    return dados if isinstance(dados, list) else [dados]


def _resultado(n: int) -> int:
    return n & 0xFFFFFFFF


def chk_tarefas(lista: list[dict] | None, agora: float) -> list[dict]:
    if lista is None:
        return [_item("tarefas", "Tarefas agendadas", "atencao", "Não deu para ler as tarefas do Windows.")]
    itens = []
    for t in lista:
        nome = t.get("nome") or "?"
        if t.get("estado") == "Disabled":
            continue
        res = _resultado(int(t.get("resultado") or 0))
        ultima = _iso_para_ts(t.get("ultima") or "")
        proxima = _iso_para_ts(t.get("proxima") or "")
        quando = f" ({datetime.fromtimestamp(ultima):%d/%m %H:%M})" if ultima else ""
        if res not in TAREFA_OK:
            itens.append(_item(f"tarefa:{nome}", nome, "atencao", f"Terminou com código {res}{quando}."))
        elif proxima and agora - proxima > ATRASO_TAREFA_S and t.get("estado") != "Running":
            itens.append(_item(f"tarefa:{nome}", nome, "atencao",
                               f"Devia ter rodado às {datetime.fromtimestamp(proxima):%d/%m %H:%M}."))
        else:
            itens.append(_item(f"tarefa:{nome}", nome, "ok", f"Última{quando}." if quando else ""))
    return itens


def chk_disco() -> dict:
    livre = shutil.disk_usage("C:\\").free / 1024**3
    if livre < DISCO_MIN_GB:
        return _item("disco", "Disco C:", "atencao", f"Só {livre:.0f} GB livres.")
    return _item("disco", "Disco C:", "ok", f"{livre:.0f} GB livres.")


_BUSCAR = object()


def checar(agora: float | None = None, lista=_BUSCAR) -> list[dict]:
    agora = agora or time.time()
    procs = processos()
    return [
        chk_watcher(procs, agora),
        chk_drenador(procs),
        chk_http("assistente", "Assistente (WhatsApp)", "http://127.0.0.1:8765/api/fila",
                 "Não responde: a fila do WhatsApp não chega ao celular."),
        chk_http("ollama", "Ollama", "http://127.0.0.1:11434/api/version",
                 "Não responde: as notas saem sem enriquecimento."),
        chk_aw(agora),
        *chk_syncthing(procs, agora),
        chk_disco(),
        *chk_tarefas(tarefas() if lista is _BUSCAR else lista, agora),
    ]


# --------------------------------------------------------------------------- memória e eventos

def _estado_path() -> Path:
    return config.STATE_DIR / "vigia.json"


def _ler_estado() -> dict:
    try:
        return json.loads(_estado_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def registrar_mudancas(itens: list[dict], tarefas_lista: list[dict] | None, agora: float) -> list[dict]:
    """Marca `desde` em cada item e põe no registro o que mudou de estado e as tarefas que rodaram."""
    antes = _ler_estado()
    estados = antes.get("estados", {})
    execucoes = antes.get("tarefas", {})
    novos = {}
    for it in itens:
        velho = estados.get(it["id"])
        if velho and velho.get("estado") == it["estado"]:
            it["desde"] = velho.get("desde", int(agora))
        else:
            it["desde"] = int(agora)
            if velho and it["estado"] != "ok":
                eventos.registrar("vigia", f"{it['nome']}: {it['detalhe'] or 'com problema'}", it["estado"], agora)
            elif velho and it["estado"] == "ok":
                eventos.registrar("vigia", f"{it['nome']}: voltou ao normal.", "info", agora)
            elif not velho and it["estado"] != "ok":
                eventos.registrar("vigia", f"{it['nome']}: {it['detalhe'] or 'com problema'}", it["estado"], agora)
        novos[it["id"]] = {"estado": it["estado"], "desde": it["desde"]}
    if tarefas_lista is not None:
        for t in tarefas_lista:
            nome, ultima = t.get("nome"), t.get("ultima")
            if not nome or not ultima:
                continue
            if nome in execucoes and execucoes[nome] != ultima:
                res = _resultado(int(t.get("resultado") or 0))
                ok = res in TAREFA_OK
                ini, prox = _iso_para_ts(ultima), _iso_para_ts(t.get("proxima") or "")
                frequente = ini is not None and prox is not None and prox - ini < TAREFA_FREQUENTE_S
                if ok and frequente:  # AW Sync push (5 min), atalhos AHK (10 min): só o erro conta
                    execucoes[nome] = ultima
                    continue
                eventos.registrar("tarefas", f"{nome} rodou" + ("." if ok else f" e terminou com código {res}."),
                                  "info" if ok else "atencao", _iso_para_ts(ultima) or agora)
            execucoes[nome] = ultima
    try:
        _estado_path().parent.mkdir(parents=True, exist_ok=True)
        _estado_path().write_text(json.dumps({"estados": novos, "tarefas": execucoes}, ensure_ascii=False),
                                  encoding="utf-8")
    except OSError as e:
        logger.warning(f"Vigia: não deu para guardar o estado: {e}")
    return itens


# --------------------------------------------------------------------------- envio

def _marca_path() -> Path:
    return config.STATE_DIR / "organiza" / "logos.hash"


def montar(agora: float) -> dict:
    lista = tarefas()
    itens = checar(agora, lista)
    registrar_mudancas(itens, lista, agora)
    return {"checagens": itens}


def passo(key: str, agora: float | None = None) -> bool | None:
    """Uma volta do drenador: confere (a cada CHECAGEM_S) e manda se mudou ou se o último envio
    ficou velho. True se mandou, False se não precisou, None se não deu (sem chave do celular)."""
    global _ultima_checagem, _ultimo_doc
    agora = agora or time.time()
    if _ultimo_doc is None or agora - _ultima_checagem >= CHECAGEM_S:
        _ultimo_doc = montar(agora)
        _ultima_checagem = agora
        eventos.limpar()
    pub = organiza.chave_celular(key)
    if not pub:
        return None
    doc = dict(_ultimo_doc, eventos=eventos.ultimos(EVENTOS_DIAS, EVENTOS_MAX))
    marca = hashlib.sha256((pub + json.dumps(doc, sort_keys=True, ensure_ascii=False)).encode("utf-8")).hexdigest()
    p = _marca_path()
    try:
        velha = p.read_text(encoding="utf-8").split()
        if velha and velha[0] == marca and agora - float(velha[1]) < PUBLICA_S:
            return False
    except (OSError, IndexError, ValueError):
        pass
    doc["gerado_em"] = int(agora)
    env = cofre.selar_para_celular(json.dumps(doc, ensure_ascii=False).encode("utf-8"), ID, pub)
    organiza._pedido("PUT", URL, key, {"chave": "logos", "env": json.dumps(env)})
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{marca} {agora}", encoding="utf-8")
    return True


def main() -> None:
    agora = time.time()
    simbolo = {"ok": "ok ", "atencao": "!! ", "falha": "XX "}
    for it in checar(agora):
        print(simbolo.get(it["estado"], "?? "), it["nome"], "—", it["detalhe"])


if __name__ == "__main__":
    main()
