"""Fila do WhatsApp para o celular: o que vai, fechado, e só quando muda."""
import json

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from logos import cofre, config, organiza, whatsapp_celular

FILA = {"voce": [{"jid": "5551999990000@s.whatsapp.net", "nome": "Celso", "novas": 2, "desde": 10, "previa": "oi"}],
        "rapida": [], "grupo": [{"jid": f"{i}@g.us", "nome": f"G{i}", "novas": 1, "desde": 5, "previa": "x"} for i in range(12)]}
CONVERSA = {"sugestao_fresca": True, "sugestao": {"resumo": "quer marcar", "falta_saber": ["dia"],
            "sugestoes": [{"rotulo": "a", "texto": "1"}, {"rotulo": "b", "texto": "2"}, {"rotulo": "c", "texto": "3"}, {"rotulo": "d", "texto": "4"}]},
            "mensagens": [{"id": str(i), "de_mim": i % 2 == 0, "autor": "Celso", "ts": 100 + i, "tipo": "texto",
                           "texto": f"m{i}", "transcricao": "", "duracao_s": 0} for i in range(30)]
            + [{"id": "a1", "de_mim": False, "autor": "Celso", "ts": 200, "tipo": "audio", "texto": "",
                "transcricao": "vamos sexta?", "duracao_s": 7},
               {"id": "a2", "de_mim": False, "autor": "Celso", "ts": 201, "tipo": "audio", "texto": "",
                "transcricao": "", "duracao_s": 3},
               {"id": "f1", "de_mim": False, "autor": "Celso", "ts": 202, "tipo": "imagem", "texto": "a sala",
                "transcricao": "", "duracao_s": 0}]}
HISTORICO = {"respondidas": [{"jid": "5551888880000@s.whatsapp.net", "nome": "Lorena", "ts": 50, "texto": "combinado"},
                             {"jid": "5551888880000@s.whatsapp.net", "nome": "Lorena", "ts": 40, "texto": "antes"},
                             {"jid": "5551999990000@s.whatsapp.net", "nome": "Celso", "ts": 60, "texto": "já na fila"}],
             "adiadas": [{"jid": "123@g.us", "nome": "Grupo", "ate": 999}]}


def _falso_get(caminho):
    if caminho == "/api/fila":
        return FILA
    if caminho == "/api/historico":
        return HISTORICO
    return CONVERSA


def test_montar_leva_resumo_tres_sugestoes_e_numero_so_de_conversa_individual(monkeypatch):
    monkeypatch.setattr(whatsapp_celular, "_get", _falso_get)
    d = whatsapp_celular.montar()
    fila = [i for i in d["itens"] if i["secao"] in ("voce", "rapida", "grupo")]
    assert len(fila) == 1 + whatsapp_celular.MAX_GRUPOS and d["grupos_total"] == 12
    celso = d["itens"][0]
    assert celso["numero"] == "5551999990000" and celso["resumo"] == "quer marcar"
    assert [s["texto"] for s in celso["sugestoes"]] == ["1", "2", "3"]
    assert all(i["numero"] is None for i in fila[1:])


def test_montar_leva_as_ultimas_mensagens_com_a_transcricao_e_sem_arquivo(monkeypatch):
    monkeypatch.setattr(whatsapp_celular, "_get", _falso_get)
    msgs = whatsapp_celular.montar()["itens"][0]["mensagens"]
    assert len(msgs) == whatsapp_celular.MAX_MENSAGENS
    audio, sem_transcricao, foto = msgs[-3:]
    assert audio == {"de_mim": False, "autor": "Celso", "ts": 200, "tipo": "audio", "texto": "vamos sexta?",
                     "duracao": 7, "transcrito": True}
    assert sem_transcricao["texto"] == "" and sem_transcricao["transcrito"] is False
    assert foto["tipo"] == "imagem" and foto["texto"] == "a sala" and "id" not in foto


def test_montar_traz_respondidas_e_adiadas_uma_vez_e_sem_repetir_a_fila(monkeypatch):
    monkeypatch.setattr(whatsapp_celular, "_get", _falso_get)
    itens = whatsapp_celular.montar()["itens"]
    resp = [i for i in itens if i["secao"] == "respondida"]
    adiadas = [i for i in itens if i["secao"] == "adiada"]
    assert [i["nome"] for i in resp] == ["Lorena"]  # a mais nova; Celso já está na fila
    assert resp[0]["previa"] == "combinado" and resp[0]["numero"] == "5551888880000" and resp[0]["mensagens"]
    assert [(i["nome"], i["desde"], i["numero"]) for i in adiadas] == [("Grupo", 999, None)]


def test_passo_fecha_para_o_celular_e_nao_repete_o_mesmo(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    cel = X25519PrivateKey.generate()
    monkeypatch.setattr(organiza, "chave_celular", lambda k: cofre.publica_b64(cel))
    monkeypatch.setattr(whatsapp_celular, "_get", _falso_get)
    enviados = []
    monkeypatch.setattr(organiza, "_pedido", lambda m, u, k, c=None: enviados.append(c) or {"ok": True})
    assert whatsapp_celular.passo("k") is True
    env = json.loads(enviados[0]["env"])
    assert "Celso" not in enviados[0]["env"]
    assert json.loads(cofre.abrir_do_pc(env, "whatsapp", cel))["itens"][0]["nome"] == "Celso"
    assert whatsapp_celular.passo("k") is False and len(enviados) == 1


def test_assistente_fora_do_ar_nao_manda(monkeypatch):
    monkeypatch.setattr(organiza, "chave_celular", lambda k: "x")
    monkeypatch.setattr(whatsapp_celular, "_get", lambda c: (_ for _ in ()).throw(ConnectionRefusedError()))
    assert whatsapp_celular.passo("k") is None


def test_conversa_que_falha_nao_manda_fila_pela_metade(monkeypatch):
    monkeypatch.setattr(organiza, "chave_celular", lambda k: "x")

    def meio(c):
        if c == "/api/fila":
            return FILA
        raise TimeoutError()

    monkeypatch.setattr(whatsapp_celular, "_get", meio)
    assert whatsapp_celular.passo("k") is None
