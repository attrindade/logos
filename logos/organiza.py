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
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from . import config, cofre, eventos, trilha
from .config import load_secrets
from .pacote import MANIFEST, write_manifest

logger = logging.getLogger(__name__)

URL = "https://semana-andre.pages.dev/api/capturas"
# O Cloudflare da Semana barra o User-Agent padrão do Python (erro 1010).
UA = "Logos-organiza/0.1"
INTERVALO_S = 60
LIMITE_S = 30
TAG = "app"  # triage.ORGANIZA_TAG: o nome do pacote diz de onde ele veio
_TIPO_PALAVRA = {"ideia": "Ideia", "tarefa": "Tarefa", "reuniao": "Reunião"}
_EXT = {"audio/mp4": ".m4a", "audio/aac": ".aac", "audio/ogg": ".ogg",
        "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "application/pdf": ".pdf"}
# Link no texto de uma nota do app: vai em "links", como o bot faz com as entidades do Telegram, e o
# pipeline busca a página. Pontuação colada no fim (ponto, vírgula, parêntese) não faz parte.
_URL = re.compile(r"https?://[^\s<>\"']+")


def _links(texto: str) -> list[str]:
    return list(dict.fromkeys(u.rstrip(".,;:!?)]}") for u in _URL.findall(texto or "")))


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


# O Diário curto do app (escolhido por ele em 14/10). O app manda o número da pergunta dentro do
# envelope e o título sai daqui: a mesma lista está em `Anotacoes.PERGUNTAS_DIARIO` no app.
# O app pergunta só as 3 primeiras desde 10/2026 (`Anotacoes.PERGUNTAS_APP = 3`): a 4ª, "a primeira
# coisa de amanhã", foi para a noite do Planeja. Fica aqui para os Diários antigos com resposta 4.
PERGUNTAS_DIARIO = (
    "Uma coisa boa de hoje, e por que ela aconteceu?",
    "O que pesou ou travou? Que nome você dá ao que sentiu?",
    "O que ficou aberto na cabeça? (lista rápida)",
    "Amanhã, a primeira coisa: o quê, a que horas e onde você começa?",
)
# As escalas que o app manda, com os valores que o /diario do bot grava (formulario.ESCALAS).
_ESCALAS_VALIDAS = {
    "humor_nota": {1, 2, 3, 4, 5},
    "energia": {1, 2, 3, 4, 5},
    "estresse": {"baixo", "médio", "alto"},
}


def _escalas(bruto) -> dict:
    """Só o que o /diario aceitaria: campo conhecido e valor da lista. O resto cai fora calado."""
    if not isinstance(bruto, dict):
        return {}
    return {k: v for k, v in bruto.items() if k in _ESCALAS_VALIDAS and isinstance(v, (int, str))
            and not isinstance(v, bool) and v in _ESCALAS_VALIDAS[k]}


def eh_diario(captura: dict) -> bool:
    """Decidido aqui, pelo conteúdo, não só pelo campo "tipo" da nuvem: quem mexesse no D1 e
    trocasse o tipo faria o título do Diário voltar à Semana em texto puro."""
    return captura.get("tipo") == "diario" or any(p.get("tipo") == "cifrada" for p in captura.get("partes", []))


def abrir_partes(captura: dict, audio, chave) -> dict:
    """Diário: cada parte "cifrada" vira texto ou voz de novo, aqui no PC (só ele tem a chave).
    Parte em claro num Diário não entra (não deveria existir: o celular e a Semana barram).
    Devolve a captura com as partes abertas. CofreErro sobe: sem a chave certa, não entrega."""
    abertas = []
    for p in captura.get("partes", []):
        if p.get("tipo") != "cifrada":
            logger.warning(f"Organiza: parte em claro {p.get('id')} no Diário {captura.get('id')} descartada")
            continue
        env = p.get("audio") or audio(p["id"])
        dentro = json.loads(cofre.abrir(env, p["id"], chave).decode("utf-8"))
        q = {k: v for k, v in p.items() if k not in ("audio", "tem_audio", "mime")}
        q["tipo"] = dentro.get("tipo", "texto")
        if q["tipo"] == "voz":
            q["audio"], q["mime"] = dentro["audio"], dentro.get("mime", "audio/mp4")
        else:
            q["texto"] = dentro.get("texto", "")
        n = dentro.get("pergunta")
        if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= len(PERGUNTAS_DIARIO):
            q["pergunta"] = n
        esc = _escalas(dentro.get("escalas"))
        if esc:
            q["escalas"] = esc
        abertas.append(q)
    return dict(captura, partes=abertas)


def _lista_nomes(x, n: int = 12) -> list[str]:
    if not isinstance(x, list):
        return []
    return [str(v).strip()[:120] for v in x if isinstance(v, str) and str(v).strip()][:n]


def abrir_marcas(captura: dict, chave) -> dict:
    """Projetos, pessoas e times que ele escolheu no app, e a nota que a anotação responde (pedidos
    dele em 14/10). Vêm num envelope fechado para o PC, com o id da nota como dado associado.
    Envelope que não abre não segura a nota: ela entra sem as marcas, e o log avisa."""
    env = captura.get("marcas")
    if not env:
        return {}
    try:
        d = json.loads(cofre.abrir(env, captura["id"], chave).decode("utf-8"))
    except (cofre.CofreErro, ValueError) as e:
        logger.warning(f"Organiza: marcas de {captura.get('id')} não abrem: {e}")
        return {}
    m = {k: _lista_nomes(d.get(k)) for k in ("projetos", "pessoas", "times")}
    r = d.get("responde")
    if isinstance(r, dict) and isinstance(r.get("titulo"), str) and r["titulo"].strip():
        m["responde"] = {"titulo": r["titulo"].strip()[:200], "caminho": str(r.get("caminho") or "")[:300],
                         "trecho": str(r.get("trecho") or "").strip()[:500]}
    return {k: v for k, v in m.items() if v}


def manifest_de(captura: dict, arquivos: dict[str, str]) -> dict:
    """Manifest no formato do bot. O tipo escolhido no app vira a primeira palavra do primeiro
    texto, que é como o triage fixa a categoria (o mesmo que o /tarefa do bot faz)."""
    mensagens = []
    formulario: dict = {}
    for p in sorted(captura.get("partes", []), key=lambda p: (p.get("ordem", 0), p.get("criada_em", ""))):
        if p.get("escalas"):
            # Escalas do Diário curto: vão para o formulário (o frontmatter do Diário), não para o texto.
            formulario.update(p["escalas"])
            continue
        if p.get("tipo") == "texto" and (p.get("texto") or "").strip():
            mensagens.append({"tipo": "texto", "texto": p["texto"].strip(), "quando": p.get("criada_em")})
            links = _links(p["texto"])
            if links:
                mensagens[-1]["links"] = links
        elif p.get("tipo") == "voz" and p["id"] in arquivos:
            mensagens.append({"tipo": "voz", "arquivo": arquivos[p["id"]], "quando": p.get("criada_em")})
        elif p.get("tipo") == "arquivo" and p["id"] in arquivos:
            # Foto ou PDF anexado no app: as mesmas entradas do bot (o pipeline lê o texto da imagem
            # e extrai o do PDF).
            mime = p.get("mime") or ""
            e = {"tipo": "foto" if mime.startswith("image/") else "documento",
                 "arquivo": arquivos[p["id"]], "mime": mime, "quando": p.get("criada_em")}
            if (p.get("texto") or "").strip():
                e["nome_original"] = p["texto"].strip()
            mensagens.append(e)
        else:
            continue
        if p.get("pergunta"):
            mensagens[-1]["pergunta"] = p["pergunta"]
    palavra = _TIPO_PALAVRA.get(captura.get("tipo", ""))
    if palavra and mensagens:  # nota vazia continua vazia: o tipo sozinho não é conteúdo
        if mensagens and mensagens[0]["tipo"] == "texto":
            mensagens[0]["texto"] = f"{palavra}: {mensagens[0]['texto']}"
        else:
            mensagens.insert(0, {"tipo": "texto", "texto": palavra, "quando": captura.get("aberta_em")})
    m = {
        "origem": "organiza",
        "captura_id": captura["id"],
        "aberto_em": captura.get("aberta_em"),
        "fechado_em": captura.get("fechada_em"),
        "motivo_fechamento": "fechada no app",
        "mensagens": mensagens,
    }
    if captura.get("marcas_abertas") and captura.get("tipo") != "diario":
        m["marcas"] = captura["marcas_abertas"]
    if captura.get("tipo") == "diario":
        m["rota"] = "diario"  # o pipeline soma no "Diário — AAAA-MM-DD.md" do dia, como o /diario
        if formulario:
            m["formulario"] = formulario
        if any(x.get("pergunta") for x in mensagens):
            m["perguntas"] = list(PERGUNTAS_DIARIO)  # títulos das respostas (o /diario do bot tem os dele)
    return m


# Captura sem fala (28/10/2026): nenhum texto, nenhum anexo e menos que isto de voz somada. Um toque
# no microfone sem falar, ou um Diário aberto e largado (os dois presos de 15 e 28/10 somavam ~470
# bytes). Não é falha: vira o evento `info` e, com a Semana nova, o estado final "descartada".
VAZIO_BYTES = 2048


class Vazia(Exception):
    """A captura fechada não tinha nada: nem texto, nem anexo, nem voz que valha (VAZIO_BYTES)."""


def entregar(captura: dict, inbox: Path, oficina: Path, audio=None) -> Path:
    """Monta o pacote fora da Inbox e só então o move para lá, inteiro: o watcher nunca vê um
    pacote pela metade. Se já foi entregue (queda entre mover e avisar a Semana), não duplica.
    `audio(id_da_parte)` devolve o base64 da voz (ou da foto e do PDF): a lista de pendentes não traz o áudio (várias
    vozes juntas estouravam a memória do Worker), então cada uma vem num pedido.

    Levanta `Vazia` quando não há nada (ver VAZIO_BYTES) e ValueError quando uma parte diz ter
    áudio e ele não veio (isso não é vazio: pode ser a Semana, e tenta de novo mais tarde)."""
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
    voz_bytes, anexo, faltou = 0, False, False
    for p in captura.get("partes", []):
        if p.get("tipo") not in ("voz", "arquivo"):
            continue
        b64 = p.get("audio") or (audio(p["id"]) if audio and p.get("tem_audio") else None)
        if not b64:
            faltou = faltou or bool(p.get("tem_audio"))
            continue
        n += 1
        arq = f"{n:02d}{_EXT.get(p.get('mime') or '', '.m4a' if p.get('tipo') == 'voz' else '.bin')}"
        cru = base64.b64decode(b64)
        (tmp / arq).write_bytes(cru)
        arquivos[p["id"]] = arq
        if p.get("tipo") == "voz":
            voz_bytes += len(cru)
        else:
            anexo = anexo or bool(cru)
    texto = any((p.get("texto") or "").strip() for p in captura.get("partes", [])
                if p.get("tipo") == "texto" and not p.get("escalas"))
    escalas = any(p.get("escalas") for p in captura.get("partes", []))
    # Sem parte nenhuma (e fora do Diário) segue o caminho de antes, em `passo`: sai da fila da Semana.
    tem_partes = bool(captura.get("partes")) or eh_diario(captura)
    if tem_partes and not (texto or anexo or escalas or faltou) and voz_bytes < VAZIO_BYTES:
        shutil.rmtree(tmp)
        raise Vazia(f"anotação {captura['id']} vazia ({voz_bytes} bytes de voz, sem texto nem anexo)")
    m = manifest_de(captura, arquivos)
    # Diário só com as escalas (humor, energia, estresse) é um check-in: o pipeline grava o formulário.
    if not m["mensagens"] and not m.get("formulario"):
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


def descartado_no_ledger(nome: str, ledger_path: Path) -> bool:
    """O watcher descartou o pacote por não ter fala nem texto (`pipeline._descartar_vazio`)."""
    try:
        dados = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return any(isinstance(e, dict) and e.get("filename") == nome and e.get("state") == "descartada"
               for e in dados.values())


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


# Notas que não abrem (chave trocada) ou falham sempre: pedidas fora da janela das pendentes.
_FALHARAM: set[str] = set()
_TENTATIVAS: dict[str, int] = {}
# Nota com partes de que nada sai (uma voz que diz ter áudio e ele não vem, um envelope que abre e
# não é JSON): depois de tantas tentativas vira falha definitiva, guardada em disco. Antes ela
# voltava a cada minuto para sempre, sem aviso nenhum (a de 16/10/2026 repetiu 5.437 vezes no log).
TENTATIVAS_ILEGIVEL = 3
# Espera antes de cada nova tentativa (28/10/2026): com o laço de 60 s, as 3 voltas davam "falha
# definitiva" em 2 min (o Diário de 28/10 02:11). A 2ª vem depois de 10 min e a 3ª depois de 6 h
# (números sugeridos, sem medida): o que é rede ou a Semana atrasada se resolve nesse tempo.
ESPERA_ILEGIVEL_S = (10 * 60, 6 * 3600)
_PROXIMA: dict[str, float] = {}  # id → antes desta hora, fica no ?pular=


def _falhas_path() -> Path:
    return config.STATE_DIR / "organiza" / "falhas.json"


def _ler_falhas() -> dict[str, dict]:
    """{id: {nome, motivo, desde, tentativas}}. Apagar a linha de um id faz o drenador tentar de novo."""
    try:
        d = json.loads(_falhas_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _falha_definitiva(c: dict, motivo: str, diario: bool) -> None:
    """Tira a nota da janela das pendentes de vez (também depois de reiniciar) e avisa UMA vez na
    aba Logos. Na nuvem nada é apagado: com o defeito corrigido, basta tirar o id de falhas.json."""
    cid = c["id"]
    falhas = _ler_falhas()
    _FALHARAM.add(cid)
    if cid in falhas:
        return
    try:
        nome = nome_pacote(c)
    except (KeyError, ValueError, AttributeError):
        nome = cid
    falhas[cid] = {"nome": nome, "motivo": motivo[:300], "desde": int(time.time()),
                   "tentativas": _TENTATIVAS.get(cid, 0)}
    p = _falhas_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(falhas, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        logger.warning(f"Organiza: não deu para guardar a falha de {cid}: {e}")
    m = re.match(r"\d{4}-(\d\d)-(\d\d) (\d\d)-(\d\d)", nome)
    quando = f"de {m[2]}/{m[1]} {m[3]}:{m[4]}" if m else "sem data"
    # Nem título nem fala, de nenhuma nota (o Diário nunca manda isso para a nuvem): data e id bastam.
    o_que = "Um Diário do celular" if diario else "Uma anotação do celular"
    eventos.registrar(
        "organiza",
        f"{o_que} {quando} não virou nota: nada legível nas partes depois de {falhas[cid]['tentativas']} "
        f"tentativas. Continua na nuvem e o PC parou de tentar (id {cid[:8]}).",
        "falha",
    )
    logger.error(f"Organiza: {cid} marcada como falha definitiva ({motivo}); ver {p}")


# ---- captura vazia: "descartada" (28/10/2026) ----
#
# A Semana nova anuncia o estado final "descartada" em `recursos` (/api/semana). Ele não apaga as
# partes nem o áudio no D1: fica reversível. Sem o recurso (Semana antiga), a vazia fica guardada em
# vazias.json, fora da janela das pendentes, sem ERROR, até a Semana anunciar; aí fecha, uma vez só.
# As entradas "sem conteúdo" de falhas.json (as de antes desta regra) fecham do mesmo jeito, sem abrir
# nada: "sem conteúdo" era manifesto sem mensagem nenhuma, que pela regra nova é vazia.

RECURSOS_S = 600
_RECURSOS = {"ate": 0.0, "lista": None}


def _vazias_path() -> Path:
    return config.STATE_DIR / "organiza" / "vazias.json"


def _ler_json(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _gravar_json(p: Path, d: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def semana_descarta(key: str) -> bool:
    """A Semana no ar sabe o estado "descartada"? Pergunta no máximo a cada RECURSOS_S, e só quando
    há o que descartar. Sem resposta, vale a regra "falta recurso = API antiga"."""
    agora = time.time()
    if _RECURSOS["lista"] is None or agora >= _RECURSOS["ate"]:
        try:
            r = _pedido("GET", URL.replace("/capturas", "/semana"), key).get("recursos")
            _RECURSOS["lista"] = r if isinstance(r, list) else []
        except Exception as e:
            logger.info(f"Organiza: não deu para ler os recursos da Semana ({e}); segue como API antiga")
            _RECURSOS["lista"] = []
        _RECURSOS["ate"] = agora + RECURSOS_S
    return "descartada" in _RECURSOS["lista"]


def _quando(nome: str) -> str:
    m = re.match(r"\d{4}-(\d\d)-(\d\d) (\d\d)-(\d\d)", nome)
    return f"de {m[2]}/{m[1]} {m[3]}:{m[4]}" if m else "sem data"


def _vazia(c: dict, diario: bool) -> None:
    """Guarda a vazia (fora da janela, também depois de reiniciar) e avisa UMA vez, como `info`.
    Do Diário vai só data e id, como na falha."""
    cid = c["id"]
    vazias = _ler_json(_vazias_path())
    if cid in vazias:
        return
    try:
        nome = nome_pacote(c)
    except (KeyError, ValueError, AttributeError):
        nome = cid
    vazias[cid] = {"nome": nome, "desde": int(time.time()), "diario": diario}
    try:
        _gravar_json(_vazias_path(), vazias)
    except OSError as e:
        logger.warning(f"Organiza: não deu para guardar a vazia {cid}: {e}")
    o_que = "Diário vazio descartado" if diario else "Anotação vazia descartada"
    eventos.registrar("organiza", f"{o_que}: a do celular {_quando(nome)} não tinha fala nem texto (id {cid[:8]}).",
                      "info")
    logger.info(f"Organiza: {cid} vazia, sem fala nem texto; descartada")


def _descartadas() -> set[str]:
    return {i for d in (_ler_json(_vazias_path()), _ler_falhas()) for i, v in d.items()
            if isinstance(v, dict) and v.get("descartada")}


def fechar_descartadas(key: str) -> int:
    """Manda "descartada" às vazias guardadas e às falhas "sem conteúdo" antigas, uma vez cada, quando
    a Semana sabe o estado. Devolve quantas fechou nesta volta."""
    vazias = _ler_json(_vazias_path())
    falhas = _ler_falhas()
    abertas = [(vazias, i) for i, v in vazias.items() if isinstance(v, dict) and not v.get("descartada")]
    abertas += [(falhas, i) for i, v in falhas.items() if isinstance(v, dict) and not v.get("descartada")
                and "sem conteúdo" in str(v.get("motivo") or "")]
    if not abertas or not semana_descarta(key):
        return 0
    n, antigas = 0, 0
    for onde, cid in abertas:
        try:
            _pedido("PUT", URL + "/estado", key, {"id": cid, "estado": "descartada"})
        except Exception as e:
            if not _sumiu(e):
                logger.warning(f"Organiza: Semana não ouviu o 'descartada' de {cid}: {e}")
                continue
        onde[cid]["descartada"] = int(time.time())
        n += 1
        antigas += onde is falhas
    try:
        if n - antigas:
            _gravar_json(_vazias_path(), vazias)
        if antigas:
            _gravar_json(_falhas_path(), falhas)
    except OSError as e:
        logger.warning(f"Organiza: não deu para guardar as descartadas: {e}")
    if antigas:
        eventos.registrar("organiza", f"{antigas} anotação(ões) vazia(s) presa(s) desde antes fechada(s) como "
                                      "descartada(s) na Semana (nada foi apagado lá).", "info")
    if n:
        logger.info(f"Organiza: {n} captura(s) vazia(s) fechada(s) como descartada(s)")
    return n


PULAR_MAX = 50  # ids no ?pular= das pendentes


def _tentar_depois(c: dict, e: Exception) -> None:
    """Falha temporária: fica para a próxima volta; depois de 5, sai da janela até reiniciar."""
    logger.warning(f"Organiza: {c.get('id')} não foi entregue nesta volta: {e}")
    _TENTATIVAS[c["id"]] = _TENTATIVAS.get(c["id"], 0) + 1
    if _TENTATIVAS[c["id"]] >= 5:
        _FALHARAM.add(c["id"])


def passo(key: str, inbox: Path, oficina: Path, ledger_path: Path) -> tuple[int, int]:
    """Uma volta: entrega as pendentes e avisa as que viraram nota. Devolve (entregues, notas).
    Cada nota tem o seu try: uma com defeito não segura as outras na fila."""
    trilha.volta()  # o PC estava ligado desde quando (a trilha das capturas, LOG 9 da onda 4)
    entregues = _ler_entregues()
    novas = 0
    chave_cofre = None
    if caminho_chave_existe():
        conferir_chave_publicada(key)

    def audio(parte_id: str) -> str:
        return _pedido("GET", f"{URL}/partes?id={parte_id}", key).get("audio") or ""

    fechar_descartadas(key)
    # Já "descartada" na Semana não volta nas pendentes (só nuvem e pegando): fora do ?pular=, senão
    # meses de toques sem fala empurrariam as temporárias para fora do corte de 50 (como o B1 de 25/10).
    def abertas(d: dict) -> dict:
        return {i: v for i, v in d.items() if not (isinstance(v, dict) and v.get("descartada"))}

    definitivas = abertas(_ler_falhas())
    _FALHARAM.update(definitivas)  # as definitivas valem também depois de reiniciar
    # As vazias guardadas também: até a Semana anunciar "descartada", elas continuam pendentes lá.
    vazias = abertas(_ler_json(_vazias_path()))
    _FALHARAM.update(vazias)
    _FALHARAM.difference_update(_descartadas())
    agora = time.time()
    esperando = {i for i, t in _PROXIMA.items() if t > agora}
    # A rota aceita 50 ids: as definitivas vão primeiro (revisão de 25/10, B1). Por ordem alfabética,
    # uma definitiva podia sair do corte, voltar à janela e ganhar "pegando" a cada minuto.
    fixas = sorted(definitivas) + sorted(vazias.keys() - definitivas.keys())
    pular = ",".join((fixas + sorted((_FALHARAM | esperando) - set(fixas)))[:PULAR_MAX])
    pendentes = _pedido("GET", URL + "/pendentes" + (f"?pular={pular}" if pular else ""), key).get("capturas", [])
    trilha.vistas(pendentes)  # só horários e id curto (LOG 9 da onda 4); nunca falha
    for c in pendentes:
        diario = eh_diario(c)
        # "pegando" antes de baixar: a partir daqui o celular não reabre nem acrescenta (revisão de
        # 14/10: uma parte que entrasse no meio da entrega sumia calada). 409 = reabriu antes.
        try:
            _pedido("PUT", URL + "/estado", key, {"id": c["id"], "estado": "pegando"})
        except urllib.error.HTTPError as e:
            if e.code == 409:
                logger.info(f"Organiza: {c.get('id')} reaberta no celular antes da entrega; fica para depois")
                continue
            if e.code != 400:  # 400 = Semana antiga, sem "pegando": segue como antes
                logger.warning(f"Organiza: 'pegando' de {c.get('id')} falhou: {e}")
                continue
        except Exception as e:
            logger.warning(f"Organiza: 'pegando' de {c.get('id')} falhou: {e}")
            continue
        try:
            if diario:
                chave_cofre = chave_cofre or cofre.carregar_ou_criar()
                c = dict(abrir_partes(c, audio, chave_cofre), tipo="diario")
            elif c.get("marcas"):
                chave_cofre = chave_cofre or cofre.carregar_ou_criar()
                c = dict(c, marcas_abertas=abrir_marcas(c, chave_cofre))
            destino = entregar(c, inbox, oficina, audio)
        except Vazia:
            _vazia(c, diario)
            _FALHARAM.add(c["id"])
            fechar_descartadas(key)
            continue
        except cofre.CofreErro as e:
            # Fica na nuvem (o envelope some quando vira "pc"): com a chave certa de volta, abre. Até
            # o drenador reiniciar, não volta a ocupar a janela de 10 das pendentes.
            logger.warning(f"Organiza: Diário {c.get('id')} não abre: {e}")
            _FALHARAM.add(c["id"])
            continue
        except ValueError as e:
            if isinstance(e, json.JSONDecodeError):
                # Resposta da Semana que não é JSON (página de erro no meio de um pedido de parte):
                # problema de rede, não nota ilegível. Não pode virar falha definitiva (revisão, B1).
                _tentar_depois(c, e)
                continue
            if c.get("partes") or diario:
                # Tem partes mas nada saiu delas: fica na nuvem para olhar, sem apagar nada lá. As
                # tentativas são espaçadas (ESPERA_ILEGIVEL_S); depois de TENTATIVAS_ILEGIVEL, falha
                # definitiva: sai da janela e avisa uma vez.
                n = _TENTATIVAS[c["id"]] = _TENTATIVAS.get(c["id"], 0) + 1
                if n >= TENTATIVAS_ILEGIVEL:
                    _falha_definitiva(c, str(e), diario)
                    continue
                espera = ESPERA_ILEGIVEL_S[min(n, len(ESPERA_ILEGIVEL_S)) - 1]
                _PROXIMA[c["id"]] = time.time() + espera
                logger.warning(f"Organiza: {c.get('id')} sem conteúdo legível, fica na nuvem "
                               f"(tentativa {n}/{TENTATIVAS_ILEGIVEL}, a próxima em {espera // 60} min): {e}")
                continue
            # Nenhuma parte: sai da fila da Semana, senão dez assim ocupariam a janela para sempre.
            # Com a Semana nova, como "descartada" (não apaga nada); na antiga, como antes.
            logger.info(f"Organiza: {e}")
            corpo = {"id": c["id"], "estado": "nota", "titulo": "Sem conteúdo: nada a guardar"}
            if semana_descarta(key):
                corpo = {"id": c["id"], "estado": "descartada"}
            try:
                _pedido("PUT", URL + "/estado", key, corpo)
            except Exception as e2:
                logger.warning(f"Organiza: não deu para tirar {c['id']} da fila: {e2}")
            continue
        except Exception as e:
            _tentar_depois(c, e)
            continue
        entregues[c["id"]] = destino.name
        if diario:
            entregues[c["id"]] = "diario:" + destino.name  # na volta, sem título (ver abaixo)
        _gravar_entregues(entregues)  # antes de avisar: se a Semana não ouvir, a próxima volta não duplica
        trilha.no_pc(c["id"])
        try:
            _pedido("PUT", URL + "/estado", key, {"id": c["id"], "estado": "pc"})
        except Exception as e:
            logger.warning(f"Organiza: Semana não ouviu o 'pc' de {c['id']}: {e}")
        novas += 1
        logger.info(f"Organiza: {destino.name} entregue à Inbox")
        eventos.registrar("organiza", "Diário do celular chegou ao PC." if diario else f"Anotação do celular chegou ao PC ({destino.name}).")
    notas = 0
    # A caixa (logos/caixa.py): as entregues fora do Diário que ainda não têm decisão, ou cuja decisão
    # foi "nenhum modelo respondeu" (LOG 6b): contam como esperando até virarem nota no app.
    esperando = sum(1 for cid, nome in entregues.items() if not nome.startswith("diario:")
                    and _esperando_caixa(caixa_da(cid)))
    for cid, nome in list(entregues.items()):
        diario = nome.startswith("diario:")
        nome = nome.removeprefix("diario:")
        dec = None if diario else aplicar_caixa(key, cid)
        nota = nota_do_pacote(nome, ledger_path)
        if nota is None:
            if not descartado_no_ledger(nome, ledger_path):
                continue
            # Toque sem fala: "descartada" na Semana nova; na antiga, "nota" com o aviso no título
            # (o áudio já saiu da nuvem no "pc"). Do Diário, nem esse título.
            if semana_descarta(key):
                corpo = {"id": cid, "estado": "descartada"}
            else:
                corpo = {"id": cid, "estado": "nota"}
                if not diario:
                    corpo["titulo"] = "Gravação vazia: nada a guardar"
        else:
            if dec and not dec.get("enviado"):
                # O efeito da caixa não chegou à Semana nesta volta (revisão da onda 2, 28/10): fica em
                # `entregues` até chegar, senão o app mostraria o destino sem o efeito (a fala do plano,
                # o complemento ou o recado) e ninguém tentaria de novo.
                continue
            # Do Diário não volta nada para a nuvem além de "virou nota": nem título, nem ação.
            corpo = {"id": cid, "estado": "nota"}
            if not diario:
                corpo["titulo"], corpo["proxima"] = resumo_da_nota(nota)
                if dec:
                    from . import caixa
                    corpo["destino"], corpo["entendi"] = dec["destino"], caixa._frase(dec.get("entendi"), caixa.MAX_ENTENDI)
        try:
            _pedido("PUT", URL + "/estado", key, corpo)
        except Exception as e:
            if not _sumiu(e):
                logger.warning(f"Organiza: Semana não ouviu o 'nota' de {cid}: {e}")
                continue  # tenta de novo na próxima volta, sem segurar as outras
        del entregues[cid]
        _gravar_entregues(entregues)
        if nota is not None:
            trilha.nota(cid)
        notas += 1
    linha_da_caixa(key, esperando)
    return novas, notas


# ---- a caixa da captura (logos/caixa.py, LOG 9 de 28/10/2026) ----

def caixa_da(cid: str) -> dict | None:
    if not config.CAIXA_LIGADA:
        return None
    from . import caixa
    return caixa.ler(cid)


def _esperando_caixa(dec: dict | None) -> bool:
    if dec is None:
        return True
    from . import caixa
    return caixa.nao_entendeu(dec)


def aplicar_caixa(key: str, cid: str) -> dict | None:
    """A decisão do watcher vira efeito (uma vez) e volta à Semana como `pc` com destino e entendi.
    Se a Semana não ouvir, tenta de novo na próxima volta: o efeito é idempotente."""
    dec = caixa_da(cid)
    if dec is None or dec.get("enviado"):
        return dec
    from . import caixa
    try:
        caixa.aplicar(key, dec, _pedido)
        caixa.enviar_estado(key, dec, _pedido)
        logger.info(f"Organiza: caixa de {cid[:8]} aplicada ({dec['destino']})")
    except Exception as e:
        logger.warning(f"Organiza: caixa de {cid[:8]} fica para a próxima volta: {e}")
    return dec


def linha_da_caixa(key: str, esperando: int) -> None:
    """A linha `caixa` de /api/linhas (só números), quando os números mudam."""
    if not config.CAIXA_LIGADA:
        return
    from . import caixa
    try:
        if caixa.publicar_linha(key, esperando, _pedido):
            logger.info("Organiza: linha da caixa atualizada")
    except Exception as e:
        logger.warning(f"Organiza: linha da caixa não foi: {e}")


# ---- a linha `manha` (LOG 12, 28/10/2026: D3 = (a), o sono vai ao cartão da manhã do Planeja) ----

MANHA_ESPERA_S = 600
_MANHA = {"proxima": 0.0}


def _manha_path() -> Path:
    return config.STATE_DIR / "organiza" / "manha.json"


def manha_ao_celular(key: str, agora=None) -> bool:
    """Publica `manha: {sono_min}` uma vez por dia, só com a noite de HOJE fechada (`sono.manha_de_hoje`).
    Uma noite de outro dia nunca sai: o relógio parou em 22/10 e uma noite velha não é a de hoje.
    Antes de a Semana conhecer a chave (SEM 3), o PUT dá 400: tenta de novo a cada 10 min, em DEBUG."""
    from . import sono
    m = sono.manha_de_hoje(agora)
    if not m or time.time() < _MANHA["proxima"]:
        return False
    if _ler_json(_manha_path()).get("dia") == m["dia"]:
        return False
    try:
        _pedido("PUT", URL.replace("/capturas", "/linhas"), key, {"chave": "manha", "dados": {"sono_min": m["sono_min"]}})
    except urllib.error.HTTPError as e:
        _MANHA["proxima"] = time.time() + MANHA_ESPERA_S
        if e.code == 400:
            logger.debug("Organiza: a Semana ainda não conhece a linha manha (SEM 3); tenta depois")
        else:
            logger.warning(f"Organiza: linha manha não foi: {e}")
        return False
    except Exception as e:
        _MANHA["proxima"] = time.time() + MANHA_ESPERA_S
        logger.warning(f"Organiza: linha manha não foi: {e}")
        return False
    _gravar_json(_manha_path(), {"dia": m["dia"]})
    logger.info("Organiza: linha manha publicada")
    return True


def caminho_chave_existe() -> bool:
    return cofre.caminho_chave().exists()


def conferir_chave_publicada(key: str) -> bool:
    """A chave "cofre" na Semana tem de ser a deste PC. Se alguém trocar, o celular passa a fixar
    a dele só com o aceite do dono, mas o PC avisa alto no log de qualquer jeito."""
    try:
        linhas = _pedido("GET", URL.replace("/capturas", "/linhas"), key).get("linhas", {})
        publicada = ((linhas.get("cofre") or {}).get("dados") or {}).get("pub")
    except Exception as e:
        logger.warning(f"Organiza: não deu para conferir a chave do cofre na Semana: {e}")
        return True
    minha = cofre.publica_b64(cofre.carregar_ou_criar())
    if publicada and publicada != minha:
        logger.error("Organiza: A CHAVE DO COFRE NA SEMANA NÃO É A DESTE PC "
                     f"(lá {cofre.impressao(publicada)}, aqui {cofre.impressao(minha)}). "
                     "Se não foi você, troque a SEMANA_KEY e rode python -m logos.cofre --publicar.")
        return False
    return True


def _celular_path() -> Path:
    return config.STATE_DIR / "organiza" / "celular.pub"


def chave_celular(key: str) -> str | None:
    """A pública do celular (linha "celular" da Semana), fixada na primeira vez que aparece.

    Quem tiver a chave da Semana poderia publicar a pública dele e passar a ler o que o PC fecha
    para o celular (WhatsApp, vault). Por isso: a primeira vista fica em `state/organiza/celular.pub`
    e, se a publicada mudar, nada é fechado para a nova e o log avisa. Celular trocado ou app
    reinstalado do zero: confira a impressão no app e apague o `celular.pub` para fixar a nova.
    """
    agora = time.time()
    if _CEL_CACHE["ate"] > agora:  # a fila do WhatsApp e o vault perguntam a cada volta
        return _CEL_CACHE["pub"]
    linhas = _pedido("GET", URL.replace("/capturas", "/linhas"), key).get("linhas", {})
    pub = ((linhas.get("celular") or {}).get("dados") or {}).get("pub")
    if not _pub_valida(pub):
        return None
    fixada_path = _celular_path()
    try:
        fixada = fixada_path.read_text(encoding="utf-8").strip()
    except OSError:
        fixada_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = fixada_path.with_suffix(".tmp")
        tmp.write_text(pub, encoding="utf-8")
        tmp.replace(fixada_path)
        logger.info(f"Organiza: chave do celular fixada ({cofre.impressao(pub)}); confira a impressão no app")
        fixada = pub
    if fixada != pub:
        logger.warning(f"Organiza: a chave do celular publicada ({cofre.impressao(pub)}) não é a fixada "
                       f"({cofre.impressao(fixada)}). Nada vai para o celular até conferir: se o app foi "
                       f"reinstalado do zero, apague {fixada_path} para fixar a nova.")
        return None
    _CEL_CACHE.update(pub=pub, ate=agora + 300)
    return pub


_CEL_CACHE = {"pub": None, "ate": 0.0}


def _pub_valida(pub) -> bool:
    """32 bytes e uma chave X25519 que dá segredo de verdade (ponto de ordem baixa, como tudo
    zero, faria toda cifra para o celular falhar para sempre depois de fixada)."""
    try:
        if not isinstance(pub, str):
            return False
        cru = base64.b64decode(pub, validate=True)
        if len(cru) != 32:
            return False
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
        X25519PrivateKey.generate().exchange(X25519PublicKey.from_public_bytes(cru))
        return True
    except Exception:
        return False


FORMATO_LOG = "%(asctime)s %(levelname)s %(message)s"


def configurar_log(laco: bool) -> None:
    """O drenador em laço grava `logs/organiza.log` com rotação e em UTF-8 (28/10/2026: o arquivo
    crescia sem limite e saía com acento quebrado, "entregue � Inbox", porque vinha do stderr
    redirecionado pelo Organiza.vbs, na página de código do Windows).

    Com o .vbs antigo (`2>> organiza.log`), o stderr já é esse arquivo e girar não dá (o cmd o mantém
    aberto): o log continua pelo stderr, agora em UTF-8, e a rotação começa quando o .vbs mandar o
    stderr para `organiza-stderr.log`. Fora do laço (`--uma-vez`, `--inbox`), só o terminal."""
    if not laco:
        logging.basicConfig(level=logging.INFO, format=FORMATO_LOG)
        return
    logging.basicConfig(level=logging.INFO, format=FORMATO_LOG, handlers=handlers_do_log(sys.stderr))


def handlers_do_log(stderr) -> list[logging.Handler]:
    from . import diagnostico
    caminho = config.LOGS_DIR / "organiza.log"
    if diagnostico.mesmo_arquivo(stderr, caminho):
        try:
            stderr.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
        return [logging.StreamHandler(stderr)]
    handlers: list[logging.Handler] = [diagnostico.arquivo_de_log(caminho)]
    try:
        if stderr is not None and stderr.isatty():
            handlers.append(logging.StreamHandler(stderr))
    except (AttributeError, ValueError):
        pass
    return handlers


def main() -> None:
    ap = argparse.ArgumentParser(description="Traz as anotações do app Organiza para a Inbox do Logos.")
    ap.add_argument("--uma-vez", action="store_true", help="uma volta só, para testar")
    ap.add_argument("--inbox", type=Path, default=None, help="outra pasta no lugar da Inbox (testes)")
    args = ap.parse_args()
    laco = args.inbox is None and not args.uma_vez
    configurar_log(laco)
    if laco:
        from . import diagnostico
        diagnostico.ligar_rastro("drenador")
    key = chave()
    if not key:
        raise SystemExit("Falta a chave da Semana: SEMANA_KEY ou semana_key em state/secrets.yaml.")
    inbox = args.inbox or config.INBOX_DIR
    oficina = config.STATE_DIR / "organiza" / "montando"
    ultima_ida = 0.0
    if args.inbox is None and not args.uma_vez:
        from . import acoes_celular, dia_celular
        dia_celular.iniciar(key)  # as falas do Planeja têm laço próprio, de 15 s
        acoes_celular.iniciar(key)  # tarefa marcada, análise de conversa, contatos: idem
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
        # O sentido contrário (PC → celular): a fila do WhatsApp a cada volta (só vai se mudou);
        # o vault mais espaçado, que muda devagar.
        if args.inbox is None:
            fila_ao_celular(key)
            pedidos_do_celular(key)
            vigia_ao_celular(key)
            indicios_ao_celular(key)
            try:
                manha_ao_celular(key)
            except Exception as e:
                logger.warning(f"Organiza: linha manha: {e}")
            from . import acoes_celular
            if time.time() - ultima_ida >= IDA_S or acoes_celular.ida_pedida() or briefing_novo(ultima_ida):
                ultima_ida = time.time()
                ida_ao_celular(key)
        trilha.descanso()  # o fim da volta: o que passar de 60 s até a próxima é PC parado
        time.sleep(INTERVALO_S)


IDA_S = 600


def briefing_novo(desde: float) -> bool:
    """O briefing das 8h mudou depois da última ida: vai já (o aviso das 8h15 abre o de hoje)."""
    try:
        from . import consulta
        pasta = consulta.vault_root() / "briefings"
        return any(f.stat().st_mtime > desde for f in pasta.glob("*.md"))
    except Exception:
        return False


def fila_ao_celular(key: str) -> None:
    """A fila do WhatsApp do Assistente, fechada para o celular (só quando mudou)."""
    from . import whatsapp_celular
    try:
        whatsapp_celular.passo(key)
    except Exception as e:
        logger.warning(f"WhatsApp no celular: volta falhou: {e}")


def pedidos_do_celular(key: str) -> None:
    """Jobs do vault que o celular pediu para rodar (a allowlist do bot)."""
    from . import pedidos_celular
    try:
        n = pedidos_celular.passo(key)
        if n:
            logger.info(f"Pedidos do celular: {n} disparado(s)")
    except Exception as e:
        logger.warning(f"Pedidos do celular: volta falhou: {e}")


def vigia_ao_celular(key: str) -> None:
    """O estado das peças e o registro de eventos, para a aba Logos (`logos/vigia.py`)."""
    from . import vigia
    try:
        vigia.passo(key)
    except Exception as e:
        logger.warning(f"Vigia: volta falhou: {e}")


def indicios_ao_celular(key: str) -> None:
    """Propostas de "parece feito" (`logos/indicios.py`). Desligado de fábrica: sai na hora."""
    if not config.INDICIOS_LIGADO:
        return
    from . import indicios
    try:
        n = indicios.passo(key)
        if n:
            logger.info(f"Indícios: {n} proposta(s) para o celular")
    except Exception as e:
        logger.warning(f"Indícios: volta falhou: {e}")


def ida_ao_celular(key: str) -> None:
    """O que o PC manda para o celular, fechado com a chave dele: as notas do vault."""
    from . import vault_celular  # aqui dentro: vault_celular importa este módulo
    try:
        r = vault_celular.passo(key)
        if r:
            # "0 enviada(s)" é o normal: só em DEBUG (28/10/2026, eram centenas de linhas no log).
            logger.log(logging.INFO if (r[0] or r[1]) else logging.DEBUG,
                       f"Vault no celular: {r[0]} enviada(s), {r[1]} apagada(s)")
    except Exception as e:
        logger.warning(f"Vault no celular: volta falhou: {e}")


if __name__ == "__main__":
    main()
