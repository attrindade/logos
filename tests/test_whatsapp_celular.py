"""Fila do WhatsApp para o celular: o que vai, fechado, e só quando muda."""
import json

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from logos import cofre, config, organiza, whatsapp_celular

FILA = {"voce": [{"jid": "5551999990000@s.whatsapp.net", "nome": "Celso", "novas": 2, "desde": 10, "previa": "oi"}],
        "rapida": [], "grupo": [{"jid": f"{i}@g.us", "nome": f"G{i}", "novas": 1, "desde": 5, "previa": "x"} for i in range(12)]}
CONVERSA = {"sugestao_fresca": True, "sugestao": {"resumo": "quer marcar", "falta_saber": ["dia"],
            "sugestoes": [{"rotulo": "a", "texto": "1"}, {"rotulo": "b", "texto": "2"}, {"rotulo": "c", "texto": "3"}, {"rotulo": "d", "texto": "4"}]}}


def _falso_get(caminho):
    return FILA if caminho == "/api/fila" else CONVERSA


def test_montar_leva_resumo_tres_sugestoes_e_numero_so_de_conversa_individual(monkeypatch):
    monkeypatch.setattr(whatsapp_celular, "_get", _falso_get)
    d = whatsapp_celular.montar()
    assert len(d["itens"]) == 1 + whatsapp_celular.MAX_GRUPOS and d["grupos_total"] == 12
    celso = d["itens"][0]
    assert celso["numero"] == "5551999990000" and celso["resumo"] == "quer marcar"
    assert [s["texto"] for s in celso["sugestoes"]] == ["1", "2", "3"]
    assert all(i["numero"] is None for i in d["itens"][1:])


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
