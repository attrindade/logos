"""O sono do relógio cruzado com o resto do dia: a mensagem da manhã e dois relatórios.

- **Mensagem no Telegram**: quando chega um banco novo do Gadgetbridge com a noite de hoje já
  fechada, o bot manda uma vez a noite comparada com as anteriores e, depois de noite curta,
  lembra do /planejamento. O watcher chama `verificar` a cada volta; ela só trabalha quando o
  arquivo do banco muda.
- **`atividade/Sono e PC.md`**: até que horas o PC ficou em uso (ActivityWatch, só o PC: o celular
  ficou fora por decisão) contra a hora de dormir e o tanto dormido.
- **`capturas/Diario/Sono e Diário.md`**: a noite contra o humor e a energia marcados no /diario.

Fronteira do Diário: o que sai dele (humor, energia, o sono que a pessoa marcou) só é lido em
`relatorio_diario` e só é escrito dentro de `capturas/Diario/`. A mensagem e o relatório do PC
não recebem nada do Diário, e a CLI imprime só o caminho desse relatório.

Quando a noite está fechada: pela regra de `relogio.classificar`, depois de acordar às 6h ou mais
tarde só um trecho a menos de EMENDA_MIN ainda emenda na noite, então basta o banco cobrir uma
hora depois do fim. Quem acordou antes das 6h, ou dormiu pouco (o trecho seguinte pode ser maior e
virar a noite), espera o banco cobrir o meio-dia. Passos não servem de sinal: em 12/10 houve 2.285
passos no meio da noite e nenhum na primeira hora depois de acordar.
"""
from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Callable

import requests
import yaml

from . import config, relogio
from . import dia as diario
from .triage import Route

logger = logging.getLogger(__name__)

NOTA_PC = "Sono e PC.md"
NOTA_DIARIO = "Sono e Diário.md"

# Noite curta: abaixo disto, ou uma hora abaixo da média, a mensagem lembra do /planejamento.
NOITE_CURTA_MIN = 6 * 60
ABAIXO_DA_MEDIA_MIN = 60
MEDIA_NOITES = 7
MIN_PARA_COMPARAR = 3
# Diferença menor que isto na hora de dormir não vale uma linha.
HORARIO_RELEVANTE_MIN = 30
# Depois disto a mensagem da manhã já não serve; o dia seguinte tem a dele.
ENVIA_ATE_H = 18
FOLGA_APOS_ACORDAR = timedelta(hours=1)
# Noite mais curta que isto espera o meio-dia: um trecho seguinte maior viraria a noite.
CONFIRMA_SE_MENOS_DE_MIN = 4 * 60
# Grupo com menos noites que isto não tira conclusão nos relatórios.
GRUPO_MIN = 5
# Janela dos relatórios. Mais que isso pesa no ActivityWatch a cada banco novo (uma consulta por vez).
HISTORICO_DIAS = 120
CHECA_A_CADA_S = 60
AW_TIMEOUT_S = 5

_ultima_checagem = 0.0

Trecho = tuple[datetime, datetime]
UsoDoPc = Callable[[datetime, datetime], "list[Trecho] | None"]


def _estado_path() -> Path:
    return config.STATE_DIR / "sono.json"


def _ler_estado() -> dict:
    try:
        return json.loads(_estado_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _gravar_estado(estado: dict) -> None:
    caminho = _estado_path()
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_suffix(".tmp")
    tmp.write_text(json.dumps(estado, ensure_ascii=False), encoding="utf-8")
    tmp.replace(caminho)


def _meia_noite(d: date) -> datetime:
    return datetime(d.year, d.month, d.day).astimezone()


def _desde_18h(momento: datetime, d: date) -> float:
    """Minutos desde as 18h da véspera: assim 23:30 e 01:10 têm média com sentido."""
    return (momento - relogio.janela_do_sono(d)[0]).total_seconds() / 60


def _hora_de(minutos_desde_18h: float, d: date) -> datetime:
    return relogio.janela_do_sono(d)[0] + timedelta(minutes=minutos_desde_18h)


def _diferenca(minutos: float) -> str:
    return relogio._hm(round(abs(minutos)))


# ---------------------------------------------------------------------------------------------
# A noite de hoje
# ---------------------------------------------------------------------------------------------

def noite_fechada(dia: relogio.Dia, cobertura: datetime | None) -> bool:
    n = dia.noite
    if n is None or cobertura is None:
        return False
    if cobertura >= _meia_noite(dia.data) + timedelta(hours=relogio.NOITE_ATE_H):
        return True
    acordou_de_dia = n.fim >= _meia_noite(dia.data) + timedelta(hours=relogio.MADRUGADA_H[1])
    return acordou_de_dia and n.dormindo_min >= CONFIRMA_SE_MENOS_DE_MIN and cobertura >= n.fim + FOLGA_APOS_ACORDAR


def mensagem(dia: relogio.Dia, anteriores: list[relogio.Dia], pc_ate: datetime | None) -> str:
    """Texto da manhã. Recebe só dados do relógio e do PC: nada do Diário entra aqui."""
    n = dia.noite
    d = dia.data
    ontem = relogio.DIAS_SEMANA[(d - timedelta(days=1)).weekday()]
    linhas = [f"🌙 Noite de {ontem} para {relogio.DIAS_SEMANA[d.weekday()]}"]
    principal = f"{relogio._hm(n.dormindo_min)} dormindo, das {n.inicio:%H:%M} às {n.fim:%H:%M}"
    if n.interrupcoes:
        principal += f", com {relogio._interrupcoes(n)}"
    linhas.append(principal + ".")

    noites = [x for x in anteriores if x.noite][-MEDIA_NOITES:]
    media = None
    if len(noites) >= MIN_PARA_COMPARAR:
        media = mean(x.noite.dormindo_min for x in noites)
        dif = n.dormindo_min - media
        if abs(dif) < 15:
            comparacao = "na média"
        else:
            comparacao = f"{_diferenca(dif)} {'acima' if dif > 0 else 'abaixo'}"
        linhas.append(f"Média das {len(noites)} noites anteriores: {relogio._hm(round(media))} ({comparacao}).")
        deitou = mean(_desde_18h(x.noite.inicio, x.data) for x in noites)
        dif_h = _desde_18h(n.inicio, d) - deitou
        if abs(dif_h) >= HORARIO_RELEVANTE_MIN:
            quando = "mais tarde" if dif_h > 0 else "mais cedo"
            linhas.append(f"Adormeceu {_diferenca(dif_h)} {quando} que de costume ({_hora_de(deitou, d):%H:%M}).")
    else:
        linhas.append(f"Comparação com a média começa com {MIN_PARA_COMPARAR} noites anteriores (há {len(noites)}).")

    fcs = [x.fc_repouso for x in noites if x.fc_repouso]
    if dia.fc_repouso:
        fc = f"FC de repouso {dia.fc_repouso}"
        if len(fcs) >= MIN_PARA_COMPARAR:
            fc += f" (média {round(mean(fcs))})"
        linhas.append(fc + ".")

    if pc_ate:
        antes = (n.inicio - pc_ate).total_seconds() / 60
        linhas.append(f"PC: último uso às {pc_ate:%H:%M}, {relogio._hm(max(0, round(antes)))} antes de dormir.")

    curta = n.dormindo_min < NOITE_CURTA_MIN or (media is not None and n.dormindo_min <= media - ABAIXO_DA_MEDIA_MIN)
    if curta:
        linhas.append("Noite curta. Se ainda for planejar o dia, um plano mais leve pode caber melhor: /planejamento")
    return "\n".join(linhas)


# ---------------------------------------------------------------------------------------------
# PC (ActivityWatch)
# ---------------------------------------------------------------------------------------------

def uso_do_pc(ini: datetime, fim: datetime) -> list[Trecho] | None:
    """Trechos com o PC em uso (`not-afk`) entre ini e fim. None quando o ActivityWatch não responde."""
    url = f"http://{config.AW_HOST}:{config.AW_PORT}/api/0/buckets/aw-watcher-afk_{socket.gethostname()}/events"
    try:
        resp = requests.get(url, params={"start": ini.isoformat(), "end": fim.isoformat(), "limit": -1},
                            timeout=AW_TIMEOUT_S)
        resp.raise_for_status()
        eventos = resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.info(f"ActivityWatch indisponível, sono sem o uso do PC: {e}")
        return None
    trechos = []
    for e in eventos:
        if (e.get("data") or {}).get("status") != "not-afk":
            continue
        a = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).astimezone()
        b = a + timedelta(seconds=float(e.get("duration") or 0))
        a, b = max(a, ini), min(b, fim)
        if b > a:
            trechos.append((a, b))
    return sorted(trechos)


@dataclass
class PcDaNoite:
    ultimo: datetime | None  # fim do último uso antes de dormir; None = PC sem uso desde as 18h
    depois_da_meia_noite_min: int


def pc_da_noite(dia: relogio.Dia, trechos: list[Trecho]) -> PcDaNoite | None:
    """O uso do PC entre as 18h da véspera e a hora em que dormiu."""
    if dia.noite is None:
        return None
    ini, dormiu = relogio.janela_do_sono(dia.data)[0], dia.noite.inicio
    meia_noite = _meia_noite(dia.data)
    ultimo, depois = None, 0.0
    for a, b in trechos:
        a, b = max(a, ini), min(b, dormiu)
        if b <= a:
            continue
        ultimo = b if ultimo is None or b > ultimo else ultimo
        if b > meia_noite:
            depois += (b - max(a, meia_noite)).total_seconds() / 60
    return PcDaNoite(ultimo, round(depois))


# ---------------------------------------------------------------------------------------------
# Relatórios
# ---------------------------------------------------------------------------------------------

def _escrever(caminho: Path, fm: dict, corpo: list[str]) -> Path:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    cabecalho = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip()
    texto = f"---\n{cabecalho}\n---\n\n" + "\n".join(corpo) + "\n"
    existente = caminho.read_text(encoding="utf-8") if caminho.exists() else None
    if texto != existente:
        tmp = caminho.with_suffix(".tmp")
        tmp.write_text(texto, encoding="utf-8")
        tmp.replace(caminho)
    return caminho


def _media_hm(valores: list[float]) -> str:
    return relogio._hm(round(mean(valores))) if valores else "—"


def _rotulo_dia(d: date) -> str:
    return f"{relogio.DIAS_SEMANA[d.weekday()]} {d:%d/%m}"


def _aviso_amostra(grupos: dict[str, list]) -> str:
    pequenos = [f"{nome} ({len(v)})" for nome, v in grupos.items() if 0 < len(v) < GRUPO_MIN]
    if not pequenos:
        return f"Todos os grupos têm pelo menos {GRUPO_MIN} noites: a diferença já diz alguma coisa, mas não prova causa."
    return (f"Ainda poucos dados em {', '.join(pequenos)}: com menos de {GRUPO_MIN} noites num grupo, "
            "a diferença pode ser acaso. Não tire conclusão ainda.")


def _faixa_pc(p: PcDaNoite, d: date) -> str:
    if p.ultimo is None:
        return "sem PC à noite"
    if p.ultimo <= _meia_noite(d):
        return "PC até 0h"
    if p.ultimo <= _meia_noite(d) + timedelta(hours=2):
        return "PC de 0h a 2h"
    return "PC depois das 2h"


def relatorio_pc(noites: list[relogio.Dia], trechos: list[Trecho], vault: Path) -> Path | None:
    linhas_tabela, grupos = [], {"PC até 0h": [], "PC de 0h a 2h": [], "PC depois das 2h": [], "sem PC à noite": []}
    for dia in noites:
        p = pc_da_noite(dia, trechos)
        if p is None:
            continue
        n = dia.noite
        grupos[_faixa_pc(p, dia.data)].append(dia)
        ate = f"{p.ultimo:%H:%M}" if p.ultimo else "—"
        intervalo = relogio._hm(max(0, round((n.inicio - p.ultimo).total_seconds() / 60))) if p.ultimo else "—"
        linhas_tabela.append(f"| {_rotulo_dia(dia.data)} | {ate} | {relogio._hm(p.depois_da_meia_noite_min) if p.depois_da_meia_noite_min else '—'} "
                             f"| {intervalo} | {n.inicio:%H:%M} | {relogio._hm(n.dormindo_min)} |")
    if not linhas_tabela:
        return None

    ultimo = noites[-1].data
    corpo = [
        "# Sono e PC", "",
        "<!-- Gerada pelo Logos (`python -m logos.sono`) a partir do relógio e do ActivityWatch. "
        "Refeita a cada banco novo: edições à mão aqui se perdem. -->", "",
        "Até que horas o PC ficou em uso na noite anterior, e como foi o sono depois. Só o PC: o "
        "celular não entra, então o intervalo entre largar o PC e dormir é tempo sem registro. "
        "A conta para quando você pega no sono: PC usado numa interrupção da noite não entra.", "",
        "## Por faixa", "",
        "| Último uso do PC | Noites | Sono médio | Dormiu, em média |",
        "|---|---|---|---|",
    ]
    for nome, dias in grupos.items():
        if not dias:
            continue
        sono = _media_hm([x.noite.dormindo_min for x in dias])
        deitou = mean(_desde_18h(x.noite.inicio, x.data) for x in dias)
        corpo.append(f"| {nome} | {len(dias)} | {sono} | {_hora_de(deitou, ultimo):%H:%M} |")
    corpo += ["", _aviso_amostra(grupos), "",
              "## Noite a noite", "",
              "| Dia | PC até | PC depois de 0h | PC → sono | Dormiu | Sono |",
              "|---|---|---|---|---|---|",
              *reversed(linhas_tabela),
              "", "## Related", "", "- [[Logos — Relógio]]"]
    fm = {
        "date": ultimo.isoformat(),
        "description": "Uso do PC à noite (ActivityWatch) contra a hora de dormir e o sono do relógio, noite a noite e por faixa.",
        "tags": ["atividade", "relogio", "private"],
        "fonte": "gadgetbridge + activitywatch",
        "noites": len(linhas_tabela),
    }
    return _escrever(vault / relogio.PASTA / NOTA_PC, fm, corpo)


_FAIXAS_SONO = (("<5h", 5 * 60), ("5–6h", 6.5 * 60), ("7–8h", 8.5 * 60))


def _faixa_do_relogio(minutos: int) -> str:
    """O sono medido na escala do /diario (que não tem 6–7h: até 6h30 conta como 5–6h)."""
    for rotulo, ate in _FAIXAS_SONO:
        if minutos < ate:
            return rotulo
    return ">8h"


def relatorio_diario(noites: list[relogio.Dia]) -> Path | None:
    """Sono × escalas do /diario. Escreve só em capturas/Diario/ e devolve só o caminho."""
    pares = []
    for dia in noites:
        fm = diario.escalas_do_diario(dia.data)
        if fm:
            pares.append((dia, fm))
    if not pares:
        return None

    def numero(fm: dict, chave: str) -> float | None:
        v = fm.get(chave)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    curtas = [(d, fm) for d, fm in pares if d.noite.dormindo_min < NOITE_CURTA_MIN]
    boas = [(d, fm) for d, fm in pares if d.noite.dormindo_min >= NOITE_CURTA_MIN]
    grupos = {"menos de 6h": curtas, "6h ou mais": boas}

    corpo = [
        "# Sono e Diário", "",
        "<!-- Gerada pelo Logos (`python -m logos.sono`) a partir do relógio e do frontmatter dos "
        "diários. Refeita a cada banco novo: edições à mão aqui se perdem. -->", "",
        "A noite medida pelo relógio contra o que você marcou no /diario do mesmo dia.", "",
        "## Por noite", "",
        "| Noite | Diários | Humor médio | Energia média |",
        "|---|---|---|---|",
    ]
    for nome, grupo in grupos.items():
        if not grupo:
            continue
        medias = []
        for chave in ("humor_nota", "energia"):
            valores = [v for _, fm in grupo if (v := numero(fm, chave)) is not None]
            medias.append(f"{mean(valores):.1f}".replace(".", ",") if valores else "—")
        corpo.append(f"| {nome} | {len(grupo)} | {medias[0]} | {medias[1]} |")
    corpo += ["", _aviso_amostra(grupos), ""]

    marcados = [(d, fm) for d, fm in pares if fm.get("sono")]
    if marcados:
        iguais = sum(1 for d, fm in marcados if fm["sono"] == _faixa_do_relogio(d.noite.dormindo_min))
        corpo += ["## O sono que você marcou", "",
                  f"Em {iguais} de {len(marcados)} dias a faixa marcada no /diario bate com a do relógio.", ""]

    corpo += ["## Dia a dia", "",
              "| Dia | Sono (relógio) | Você marcou | Humor | Energia | Estresse | O dia foi |",
              "|---|---|---|---|---|---|---|"]
    for d, fm in reversed(pares):
        corpo.append(f"| {_rotulo_dia(d.data)} | {relogio._hm(d.noite.dormindo_min)} | {fm.get('sono') or '—'} "
                     f"| {fm.get('humor_nota') or '—'} | {fm.get('energia') or '—'} | {fm.get('estresse') or '—'} "
                     f"| {fm.get('dia_foi') or '—'} |")
    fm_nota = {
        "date": pares[-1][0].data.isoformat(),
        "description": "Sono do relógio contra humor, energia e o sono marcados no /diario.",
        "tags": ["diario", "relogio", "private"],
        "dias": len(pares),
    }
    pasta = diario.caminho_nota(Route.DIARIO, pares[-1][0].data).parent
    return _escrever(pasta / NOTA_DIARIO, fm_nota, corpo)


# ---------------------------------------------------------------------------------------------
# Orquestração
# ---------------------------------------------------------------------------------------------

def _historico(con, hoje: date) -> list[relogio.Dia]:
    return [relogio.ler_dia(con, hoje - timedelta(days=i)) for i in range(HISTORICO_DIAS - 1, -1, -1)]


def processar(db: Path, vault: Path, agora: datetime, estado: dict,
              avisar: Callable[[str], bool], uso_pc: UsoDoPc = uso_do_pc, *, forcar: bool = False) -> str | None:
    """Refaz as notas e os relatórios; manda a mensagem da noite se ela fechou e ainda não foi.
    Devolve a mensagem de hoje quando há noite (enviada ou não)."""
    hoje = agora.date()
    relogio.rodar(db, vault, dias=2, ate=hoje, agora=agora)
    con = relogio.abrir(db)
    try:
        historico = _historico(con, hoje)
        cob = relogio.cobertura(con)
    finally:
        con.close()

    noites = [d for d in historico if d.noite]
    trechos = uso_pc(relogio.janela_do_sono(historico[0].data)[0], agora) if noites else None
    if noites and trechos is not None:
        relatorio_pc(noites, trechos, vault)
    relatorio_diario(noites)

    dia = historico[-1]
    if dia.noite is None:
        return None
    pc = pc_da_noite(dia, trechos) if trechos is not None else None
    texto = mensagem(dia, historico[:-1], pc.ultimo if pc else None)
    pode = estado.get("enviada") != hoje.isoformat() and agora.hour < ENVIA_ATE_H and noite_fechada(dia, cob)
    if forcar or pode:
        if avisar(texto):
            estado["enviada"] = hoje.isoformat()
    return texto


def verificar(agora: datetime | None = None, avisar: Callable[[str], bool] | None = None,
              uso_pc: UsoDoPc = uso_do_pc) -> None:
    """Chamada a cada volta do watcher. Barata quando nada mudou: um stat por minuto. Nunca levanta."""
    global _ultima_checagem
    db, vault = config.GADGETBRIDGE_DB, config.VAULT_DIR
    if db is None or vault is None or time.monotonic() - _ultima_checagem < CHECA_A_CADA_S:
        return
    _ultima_checagem = time.monotonic()
    try:
        mtime = db.stat().st_mtime
    except OSError:
        return
    estado = _ler_estado()
    if estado.get("mtime") == mtime:
        return
    if avisar is None:
        from .telegram import notify as avisar
    try:
        processar(db, vault, agora or datetime.now().astimezone(), estado, avisar, uso_pc)
    except Exception:
        # Banco novo que não abre é registrado uma vez, não a cada minuto: o mtime fica gravado.
        logger.exception("Sono: falha ao processar o banco novo do Gadgetbridge")
    estado["mtime"] = mtime
    try:
        _gravar_estado(estado)
    except OSError as e:
        logger.warning(f"Sono: estado não gravado ({e}); o próximo banco refaz tudo")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m logos.sono",
        description="Refaz os relatórios de sono e mostra a mensagem da noite de hoje (sem enviar).",
    )
    p.add_argument("--enviar", action="store_true", help="manda a mensagem de hoje pelo Telegram, mesmo se já foi")
    p.add_argument("--db", type=Path, default=config.GADGETBRIDGE_DB)
    p.add_argument("--vault", type=Path, default=config.VAULT_DIR)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.db is None or args.vault is None:
        print("Sem banco ou sem vault: defina gadgetbridge_db e vault_dir no logos_config.yaml.", file=sys.stderr)
        return 2

    estado = _ler_estado()
    if args.enviar:
        from .telegram import notify as avisar
    else:
        def avisar(_texto: str) -> bool:
            return False
    try:
        texto = processar(args.db, args.vault, datetime.now().astimezone(), estado, avisar, forcar=args.enviar)
    except relogio.RelogioError as e:
        print(str(e), file=sys.stderr)
        return 1
    if args.enviar:
        _gravar_estado(estado)
    print(texto or "Sem noite registrada hoje.")
    print(f"\nRelatórios: {args.vault / relogio.PASTA / NOTA_PC} · "
          f"{diario.caminho_nota(Route.DIARIO, date.today()).parent / NOTA_DIARIO}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
