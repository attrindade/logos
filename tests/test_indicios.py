"""Coletor de indícios (K §1.3, K·A14): repositório git e banco do Assistente falsos. Nada vai à rede."""
import json
import os
import sqlite3
import subprocess
from datetime import datetime

import pytest

from logos import config, indicios

FUSO = indicios.FUSO
DIA = "2026-10-26"
AGORA = datetime(2026, 10, 26, 18, 0, tzinfo=FUSO).timestamp()


def _semana(**extra):
    s = {
        "recursos": ["propostas"],
        "dias": [{"id": DIA, "blocos": [
            {"i": "10:00", "f": "10:20", "k": "leve", "nome": "WhatsApp", "hab": "whats"},
            {"i": "14:00", "f": "15:00", "k": "ren", "nome": "Renato", "tarefas": [
                {"id": f"{DIA}|14:00|aaa", "t": "Mandar ao Igor o relatório"}]},
            {"i": "15:00", "f": "17:00", "k": "gov", "nome": "Governa", "tarefas": [
                {"id": f"{DIA}|15:00|bbb", "t": "Corrigir o calendário"}]},
        ]}],
        "checks": {}, "propostas": [],
    }
    s.update(extra)
    return s


def _banco(tmp_path, com_mensagem=True):
    p = tmp_path / "assistente.db"
    con = sqlite3.connect(p)
    con.executescript("""
        CREATE TABLE mensagens (id TEXT PRIMARY KEY, jid TEXT, de_mim INTEGER, ts INTEGER, tipo TEXT,
                                texto TEXT, autor TEXT DEFAULT '', autor_jid TEXT DEFAULT '');
        CREATE TABLE conversas (jid TEXT PRIMARY KEY, nome TEXT DEFAULT '', apelido TEXT DEFAULT '');
        CREATE TABLE enviadas (id INTEGER PRIMARY KEY, jid TEXT, ts INTEGER, texto TEXT);
    """)
    con.execute("INSERT INTO conversas VALUES ('igor@s', 'Igor Silva', '')")
    if com_mensagem:
        ts = int(datetime(2026, 10, 26, 14, 12, tzinfo=FUSO).timestamp())
        con.execute("INSERT INTO mensagens VALUES ('m1', 'igor@s', 1, ?, 'texto', 'SEGREDO DA CONVERSA', '', '')", (ts,))
    con.execute("INSERT INTO mensagens VALUES ('m0', 'igor@s', 1, ?, 'texto', 'antes do bloco', '', '')",
                (int(datetime(2026, 10, 26, 9, 0, tzinfo=FUSO).timestamp()),))
    con.commit()
    con.close()
    return p


def _workspace(tmp_path, repo):
    w = tmp_path / "workspace.json"
    w.write_text(json.dumps({"pastas": [
        {"id": "govclima", "caminho": str(repo).replace("\\", "/"), "tipo": "repo"},
        {"id": "baradv-front", "caminho": str(tmp_path / "nao-existe"), "tipo": "repo"},
    ]}), encoding="utf-8")
    return w


def _repo_sem_nada(tmp_path):
    r = tmp_path / "govclima"
    r.mkdir()
    return r


SEM_SESSAO = {"sessao": lambda rid, a, b: 0.0, "arquivos": lambda repo, a, b: 0}


def test_a14_mensagem_ao_igor_vira_proposta_de_forca_3_sem_o_texto(tmp_path):
    banco = _banco(tmp_path)
    ws = _workspace(tmp_path, _repo_sem_nada(tmp_path))
    props = indicios.coletar(_semana(), AGORA, ws, banco, pessoas=["Igor Silva", "Renato Bar"], fontes=SEM_SESSAO)
    assert props == [{"fonte": "whats", "forca": 3, "evidencia": "Você mandou mensagem ao Igor às 14h12",
                      "tarefa": f"{DIA}|14:00|aaa"}]
    assert "SEGREDO" not in json.dumps(props)


def test_a14_sem_commit_nem_mensagem_nao_propoe_nada(tmp_path):
    banco = _banco(tmp_path, com_mensagem=False)
    ws = _workspace(tmp_path, _repo_sem_nada(tmp_path))
    assert indicios.coletar(_semana(), AGORA, ws, banco, pessoas=["Igor Silva"], fontes=SEM_SESSAO) == []


@pytest.mark.skipif(not __import__("shutil").which("git"), reason="sem git")
def test_commit_no_repositorio_do_bloco_vira_forca_2_com_reforco_da_sessao(tmp_path):
    repo = _repo_sem_nada(tmp_path)
    env = dict(os.environ, GIT_AUTHOR_DATE="2026-10-26T15:40:00-03:00", GIT_COMMITTER_DATE="2026-10-26T15:40:00-03:00",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "título privado"], check=True, env=env)
    ws = _workspace(tmp_path, repo)
    fontes = {"sessao": lambda rid, a, b: 6600.0 if rid == "govclima" else 0.0, "arquivos": lambda r, a, b: 0}
    props = indicios.coletar(_semana(), AGORA, ws, tmp_path / "sem-banco.db", pessoas=[], fontes=fontes)
    assert props == [{"fonte": "git", "forca": 2, "tarefa": f"{DIA}|15:00|bbb",
                      "evidencia": "1 commit no govclima às 15h40 e 1h50 de sessão do Claude no govclima"}]
    assert "título privado" not in json.dumps(props)


def test_sessao_sozinha_nao_propoe_e_arquivos_propoem(tmp_path):
    ws = _workspace(tmp_path, _repo_sem_nada(tmp_path))
    so_sessao = {"sessao": lambda rid, a, b: 9000.0, "arquivos": lambda r, a, b: 3, "commits": lambda r, a, b: []}
    assert indicios.coletar(_semana(), AGORA, ws, tmp_path / "x.db", pessoas=[], fontes=so_sessao) == []
    arquivos = dict(so_sessao, arquivos=lambda r, a, b: 38 if r.name == "govclima" else 0)
    props = indicios.coletar(_semana(), AGORA, ws, tmp_path / "x.db", pessoas=[], fontes=arquivos)
    assert [(p["fonte"], p["forca"]) for p in props] == [("arquivos", 2)]
    assert props[0]["evidencia"].startswith("38 arquivos mudados no govclima no dia")


def test_marcada_com_proposta_aberta_ou_ja_mandada_e_o_limite_de_12(tmp_path):
    banco = _banco(tmp_path)
    ws = _workspace(tmp_path, _repo_sem_nada(tmp_path))
    pessoas = ["Igor Silva"]
    tid = f"{DIA}|14:00|aaa"
    assert indicios.coletar(_semana(checks={tid: True}), AGORA, ws, banco, pessoas, SEM_SESSAO) == []
    assert indicios.coletar(_semana(propostas=[{"tarefa": tid}]), AGORA, ws, banco, pessoas, SEM_SESSAO) == []
    assert indicios.coletar(_semana(), AGORA, ws, banco, pessoas, SEM_SESSAO, ja={f"whats:{tid}"}) == []
    cheias = [{"tarefa": f"x{i}"} for i in range(12)]
    assert indicios.coletar(_semana(propostas=cheias), AGORA, ws, banco, pessoas, SEM_SESSAO) == []


def test_banco_aberto_so_para_leitura(tmp_path):
    banco = _banco(tmp_path)
    antes = banco.stat().st_mtime_ns
    assert indicios.mensagem_ao_contato(banco, "Igor", 0) is not None
    assert banco.stat().st_mtime_ns == antes
    assert indicios.mensagem_ao_contato(tmp_path / "nao.db", "Igor", 0) is None


def test_desligado_de_fabrica_e_api_antiga_nao_fazem_nada(monkeypatch):
    monkeypatch.setattr(config, "INDICIOS_LIGADO", False)
    assert indicios.passo("k", AGORA, http=lambda *a, **k: pytest.fail("não devia ir à rede")) is None
    monkeypatch.setattr(config, "INDICIOS_LIGADO", True)
    monkeypatch.setattr(indicios, "_ultima", 0.0)
    assert indicios.passo("k", AGORA, http=lambda m, c, k, corpo=None: {"dias": []}) is None  # sem recursos
    monkeypatch.setattr(indicios, "_ultima", 0.0)
    noite = datetime(2026, 10, 26, 23, 30, tzinfo=FUSO).timestamp()
    assert indicios.passo("k", noite, http=lambda *a, **k: pytest.fail("fora da janela")) == 0


def test_passo_ligado_manda_as_propostas_e_lembra(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INDICIOS_LIGADO", True)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(indicios, "_ultima", 0.0)
    monkeypatch.setattr(indicios, "coletar", lambda semana, agora, ja=None: [
        {"fonte": "whats", "forca": 3, "evidencia": "e", "tarefa": "t1"}] if "whats:t1" not in (ja or set()) else [])
    mandados = []

    def http(metodo, caminho, key, corpo=None):
        if metodo == "GET":
            return _semana()
        mandados.append((caminho, corpo))
        return {"ok": True}

    assert indicios.passo("k", AGORA, http=http) == 1
    assert mandados == [("/api/propostas", {"fonte": "whats", "forca": 3, "evidencia": "e", "tarefa": "t1"})]
    monkeypatch.setattr(indicios, "_ultima", 0.0)
    assert indicios.passo("k", AGORA + 3600, http=http) == 0


def test_contatos_e_repos_do_tipo(tmp_path):
    assert indicios.contatos_da_tarefa("Mandar ao Ígor o relatório", ["Ígor Silva", "Bruna X", "Al Y"]) == ["Ígor"]
    ws = _workspace(tmp_path, tmp_path / "govclima")
    assert [r for r, _ in indicios.repos_do_tipo("gov", ws)] == ["govclima"]
    assert indicios.repos_do_tipo("leve", ws) == []
