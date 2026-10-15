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

from . import config, cofre, eventos
from .telegram import MANIFEST, load_secrets, write_manifest

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


def entregar(captura: dict, inbox: Path, oficina: Path, audio=None) -> Path:
    """Monta o pacote fora da Inbox e só então o move para lá, inteiro: o watcher nunca vê um
    pacote pela metade. Se já foi entregue (queda entre mover e avisar a Semana), não duplica.
    `audio(id_da_parte)` devolve o base64 da voz (ou da foto e do PDF): a lista de pendentes não traz o áudio (várias
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
        if p.get("tipo") not in ("voz", "arquivo"):
            continue
        b64 = p.get("audio") or (audio(p["id"]) if audio and p.get("tem_audio") else None)
        if not b64:
            continue
        n += 1
        arq = f"{n:02d}{_EXT.get(p.get('mime') or '', '.m4a' if p.get('tipo') == 'voz' else '.bin')}"
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
    chave_cofre = None
    if caminho_chave_existe():
        conferir_chave_publicada(key)

    def audio(parte_id: str) -> str:
        return _pedido("GET", f"{URL}/partes?id={parte_id}", key).get("audio") or ""

    for c in _pedido("GET", URL + "/pendentes", key).get("capturas", []):
        diario = eh_diario(c)
        try:
            if diario:
                chave_cofre = chave_cofre or cofre.carregar_ou_criar()
                c = dict(abrir_partes(c, audio, chave_cofre), tipo="diario")
            elif c.get("marcas"):
                chave_cofre = chave_cofre or cofre.carregar_ou_criar()
                c = dict(c, marcas_abertas=abrir_marcas(c, chave_cofre))
            destino = entregar(c, inbox, oficina, audio)
        except cofre.CofreErro as e:
            # Fica na nuvem (o envelope some quando vira "pc"): com a chave certa de volta, abre.
            logger.warning(f"Organiza: Diário {c.get('id')} não abre: {e}")
            continue
        except ValueError as e:
            if c.get("partes") or diario:
                # Tem partes mas nada saiu delas: fica na nuvem para olhar, sem apagar nada lá.
                logger.warning(f"Organiza: {c.get('id')} sem conteúdo legível, fica na nuvem: {e}")
                continue
            # Nenhuma parte: sai da fila da Semana, senão dez assim ocupariam a janela para sempre.
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
        if diario:
            entregues[c["id"]] = "diario:" + destino.name  # na volta, sem título (ver abaixo)
        _gravar_entregues(entregues)  # antes de avisar: se a Semana não ouvir, a próxima volta não duplica
        try:
            _pedido("PUT", URL + "/estado", key, {"id": c["id"], "estado": "pc"})
        except Exception as e:
            logger.warning(f"Organiza: Semana não ouviu o 'pc' de {c['id']}: {e}")
        novas += 1
        logger.info(f"Organiza: {destino.name} entregue à Inbox")
        eventos.registrar("organiza", "Diário do celular chegou ao PC." if diario else f"Anotação do celular chegou ao PC ({destino.name}).")
    notas = 0
    for cid, nome in list(entregues.items()):
        diario = nome.startswith("diario:")
        nome = nome.removeprefix("diario:")
        nota = nota_do_pacote(nome, ledger_path)
        if nota is None:
            continue
        # Do Diário não volta nada para a nuvem além de "virou nota": nem título, nem ação.
        corpo = {"id": cid, "estado": "nota"}
        if not diario:
            corpo["titulo"], corpo["proxima"] = resumo_da_nota(nota)
        try:
            _pedido("PUT", URL + "/estado", key, corpo)
        except Exception as e:
            if not _sumiu(e):
                logger.warning(f"Organiza: Semana não ouviu o 'nota' de {cid}: {e}")
                continue  # tenta de novo na próxima volta, sem segurar as outras
        del entregues[cid]
        _gravar_entregues(entregues)
        notas += 1
    return novas, notas


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
            from . import acoes_celular
            if time.time() - ultima_ida >= IDA_S or acoes_celular.ida_pedida():
                ultima_ida = time.time()
                ida_ao_celular(key)
        time.sleep(INTERVALO_S)


IDA_S = 600


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


def ida_ao_celular(key: str) -> None:
    """O que o PC manda para o celular, fechado com a chave dele: as notas do vault."""
    from . import vault_celular  # aqui dentro: vault_celular importa este módulo
    try:
        r = vault_celular.passo(key)
        if r and (r[0] or r[1]):
            logger.info(f"Vault no celular: {r[0]} enviada(s), {r[1]} apagada(s)")
    except Exception as e:
        logger.warning(f"Vault no celular: volta falhou: {e}")


if __name__ == "__main__":
    main()
