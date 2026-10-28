"""A caixa da captura (Fase B, sessão 2; spec-captura §2, decisão D1a de 28/10/2026).

Uma anotação do app Organiza chega ao PC, o Whisper transcreve e, antes do enriquecimento, este
módulo decide para onde ela vai e o que o PC entendeu. A decisão fica em
`state/caixa/<captura_id>.json`; o drenador (`organiza.passo`) lê, aplica o efeito uma vez e manda
`destino` e `entendi` à Semana. O contrato com os apps está no `CONTRATO-CAIXA.md` da rodada de 28/10.

    nota           só a nota na Inbox (que existe sempre, com a transcrição literal)
    plano          uma fala do dia em /api/dia: o PC propõe e ele aceita no Planeja
    fim_de_semana  um complemento para a preparação da semana (/api/complementos)
    recado         um recado sobre a página e os apps (/api/recados)

Quem classifica: o Claude sem tela, pelo id completo (`caixa_modelo`, de fábrica `claude-opus-5-5`;
alias curto como "opus" o CLI ignora e roda outro modelo). Se ele falhar ou passar do tempo, o
Ollama local (`gemma4:e4b`, sem raciocínio). Se os dois falharem, Nota.

Nunca vem para cá: a anotação privada (o cadeado vira trecho do Diário, cifrado), a rota fixada
(`rota: diario`), a fala cuja 1ª palavra é "Diário" e o que não vem do Organiza. Quem barra é
`elegivel`, e `decidir` confere de novo antes de chamar qualquer modelo.

Na dúvida, Nota: confiança baixa, classificador fora ou fala velha (um "hoje" que já passou).
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from . import config
from .dia import dia_logico
from .triage import Route

logger = logging.getLogger(__name__)

DESTINOS = ("nota", "plano", "fim_de_semana", "recado")
ROTULOS = {"nota": "Nota", "plano": "Plano", "fim_de_semana": "Fim de semana", "recado": "Recado"}
CONFIANCA_MIN = 0.6  # sugestão: abaixo disso, Nota
MAX_ENTENDI = 140  # o limite da Semana (`lib/caixa.mjs`)
JANELA_LINHA_S = 12 * 3600  # a linha `caixa` conta as decisões das últimas 12 h
MAX_NOMES = 120
MAX_CORRECOES = 5
NOMES_DIAS = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")

FRASE_FALHOU = "Ficou como nota. Não consegui entender agora."
FRASE_VELHA = "Ficou como nota: isto era para ontem."

SCHEMA = {
    "type": "object",
    "properties": {
        "destino": {"type": "string", "enum": list(DESTINOS)},
        "confianca": {"type": "number", "description": "0 a 1"},
        "entendi": {"type": "string", "description": "uma frase, em segunda pessoa, até 90 caracteres"},
        "texto_limpo": {"type": "string", "description": "a fala sem muletas, com os nomes corrigidos"},
        "quando": {"type": "string", "description": "hoje, amanha, AAAA-MM-DD ou vazio"},
        "titulo": {"type": "string", "description": "título curto da nota, até 8 palavras"},
    },
    "required": ["destino", "confianca", "entendi", "texto_limpo", "quando", "titulo"],
}

SISTEMA = """Você é a caixa de entrada do André. Ele falou ou escreveu uma anotação rápida no celular;
você decide para onde ela vai e diz em uma frase o que entendeu. Responda só o JSON pedido.

Destinos:
- "plano": algo para fazer hoje ou amanhã que muda o dia dele (encaixar, mover, tirar, "preciso
  fazer X hoje à tarde", "amanhã cedo ligo para Fulano", "o dia de hoje mudou"). Ele aceita a
  mudança depois, no app Planeja.
- "fim_de_semana": algo para a preparação da semana que vem, que ele faz no fim de semana ("para a
  semana que vem", "quando eu planejar a semana", uma ideia de projeto para encaixar depois).
- "recado": um comentário ou pedido sobre os próprios apps ou a página da Semana (Planeja,
  Organiza, launcher, a página): "o botão tal está ruim", "a página devia mostrar...".
- "nota": todo o resto. Ideia, registro, reflexão, lembrete sem dia, tarefa sem data, dúvida.
Na dúvida, "nota" com confiança baixa. "Talvez", "acho que", "um dia" puxam para "nota".

Campos:
- "confianca": de 0 a 1, o quanto você tem certeza do destino.
- "entendi": uma frase em segunda pessoa, até 90 caracteres, sem emoji ("Ligar para o Osmar amanhã de manhã.").
- "texto_limpo": a fala sem as muletas ("né", "tipo", "é...") e com os nomes da lista corrigidos.
  Não invente nada que ele não disse.
- "quando": "hoje", "amanha", uma data AAAA-MM-DD ou "" se ele não disse quando. Conte a partir da
  hora em que ele falou, não de agora.
- "titulo": até 8 palavras.
"""


# ---------------------------------------------------------------------------------------------
# Onde mora a decisão
# ---------------------------------------------------------------------------------------------

def pasta() -> Path:
    return config.STATE_DIR / "caixa"


def _seguro(cid: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "", str(cid or ""))[:64]


def caminho(cid: str) -> Path:
    return pasta() / f"{_seguro(cid)}.json"


def ler(cid: str) -> dict | None:
    if not _seguro(cid):
        return None
    try:
        d = json.loads(caminho(cid).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def gravar(dec: dict) -> None:
    p = caminho(dec["captura_id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(dec, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def todas() -> list[dict]:
    saida = []
    for p in sorted(pasta().glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("captura_id"):
            saida.append(d)
    return saida


# ---------------------------------------------------------------------------------------------
# Quem pode passar pelo classificador
# ---------------------------------------------------------------------------------------------

_MARCAS = re.compile(r"^\s*(?:\[[^\]]*\]\s*)+")  # "[voz 0:12] ", "[foto 1: …] "
_PALAVRA_DO_TIPO = re.compile(r"^\s*(?:ideia|tarefa|reuni[aã]o)\s*:\s*", re.IGNORECASE)


def _primeira_palavra(texto: str) -> str:
    """A 1ª palavra falada, sem as marcas do pacote e sem a palavra do tipo que o drenador põe na
    frente ("Ideia: …"): "Ideia: Diário, hoje…" também não passa pelo classificador."""
    from .triage import _normalize_word
    t = _PALAVRA_DO_TIPO.sub("", _MARCAS.sub("", texto or ""), count=1)
    for w in t.split():
        n = _normalize_word(w)
        if n:
            return n
    return ""


def elegivel(manifest: dict, route: Route | None = None, texto: str = "") -> bool:
    """Só anotação do Organiza, sem rota fixada e fora do Diário. A rota Planejamento (1ª palavra)
    passa, com o destino fixado em plano (`forcar_de`)."""
    if not config.CAIXA_LIGADA or not isinstance(manifest, dict):
        return False
    if manifest.get("origem") != "organiza" or not manifest.get("captura_id"):
        return False
    if manifest.get("rota"):  # "diario" (o cadeado e o Diário da noite) ou qualquer rota fixada
        return False
    if route is not None and route not in (Route.INBOX, Route.PLANEJAMENTO):
        return False
    if _primeira_palavra(texto) in ("diario", "diarios"):
        return False
    return True


def forcar_de(route: Route | None) -> str | None:
    """A rota de voz "planejamento" cai no destino Plano (sugestão do EXECUCAO de 28/10)."""
    return "plano" if route == Route.PLANEJAMENTO else None


# ---------------------------------------------------------------------------------------------
# O prompt
# ---------------------------------------------------------------------------------------------

def nomes_do_vault(raiz: Path | None = None) -> list[str]:
    """Pessoas e projetos que o texto pode citar: os nomes das notas de org/people e work/active."""
    raiz = raiz if raiz is not None else config.VAULT_DIR
    if raiz is None:
        return []
    nomes: list[str] = []
    for sub in ("org/people", "work/active"):
        base = Path(raiz) / sub
        try:
            for p in sorted(base.rglob("*.md")):
                if p.stem.lower() not in ("readme", "index") and " — " not in p.stem:
                    nomes.append(p.stem)
        except OSError:
            continue
    return list(dict.fromkeys(nomes))[:MAX_NOMES]


def _correcoes_path() -> Path:
    return pasta() / "correcoes.jsonl"


def ultimas_correcoes(n: int = MAX_CORRECOES) -> list[dict]:
    try:
        linhas = _correcoes_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    saida = []
    for linha in linhas[-n:]:
        try:
            d = json.loads(linha)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("para") in DESTINOS:
            saida.append(d)
    return saida


def _quando_em_texto(m: datetime) -> str:
    return f"{NOMES_DIAS[m.weekday()]} {m:%Y-%m-%d %H:%M}"


def montar_prompt(texto: str, falado_em: datetime, agora: datetime, nomes: list[str] | None = None,
                  correcoes: list[dict] | None = None, forcar: str | None = None) -> str:
    partes = [
        f"Ele falou: {_quando_em_texto(falado_em)} (horário de Brasília).",
        f"Agora: {_quando_em_texto(agora)}. O dia dele vira às {config.VIRADA_DO_DIA_H}h.",
        "",
    ]
    if nomes:
        partes += ["# Nomes que ele usa (pessoas e projetos)", ", ".join(nomes), ""]
    if correcoes:
        partes.append("# Correções recentes dele (o PC achou X, era Y)")
        for c in correcoes:
            partes.append(f"- \"{str(c.get('texto') or '')[:160]}\": {c.get('de')} → {c.get('para')}")
        partes.append("")
    if forcar:
        partes += [f"Ele começou dizendo que é planejamento: o destino é \"{forcar}\".", ""]
    partes += ["# A anotação", texto.strip()]
    return "\n".join(partes)


# ---------------------------------------------------------------------------------------------
# Os modelos
# ---------------------------------------------------------------------------------------------

def chamar_claude(prompt: str, modelo: str | None = None, limite: int | None = None) -> tuple[dict, str]:
    """O Claude sem tela, do jeito do `dia_celular` (sem ferramentas, sem as configurações dele).
    Devolve (o JSON, o modelo que de fato respondeu, lido de `modelUsage`)."""
    from . import dia_celular
    modelo = modelo or config.CAIXA_MODELO
    saida = dia_celular.rodar_claude(prompt, SISTEMA, SCHEMA, modelo=modelo,
                                     limite=limite or config.CAIXA_LIMITE_CLAUDE_S,
                                     pasta=pasta() / "claude", inteiro=True)
    usados = list((saida.get("modelUsage") or {}).keys())
    return saida["structured_output"], (",".join(usados) or f"{modelo} (pedido)")


def chamar_ollama(prompt: str, modelo: str | None = None) -> tuple[dict, str]:
    """A reserva local: `format` com o esquema e `think: false` (21–32 s quente, minutos frio)."""
    modelo = modelo or config.CAIXA_RESERVA
    payload = {"model": modelo, "system": SISTEMA, "prompt": prompt, "stream": False, "think": False,
               "format": SCHEMA, "options": {"num_ctx": config.OLLAMA_NUM_CTX},
               "keep_alive": config.OLLAMA_KEEP_ALIVE}
    r = requests.post(f"{config.OLLAMA_HOST}/api/generate", json=payload, timeout=config.CAIXA_LIMITE_OLLAMA_S)
    r.raise_for_status()
    return json.loads(r.json()["response"]), modelo


# ---------------------------------------------------------------------------------------------
# A decisão
# ---------------------------------------------------------------------------------------------

def _alvo(quando: str, falado_em: datetime) -> date | None:
    q = (quando or "").strip().lower()
    base = dia_logico(falado_em)
    if q in ("hoje", ""):
        return base
    if q in ("amanha", "amanhã"):
        return base + timedelta(days=1)
    try:
        return date.fromisoformat(q[:10])
    except ValueError:
        return base


def _frase(s, n: int) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def normalizar(bruto: dict, falado_em: datetime, agora: datetime, forcar: str | None = None) -> dict:
    """As regras do PC sobre a resposta do modelo: lista fechada, confiança, fala velha."""
    destino = bruto.get("destino") if bruto.get("destino") in DESTINOS else "nota"
    try:
        confianca = max(0.0, min(1.0, float(bruto.get("confianca"))))
    except (TypeError, ValueError):
        confianca = 0.0
    if forcar in DESTINOS:
        destino, confianca = forcar, max(confianca, 1.0)
    entendi = _frase(bruto.get("entendi"), MAX_ENTENDI)
    quando = str(bruto.get("quando") or "").strip()[:10]
    motivo = ""
    if destino != "nota" and confianca < CONFIANCA_MIN:
        destino, motivo = "nota", "confiança baixa"
    alvo = None
    if destino == "plano":
        alvo = _alvo(quando, falado_em)
        if alvo < dia_logico(agora):
            destino, entendi, motivo = "nota", FRASE_VELHA, "fala velha"
    return {
        "destino": destino, "confianca": round(confianca, 2), "entendi": entendi or FRASE_FALHOU,
        "texto_limpo": _frase(bruto.get("texto_limpo"), 2000), "quando": quando,
        "alvo": alvo.isoformat() if alvo and destino == "plano" else "",
        "titulo": _frase(bruto.get("titulo"), 90), "motivo": motivo,
    }


def decidir(manifest: dict, texto: str, falado_em: datetime, route: Route | None = None,
            agora: datetime | None = None, claude=None, ollama=None, pacote: str = "") -> dict | None:
    """Classifica uma anotação e grava a decisão. Devolve None quando ela não pode passar por aqui
    (Diário, rota fixada, outra origem, caixa desligada). Uma decisão já gravada vale de novo: o mesmo
    pacote reprocessado não chama o Claude outra vez."""
    if not elegivel(manifest, route, texto) or not (texto or "").strip():
        return None
    cid = str(manifest["captura_id"])
    ja = ler(cid)
    if ja is not None:
        return ja
    agora = agora or datetime.now()
    forcar = forcar_de(route)
    prompt = montar_prompt(texto, falado_em, agora, nomes_do_vault(), ultimas_correcoes(), forcar)
    claude = claude or chamar_claude
    ollama = ollama or chamar_ollama
    bruto, modelo, t0 = None, "", time.monotonic()
    curto = cid[:8]
    try:
        bruto, modelo = claude(prompt)
    except Exception as e:
        logger.warning(f"Caixa {curto}: o Claude não respondeu ({type(e).__name__}: {str(e)[:160]}); vai o Ollama")
        t0 = time.monotonic()
        try:
            bruto, modelo = ollama(prompt)
        except Exception as e2:
            logger.warning(f"Caixa {curto}: o Ollama também não ({type(e2).__name__}: {str(e2)[:160]}); fica Nota")
    segundos = round(time.monotonic() - t0, 1)
    if isinstance(bruto, dict):
        dec = normalizar(bruto, falado_em, agora, forcar)
    else:
        dec = {"destino": "nota", "confianca": 0.0, "entendi": FRASE_FALHOU, "texto_limpo": "", "quando": "",
               "alvo": "", "titulo": "", "motivo": "classificador fora"}
        modelo = "nenhum"
    dec.update({
        "captura_id": cid, "pacote": pacote, "falado_em": falado_em.isoformat(timespec="seconds"),
        "decidido_em": int(time.time()), "modelo": modelo, "segundos": segundos,
        "efeito": None, "enviado": None, "correcoes": [],
    })
    gravar(dec)
    extra = f", {dec['motivo']}" if dec["motivo"] else ""
    logger.info(f"Caixa {curto}: {dec['destino']} ({str(dec['confianca']).replace('.', ',')}) "
                f"por {modelo} em {str(segundos).replace('.', ',')} s{extra}")
    return dec


# ---------------------------------------------------------------------------------------------
# Os efeitos (no drenador, que tem a chave da Semana)
# ---------------------------------------------------------------------------------------------

def _base() -> str:
    from . import organiza
    return organiza.URL.rsplit("/api/", 1)[0]


def _id_plano(dec: dict) -> str:
    n = len(dec.get("correcoes") or [])
    return f"caixa-{dec['captura_id'][:48]}" + (f"-{n}" if n else "")


def _texto_efeito(dec: dict) -> str:
    return (dec.get("texto_limpo") or dec.get("entendi") or "").strip()[:1900]


def _texto_plano(dec: dict) -> str:
    t = _texto_efeito(dec)
    if dec.get("alvo"):
        d = date.fromisoformat(dec["alvo"])
        return f"Para {NOMES_DIAS[d.weekday()]}, {d:%d/%m}: {t}"
    return t


def _contexto_recado(dec: dict) -> str:
    return f"da caixa · {dec['captura_id'][:8]}"


def aplicar(key: str, dec: dict, pedido, agora: datetime | None = None) -> dict:
    """Aplica o efeito do destino uma vez. Grava `efeito` na decisão logo depois de a Semana aceitar.
    Idempotente: o plano e o complemento repetem o mesmo id; o recado é procurado antes de postar."""
    if dec.get("efeito"):
        return dec
    destino = dec.get("destino")
    base = _base()
    agora = agora or datetime.now()
    efeito = {"tipo": destino, "em": int(time.time())}
    if destino == "plano":
        fid = _id_plano(dec)
        pedido("PUT", base + "/api/dia", key, {
            "id": fid, "tipo": "dia", "dia": dia_logico(agora).isoformat(), "texto": _texto_plano(dec),
            "contexto": {"origem": "caixa", "captura_id": dec["captura_id"], "falado_em": dec.get("falado_em")},
        })
        efeito["ref"] = fid
    elif destino == "fim_de_semana":
        pedido("PUT", base + "/api/complementos", key, {"id": dec["captura_id"], "texto": _texto_efeito(dec)})
        efeito["ref"] = dec["captura_id"]
    elif destino == "recado":
        ctx = _contexto_recado(dec)
        ja = [r for r in pedido("GET", base + "/api/recados", key).get("recados") or [] if r.get("contexto") == ctx]
        if ja:
            efeito["ref"] = ja[0].get("id")
        else:
            efeito["ref"] = pedido("POST", base + "/api/recados", key,
                                   {"texto": _texto_efeito(dec), "contexto": ctx}).get("id")
    dec["efeito"] = efeito
    gravar(dec)
    return dec


def desfazer(key: str, dec: dict, pedido) -> str:
    """Tira o efeito anterior. Devolve uma frase quando não deu para tirar tudo (o recado)."""
    ef = dec.get("efeito") or {}
    base = _base()
    frase = ""
    if ef.get("tipo") == "plano" and ef.get("ref"):
        for acao in ("recusar", "desfazer"):
            try:
                pedido("PUT", base + "/api/dia", key, {"id": ef["ref"], "acao": acao})
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    break
                if e.code != 409:
                    raise
        else:
            frase = "A proposta do plano ainda estava sendo pensada; se aparecer no Planeja, toque em Agora não."
    elif ef.get("tipo") == "fim_de_semana":
        pedido("PUT", base + "/api/complementos", key, {"id": ef.get("ref") or dec["captura_id"], "apagar": True})
    elif ef.get("tipo") == "recado":
        frase = "O recado já tinha ido; continua na lista."
    dec["efeito"] = None
    return frase


def enviar_estado(key: str, dec: dict, pedido) -> None:
    from . import organiza
    pedido("PUT", organiza.URL + "/estado", key,
           {"id": dec["captura_id"], "estado": "pc", "destino": dec["destino"],
            "entendi": _frase(dec.get("entendi"), MAX_ENTENDI)})
    dec["enviado"] = int(time.time())
    gravar(dec)


def corrigir(key: str, cid: str, novo: str, pedido, nota: Path | None = None) -> tuple[bool, str]:
    """A correção em 1 toque (`/api/acoes`, tipo `caixa_mudar`): desfaz o efeito anterior, aplica o
    novo, troca o `destino:` da nota e guarda o par para os próximos prompts."""
    if novo not in DESTINOS:
        return False, "Esse destino não existe na caixa."
    dec = ler(cid)
    if dec is None:
        return False, "Essa anotação não passou pela caixa."
    antes = dec.get("destino")
    if antes == novo:
        return True, f"Já estava em {ROTULOS[novo]}."
    frase = desfazer(key, dec, pedido)
    dec["correcoes"] = list(dec.get("correcoes") or []) + [{"de": antes, "para": novo, "em": int(time.time())}]
    dec["destino"], dec["motivo"] = novo, "corrigido por ele"
    gravar(dec)
    aplicar(key, dec, pedido)
    enviar_estado(key, dec, pedido)
    if nota is not None:
        try:
            trocar_destino_na_nota(nota, antes, novo)
        except OSError as e:
            logger.warning(f"Caixa {cid[:8]}: a nota não mudou ({e})")
    try:
        pasta().mkdir(parents=True, exist_ok=True)
        with open(_correcoes_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps({"texto": (dec.get("texto_limpo") or "")[:200], "de": antes, "para": novo,
                                "em": int(time.time())}, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning(f"Caixa: correção não guardada para o prompt ({e})")
    logger.info(f"Caixa {cid[:8]}: corrigida de {antes} para {novo}")
    return True, " ".join(filter(None, [f"Mudei para {ROTULOS[novo]}.", frase]))


def trocar_destino_na_nota(nota: Path, antes: str, novo: str) -> None:
    """Só o frontmatter: `destino:` novo e uma linha em `correcoes:`. A transcrição não muda."""
    texto = nota.read_text(encoding="utf-8")
    linhas = texto.split("\n")
    if not linhas or linhas[0].strip() != "---":
        return
    try:
        fim = next(i for i in range(1, len(linhas)) if linhas[i].strip() == "---")
    except StopIteration:
        return
    fm = linhas[1:fim]
    item = f"  - {antes} → {novo} ({datetime.now():%Y-%m-%d %H:%M})"
    i_dest = next((i for i, l in enumerate(fm) if l.startswith("destino:")), None)
    if i_dest is None:
        fm.append(f"destino: {novo}")
    else:
        fm[i_dest] = f"destino: {novo}"
    i_corr = next((i for i, l in enumerate(fm) if l.rstrip() == "correcoes:"), None)
    if i_corr is None:
        fm += ["correcoes:", item]
    else:
        j = i_corr + 1
        while j < len(fm) and fm[j].startswith("  - "):
            j += 1
        fm.insert(j, item)
    tmp = nota.with_name(nota.name + ".caixa.tmp")
    tmp.write_text("\n".join([linhas[0], *fm, *linhas[fim:]]), encoding="utf-8")
    tmp.replace(nota)


# ---------------------------------------------------------------------------------------------
# A linha `caixa` (para o launcher): só números
# ---------------------------------------------------------------------------------------------

def contagens(esperando: int, agora: float | None = None) -> dict:
    agora = agora or time.time()
    por: dict[str, int] = {}
    for d in todas():
        if agora - float(d.get("decidido_em") or 0) <= JANELA_LINHA_S and d.get("destino") in DESTINOS:
            por[d["destino"]] = por.get(d["destino"], 0) + 1
    return {"entendeu": sum(por.values()), "esperando": max(0, int(esperando)), "por_destino": por}


def _linha_path() -> Path:
    return pasta() / "linha.json"


def publicar_linha(key: str, esperando: int, pedido, agora: float | None = None) -> bool:
    """PUT só quando os números mudam (o `atualizado` diz quando foi a última mudança)."""
    dados = contagens(esperando, agora)
    try:
        antes = json.loads(_linha_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        antes = {"entendeu": 0, "esperando": 0, "por_destino": {}}
    if antes == dados:
        return False
    from . import organiza
    pedido("PUT", organiza.URL.replace("/capturas", "/linhas"), key, {"chave": "caixa", "dados": dados})
    _linha_path().parent.mkdir(parents=True, exist_ok=True)
    _linha_path().write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
    return True
