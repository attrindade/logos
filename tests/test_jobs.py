"""Disparo dos jobs headless do vault pelo bot (`/j`). Sem rede e sem rodar PowerShell."""
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from logos import config, jobs, telegram
from logos.telegram import Bot

TOKEN = "123456:SEGREDO-NAO-VAZAR"
OWNER = 42


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Vault de teste com os três scripts existindo — `disparar` recusa script ausente."""
    monkeypatch.setattr(config, "VAULT_DIR", tmp_path)
    (tmp_path / ".scripts" / "jobs").mkdir(parents=True)
    (tmp_path / ".claude" / "om-jobs-state").mkdir(parents=True)
    for job in jobs.JOBS:
        (tmp_path / ".scripts" / "jobs" / job.script).write_text("# stub", encoding="utf-8")
    monkeypatch.setattr(jobs, "_rodando", None)
    return tmp_path


def _lock(vault: Path, owner: str = "promover-noturno", pid: int = 4242) -> None:
    (vault / ".claude" / "om-jobs-state" / "jobs.lock").write_text(
        json.dumps({"pid": pid, "owner": owner, "started": datetime.now().isoformat(timespec="seconds")}),
        encoding="utf-8",
    )


def _last_run(vault: Path, entradas: dict) -> None:
    (vault / ".claude" / "om-jobs-state" / "last-run.json").write_text(
        json.dumps(entradas), encoding="utf-8"
    )


# --------------------------------------------------------------------------- allowlist


def test_allowlist_exata():
    """Job novo em `.scripts/jobs/` nasce invisível para o Telegram.

    Mesma razão do `test_diario_nunca_aparece` na consulta: descobrir os `.ps1` da pasta
    dispararia qualquer coisa que alguém deixasse lá. Entrar na lista é um diff deliberado.
    """
    assert [j.script for j in jobs.JOBS] == [
        "promover-noturno.ps1", "drive-meetings.ps1", "briefing-matinal.ps1",
    ]
    assert jobs.por_indice(1).chave == "promover"
    assert jobs.por_indice(0) is None
    assert jobs.por_indice(len(jobs.JOBS) + 1) is None


def test_intake_sozinho_nao_esta_na_lista():
    """Não há entrada que rode só o `/om-intake`: ele vem junto do promover, como à noite."""
    assert all("intake" not in j.script for j in jobs.JOBS)


# --------------------------------------------------------------------------- lock


def test_ocupado_nao_dispara(vault):
    _lock(vault, owner="drive-meetings")
    with patch.object(jobs.subprocess, "run") as run:
        texto = jobs.disparar(jobs.JOBS[0], notificar=MagicMock())
    run.assert_not_called()
    assert "Ocupado" in texto and "drive-meetings" in texto


def test_lock_e_lido_nunca_apagado(vault):
    """O bot não rouba o lock: quem decide é o script, que sai com 75."""
    _lock(vault)
    caminho = vault / ".claude" / "om-jobs-state" / "jobs.lock"
    jobs.disparar(jobs.JOBS[0], notificar=MagicMock())
    assert caminho.is_file()


def test_lock_ausente_libera_disparo(vault):
    assert jobs.lock_dono() is None
    with patch.object(jobs.threading, "Thread") as thread:
        texto = jobs.disparar(jobs.JOBS[0], notificar=MagicMock())
    thread.assert_called_once()
    assert "Promover capturas" in texto


def test_lock_ilegivel_nao_derruba(vault):
    (vault / ".claude" / "om-jobs-state" / "jobs.lock").write_text("{lixo", encoding="utf-8")
    assert jobs.lock_dono() is None


# --------------------------------------------------------------------------- execução


def test_executa_o_script_certo(vault):
    avisos = []
    _last_run(vault, {"promover": {"status": "ok", "custo_usd": 2.1},
                      "intake": {"status": "ok", "custo_usd": 5.3125}})
    with patch.object(jobs.subprocess, "run") as run, patch.object(jobs, "_pwsh", return_value="pwsh"):
        run.return_value = MagicMock(returncode=0, stderr="")
        jobs._executar(jobs.JOBS[0], avisos.append)
    argv = run.call_args[0][0]
    assert argv[0] == "pwsh" and argv[-1].endswith("promover-noturno.ps1")
    assert run.call_args[1]["cwd"] == str(vault)
    # O resumo cita os dois estados que o script noturno escreve.
    assert "promover: ok" in avisos[0] and "intake: ok" in avisos[0]
    assert "US$ 2,10" in avisos[0]


def test_exit_75_avisa_ocupado(vault):
    avisos = []
    _lock(vault, owner="briefing-matinal")
    with patch.object(jobs.subprocess, "run") as run, patch.object(jobs, "_pwsh", return_value="pwsh"):
        run.return_value = MagicMock(returncode=jobs.EXIT_OCUPADO, stderr="")
        jobs._executar(jobs.JOBS[0], avisos.append)
    assert "não rodou" in avisos[0] and "briefing-matinal" in avisos[0]


def test_falha_do_job_chega_no_chat(vault):
    avisos = []
    _last_run(vault, {"promover": {"status": "falha:api_error", "custo_usd": 0}})
    with patch.object(jobs.subprocess, "run") as run, patch.object(jobs, "_pwsh", return_value="pwsh"):
        run.return_value = MagicMock(returncode=1, stderr="boom")
        jobs._executar(jobs.JOBS[0], avisos.append)
    assert "falha:api_error" in avisos[0]


def test_resumo_nunca_sai(vault):
    """`last-run.json` vive numa pasta que o bot não lê por decisão: o `resumo` do
    `drive-meetings` cita os participantes das reuniões, incluindo sessão de terapia
    (ver Key Decisions, allowlist de pastas). Daqui saem `status`, `custo_usd` e `owner`,
    nunca `resumo` — nas duas pontas, sucesso e falha."""
    avisos = []
    _last_run(vault, {
        "promover": {"status": "ok", "custo_usd": 1.0, "resumo": "NOME-SENSIVEL esteve na reunião"},
        "intake": {"status": "falha:api_error", "custo_usd": 0, "resumo": "NOME-SENSIVEL de novo"},
    })
    with patch.object(jobs.subprocess, "run") as run, patch.object(jobs, "_pwsh", return_value="pwsh"):
        for codigo in (0, 1):
            run.return_value = MagicMock(returncode=codigo, stderr="")
            jobs._executar(jobs.JOBS[0], avisos.append)
            jobs._rodando = None
    assert avisos
    for aviso in avisos:
        assert "NOME-SENSIVEL" not in aviso
    assert "falha:api_error" in avisos[0]


def test_script_ausente_e_erro_legivel(vault):
    (vault / ".scripts" / "jobs" / "promover-noturno.ps1").unlink()
    with pytest.raises(jobs.JobError, match="promover-noturno.ps1"):
        jobs.disparar(jobs.JOBS[0], notificar=MagicMock())


def test_sem_vault_configurado(monkeypatch):
    monkeypatch.setattr(config, "VAULT_DIR", None)
    with pytest.raises(jobs.JobError, match="vault_dir"):
        jobs.disparar(jobs.JOBS[0], notificar=MagicMock())


def test_um_disparo_por_vez(vault):
    with patch.object(jobs.threading, "Thread"):
        primeiro = jobs.disparar(jobs.JOBS[0], notificar=MagicMock())
        segundo = jobs.disparar(jobs.JOBS[1], notificar=MagicMock())
    assert "começou" in primeiro
    assert "Já disparei" in segundo


# --------------------------------------------------------------------------- bot


@pytest.fixture
def bot(vault, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", vault / "state")
    telegram.save_secrets({"telegram_token": TOKEN, "telegram_chat_id": OWNER})
    return Bot(TOKEN)


def _msg(text: str) -> dict:
    return {"message": {"message_id": 1, "chat": {"id": OWNER}, "text": text,
                        "date": 0, "from": {"id": OWNER}}}


def _toque(dado: str) -> dict:
    return {"update_id": 2, "callback_query": {
        "id": "cb1", "data": dado, "from": {"id": OWNER},
        "message": {"message_id": 5, "chat": {"id": OWNER}, "date": 0},
    }}


def _metodos(call) -> list[str]:
    return [c.args[1] for c in call.call_args_list]


def test_barra_j_lista_em_botoes(bot):
    with patch.object(telegram, "call") as call:
        bot.handle_update(_msg("/j"))
    enviado = call.call_args[1]
    assert "Promover capturas" in enviado["text"] and "/j1" not in enviado["text"]
    dados = [b["callback_data"] for linha in enviado["reply_markup"]["inline_keyboard"] for b in linha]
    assert "j:1" in dados


def test_toque_no_job_pede_confirmacao_e_nao_dispara(bot):
    """Botão é fácil de tocar sem querer; promover leva 20 min e custa API."""
    with patch.object(telegram, "call") as call, patch.object(jobs, "disparar") as disparar:
        bot.handle_update(_toque("j:1"))
    disparar.assert_not_called()
    assert "editMessageText" in _metodos(call)
    edit = next(c for c in call.call_args_list if c.args[1] == "editMessageText").kwargs
    assert "Rodar agora" in str(edit["reply_markup"])


def test_confirmar_dispara(bot):
    with patch.object(telegram, "call") as call, patch.object(jobs, "disparar",
                                                              return_value="começou") as disparar:
        bot.handle_update(_toque("jr:1"))
    disparar.assert_called_once()
    assert disparar.call_args[0][0].chave == "promover"
    edit = next(c for c in call.call_args_list if c.args[1] == "editMessageText").kwargs
    assert "começou" in edit["text"]


def test_indice_inexistente_vira_aviso(bot):
    with patch.object(telegram, "call") as call:
        bot.handle_update(_toque("jr:9"))
    aviso = next(c for c in call.call_args_list if c.args[1] == "answerCallbackQuery").kwargs
    assert "não existe" in aviso["text"]
    assert "editMessageText" not in _metodos(call)


def test_erro_de_job_vira_aviso(bot):
    with patch.object(telegram, "call") as call, patch.object(
        jobs, "disparar", side_effect=jobs.JobError("sem pwsh aqui")
    ):
        bot.handle_update(_toque("jr:1"))
    aviso = next(c for c in call.call_args_list if c.args[1] == "answerCallbackQuery").kwargs
    assert "sem pwsh aqui" in aviso["text"]


def test_job_nao_toca_a_sessao_de_captura(bot):
    with patch.object(telegram, "call"), patch.object(jobs, "disparar", return_value="ok"):
        bot.handle_update(_toque("jr:1"))
    assert bot.session is None


def test_menu_publica_o_j():
    assert any(nome == "j" for nome, _ in telegram.MENU)
