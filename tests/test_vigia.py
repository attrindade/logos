"""Vigia e registro de eventos: o que a aba Logos do Organiza recebe."""
import json
import time
from datetime import date, datetime

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from logos import cofre, config, eventos, organiza, telegram, vigia


def _estado(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")


# --------------------------------------------------------------------------- eventos

def test_registrar_e_ler_do_mais_novo_ao_mais_velho(tmp_path, monkeypatch):
    _estado(tmp_path, monkeypatch)
    agora = time.time()
    eventos.registrar("logos", "primeiro", quando=agora - 600)
    eventos.registrar("vigia", "segundo", "falha", quando=agora)
    lidos = eventos.ultimos()
    assert [e["texto"] for e in lidos] == ["segundo", "primeiro"]
    assert lidos[0]["nivel"] == "falha" and lidos[0]["origem"] == "vigia"


def test_o_mesmo_texto_em_seguida_nao_duplica(tmp_path, monkeypatch):
    # Os jobs do vault tentam o Telegram 3 vezes: o registro fica com uma linha só.
    _estado(tmp_path, monkeypatch)
    agora = time.time()
    for n in range(3):
        eventos.registrar("jobs", "Job promover falhou: erro.", quando=agora + n)
    eventos.registrar("jobs", "Job promover falhou: erro.", quando=agora + eventos.REPETIDO_S + 5)
    assert len(eventos.ultimos()) == 2


def test_linha_estragada_e_nivel_desconhecido_nao_derrubam(tmp_path, monkeypatch):
    _estado(tmp_path, monkeypatch)
    eventos.registrar("x", "ok", "qualquer")
    p = eventos.pasta() / f"{date.today():%Y-%m-%d}.jsonl"
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"em": 1, "tex')
    lidos = eventos.ultimos()
    assert len(lidos) == 1 and lidos[0]["nivel"] == "info"


def test_limpar_apaga_so_os_dias_velhos(tmp_path, monkeypatch):
    _estado(tmp_path, monkeypatch)
    eventos.pasta().mkdir(parents=True)
    (eventos.pasta() / "2026-08-01.jsonl").write_text("", encoding="utf-8")
    (eventos.pasta() / "2026-10-14.jsonl").write_text("", encoding="utf-8")
    assert eventos.limpar(date(2026, 10, 15)) == 1
    assert [p.name for p in eventos.pasta().iterdir()] == ["2026-10-14.jsonl"]


def test_nivel_pelos_moldes_dos_avisos():
    assert eventos.nivel_de("Job promover-noturno falhou: erro.\nLog: x") == "falha"
    assert eventos.nivel_de("Logos: a.m4a virou nota sem enriquecimento.\nO Ollama não respondeu") == "atencao"
    # Título de nota não vira falha, mesmo com a palavra.
    assert eventos.nivel_de("Logos: Por que o plano falhou\nPróxima ação: rever") == "info"
    assert eventos.nivel_de("Logos: diário registrado.") == "info"


def test_aviso_do_telegram_entra_no_registro_mesmo_sem_bot(tmp_path, monkeypatch):
    _estado(tmp_path, monkeypatch)
    monkeypatch.setattr(telegram, "get_token", lambda: None)
    # Bot desligado (14/10): o registro é o canal, então o aviso conta como dado.
    assert telegram.notify("Logos: diário registrado.") is True
    assert eventos.ultimos()[0]["texto"] == "Logos: diário registrado."


def test_send_dos_jobs_entra_no_registro(tmp_path, monkeypatch):
    _estado(tmp_path, monkeypatch)
    monkeypatch.setattr(telegram, "get_token", lambda: None)
    telegram.send("Job promover falhou: timeout.", None)
    telegram.send(None, tmp_path / "Briefing 2026-10-15.md")
    textos = [e["texto"] for e in eventos.ultimos()]
    assert "Enviado: Briefing 2026-10-15.md" in textos
    assert any(e["nivel"] == "falha" for e in eventos.ultimos())


# --------------------------------------------------------------------------- checagens

def test_lancador_do_venv_e_o_filho_contam_como_uma_instancia():
    cmd = r'"C:\logos\.venv\Scripts\pythonw.exe" "C:\logos\main.py"'
    procs = [(10, 1, cmd), (11, 10, cmd), (20, 1, "outro.exe")]
    assert vigia.instancias(r"logos[\\/]main\.py", procs) == 1
    procs.append((30, 1, cmd))
    assert vigia.instancias(r"logos[\\/]main\.py", procs) == 2


def test_watcher_parado_e_watcher_duplicado(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATUS_PATH", tmp_path / "status.json")
    cmd = r"C:\logos\main.py"
    assert vigia.chk_watcher([], time.time())["estado"] == "falha"
    assert vigia.chk_watcher([(1, 0, cmd), (2, 0, cmd)], time.time())["estado"] == "falha"
    velho = datetime.fromtimestamp(time.time() - 900).isoformat()
    (tmp_path / "status.json").write_text(json.dumps({"heartbeat": velho}), encoding="utf-8")
    it = vigia.chk_watcher([(1, 0, cmd)], time.time())
    assert it["estado"] == "falha" and "15 min" in it["detalhe"]
    (tmp_path / "status.json").write_text(json.dumps({"heartbeat": datetime.now().isoformat()}), encoding="utf-8")
    assert vigia.chk_watcher([(1, 0, cmd)], time.time())["estado"] == "ok"


def test_tarefas_erro_atraso_e_codigos_que_nao_sao_erro():
    agora = datetime(2026, 10, 14, 21, 0).timestamp()
    lista = [
        {"nome": "Backup restic", "estado": "Ready", "ultima": "2026-10-13T23:00:01-03:00",
         "proxima": "2026-10-14T23:00:00-03:00", "resultado": 3},
        {"nome": "AHK atalho", "estado": "Running", "ultima": "2026-10-14T20:40:00-03:00",
         "proxima": "2026-10-14T20:50:00-03:00", "resultado": 2147946720},
        {"nome": "Briefing", "estado": "Ready", "ultima": "2026-10-13T08:00:00-03:00",
         "proxima": "2026-10-14T08:00:00-03:00", "resultado": 0},
        {"nome": "Desligada", "estado": "Disabled", "resultado": 1},
    ]
    itens = {i["nome"]: i for i in vigia.chk_tarefas(lista, agora)}
    assert itens["Backup restic"]["estado"] == "atencao" and "código 3" in itens["Backup restic"]["detalhe"]
    assert itens["AHK atalho"]["estado"] == "ok"
    assert itens["Briefing"]["estado"] == "atencao" and "Devia ter rodado" in itens["Briefing"]["detalhe"]
    assert "Desligada" not in itens
    assert vigia.chk_tarefas(None, agora)[0]["estado"] == "atencao"


def test_mudanca_de_estado_vira_evento_e_guarda_desde(tmp_path, monkeypatch):
    _estado(tmp_path, monkeypatch)
    t0 = time.time()
    vigia.registrar_mudancas([vigia._item("ollama", "Ollama", "ok")], [], t0)
    assert eventos.ultimos() == []  # primeira volta, tudo bem: nada a contar
    itens = vigia.registrar_mudancas([vigia._item("ollama", "Ollama", "falha", "Não responde.")], [], t0 + 60)
    assert itens[0]["desde"] == int(t0 + 60)
    assert eventos.ultimos()[0]["texto"] == "Ollama: Não responde."
    itens = vigia.registrar_mudancas([vigia._item("ollama", "Ollama", "falha", "Não responde.")], [], t0 + 120)
    assert itens[0]["desde"] == int(t0 + 60) and len(eventos.ultimos()) == 1
    vigia.registrar_mudancas([vigia._item("ollama", "Ollama", "ok")], [], t0 + 180)
    assert eventos.ultimos()[0]["texto"] == "Ollama: voltou ao normal."


def test_tarefa_que_rodou_vira_evento_e_a_primeira_leitura_nao(tmp_path, monkeypatch):
    _estado(tmp_path, monkeypatch)
    t = {"nome": "Vault briefing matinal", "ultima": "2026-10-14T08:00:00-03:00", "resultado": 0}
    vigia.registrar_mudancas([], [t], time.time())
    assert eventos.ultimos() == []
    t2 = dict(t, ultima="2026-10-15T08:00:00-03:00", resultado=1)
    vigia.registrar_mudancas([], [t2], time.time())
    e = eventos.ultimos(dias=3, hoje=date(2026, 10, 15))[0]
    assert e["texto"] == "Vault briefing matinal rodou e terminou com código 1." and e["nivel"] == "atencao"


# --------------------------------------------------------------------------- envio

def test_passo_fecha_para_o_celular_e_so_repete_quando_muda_ou_envelhece(tmp_path, monkeypatch):
    _estado(tmp_path, monkeypatch)
    cel = X25519PrivateKey.generate()
    monkeypatch.setattr(organiza, "chave_celular", lambda k: cofre.publica_b64(cel))
    monkeypatch.setattr(vigia, "montar", lambda agora: {"checagens": [vigia._item("aw", "ActivityWatch", "ok")]})
    monkeypatch.setattr(vigia, "_ultimo_doc", None)
    enviados = []
    monkeypatch.setattr(organiza, "_pedido", lambda m, u, k, c=None: enviados.append(c) or {"ok": True})
    eventos.registrar("logos", "Logos: Nota secreta")
    t0 = time.time()
    assert vigia.passo("k", t0) is True
    assert enviados[0]["chave"] == "logos" and "Nota secreta" not in enviados[0]["env"]
    doc = json.loads(cofre.abrir_do_pc(json.loads(enviados[0]["env"]), "logos", cel))
    assert doc["checagens"][0]["nome"] == "ActivityWatch"
    assert doc["eventos"][0]["texto"] == "Logos: Nota secreta" and doc["gerado_em"] == int(t0)
    assert vigia.passo("k", t0 + 60) is False  # nada mudou
    eventos.registrar("logos", "Logos: outra")
    assert vigia.passo("k", t0 + 120) is True  # evento novo vai na hora
    assert vigia.passo("k", t0 + 120 + vigia.PUBLICA_S) is True  # sinal de vida, mesmo sem mudança
    assert len(enviados) == 3


def test_sem_chave_do_celular_nao_manda(tmp_path, monkeypatch):
    _estado(tmp_path, monkeypatch)
    monkeypatch.setattr(organiza, "chave_celular", lambda k: None)
    monkeypatch.setattr(vigia, "montar", lambda agora: {"checagens": []})
    monkeypatch.setattr(vigia, "_ultimo_doc", None)
    assert vigia.passo("k") is None


def test_tarefa_frequente_so_entra_no_registro_quando_falha(tmp_path, monkeypatch):
    _estado(tmp_path, monkeypatch)
    t = {"nome": "AW Sync push", "ultima": "2026-10-14T20:35:00-03:00", "proxima": "2026-10-14T20:40:00-03:00", "resultado": 0}
    vigia.registrar_mudancas([], [t], time.time())
    vigia.registrar_mudancas([], [dict(t, ultima="2026-10-14T20:40:00-03:00", proxima="2026-10-14T20:45:00-03:00")], time.time())
    assert eventos.ultimos(hoje=date(2026, 10, 14)) == []
    vigia.registrar_mudancas([], [dict(t, ultima="2026-10-14T20:45:00-03:00", proxima="2026-10-14T20:50:00-03:00", resultado=1)], time.time())
    assert eventos.ultimos(hoje=date(2026, 10, 14))[0]["nivel"] == "atencao"


def test_watcher_processando_gravacao_longa_nao_e_falha(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATUS_PATH", tmp_path / "status.json")
    meia_hora = datetime.fromtimestamp(time.time() - 1800).isoformat()
    (tmp_path / "status.json").write_text(json.dumps({"heartbeat": meia_hora, "stage": "transcribing", "filename": "rpg.m4a"}), encoding="utf-8")
    it = vigia.chk_watcher([(1, 0, r"C:\logos\main.py")], time.time())
    assert it["estado"] == "ok" and "rpg.m4a" in it["detalhe"]
