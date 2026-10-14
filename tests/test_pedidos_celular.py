"""Pedidos do celular: só a allowlist, o estado volta à Semana e o fim não é coberto."""
from logos import jobs, organiza, pedidos_celular


def _semana(monkeypatch, novos):
    feitos = []

    def pedido(metodo, url, key, corpo=None):
        if metodo == "GET":
            return {"pedidos": novos}
        feitos.append((corpo["id"], corpo["estado"], corpo.get("resposta")))
        return {"ok": True}

    monkeypatch.setattr(organiza, "_pedido", pedido)
    return feitos


def test_job_fora_da_lista_vira_falha_sem_disparar(monkeypatch):
    feitos = _semana(monkeypatch, [{"id": "p-00000001", "job": "rm -rf"}])
    monkeypatch.setattr(jobs, "disparar", lambda *a, **k: (_ for _ in ()).throw(AssertionError("disparou")))
    assert pedidos_celular.passo("k") == 1
    assert feitos == [("p-00000001", "falha", "Este job não está na lista do PC.")]


def test_dispara_e_o_fim_chega_depois_como_ok(monkeypatch):
    feitos = _semana(monkeypatch, [{"id": "p-00000002", "job": "briefing"}])
    avisos = []

    def disparar(job, notificar=None):
        avisos.append(notificar)
        return f"{jobs.MARCA_JOB} <b>{job.titulo}</b> começou — <i>{job.espera}</i>."

    monkeypatch.setattr(jobs, "disparar", disparar)
    pedidos_celular.passo("k")
    assert [f[1] for f in feitos] == ["rodando", "rodando"]
    assert feitos[1][2] == "⚙️ Refazer o briefing começou — 1 a 5 min."  # sem HTML
    avisos[0](f"Refazer o briefing — 2 min\n{jobs.MARCA_OK} briefing: ok")
    assert feitos[-1][:2] == ("p-00000002", "ok")


def test_ocupado_e_falha_do_job(monkeypatch):
    feitos = _semana(monkeypatch, [{"id": "p-00000003", "job": "promover"}])
    monkeypatch.setattr(jobs, "disparar", lambda job, notificar=None: f"{jobs.MARCA_FALHA} Ocupado: <b>intake</b> começou 10:00.")
    pedidos_celular.passo("k")
    assert feitos[-1][1] == "ocupado" and "Ocupado: intake" in feitos[-1][2]

    feitos.clear()
    avisos = []
    monkeypatch.setattr(jobs, "disparar", lambda job, notificar=None: avisos.append(notificar) or "começou")
    pedidos_celular.passo("k")
    avisos[0](f"{jobs.MARCA_FALHA} Promover capturas: terminou com código 1")
    assert feitos[-1][1] == "falha"


def test_job_que_termina_antes_da_resposta_nao_volta_a_rodando(monkeypatch):
    feitos = _semana(monkeypatch, [{"id": "p-00000004", "job": "reunioes"}])

    def disparar(job, notificar=None):
        notificar(f"{jobs.MARCA_OK} drive-meetings: ok")  # terminou já
        return "começou"

    monkeypatch.setattr(jobs, "disparar", disparar)
    pedidos_celular.passo("k")
    assert [f[1] for f in feitos] == ["rodando", "ok"]
