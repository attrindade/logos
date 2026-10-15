"""Sono, passos e frequência cardíaca do Huawei Watch Fit (modelo TIA-B09), lidos do banco do Gadgetbridge.

O Gadgetbridge substitui o app Huawei Saúde no celular e guarda tudo num SQLite local. O
"Exportação automática" dele grava uma cópia do banco numa pasta que o Syncthing traz para o PC,
o mesmo caminho dos áudios. Este módulo lê essa cópia e escreve uma nota por dia em
`atividade/Relógio — YYYY-MM-DD.md`, ao lado do resumo do ActivityWatch.

A nota é derivada: o banco é a fonte, e cada execução refaz os últimos dias por inteiro. Rodar
de novo é sempre seguro, e é assim que o dia de ontem ganha os passos da noite, que só chegam
na sincronização seguinte. Chaves do frontmatter que não são deste módulo ficam como estão.

Esquema conferido no código do Gadgetbridge (GBDaoGenerator, HuaweiSampleProvider,
HuaweiSupportProvider) em 2026-10-13:

- `HUAWEI_ACTIVITY_SAMPLE`: uma amostra por minuto, em segundos. Cada intervalo é gravado duas
  vezes, um marcador no início e outro no fim; o de início é o que tem TIMESTAMP < OTHER_TIMESTAMP,
  e só ele carrega passos e FC. Passos vêm da fonte 0x0b. Sono antigo (sem TruSleep) vem das
  fontes 0x0d/0x0a, com RAW_KIND 0x06 leve e 0x07 profundo.
- `HUAWEI_SLEEP_STATS_SAMPLE`: um resumo por noite (TruSleep), em milissegundos. TIMESTAMP é a
  hora em que adormeceu e WAKEUP_TIME a que acordou.
- `HUAWEI_SLEEP_STAGE_SAMPLE`: uma linha por minuto de sono, em milissegundos. STAGE 1 leve,
  2 REM, 3 profundo, 4 acordado, 5 cochilo.

As duas tabelas de sono só existem em versões novas do Gadgetbridge e só são preenchidas se o
relógio mandar TruSleep. Sem elas, o sono sai das amostras de atividade.

Noite e cochilo (regra de 2026-10-13). O sono chega em trechos, e uma noite com uma ida à cozinha
vira dois. Cada trecho pertence ao dia em que começa, contado das 18h da véspera às 18h. A noite do
dia é o trecho mais longo que começa antes do meio-dia, mais os vizinhos ligados a ele: dois
trechos se ligam quando o intervalo entre eles é curto (EMENDA_MIN) ou quando a pessoa acordou de
madrugada, entre 22h e 6h. O resto é cochilo. `interrupcoes` e `fora_min` contam esses intervalos;
`acordou` continua sendo o WAKE_COUNT do TruSleep, que só existe com ele.
"""
from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from . import config

logger = logging.getLogger(__name__)

PASTA = "atividade"
DIAS_SEMANA = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")

FONTE_PASSOS = 0x0B
FONTES_SONO = (0x0D, 0x0A)
KIND_LEVE, KIND_PROFUNDO, KIND_REM, KIND_ACORDADO = 0x06, 0x07, 0x5656, 0x5658
KINDS_DORMINDO = (KIND_LEVE, KIND_PROFUNDO, KIND_REM)

ESTAGIOS = {1: "leve", 2: "REM", 3: "profundo", 4: "acordado", 5: "cochilo"}
ESTAGIOS_DORMINDO = (1, 2, 3, 5)

# O dia do sono vai das 18h da véspera às 18h: o trecho pertence ao dia em que começa.
VIRADA_SONO_H = 18
# A noite começa antes do meio-dia; o que começa depois é cochilo.
NOITE_ATE_H = 12
# Acordar nesse horário e voltar a dormir é interrupção da noite, não fim dela.
MADRUGADA_H = (22, 6)
# Trecho mais curto que isso não vira noite sozinho.
NOITE_MIN_MIN = 60
# Trechos separados por menos que isso são a mesma sessão, a qualquer hora.
EMENDA_MIN = 30

# Chaves que este módulo refaz a cada execução. O resto do frontmatter é de outro dono.
_CHAVES = ("date", "description", "tags", "fonte", "atualizado", "sono_h", "sono_inicio", "sono_fim",
           "sono_nota", "acordou", "sono_interrupcoes", "cochilos", "passos", "fc_repouso", "fc_min", "fc_max")
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


class RelogioError(Exception):
    pass


@dataclass
class Sono:
    inicio: datetime
    fim: datetime
    dormindo_min: int
    estagios_min: dict[str, int] = field(default_factory=dict)
    acordou: int | None = None
    nota: int | None = None
    interrupcoes: int = 0
    fora_min: int = 0

    @property
    def horas(self) -> float:
        return round(self.dormindo_min / 60, 1)


@dataclass
class Dia:
    data: date
    noite: Sono | None = None
    cochilos: list[Sono] = field(default_factory=list)
    passos: int | None = None
    ultima_amostra: datetime | None = None
    fc_repouso: int | None = None
    fc_min: int | None = None
    fc_max: int | None = None

    @property
    def vazio(self) -> bool:
        return self.noite is None and not self.cochilos and self.passos is None and self.fc_min is None


# ---------------------------------------------------------------------------------------------
# Leitura do banco
# ---------------------------------------------------------------------------------------------

def abrir(caminho: Path) -> sqlite3.Connection:
    """Abre a cópia exportada só para leitura. `immutable=1` porque ninguém escreve nela pelo
    SQLite: o Syncthing troca o arquivo inteiro, e sem a flag o SQLite tentaria criar -wal e
    -shm dentro da pasta sincronizada."""
    if not caminho.is_file():
        raise RelogioError(f"Banco do Gadgetbridge não encontrado: {caminho}")
    uri = f"{caminho.resolve().as_uri()}?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True)
    try:
        con.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    except sqlite3.DatabaseError as e:
        con.close()
        raise RelogioError(f"{caminho} não é um banco SQLite (a exportação gravou outro formato?): {e}")
    return con


def _tabelas(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _bpm(valor: int | None) -> int | None:
    """FC é gravada como byte com sinal: 140 bpm vira -116, e -1 é "não medido"."""
    if valor is None:
        return None
    v = valor + 256 if valor < 0 else valor
    return v if 25 <= v <= 240 else None


def _epoch(d: datetime) -> int:
    return int(d.timestamp())


def _inicio_do_dia(d: date) -> datetime:
    return datetime(d.year, d.month, d.day).astimezone()


def _dia_de_atividade(con: sqlite3.Connection, d: date, dia: Dia) -> None:
    ini, fim = _epoch(_inicio_do_dia(d)), _epoch(_inicio_do_dia(d + timedelta(days=1)))
    rows = con.execute(
        "SELECT TIMESTAMP, STEPS, HEART_RATE, RESTING_HEART_RATE FROM HUAWEI_ACTIVITY_SAMPLE "
        "WHERE SOURCE = ? AND TIMESTAMP >= ? AND TIMESTAMP < ? AND TIMESTAMP < OTHER_TIMESTAMP "
        "ORDER BY TIMESTAMP",
        (FONTE_PASSOS, ini, fim),
    ).fetchall()
    if not rows:
        return
    dia.passos = sum(s for _, s, _, _ in rows if s and s > 0)
    dia.ultima_amostra = datetime.fromtimestamp(max(r[0] for r in rows)).astimezone()
    fcs = [v for v in (_bpm(r[2]) for r in rows) if v]
    if fcs:
        dia.fc_min, dia.fc_max = min(fcs), max(fcs)
    repousos = [v for v in (_bpm(r[3]) for r in rows) if v]
    if repousos:
        # O relógio recalcula o repouso ao longo do dia; o último valor é o do dia inteiro.
        dia.fc_repouso = repousos[-1]


def _sonos_truesleep(con: sqlite3.Connection, ini: datetime, fim: datetime) -> list[Sono]:
    """Sessões do TruSleep que começam em [ini, fim)."""
    tem_estagios = "HUAWEI_SLEEP_STAGE_SAMPLE" in _tabelas(con)
    sonos = []
    for adormeceu, acordou, nota, vezes in con.execute(
        "SELECT TIMESTAMP, WAKEUP_TIME, SLEEP_SCORE, WAKE_COUNT FROM HUAWEI_SLEEP_STATS_SAMPLE "
        "WHERE TIMESTAMP >= ? AND TIMESTAMP < ? ORDER BY TIMESTAMP",
        (_epoch(ini) * 1000, _epoch(fim) * 1000),
    ):
        if not acordou or acordou <= adormeceu:
            continue
        estagios: dict[str, int] = {}
        if tem_estagios:
            for estagio, n in con.execute(
                "SELECT STAGE, COUNT(*) FROM HUAWEI_SLEEP_STAGE_SAMPLE "
                "WHERE TIMESTAMP >= ? AND TIMESTAMP < ? GROUP BY STAGE",
                (adormeceu, acordou),
            ):
                if estagio in ESTAGIOS:
                    estagios[ESTAGIOS[estagio]] = n
        dormindo = sum(n for k, n in estagios.items() if k != "acordado")
        if not dormindo:
            dormindo = (acordou - adormeceu) // 60000
        sonos.append(Sono(
            inicio=datetime.fromtimestamp(adormeceu / 1000).astimezone(),
            fim=datetime.fromtimestamp(acordou / 1000).astimezone(),
            dormindo_min=int(dormindo),
            estagios_min=estagios,
            acordou=vezes if vezes is not None and vezes >= 0 else None,
            nota=nota if nota and nota > 0 else None,
        ))
    return sonos


def _sonos_de_atividade(con: sqlite3.Connection, ini: datetime, fim: datetime) -> list[Sono]:
    """Sono sem TruSleep: trechos de leve/profundo emendados em sessões que começam em [ini, fim)."""
    margem = 16 * 3600  # lê antes de `ini` para não cortar ao meio uma sessão que começou antes
    rows = con.execute(
        f"SELECT TIMESTAMP, OTHER_TIMESTAMP, RAW_KIND FROM HUAWEI_ACTIVITY_SAMPLE "
        f"WHERE SOURCE IN ({','.join('?' * len(FONTES_SONO))}) AND TIMESTAMP < OTHER_TIMESTAMP "
        f"AND RAW_KIND IN ({','.join('?' * (len(KINDS_DORMINDO) + 1))}) "
        f"AND OTHER_TIMESTAMP >= ? AND TIMESTAMP < ? ORDER BY TIMESTAMP",
        (*FONTES_SONO, *KINDS_DORMINDO, KIND_ACORDADO, _epoch(ini) - margem, _epoch(fim)),
    ).fetchall()

    nomes = {KIND_LEVE: "leve", KIND_PROFUNDO: "profundo", KIND_REM: "REM", KIND_ACORDADO: "acordado"}
    sessoes: list[list[tuple[int, int, int]]] = []
    for a, b, kind in rows:
        if sessoes and a - sessoes[-1][-1][1] <= EMENDA_MIN * 60:
            sessoes[-1].append((a, b, kind))
        else:
            sessoes.append([(a, b, kind)])

    sonos = []
    for s in sessoes:
        estagios: dict[str, int] = {}
        for a, b, kind in s:
            estagios[nomes[kind]] = estagios.get(nomes[kind], 0) + (b - a) // 60
        dormindo = sum(n for k, n in estagios.items() if k != "acordado")
        inicio = datetime.fromtimestamp(s[0][0]).astimezone()
        if not dormindo or not (ini <= inicio < fim):
            continue
        sonos.append(Sono(
            inicio=inicio,
            fim=datetime.fromtimestamp(max(b for _, b, _ in s)).astimezone(),
            dormindo_min=dormindo,
            estagios_min=estagios,
        ))
    return sonos


def janela_do_sono(d: date) -> tuple[datetime, datetime]:
    """Os trechos que começam aqui são do dia `d`: das 18h da véspera às 18h."""
    ini = _inicio_do_dia(d) - timedelta(hours=24 - VIRADA_SONO_H)
    return ini, ini + timedelta(days=1)


def _ligados(a: Sono, b: Sono, d: date) -> bool:
    """`b` continua a noite de `a`: intervalo curto, ou acordou de madrugada e voltou a dormir."""
    if (b.inicio - a.fim).total_seconds() <= EMENDA_MIN * 60:
        return True
    madrugada_ini = _inicio_do_dia(d) - timedelta(hours=24 - MADRUGADA_H[0])
    madrugada_fim = _inicio_do_dia(d) + timedelta(hours=MADRUGADA_H[1])
    return madrugada_ini <= a.fim < madrugada_fim


def _juntar(trechos: list[Sono]) -> Sono:
    estagios: dict[str, int] = {}
    for t in trechos:
        for k, v in t.estagios_min.items():
            estagios[k] = estagios.get(k, 0) + v
    vezes = [t.acordou for t in trechos if t.acordou is not None]
    principal = max(trechos, key=lambda t: t.dormindo_min)
    return Sono(
        inicio=trechos[0].inicio,
        fim=max(t.fim for t in trechos),
        dormindo_min=sum(t.dormindo_min for t in trechos),
        estagios_min=estagios,
        acordou=sum(vezes) if vezes else None,
        nota=principal.nota,
        interrupcoes=len(trechos) - 1,
        fora_min=sum(max(0, int((b.inicio - a.fim).total_seconds() // 60)) for a, b in zip(trechos, trechos[1:])),
    )


def classificar(trechos: list[Sono], d: date) -> tuple[Sono | None, list[Sono]]:
    """Separa os trechos do dia `d` em noite e cochilos (regra no docstring do módulo)."""
    trechos = sorted(trechos, key=lambda t: t.inicio)
    limite = _inicio_do_dia(d) + timedelta(hours=NOITE_ATE_H)
    candidatos = [i for i, t in enumerate(trechos) if t.inicio < limite and t.dormindo_min >= NOITE_MIN_MIN]
    if not candidatos:
        return None, trechos
    ancora = max(candidatos, key=lambda i: trechos[i].dormindo_min)
    ini = fim = ancora
    while ini > 0 and _ligados(trechos[ini - 1], trechos[ini], d):
        ini -= 1
    while fim + 1 < len(trechos) and trechos[fim + 1].inicio < limite and _ligados(trechos[fim], trechos[fim + 1], d):
        fim += 1
    return _juntar(trechos[ini:fim + 1]), trechos[:ini] + trechos[fim + 1:]


def cobertura(con: sqlite3.Connection) -> datetime | None:
    """Até quando o banco tem amostras: o relógio só manda o que viu até a última sincronização."""
    ts = con.execute("SELECT MAX(TIMESTAMP) FROM HUAWEI_ACTIVITY_SAMPLE").fetchone()[0]
    return datetime.fromtimestamp(ts).astimezone() if ts else None


def ler_dia(con: sqlite3.Connection, d: date) -> Dia:
    dia = Dia(data=d)
    tabelas = _tabelas(con)
    if "HUAWEI_ACTIVITY_SAMPLE" not in tabelas:
        raise RelogioError("O banco não tem HUAWEI_ACTIVITY_SAMPLE: é do Gadgetbridge com um relógio Huawei?")

    _dia_de_atividade(con, d, dia)

    ini, fim = janela_do_sono(d)
    sonos = _sonos_truesleep(con, ini, fim) if "HUAWEI_SLEEP_STATS_SAMPLE" in tabelas else []
    if not sonos:
        sonos = _sonos_de_atividade(con, ini, fim)

    dia.noite, dia.cochilos = classificar(sonos, d)
    return dia


# ---------------------------------------------------------------------------------------------
# Nota
# ---------------------------------------------------------------------------------------------

def _hm(minutos: int) -> str:
    h, m = divmod(int(minutos), 60)
    if not h:
        return f"{m} min"
    return f"{h}h{m:02d}"


def _milhar(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def nome_nota(d: date) -> str:
    return f"Relógio — {d.isoformat()}.md"


def linha_noite(dia: Dia) -> str:
    """Uma linha para o briefing das 8h. Sem dado, diz isso, em vez de sumir."""
    n = dia.noite
    if n is None:
        return "Sono: sem dado do relógio para esta noite (sincronizou o Gadgetbridge?)."
    partes = [f"Sono: {_hm(n.dormindo_min)} ({n.inicio:%H:%M} → {n.fim:%H:%M})"]
    if n.interrupcoes:
        partes.append(_interrupcoes(n))
    if n.acordou:
        partes.append(f"acordou {n.acordou}x")
    if n.nota:
        partes.append(f"nota {n.nota}")
    if dia.fc_repouso:
        partes.append(f"FC repouso {dia.fc_repouso}")
    return ", ".join(partes) + "."


def _interrupcoes(s: Sono) -> str:
    vezes = "1 interrupção" if s.interrupcoes == 1 else f"{s.interrupcoes} interrupções"
    return f"{vezes} ({_hm(s.fora_min)} acordado)"


def _linha_sono(rotulo: str, s: Sono) -> str:
    linha = f"- {rotulo}: {s.inicio:%H:%M} → {s.fim:%H:%M}, **{_hm(s.dormindo_min)}** dormindo"
    detalhe = [f"{k} {_hm(v)}" for k, v in sorted(s.estagios_min.items(), key=lambda kv: -kv[1]) if v]
    if detalhe:
        linha += f" ({' · '.join(detalhe)})"
    if s.interrupcoes:
        linha += f", {_interrupcoes(s)}"
    if s.acordou:
        linha += f", acordou {s.acordou}x"
    if s.nota:
        linha += f". Nota {s.nota}"
    return linha + "."


def _frontmatter(dia: Dia, agora: datetime) -> dict:
    fm: dict = {
        "date": dia.data.isoformat(),
        "description": f"Relógio (Huawei Watch Fit) em {dia.data.isoformat()}: sono da noite, "
                       f"cochilos, passos e frequência cardíaca, lidos do Gadgetbridge.",
        "tags": ["atividade", "relogio", "private"],
        "fonte": "gadgetbridge",
        "atualizado": agora.strftime("%Y-%m-%dT%H:%M"),
    }
    if dia.noite:
        fm["sono_h"] = dia.noite.horas
        fm["sono_inicio"] = f"{dia.noite.inicio:%H:%M}"
        fm["sono_fim"] = f"{dia.noite.fim:%H:%M}"
        if dia.noite.nota:
            fm["sono_nota"] = dia.noite.nota
        if dia.noite.acordou is not None:
            fm["acordou"] = dia.noite.acordou
        if dia.noite.interrupcoes:
            fm["sono_interrupcoes"] = dia.noite.interrupcoes
    if dia.cochilos:
        fm["cochilos"] = len(dia.cochilos)
    for chave in ("passos", "fc_repouso", "fc_min", "fc_max"):
        valor = getattr(dia, chave)
        if valor is not None:
            fm[chave] = valor
    return fm


def montar_nota(dia: Dia, agora: datetime, existente: str | None = None, *, link_atividade: bool = False) -> str:
    fm = _frontmatter(dia, agora)
    if existente:
        m = _FRONTMATTER_RE.match(existente)
        antigo = yaml.safe_load(m.group(1)) if m else None
        if isinstance(antigo, dict):
            fm.update({k: v for k, v in antigo.items() if k not in _CHAVES})

    d = dia.data
    corpo = [f"# Relógio — {d.isoformat()} ({DIAS_SEMANA[d.weekday()]})", "",
             "<!-- Gerada por `python -m logos.relogio` a partir do banco do Gadgetbridge. "
             "Refeita a cada execução: edições à mão aqui se perdem. -->", "", "## Sono", ""]
    if dia.noite:
        corpo.append(_linha_sono("Noite", dia.noite))
    else:
        corpo.append("- Noite: sem dado.")
    corpo += [_linha_sono("Cochilo", c) for c in dia.cochilos]

    corpo += ["", "## Dia", ""]
    if dia.passos is not None:
        ate = f" (até {dia.ultima_amostra:%H:%M})" if dia.ultima_amostra and dia.data == agora.date() else ""
        corpo.append(f"- Passos: **{_milhar(dia.passos)}**{ate}")
    else:
        corpo.append("- Passos: sem dado.")
    fc = []
    if dia.fc_repouso:
        fc.append(f"repouso {dia.fc_repouso} bpm")
    if dia.fc_min:
        fc.append(f"mínima {dia.fc_min}")
    if dia.fc_max:
        fc.append(f"máxima {dia.fc_max}")
    if fc:
        corpo.append(f"- Frequência cardíaca: {' · '.join(fc)}")

    related = ["[[Logos — Relógio]]"]
    if link_atividade:
        related.append(f"[[{PASTA}/{d.isoformat()}|Atividade no PC]]")
    corpo += ["", "## Related", "", f"- {' · '.join(related)}", ""]

    cabecalho = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{cabecalho}\n---\n\n" + "\n".join(corpo)


def escrever(dia: Dia, vault: Path, agora: datetime) -> Path:
    pasta = vault / PASTA
    pasta.mkdir(parents=True, exist_ok=True)
    caminho = pasta / nome_nota(dia.data)
    existente = caminho.read_text(encoding="utf-8") if caminho.exists() else None
    texto = montar_nota(dia, agora, existente, link_atividade=(pasta / f"{dia.data.isoformat()}.md").exists())
    if texto != existente:
        tmp = caminho.with_suffix(".tmp")
        tmp.write_text(texto, encoding="utf-8")
        tmp.replace(caminho)
    return caminho


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------

def rodar(db: Path, vault: Path, dias: int, ate: date, agora: datetime | None = None) -> list[Dia]:
    agora = agora or datetime.now().astimezone()
    con = abrir(db)
    try:
        lidos = [ler_dia(con, ate - timedelta(days=i)) for i in range(dias - 1, -1, -1)]
    finally:
        con.close()
    for dia in lidos:
        # Dia sem nenhum dado não vira nota: seria uma página de "sem dado" por dia sem relógio.
        if not dia.vazio:
            escrever(dia, vault, agora)
    return lidos


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m logos.relogio",
        description="Escreve atividade/Relógio — YYYY-MM-DD.md a partir do banco exportado pelo Gadgetbridge.",
    )
    p.add_argument("--db", type=Path, default=config.GADGETBRIDGE_DB,
                   help="cópia do banco exportada pelo Gadgetbridge (padrão: gadgetbridge_db do logos_config.yaml)")
    p.add_argument("--vault", type=Path, default=config.VAULT_DIR, help="raiz do vault (padrão: vault_dir)")
    p.add_argument("--dias", type=int, default=2,
                   help="quantos dias refazer, terminando em --data (padrão 2: ontem ganha os passos da noite)")
    p.add_argument("--data", type=date.fromisoformat, default=None, help="último dia, YYYY-MM-DD (padrão: hoje)")
    p.add_argument("--linha", action="store_true",
                   help="imprime só a linha do sono do último dia, para o briefing das 8h")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.db is None:
        print("Sem banco: defina gadgetbridge_db no logos_config.yaml ou passe --db.", file=sys.stderr)
        return 2
    if args.vault is None:
        print("Sem vault: defina vault_dir no logos_config.yaml ou passe --vault.", file=sys.stderr)
        return 2
    try:
        lidos = rodar(args.db, args.vault, max(1, args.dias), args.data or date.today())
    except RelogioError as e:
        print(str(e), file=sys.stderr)
        return 1

    if args.linha:
        print(linha_noite(lidos[-1]))
    else:
        for dia in lidos:
            estado = "sem dado" if dia.vazio else nome_nota(dia.data)
            print(f"{dia.data.isoformat()}: {estado} · {linha_noite(dia)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
