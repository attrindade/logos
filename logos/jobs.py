"""Disparo sob demanda dos jobs headless do vault, pelo Telegram.

Os três jobs já rodavam sozinhos pelo Agendador (`notes/.scripts/jobs/`): reuniões do Drive
às 21h, promover + intake às 22h30, briefing às 8h. O que faltava era poder pedir "roda
agora" sem estar na frente do PC — sentar no terminal, `cd` no vault e abrir o Claude é o
passo manual que isto remove.

Duas regras que não são detalhe:

1. **A lista é allowlist, como `consulta.ALLOWED_SUBDIRS`.** Job novo em `.scripts/jobs/`
   nasce invisível para o Telegram; entra aqui por diff, com teste. A correção que parece
   mais limpa — descobrir os `.ps1` da pasta — é a que dispara qualquer coisa que alguém
   deixar lá.
2. **O lock é lido, nunca tomado.** Quem decide se pode rodar é o próprio script, que pega o
   lock e sai com 75 se estiver ocupado. Aqui o lock só é lido para a resposta imediata dizer
   quem está rodando. Dois donos da verdade seria como o intake duplica seção.

Nada interativo entra nesta lista: sessão headless não pergunta, então captura que pede
decisão volta em "Precisou de Julgamento" e `capturas/Diario/` continua fora por construção.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import config, consulta

logger = logging.getLogger(__name__)

# Combinado com `$script:EXIT_OCUPADO` em `_run-claude.ps1`.
EXIT_OCUPADO = 75

MARCA_JOB = "⚙️"
MARCA_OK = "✔️"
MARCA_FALHA = "⚠️"


@dataclass(frozen=True)
class Job:
    chave: str
    script: str
    titulo: str
    detalhe: str
    espera: str
    # Chaves que o job escreve em `last-run.json` — `promover-noturno.ps1` roda dois.
    estado: tuple[str, ...]


JOBS: tuple[Job, ...] = (
    Job(
        "promover", "promover-noturno.ps1", "Promover capturas",
        "roteia as capturas sem <code>promoted:</code> e, se houver reunião nova, faz o intake",
        "2 a 20 min", ("promover", "intake"),
    ),
    Job(
        "reunioes", "drive-meetings.ps1", "Baixar reuniões",
        "exporta as transcrições do Tactiq no Drive (48 h) para <code>work/meetings/</code>",
        "1 a 10 min", ("drive-meetings",),
    ),
    Job(
        "briefing", "briefing-matinal.ps1", "Refazer o briefing",
        "reescreve o briefing de hoje com agenda, tarefas e e-mails",
        "1 a 5 min", ("briefing",),
    ),
)


class JobError(Exception):
    """Erro que o dono lê no chat, não no log."""


# Um disparo por vez vindo do bot. O lock do script é a garantia de verdade; isto só evita
# duas threads nossas esperando pelo mesmo resultado.
_thread_lock = threading.Lock()
_rodando: str | None = None


def _vault() -> Path:
    if config.VAULT_DIR is None:
        raise JobError("Sem vault configurado: `vault_dir` não está no logos_config.yaml.")
    return config.VAULT_DIR


def script_path(job: Job) -> Path:
    return _vault() / ".scripts" / "jobs" / job.script


def _state_dir() -> Path:
    return _vault() / ".claude" / "om-jobs-state"


def por_indice(n: int) -> Job | None:
    return JOBS[n - 1] if 1 <= n <= len(JOBS) else None


def lock_dono() -> dict | None:
    """Quem detém o lock dos jobs, ou None. Só leitura: nunca rouba, nunca apaga."""
    path = _state_dir() / "jobs.lock"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def ultimo(chave: str) -> dict | None:
    try:
        todos = json.loads((_state_dir() / "last-run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entrada = todos.get(chave)
    return entrada if isinstance(entrada, dict) else None


def _pwsh() -> str:
    """O Agendador chama `pwsh`; aqui é o mesmo binário ou nada.

    Sem fallback para o `powershell` 5.1 de propósito: o job registrado roda em pwsh 7, e um
    resultado diferente conforme quem disparou é o tipo de diferença que ninguém investiga.
    """
    from shutil import which

    caminho = which("pwsh")
    if not caminho:
        raise JobError("Não achei o `pwsh` nesta máquina; o job só roda pelo Agendador.")
    return caminho


def render_lista() -> consulta.Tela:
    """O que `/j` responde: um botão por job. O toque não dispara — abre a confirmação."""
    partes = [f"{MARCA_JOB} <b>Rodar no PC</b>", ""]
    for job in JOBS:
        partes.append(f"<b>{job.titulo}</b>  <i>{job.espera}</i>")
        partes.append(job.detalhe)
        partes.append("")
    dono = lock_dono()
    if dono:
        partes.append(f"{MARCA_FALHA} ocupado agora: <b>{consulta.esc(str(dono.get('owner')))}</b>")
    botoes = [[(f"{MARCA_JOB} {job.titulo}", f"j:{indice}")] for indice, job in enumerate(JOBS, 1)]
    return consulta.Tela("\n".join(partes).rstrip(), botoes + [[consulta.INICIO]])


def render_confirmar(indice: int, job: Job) -> consulta.Tela:
    """Um passo a mais de propósito: botão é fácil de tocar sem querer, e promover roda até
    20 min e custa dinheiro de API. Digitar `/j1` era a confirmação; o toque não é."""
    texto = (f"{MARCA_JOB} <b>{job.titulo}</b>?\n"
             f"{job.detalhe}\n<i>Leva {job.espera}. O resultado chega aqui quando terminar.</i>")
    return consulta.Tela(texto, [[("▶ Rodar agora", f"jr:{indice}"), (f"{consulta.MARCA_VOLTA} Voltar", "j")]])


def cmd_botao(dado: str) -> consulta.Tela:
    """`j` lista, `j:<n>` pede confirmação, `jr:<n>` dispara."""
    verbo, _, numero = dado.partition(":")
    if verbo == "j" and not numero:
        return render_lista()
    job = por_indice(int(numero)) if numero.isdigit() else None
    if verbo not in ("j", "jr") or job is None:
        raise JobError("Esse job não existe mais. Abra /j de novo.")
    if verbo == "j":
        return render_confirmar(int(numero), job)
    return consulta.Tela(disparar(job), [[consulta.INICIO]])


def _fmt_custo(entrada: dict) -> str:
    valor = entrada.get("custo_usd")
    try:
        return f" · US$ {float(valor):.2f}".replace(".", ",")
    except (TypeError, ValueError):
        return ""


def _resultado(job: Job, codigo: int, minutos: float) -> str:
    if codigo == EXIT_OCUPADO:
        dono = lock_dono()
        quem = dono.get("owner") if dono else "outro job"
        return f"{MARCA_FALHA} {job.titulo}: não rodou, {quem} estava em andamento."

    linhas = []
    for chave in job.estado:
        entrada = ultimo(chave)
        if not entrada:
            continue
        status = str(entrada.get("status") or "?")
        marca = MARCA_OK if status.startswith("ok") else MARCA_FALHA
        linhas.append(f"{marca} {chave}: {status}{_fmt_custo(entrada)}")
    if not linhas:
        marca = MARCA_OK if codigo == 0 else MARCA_FALHA
        linhas.append(f"{marca} {job.titulo}: terminou com código {codigo}")
    return f"{job.titulo} — {minutos:.0f} min\n" + "\n".join(linhas)


def _executar(job: Job, notificar) -> None:
    global _rodando
    inicio = time.monotonic()
    try:
        proc = subprocess.run(
            [_pwsh(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path(job))],
            cwd=str(_vault()), capture_output=True, text=True,
            # Sem isto o pythonw do watcher pisca um console a cada disparo.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        codigo = proc.returncode
        if proc.stderr:
            logger.info(f"Job {job.chave}: stderr: {proc.stderr.strip()[:500]}")
    except JobError as e:
        notificar(f"{MARCA_FALHA} {job.titulo}: {e}")
        return
    except Exception:
        logger.exception(f"Job {job.chave}: falha ao rodar o script")
        notificar(f"{MARCA_FALHA} {job.titulo}: não consegui rodar o script. O log do Logos tem o motivo.")
        return
    finally:
        with _thread_lock:
            _rodando = None
    minutos = (time.monotonic() - inicio) / 60
    logger.info(f"Job {job.chave}: exit={codigo} em {minutos:.1f} min")
    notificar(_resultado(job, codigo, minutos))


def disparar(job: Job, notificar=None) -> str:
    """Começa o job numa thread e devolve o texto da resposta imediata.

    A resposta não espera o job: promover leva até 20 min, e o Telegram (e ele) precisam de
    uma confirmação agora. O resultado chega depois pelo mesmo chat.
    """
    global _rodando
    if notificar is None:
        from .telegram import notify as notificar  # aqui, para jobs.py não depender do bot

    caminho = script_path(job)
    if not caminho.is_file():
        raise JobError(f"Não achei {job.script} em {caminho.parent}.")

    dono = lock_dono()
    if dono:
        return (f"{MARCA_FALHA} Ocupado: <b>{consulta.esc(str(dono.get('owner')))}</b> começou "
                f"{consulta.esc(str(dono.get('started')))}. Tenta de novo em alguns minutos.")

    with _thread_lock:
        if _rodando:
            return f"{MARCA_FALHA} Já disparei <b>{_rodando}</b> daqui. Espera o aviso dele."
        _rodando = job.titulo

    threading.Thread(target=_executar, args=(job, notificar), daemon=True,
                     name=f"job-{job.chave}").start()
    return (f"{MARCA_JOB} <b>{job.titulo}</b> começou — <i>{job.espera}</i>.\n"
            "Aviso aqui quando terminar.")
