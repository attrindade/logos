"""Ações que o app Organiza pede e só o PC executa (pedidos dele em 14/10/2026, à noite).

O celular fecha cada ação com a chave do PC (o mesmo cofre do Diário) e põe na Semana
(`/api/acoes`); este laço, dentro do drenador e numa thread própria de 15 s, abre e executa:

    concluir   marca `- [ ]` → `- [x]` numa nota do vault. Casa pelo TEXTO da tarefa, nunca pelo
               número da linha (a nota pode ter mudado no PC depois da cópia do celular). Uma
               linha só: marca. Nenhuma ou várias: não mexe e conta por quê. Só notas que vão
               ao celular (`vault_celular._permitida`). Terceira exceção ao congelamento do vault.
    analisar   lê a conversa no Assistente (API local, só GET) e pede ao Ollama uma leitura da
               situação e caminhos possíveis; a análise volta fechada para o celular
               (`/api/cifrados`, chave "analises"). O Assistente não muda.
    contatos   a lista "manter contato" (quem e a cada quantos dias). A última que chega vale; a
               fila do WhatsApp (`whatsapp_celular`) devolve a data da última conversa de cada um.

Responder a uma nota não passa por aqui: vai como anotação comum, com a nota nas marcas.
Teste: `python -m logos.acoes_celular --uma-vez`.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import threading
import time
import unicodedata
from pathlib import Path

import requests

from . import cofre, config, eventos, organiza

logger = logging.getLogger(__name__)

URL = organiza.URL.replace("/capturas", "/acoes")
URL_CIFRADOS = organiza.URL.replace("/capturas", "/cifrados")
INTERVALO_S = 15
MAX_ANALISES = 15
MAX_MSGS_ANALISE = 60
MAX_CONTATOS = 200
ID_ANALISES = "analises"  # dados associados do envelope

_TAREFA = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)\[ \](\s.*)?$")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFC", s or "")
    return re.sub(r"\s+", " ", s).strip()


def _pedido(metodo: str, url: str, key: str, corpo: dict | None = None) -> dict:
    return organiza._pedido(metodo, url, key, corpo)


# ---- concluir ----

def concluir(raiz: Path, caminho: str, texto: str) -> tuple[bool, str]:
    """Marca a tarefa cujo texto é `texto` na nota `caminho` (relativo ao vault). (ok, frase)."""
    from . import vault_celular
    f = raiz / caminho
    if not caminho or ".." in Path(caminho).parts or not vault_celular._permitida(raiz, f):
        return False, "Essa nota não está entre as que vão ao celular."
    alvo = _norm(texto)
    if not alvo:
        return False, "Tarefa sem texto."
    dados = f.read_bytes()
    try:
        conteudo = dados.decode("utf-8")
    except UnicodeDecodeError:
        return False, "A nota não está em UTF-8; não mexi."
    linhas = conteudo.splitlines(keepends=True)
    achadas = []
    for i, linha in enumerate(linhas):
        corpo = linha.rstrip("\r\n")
        m = _TAREFA.match(corpo)
        if m and _norm(m.group(2) or "") == alvo:
            achadas.append(i)
    if not achadas:
        return False, "Não achei essa tarefa aberta na nota do PC (já marcada, mudada ou apagada)."
    if len(achadas) > 1:
        return False, f"Há {len(achadas)} tarefas abertas com esse texto na nota; não sei qual marcar."
    i = achadas[0]
    corpo = linhas[i].rstrip("\r\n")
    fim = linhas[i][len(corpo):]
    m = _TAREFA.match(corpo)
    linhas[i] = m.group(1) + "[x]" + (m.group(2) or "") + fim
    tmp = f.with_name(f.name + ".organiza.tmp")
    tmp.write_bytes("".join(linhas).encode("utf-8"))
    tmp.replace(f)
    return True, "Marcada como concluída no PC."


# ---- analisar ----

_PROMPT = """Você ajuda o André a decidir como lidar com uma conversa de WhatsApp. Ele pediu esta
análise pelo celular; nem toda conversa é alguém esperando resposta: às vezes é uma situação que ele
quer entender antes de agir. Leia a conversa e responda APENAS com um objeto JSON, em português do
Brasil, com estes campos:

- "situacao": 2 a 4 frases com o que está acontecendo, sem julgar ninguém.
- "o_que_a_pessoa_quer": 1 frase (ou "" se não der para saber).
- "atencao": lista de até 3 pontos a observar (tom, prazos, algo não respondido).
- "caminhos": lista de 2 ou 3 objetos {{"rotulo": 2 a 4 palavras, "como": 1 frase, "rascunho": a
  mensagem pronta, curta, no jeito de falar dele}}.
- "sugestao": 1 frase dizendo qual caminho você escolheria e por quê.

Não invente fatos que não estão na conversa. Mensagens "Eu:" são dele.
{pergunta}
Conversa com {nome}:
{conversa}
"""


def _texto_conversa(mensagens: list[dict], nome: str) -> str:
    linhas = []
    for m in mensagens[-MAX_MSGS_ANALISE:]:
        quando = time.strftime("%d/%m %H:%M", time.localtime(m.get("ts") or 0))
        quem = "Eu" if m.get("de_mim") else (m.get("autor") or nome or "Pessoa")
        tipo = m.get("tipo") or "texto"
        t = (m.get("transcricao") or m.get("texto") or "") if tipo == "audio" else (m.get("texto") or "")
        if not t:
            t = f"[{tipo}]"
        elif tipo == "audio":
            t = "[áudio] " + t
        linhas.append(f"[{quando}] {quem}: {t[:1500]}")
    return "\n".join(linhas)


def _ollama_json(prompt: str) -> dict:
    payload = {"model": config.LLM_MODEL, "prompt": prompt, "stream": False, "format": "json",
               "options": {"num_ctx": config.OLLAMA_NUM_CTX}, "keep_alive": config.OLLAMA_KEEP_ALIVE}
    r = requests.post(f"{config.OLLAMA_HOST}/api/generate", json=payload, timeout=600)
    r.raise_for_status()
    return json.loads(r.json()["response"])


def _limpa_analise(d: dict) -> dict:
    def s(x, n=600):
        return str(x or "").strip()[:n]
    caminhos = []
    for c in (d.get("caminhos") or [])[:3]:
        if isinstance(c, dict):
            caminhos.append({"rotulo": s(c.get("rotulo"), 60), "como": s(c.get("como"), 300),
                             "rascunho": s(c.get("rascunho"), 1000)})
    return {"situacao": s(d.get("situacao"), 1200), "o_que_a_pessoa_quer": s(d.get("o_que_a_pessoa_quer"), 300),
            "atencao": [s(x, 300) for x in (d.get("atencao") or [])[:3] if s(x)],
            "caminhos": caminhos, "sugestao": s(d.get("sugestao"), 400)}


def analisar(jid: str, nome: str, pergunta: str = "", gerar=_ollama_json) -> dict:
    from . import whatsapp_celular
    conversa = whatsapp_celular._conversa(jid)
    msgs = conversa.get("mensagens") or []
    if not msgs:
        raise ValueError("conversa vazia no Assistente")
    p = f"O que ele quer decidir: {pergunta.strip()}\n" if pergunta.strip() else ""
    prompt = _PROMPT.format(pergunta=p, nome=nome or "a pessoa", conversa=_texto_conversa(msgs, nome))
    return _limpa_analise(gerar(prompt))


def _analises_path() -> Path:
    return config.STATE_DIR / "organiza" / "analises.json"


def _ler_json(p: Path, padrao):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return padrao


def _gravar_json(p: Path, dados) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def guardar_analise(item: dict) -> list[dict]:
    lista = [a for a in _ler_json(_analises_path(), []) if a.get("id") != item.get("id")]
    lista.insert(0, item)
    lista = lista[:MAX_ANALISES]
    _gravar_json(_analises_path(), lista)
    return lista


def publicar_analises(key: str, lista: list[dict]) -> bool:
    pub = organiza.chave_celular(key)
    if not pub:
        return False
    doc = {"analises": lista, "gerado_em": int(time.time())}
    env = cofre.selar_para_celular(json.dumps(doc, ensure_ascii=False).encode("utf-8"), ID_ANALISES, pub)
    _pedido("PUT", URL_CIFRADOS, key, {"chave": "analises", "env": json.dumps(env)})
    return True


# ---- contatos ----

def contatos_path() -> Path:
    return config.STATE_DIR / "organiza" / "contatos.json"


def guardar_contatos(lista) -> int:
    limpa = []
    for c in (lista or [])[:MAX_CONTATOS]:
        if not isinstance(c, dict) or not isinstance(c.get("jid"), str) or not c["jid"]:
            continue
        dias = c.get("dias")
        if not isinstance(dias, int) or isinstance(dias, bool) or not 1 <= dias <= 366:
            continue
        limpa.append({"jid": c["jid"][:100], "nome": str(c.get("nome") or "")[:100], "dias": dias})
    _gravar_json(contatos_path(), limpa)
    return len(limpa)


def contatos() -> list[dict]:
    return _ler_json(contatos_path(), [])


# ---- o laço ----

def _executar(key: str, a: dict, chave, raiz: Path) -> tuple[str, str]:
    dados = json.loads(cofre.abrir(a.get("env") or "", a["id"], chave).decode("utf-8"))
    tipo = a.get("tipo")
    if tipo == "concluir":
        ok, frase = concluir(raiz, str(dados.get("caminho") or ""), str(dados.get("texto") or ""))
        titulo = Path(str(dados.get("caminho") or "")).stem
        eventos.registrar("organiza", f"Tarefa marcada pelo celular em {titulo}." if ok
                          else f"Tarefa pedida pelo celular não foi marcada em {titulo}: {frase}", "info" if ok else "atencao")
        if ok:
            pedir_ida()
        return ("ok" if ok else "falha"), frase
    if tipo == "analisar":
        jid, nome = str(dados.get("jid") or ""), str(dados.get("nome") or "")
        item = {"id": a["id"], "jid": jid, "nome": nome, "pergunta": str(dados.get("pergunta") or "")[:300],
                "quando": int(time.time())}
        item.update(analisar(jid, nome, item["pergunta"]))
        publicar_analises(key, guardar_analise(item))
        eventos.registrar("organiza", f"Análise de conversa pronta ({nome or 'WhatsApp'}).")
        return "ok", "Análise pronta."
    if tipo == "contatos":
        n = guardar_contatos(dados.get("lista"))
        return "ok", f"{n} pessoa(s) para manter contato."
    return "falha", "Ação desconhecida."


_IDA = threading.Event()


def pedir_ida() -> None:
    """A nota mudou no PC: o drenador manda o vault ao celular na próxima volta, sem esperar 10 min."""
    _IDA.set()


def ida_pedida() -> bool:
    if _IDA.is_set():
        _IDA.clear()
        return True
    return False


def passo(key: str, raiz: Path | None = None) -> int:
    novas = _pedido("GET", URL + "?novas=1", key).get("acoes") or []
    if not novas:
        return 0
    from . import consulta
    raiz = raiz or consulta.vault_root()
    chave = cofre.carregar_ou_criar()
    feitas = 0
    # Lista de contatos: só a última conta; as anteriores saem como ok sem rodar.
    ultima_lista = max((a for a in novas if a.get("tipo") == "contatos"), key=lambda a: a.get("criada") or "", default=None)
    for a in novas:
        try:
            if a.get("tipo") == "contatos" and a is not ultima_lista:
                estado, frase = "ok", "Substituída por uma lista mais nova."
            else:
                estado, frase = _executar(key, a, chave, raiz)
        except cofre.CofreErro as e:
            estado, frase = "falha", f"Não abriu no PC ({e})."
        except Exception as e:  # Ollama ou Assistente fora: conta e segue com as outras
            logger.warning(f"Ação do celular {a.get('id')} ({a.get('tipo')}) falhou: {e}")
            estado, frase = "falha", f"Não deu agora: {type(e).__name__}."
        try:
            _pedido("PUT", URL, key, {"id": a["id"], "estado": estado, "resposta": frase[:500]})
        except Exception as e:
            logger.warning(f"Semana não ouviu o estado da ação {a.get('id')}: {e}")
        feitas += 1
    return feitas


def _laco(key: str) -> None:
    while True:
        try:
            n = passo(key)
            if n:
                logger.info(f"Ações do celular: {n} feita(s)")
        except Exception as e:
            logger.warning(f"Ações do celular: volta falhou: {e}")
        time.sleep(INTERVALO_S)


def iniciar(key: str) -> threading.Thread:
    t = threading.Thread(target=_laco, args=(key,), name="acoes-celular", daemon=True)
    t.start()
    return t


def main() -> None:
    ap = argparse.ArgumentParser(description="Executa as ações pedidas pelo app Organiza.")
    ap.add_argument("--uma-vez", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    key = organiza.chave()
    if not key:
        raise SystemExit("Falta a chave da Semana.")
    if args.uma_vez:
        print(passo(key))
    else:
        _laco(key)


if __name__ == "__main__":
    main()
