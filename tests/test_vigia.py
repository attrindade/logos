"""Vigia e registro de eventos: o que a aba Logos do Organiza recebe."""
import json
import time
from datetime import date, datetime, timedelta

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
    assert "Pronto: Briefing 2026-10-15.md" in textos
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
    t0 = time.time() - 3600  # no passado: t0 + minutos não pode cair no arquivo de amanhã (perto da meia-noite)
    vigia.registrar_mudancas([vigia._item("ollama", "Ollama", "ok")], [], t0)
    assert eventos.ultimos() == []  # primeira volta, tudo bem: nada a contar
    itens = vigia.registrar_mudancas([vigia._item("ollama", "Ollama", "falha", "Não responde.")], [], t0 + 60)
    assert itens[0]["desde"] == int(t0 + 60)
    # falha entra na hora (revisão de 25/10, M2); só a atenção espera a 2ª checagem
    assert eventos.ultimos()[0]["texto"] == "Ollama: Não responde." and len(eventos.ultimos()) == 1
    itens = vigia.registrar_mudancas([vigia._item("ollama", "Ollama", "falha", "Não responde.")], [], t0 + 120)
    assert itens[0]["desde"] == int(t0 + 60)
    assert len(eventos.ultimos()) == 1
    vigia.registrar_mudancas([vigia._item("ollama", "Ollama", "falha", "Não responde.")], [], t0 + 150)
    assert len(eventos.ultimos()) == 1
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
    t0 = time.time() - 3600  # no passado: t0 + minutos não pode cair no arquivo de amanhã (perto da meia-noite)
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


# --------------------------------------------------------------------------- ruído de boot (25/10)

def test_ruido_de_boot_nao_vira_atencao_e_os_reais_continuam():
    agora = datetime(2026, 10, 25, 16, 20).timestamp()
    lista = [
        # O Windows encerrou no desligamento (0x40010004).
        {"nome": "AW Alertas", "estado": "Ready", "ultima": "2026-10-24T09:30:00-03:00",
         "proxima": None, "resultado": 1073807364},
        # Perdeu a trava dos jobs do vault no boot.
        {"nome": "Vault briefing matinal", "estado": "Ready", "ultima": "2026-10-25T16:03:37-03:00",
         "proxima": "2026-10-26T08:00:00-03:00", "resultado": 75},
        # 75 numa tarefa que não usa a trava continua sendo erro.
        {"nome": "Outra coisa", "estado": "Ready", "ultima": "2026-10-25T16:03:37-03:00",
         "proxima": "2026-10-26T08:00:00-03:00", "resultado": 75},
        # Os reais: restic com instantâneo incompleto.
        {"nome": "Backup restic", "estado": "Ready", "ultima": "2026-10-24T23:00:01-03:00",
         "proxima": "2026-10-25T23:00:00-03:00", "resultado": 3},
    ]
    itens = {i["nome"]: i for i in vigia.chk_tarefas(lista, agora)}
    assert itens["AW Alertas"]["estado"] == "ok" and "desligamento" in itens["AW Alertas"]["detalhe"]
    assert itens["Vault briefing matinal"]["estado"] == "ok" and "75" in itens["Vault briefing matinal"]["detalhe"]
    assert itens["Outra coisa"]["estado"] == "atencao"
    assert itens["Backup restic"]["estado"] == "atencao" and "código 3" in itens["Backup restic"]["detalhe"]


def test_ruido_de_boot_nao_entra_como_atencao_no_registro(tmp_path, monkeypatch):
    _estado(tmp_path, monkeypatch)
    base = [{"nome": "AW Alertas", "ultima": "2026-10-24T09:30:00-03:00", "resultado": 0},
            {"nome": "Vault promover noturno", "ultima": "2026-10-24T22:30:00-03:00", "resultado": 0},
            {"nome": "Backup restic", "ultima": "2026-10-24T23:00:00-03:00", "resultado": 0}]
    vigia.registrar_mudancas([], base, time.time())
    boot = [dict(base[0], ultima="2026-10-25T16:01:00-03:00", resultado=1073807364),
            dict(base[1], ultima="2026-10-25T16:03:37-03:00", resultado=75),
            dict(base[2], ultima="2026-10-25T16:03:37-03:00", resultado=3)]
    vigia.registrar_mudancas([], boot, time.time())
    evs = eventos.ultimos(dias=3, hoje=date(2026, 10, 25))
    assert not [e for e in evs if "AW Alertas" in e["texto"]]  # desligamento: nem entra
    promover = [e for e in evs if "promover" in e["texto"]]
    assert len(promover) == 1 and promover[0]["nivel"] == "info"
    restic = [e for e in evs if "restic" in e["texto"]]
    assert len(restic) == 1 and restic[0]["nivel"] == "atencao" and "código 3" in restic[0]["texto"]


def test_ollama_subindo_no_boot_nao_e_falha(monkeypatch):
    monkeypatch.setattr(vigia, "_get_json", lambda *a, **k: (_ for _ in ()).throw(OSError("recusada")))
    agora = time.time()
    assert vigia.chk_ollama(agora, boot=agora - 120)["estado"] == "ok"
    assert "Subindo" in vigia.chk_ollama(agora, boot=agora - 120)["detalhe"]
    # Passado o prazo, volta a ser falha de verdade.
    assert vigia.chk_ollama(agora, boot=agora - vigia.BOOT_GRACA_S - 60)["estado"] == "falha"
    monkeypatch.setattr(vigia, "_get_json", lambda *a, **k: {"version": "x"})
    assert vigia.chk_ollama(agora, boot=agora - 120) == vigia._item("ollama", "Ollama", "ok", "")


def test_syncthing_do_celular_sem_conectar_continua_visivel(monkeypatch):
    agora = time.time()
    procs = [(1, 0, r"C:\syncthing\syncthing.exe")]
    monkeypatch.setattr(vigia, "_syncthing_api", lambda: ("http://x", "chave", {"EU": "PC", "CEL": "Celular"}))
    visto = datetime.fromtimestamp(agora - 30 * 3600).isoformat()
    respostas = {"/rest/system/status": {"myID": "EU"}, "/rest/system/connections": {"connections": {}},
                 "/rest/stats/device": {"CEL": {"lastSeen": visto}}, "/rest/system/error": {"errors": []}}
    monkeypatch.setattr(vigia, "_get_json", lambda url, h=None, timeout=4: respostas[url.replace("http://x", "")])
    itens = {i["id"]: i for i in vigia.chk_syncthing(procs, agora, boot=agora - 48 * 3600)}
    assert itens["sync:Celular"]["estado"] == "atencao" and "Sem conectar" in itens["sync:Celular"]["detalhe"]


# --------------------------------------------------------------------------- rodada de 25/10/2026 (noite)

def _sync_falso(monkeypatch, visto_ts, conectado=False):
    monkeypatch.setattr(vigia, "_syncthing_api", lambda: ("http://x", "chave", {"EU": "PC", "CEL": "Celular"}))
    respostas = {"/rest/system/status": {"myID": "EU"},
                 "/rest/system/connections": {"connections": {"CEL": {"connected": conectado}}},
                 "/rest/stats/device": {"CEL": {"lastSeen": datetime.fromtimestamp(visto_ts).isoformat()}},
                 "/rest/system/error": {"errors": []}}
    monkeypatch.setattr(vigia, "_get_json", lambda url, h=None, timeout=4: respostas[url.replace("http://x", "")])
    return [(1, 0, r"C:\syncthing\syncthing.exe")]


def test_syncthing_pc_desligado_nao_conta_caso_real_de_24_25_10(monkeypatch):
    # PC desligado de 24/10 10:02 a 25/10 15:58, checado às 15:59: não pode dar atenção.
    visto = datetime(2026, 10, 24, 10, 1).timestamp()
    boot = datetime(2026, 10, 25, 15, 58).timestamp()
    agora = datetime(2026, 10, 25, 15, 59).timestamp()
    procs = _sync_falso(monkeypatch, visto)
    it = {i["id"]: i for i in vigia.chk_syncthing(procs, agora, boot=boot)}["sync:Celular"]
    assert it["estado"] == "ok" and it["detalhe"] == "PC ligado há 60 s; esperando o celular."


def test_syncthing_depois_da_graca_sem_aparecer_desde_o_boot_e_atencao(monkeypatch):
    visto = datetime(2026, 10, 24, 10, 1).timestamp()
    boot = datetime(2026, 10, 25, 15, 58).timestamp()
    procs = _sync_falso(monkeypatch, visto)
    # 3 h depois do boot sem o celular: passou a graça, é atenção (revisão de 25/10, A1)
    it = vigia.chk_syncthing(procs, boot + 3 * 3600, boot=boot)[1]
    assert it["estado"] == "atencao"
    assert it["detalhe"] == "Não apareceu desde que o PC ligou, há 3 h; visto pela última vez há 32 h."
    it = vigia.chk_syncthing(procs, boot + 25 * 3600, boot=boot)[1]
    assert it["estado"] == "atencao" and it["detalhe"].startswith("Não apareceu desde que o PC ligou, há 25 h;")
    # último contato acima de 72 h: "sem conectar", com o boot no meio ou não
    it = vigia.chk_syncthing(procs, boot + 43 * 3600, boot=boot)[1]
    assert it == vigia._item("sync:Celular", "Syncthing: Celular", "atencao", "Sem conectar há 3 dias.")


def test_syncthing_pc_ligado_10_h_por_dia_por_5_dias_sem_o_celular(monkeypatch):
    # Cenário da revisão (A1): o PC liga às 8h e desliga às 18h; o celular apareceu pela última vez
    # no 1º dia às 9h. Antes o boot zerava a conta todo dia e o item ficava verde para sempre.
    dia1 = datetime(2026, 10, 19, 8, 0)
    procs = _sync_falso(monkeypatch, (dia1 + timedelta(hours=1)).timestamp())
    estados = {}
    for d in range(5):
        boot = (dia1 + timedelta(days=d)).timestamp()
        for h in (0.5, 1.6, 3, 9.9):  # 8h30 (graça), 9h36, 11h, 17h54
            it = vigia.chk_syncthing(procs, boot + h * 3600, boot=boot)[1]
            estados[(d + 1, h)] = it["estado"]
    assert estados[(1, 3)] == "ok" and estados[(1, 9.9)] == "ok"  # visto depois do boot, há < 24 h
    assert estados[(2, 0.5)] == "ok"  # graça de 90 min do 2º boot
    assert estados[(2, 1.6)] == "atencao" and estados[(2, 9.9)] == "atencao"
    assert all(estados[(d, h)] == "atencao" for d in (3, 5) for h in (1.6, 3, 9.9))
    # 4º dia às 8h30: 71,5 h desde o último contato, ainda na graça; 5º dia às 8h30: 95,5 h, atenção
    # mesmo dentro da graça do boot
    assert estados[(4, 0.5)] == "ok" and estados[(5, 0.5)] == "atencao"


def test_syncthing_visto_depois_do_boot_segue_a_regra_de_antes(monkeypatch):
    # Relógio congelado (revisão de 25/10, B5): com time.time() a fração arredondada no isoformat do
    # lastSeen falso às vezes dava 1h59m59s, e o "2 h" virava "1 h".
    boot = datetime(2026, 10, 24, 8, 0).timestamp()
    procs = _sync_falso(monkeypatch, boot + 3600)
    it = vigia.chk_syncthing(procs, boot + 3 * 3600, boot=boot)[1]
    assert it == vigia._item("sync:Celular", "Syncthing: Celular", "ok", "Visto há 2 h.")
    it = vigia.chk_syncthing(procs, boot + 3600 + 25 * 3600, boot=boot)[1]
    assert it["estado"] == "atencao" and it["detalhe"] == "Sem conectar há 25 h."


def test_aw_subindo_no_boot_nao_e_falha(monkeypatch):
    monkeypatch.setattr(vigia, "_get_json", lambda *a, **k: (_ for _ in ()).throw(OSError("recusada")))
    agora = datetime(2026, 10, 25, 16, 0).timestamp()
    it = vigia.chk_aw(agora, boot=agora - 120)
    assert it["estado"] == "ok" and "Subindo" in it["detalhe"]
    assert vigia.chk_aw(agora, boot=agora - vigia.BOOT_GRACA_S - 60)["estado"] == "falha"


def test_atencao_de_uma_checagem_so_nao_entra_no_registro(tmp_path, monkeypatch):
    # Uma atenção numa checagem e ok na seguinte (5 min) -> nada no registro.
    _estado(tmp_path, monkeypatch)
    t0 = time.time() - 3600  # no passado: t0 + minutos não pode cair no arquivo de amanhã (perto da meia-noite)
    ok = [vigia._item("aw", "ActivityWatch", "ok")]
    ruim = [vigia._item("aw", "ActivityWatch", "atencao", "O observador de janelas parou há 21 min.")]
    vigia.registrar_mudancas(ok, [], t0)
    vigia.registrar_mudancas(ruim, [], t0 + 300)
    vigia.registrar_mudancas(ok, [], t0 + 600)
    assert eventos.ultimos() == []
    # na primeira leitura de todas (estado vazio) também espera a segunda
    _estado(tmp_path / "b", monkeypatch)
    vigia.registrar_mudancas([vigia._item("aw", "ActivityWatch", "atencao", "x")], [], t0)
    assert eventos.ultimos() == []
    vigia.registrar_mudancas([vigia._item("aw", "ActivityWatch", "atencao", "x")], [], t0 + 300)
    assert [e["texto"] for e in eventos.ultimos()] == ["ActivityWatch: x"]
    vigia.registrar_mudancas(ok, [], t0 + 600)
    assert eventos.ultimos()[0]["texto"] == "ActivityWatch: voltou ao normal."


def test_falha_entra_na_hora_mesmo_alternando_com_ok(tmp_path, monkeypatch):
    # Revisão de 25/10, M2: o Assistente responde uma checagem sim, outra não.
    _estado(tmp_path, monkeypatch)
    t0 = time.time() - 3600  # no passado: t0 + minutos não pode cair no arquivo de amanhã (perto da meia-noite)
    ok = [vigia._item("assistente", "Assistente", "ok")]
    ruim = [vigia._item("assistente", "Assistente", "falha", "Não responde.")]
    vigia.registrar_mudancas(ok, [], t0)
    for k in range(1, 5):
        vigia.registrar_mudancas(ruim if k % 2 else ok, [], t0 + k * 300)
    assert [e["texto"] for e in eventos.ultimos()] == [
        "Assistente: voltou ao normal.", "Assistente: Não responde.",
        "Assistente: voltou ao normal.", "Assistente: Não responde."]


def test_atencao_e_falha_alternando_entram_no_registro(tmp_path, monkeypatch):
    # Antes, cada troca zerava a contagem e nada chegava ao registro.
    _estado(tmp_path, monkeypatch)
    t0 = time.time() - 3600  # no passado: t0 + minutos não pode cair no arquivo de amanhã (perto da meia-noite)
    a = [vigia._item("w", "Watcher", "atencao", "parou de dar sinal")]
    f = [vigia._item("w", "Watcher", "falha", "não está rodando")]
    vigia.registrar_mudancas(a, [], t0)
    assert eventos.ultimos() == []  # atenção sozinha espera
    vigia.registrar_mudancas(f, [], t0 + 300)
    vigia.registrar_mudancas(a, [], t0 + 600)
    vigia.registrar_mudancas(f, [], t0 + 900)
    assert [e["texto"] for e in eventos.ultimos()] == [
        "Watcher: não está rodando", "Watcher: parou de dar sinal", "Watcher: não está rodando"]
    # a sequência ruim conta atenção e falha juntas: atenção depois de 2 ruins não registrados também entra
    _estado(tmp_path / "b", monkeypatch)
    estado = config.STATE_DIR / "vigia.json"
    estado.parent.mkdir(parents=True, exist_ok=True)
    estado.write_text(json.dumps({"estados": {"w": {"estado": "falha", "desde": 1, "reg": False, "n": 1}},
                                  "tarefas": {}}), encoding="utf-8")
    vigia.registrar_mudancas(a, [], t0)
    assert [e["texto"] for e in eventos.ultimos()] == ["Watcher: parou de dar sinal"]


def test_problema_registrado_que_muda_de_nivel_entra_na_hora(tmp_path, monkeypatch):
    _estado(tmp_path, monkeypatch)
    t0 = time.time() - 3600  # no passado: t0 + minutos não pode cair no arquivo de amanhã (perto da meia-noite)
    for k in range(2):
        vigia.registrar_mudancas([vigia._item("w", "Watcher", "atencao", "a")], [], t0 + k)
    vigia.registrar_mudancas([vigia._item("w", "Watcher", "falha", "b")], [], t0 + 10)
    assert [e["texto"] for e in eventos.ultimos()] == ["Watcher: b", "Watcher: a"]


def test_estado_antigo_sem_reg_ainda_registra_a_volta(tmp_path, monkeypatch):
    # vigia.json escrito pelo código de antes (sem "reg"): a atenção já estava no registro.
    _estado(tmp_path, monkeypatch)
    p = config.STATE_DIR / "vigia.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"estados": {"tarefa:Backup restic": {"estado": "atencao", "desde": 1}}, "tarefas": {}}),
                 encoding="utf-8")
    vigia.registrar_mudancas([vigia._item("tarefa:Backup restic", "Backup restic", "ok")], [], time.time())
    assert eventos.ultimos()[0]["texto"] == "Backup restic: voltou ao normal."
