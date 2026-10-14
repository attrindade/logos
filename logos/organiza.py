"""Drenador do app Organiza: traz as anotações do celular para a Inbox.

O celular não alcança o PC; as anotações fechadas no app ficam na página da Semana (Cloudflare,
/api/capturas). Este processo, separado do watcher, busca as pendentes, monta cada uma como um
pacote igual ao do bot ("AAAA-MM-DD HH-MM-SS app <id>/manifest.json" + áudios) direto na Inbox
e devolve o estado ao app:

    nuvem → pc     quando o pacote está na Inbox (a Semana apaga o áudio dela)
    pc → nota      quando o ledger diz que o pacote virou nota (título e próxima ação vão junto)

Rodar: `python -m logos.organiza` (laço a cada minuto) ou `python -m logos.organiza --uma-vez`.
A chave da Semana vem de SEMANA_KEY ou de `semana_key` em state/secrets.yaml.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from . import config
from .telegram import MANIFEST, load_secrets, write_manifest

logger = logging.getLogger(__name__)

URL = "https://semana-andre.pages.dev/api/capturas"
# O Cloudflare da Semana barra o User-Agent padrão do Python (erro 1010).
UA = "Logos-organiza/0.1"
INTERVALO_S = 60
LIMITE_S = 30
TAG = "app"  # triage.ORGANIZA_TAG: o nome do pacote diz de onde ele veio
_TIPO_PALAVRA = {"ideia": "Ideia", "tarefa": "Tarefa", "reuniao": "Reunião"}
_EXT = {"audio/mp4": ".m4a", "audio/aac": ".aac", "audio/ogg": ".ogg"}


def chave() -> str:
    return os.getenv("SEMANA_KEY") or str(load_secrets().get("semana_key") or "")


def _pedido(metodo: str, url: str, key: str, corpo: dict | None = None) -> dict:
    dados = json.dumps(corpo, ensure_ascii=False).encode("utf-8") if corpo is not None else None
    req = urllib.request.Request(url, data=dados, method=metodo, headers={
        "X-Semana-Key": key, "User-Agent": UA, "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=LIMITE_S) as r:
        return json.loads(r.read().decode("utf-8"))


def nome_pacote(captura: dict) -> str:
    """'2026-10-14T01:25:11' + id → '2026-10-14 01-25-11 app f1cd4982' (o padrão do triage)."""
    aberta = captura["aberta_em"][:19]
    data, hora = aberta.split("T")
    curto = re.sub(r"[^A-Za-z0-9]", "", captura["id"])[:8]
    return f"{data} {hora.replace(':', '-')} {TAG} {curto}"


def manifest_de(captura: dict, arquivos: dict[str, str]) -> dict:
    """Manifest no formato do bot. O tipo escolhido no app vira a primeira palavra do primeiro
    texto, que é como o triage fixa a categoria (o mesmo que o /tarefa do bot faz)."""
    mensagens = []
    for p in sorted(captura.get("partes", []), key=lambda p: (p.get("ordem", 0), p.get("criada_em", ""))):
        if p.get("tipo") == "texto" and (p.get("texto") or "").strip():
            mensagens.append({"tipo": "texto", "texto": p["texto"].strip(), "quando": p.get("criada_em")})
        elif p.get("tipo") == "voz" and p["id"] in arquivos:
            mensagens.append({"tipo": "voz", "arquivo": arquivos[p["id"]], "quando": p.get("criada_em")})
    palavra = _TIPO_PALAVRA.get(captura.get("tipo", ""))
    if palavra and mensagens:  # nota vazia continua vazia: o tipo sozinho não é conteúdo
        if mensagens and mensagens[0]["tipo"] == "texto":
            mensagens[0]["texto"] = f"{palavra}: {mensagens[0]['texto']}"
        else:
            mensagens.insert(0, {"tipo": "texto", "texto": palavra, "quando": captura.get("aberta_em")})
    return {
        "origem": "organiza",
        "captura_id": captura["id"],
        "aberto_em": captura.get("aberta_em"),
        "fechado_em": captura.get("fechada_em"),
        "motivo_fechamento": "fechada no app",
        "mensagens": mensagens,
    }


def entregar(captura: dict, inbox: Path, oficina: Path, audio=None) -> Path:
    """Monta o pacote fora da Inbox e só então o move para lá, inteiro: o watcher nunca vê um
    pacote pela metade. Se já foi entregue (queda entre mover e avisar a Semana), não duplica.
    `audio(id_da_parte)` devolve o base64 da voz: a lista de pendentes não traz o áudio (várias
    vozes juntas estouravam a memória do Worker), então cada uma vem num pedido."""
    nome = nome_pacote(captura)
    destino = inbox / nome
    if (destino / MANIFEST).exists():
        return destino
    tmp = oficina / nome
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    arquivos: dict[str, str] = {}
    n = 0
    for p in captura.get("partes", []):
        if p.get("tipo") != "voz":
            continue
        b64 = p.get("audio") or (audio(p["id"]) if audio and p.get("tem_audio") else None)
        if not b64:
            continue
        n += 1
        arq = f"{n:02d}{_EXT.get(p.get('mime') or '', '.m4a')}"
        (tmp / arq).write_bytes(base64.b64decode(b64))
        arquivos[p["id"]] = arq
    m = manifest_de(captura, arquivos)
    if not m["mensagens"]:
        shutil.rmtree(tmp)
        raise ValueError(f"anotação {captura['id']} sem conteúdo")
    write_manifest(tmp, m)
    inbox.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp), str(destino))
    return destino


# ---- o que já foi entregue e espera virar nota ----

def _estado_path() -> Path:
    return config.STATE_DIR / "organiza" / "entregues.json"


def _ler_entregues() -> dict[str, str]:
    try:
        return json.loads(_estado_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _gravar_entregues(d: dict[str, str]) -> None:
    p = _estado_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def nota_do_pacote(nome: str, ledger_path: Path) -> Path | None:
    """O caminho da nota que o pacote virou, lendo o ledger (só leitura: quem escreve é o watcher)."""
    try:
        dados = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for e in dados.values():
        if isinstance(e, dict) and e.get("filename") == nome and e.get("state") == "enriched" and e.get("note_path"):
            return Path(e["note_path"])
    return None


def resumo_da_nota(nota: Path) -> tuple[str | None, str | None]:
    """Título e próxima ação do frontmatter da nota, para o app mostrar."""
    try:
        texto = nota.read_text(encoding="utf-8")
    except OSError:
        return None, None
    fm: dict = {}
    if texto.startswith("---"):
        fim = texto.find("\n---", 3)
        if fim > 0:
            try:
                fm = yaml.safe_load(texto[3:fim]) or {}
            except yaml.YAMLError:
                fm = {}
    titulo = fm.get("titulo") or nota.stem
    # O writer põe a próxima ação no corpo: "## ⚡ Próxima Ação Imediata" + "- [ ] <ação> ⏫".
    proxima = None
    m = re.search(r"^## .*Próxima Ação Imediata\s*\n- \[[ x]\] (.+?)(?: ⏫)?\s*$", texto, re.MULTILINE)
    if m:
        proxima = m.group(1).strip()
    return (str(titulo) if titulo else None), proxima


def _sumiu(e: Exception) -> bool:
    """404 da Semana: a nota foi apagada lá (pelo app) entre a busca e o aviso."""
    return isinstance(e, urllib.error.HTTPError) and e.code == 404


def passo(key: str, inbox: Path, oficina: Path, ledger_path: Path) -> tuple[int, int]:
    """Uma volta: entrega as pendentes e avisa as que viraram nota. Devolve (entregues, notas).
    Cada nota tem o seu try: uma com defeito não segura as outras na fila."""
    entregues = _ler_entregues()
    novas = 0

    def audio(parte_id: str) -> str:
        return _pedido("GET", f"{URL}/partes?id={parte_id}", key).get("audio") or ""

    for c in _pedido("GET", URL + "/pendentes", key).get("capturas", []):
        try:
            destino = entregar(c, inbox, oficina, audio)
        except ValueError as e:
            # Sem nada para guardar (as partes não chegaram): sai da fila da Semana, senão dez
            # assim ocupariam a janela de pendentes para sempre.
            logger.warning(f"Organiza: {e}")
            try:
                _pedido("PUT", URL + "/estado", key, {"id": c["id"], "estado": "nota", "titulo": "Sem conteúdo: nada a guardar"})
            except Exception as e2:
                logger.warning(f"Organiza: não deu para tirar {c['id']} da fila: {e2}")
            continue
        except Exception as e:
            logger.warning(f"Organiza: {c.get('id')} não foi entregue nesta volta: {e}")
            continue
        entregues[c["id"]] = destino.name
        _gravar_entregues(entregues)  # antes de avisar: se a Semana não ouvir, a próxima volta não duplica
        try:
            _pedido("PUT", URL + "/estado", key, {"id": c["id"], "estado": "pc"})
        except Exception as e:
            logger.warning(f"Organiza: Semana não ouviu o 'pc' de {c['id']}: {e}")
        novas += 1
        logger.info(f"Organiza: {destino.name} entregue à Inbox")
    notas = 0
    for cid, nome in list(entregues.items()):
        nota = nota_do_pacote(nome, ledger_path)
        if nota is None:
            continue
        titulo, proxima = resumo_da_nota(nota)
        try:
            _pedido("PUT", URL + "/estado", key, {"id": cid, "estado": "nota", "titulo": titulo, "proxima": proxima})
        except Exception as e:
            if not _sumiu(e):
                logger.warning(f"Organiza: Semana não ouviu o 'nota' de {cid}: {e}")
                continue  # tenta de novo na próxima volta, sem segurar as outras
        del entregues[cid]
        _gravar_entregues(entregues)
        notas += 1
    return novas, notas


def main() -> None:
    ap = argparse.ArgumentParser(description="Traz as anotações do app Organiza para a Inbox do Logos.")
    ap.add_argument("--uma-vez", action="store_true", help="uma volta só, para testar")
    ap.add_argument("--inbox", type=Path, default=None, help="outra pasta no lugar da Inbox (testes)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    key = chave()
    if not key:
        raise SystemExit("Falta a chave da Semana: SEMANA_KEY ou semana_key em state/secrets.yaml.")
    inbox = args.inbox or config.INBOX_DIR
    oficina = config.STATE_DIR / "organiza" / "montando"
    while True:
        try:
            novas, notas = passo(key, inbox, oficina, config.LEDGER_PATH)
            if novas or notas:
                logger.info(f"Organiza: {novas} entregue(s), {notas} virou(aram) nota")
        except Exception as e:  # rede fora, Semana fora: tenta de novo na próxima volta
            logger.warning(f"Organiza: volta falhou: {e}")
            if args.uma_vez:
                raise
        if args.uma_vez:
            return
        time.sleep(INTERVALO_S)


if __name__ == "__main__":
    main()
