"""LOG 8 (28/10/2026): os avisos com nome próprio (`logos.aviso`), o `logos.telegram` como apelido,
e o watcher e o drenador de pé sem a chave do bot no secrets.yaml (o token vai ser revogado)."""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import yaml

from logos import aviso, config, eventos, organiza, telegram

RAIZ = Path(__file__).resolve().parent.parent


def _cli(modulo: str, data_root: Path, *args: str) -> list[dict]:
    env = dict(os.environ, LOGOS_DATA_ROOT=str(data_root))
    r = subprocess.run([sys.executable, "-m", modulo, "send", *args], cwd=RAIZ, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    linhas = []
    for f in sorted((data_root / "state" / "eventos").glob("*.jsonl")):
        linhas += [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
    return linhas


def test_as_duas_linhas_de_comando_gravam_o_mesmo_evento(tmp_path):
    texto = "Job promover-noturno falhou: exit:1."
    novo = _cli("logos.aviso", tmp_path / "a", "--text", texto)
    velho = _cli("logos.telegram", tmp_path / "b", "--text", texto)
    assert len(novo) == len(velho) == 1
    sem_hora = [{k: v for k, v in e.items() if k != "em"} for e in (novo[0], velho[0])]
    assert sem_hora[0] == sem_hora[1] == {"origem": "jobs", "texto": texto, "nivel": "falha"}


def test_arquivo_entra_so_pelo_nome(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    assert aviso.main(["send", "--file", str(tmp_path / "Briefing 2026-10-28.md")]) == 0
    assert eventos.ultimos()[0]["texto"] == "Pronto: Briefing 2026-10-28.md"
    with pytest.raises(SystemExit):
        aviso.main(["send"])


def test_notify_registra_e_telegram_reexporta(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    assert aviso.notify("Logos: diário registrado.") is True
    assert eventos.ultimos()[0] == {**eventos.ultimos()[0], "origem": "logos", "nivel": "info"}
    assert telegram.registrar is aviso.registrar
    assert telegram.write_manifest is organiza.write_manifest and telegram.load_secrets is config.load_secrets


def _segredos_sem_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.delenv("LOGOS_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("SEMANA_KEY", raising=False)
    (tmp_path / "state").mkdir(parents=True)
    # Só a chave da Semana: nada de telegram_token nem telegram_chat_id.
    (tmp_path / "state" / "secrets.yaml").write_text(yaml.safe_dump({"semana_key": "k-de-teste"}), encoding="utf-8")


def test_drenador_sobe_sem_a_chave_do_bot(tmp_path, monkeypatch):
    _segredos_sem_bot(tmp_path, monkeypatch)
    assert organiza.chave() == "k-de-teste"
    pedidos = []
    monkeypatch.setattr(organiza, "_pedido", lambda m, u, k, c=None: pedidos.append(u) or {"capturas": []})
    monkeypatch.setattr(organiza, "caminho_chave_existe", lambda: False)
    monkeypatch.setattr(sys, "argv", ["organiza", "--uma-vez", "--inbox", str(tmp_path / "Inbox")])
    organiza.main()
    assert any("/pendentes" in u for u in pedidos)


def test_watcher_sobe_sem_a_chave_do_bot(tmp_path, monkeypatch):
    from logos import watcher
    _segredos_sem_bot(tmp_path, monkeypatch)

    class Observador:
        def schedule(self, *a, **k): pass
        def start(self): pass
        def stop(self): pass
        def join(self): pass

    chamadas = []
    monkeypatch.setattr(watcher, "Observer", Observador)
    monkeypatch.setattr(config, "inbox_dirs", lambda: [tmp_path / "Inbox"])
    monkeypatch.setattr(config, "ensure_directories", lambda: (tmp_path / "logs").mkdir(exist_ok=True))
    for nome in ("retry_failed", "scan_inbox", "expire_archive", "_espelhar_grava"):
        monkeypatch.setattr(watcher, nome, lambda *a, _n=nome, **k: chamadas.append(_n))
    monkeypatch.setattr(watcher.sono, "verificar", lambda: None)

    def para(_s):
        raise KeyboardInterrupt  # a primeira volta do laço encerra o watcher, como o Ctrl+C

    monkeypatch.setattr(watcher.time, "sleep", para)
    for ligado in (False, True):  # desligado (hoje) e, mesmo religado, sem token: não sobe o bot
        monkeypatch.setattr(telegram, "BOT_LIGADO", ligado)
        antes = threading.active_count()
        watcher.run()
        assert threading.active_count() == antes
    assert chamadas.count("scan_inbox") == 2
    assert (tmp_path / "logs" / "watcher.log").exists()
