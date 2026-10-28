"""Captura vazia (28/10/2026): toque sem fala ou Diário largado não vira "falha definitiva".

Os dois Diários presos (15/10 e 28/10) somavam ~470 bytes de áudio. A regra nova: sem texto, sem
anexo e menos de VAZIO_BYTES de voz → evento `info`, nada em falhas.json e, com a Semana que anuncia
`descartada` em `recursos`, esse estado final (que não apaga nada no D1).
"""
import base64
import json
import logging

import pytest

from logos import config, cofre, eventos, organiza


@pytest.fixture(autouse=True)
def _limpo(tmp_path, monkeypatch):
    organiza._FALHARAM.clear()
    organiza._TENTATIVAS.clear()
    organiza._PROXIMA.clear()
    organiza._RECURSOS.update(ate=0.0, lista=None)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(organiza, "caminho_chave_existe", lambda: False)


class Semana:
    """A Semana falsa: devolve as pendentes (fora as do ?pular= e as que já estão num estado final,
    como a de verdade, que só lista nuvem e pegando) e guarda os PUT."""

    def __init__(self, capturas, recursos=None, audios=None):
        self.capturas, self.recursos, self.audios = capturas, recursos, audios or {}
        self.pedidos = []

    def __call__(self, metodo, url, key, corpo=None):
        self.pedidos.append((metodo, url, corpo))
        if "/pendentes" in url:
            pulados = url.split("pular=", 1)[1].split(",") if "pular=" in url else []
            finais = set(self.estados("descartada")) | set(self.estados("nota"))
            return {"capturas": [c for c in self.capturas if c["id"] not in pulados and c["id"] not in finais]}
        if url.endswith("/semana"):
            return {"dias": []} if self.recursos is None else {"dias": [], "recursos": self.recursos}
        if "/partes?id=" in url:
            return {"audio": self.audios.get(url.rsplit("=", 1)[1], "")}
        return {"ok": True}

    def estados(self, estado):
        return [c["id"] for m, u, c in self.pedidos if m == "PUT" and c and c.get("estado") == estado]


def _diario_cifrado(cid, bytes_de_voz):
    """Diário do app com uma voz cifrada do tamanho pedido (só bytes, nada de fala)."""
    pub = cofre.publica_b64(cofre.carregar_ou_criar())
    voz = base64.b64encode(b"\x00" * bytes_de_voz).decode()
    env = cofre.selar(json.dumps({"tipo": "voz", "audio": voz, "mime": "audio/mp4"}).encode(), "v1", pub)
    return {"id": cid, "tipo": "diario", "aberta_em": "2026-10-28T02:11:19", "fechada_em": "2026-10-28T02:11:40",
            "partes": [{"id": "v1", "ordem": 0, "tipo": "cifrada", "audio": json.dumps(env),
                        "criada_em": "2026-10-28T02:11:30"}]}


def _passo(tmp_path):
    return organiza.passo("k", tmp_path / "Inbox", tmp_path / "m", tmp_path / "l.json")


def test_diario_de_470_bytes_vira_info_e_falhas_nao_cresce(tmp_path, monkeypatch, caplog):
    c = _diario_cifrado("b9fb5064-0000-0000-0000-000000000001", 470)
    semana = Semana([c])  # Semana antiga: sem `recursos`
    monkeypatch.setattr(organiza, "_pedido", semana)
    with caplog.at_level(logging.INFO):
        for _ in range(5):
            assert _passo(tmp_path) == (0, 0)
    assert organiza._ler_falhas() == {}
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    evs = eventos.ultimos()
    assert len(evs) == 1 and evs[0]["nivel"] == "info" and evs[0]["origem"] == "organiza"
    assert evs[0]["texto"].startswith("Diário vazio descartado") and "28/10 02:11" in evs[0]["texto"]
    assert len(semana.estados("pegando")) == 1          # depois fica no ?pular=, também ao reiniciar
    assert semana.estados("descartada") == [] and semana.estados("nota") == []  # API antiga: nada muda lá
    assert not (tmp_path / "Inbox").exists()

    # A Semana passa a anunciar "descartada" (depois do SEM 2): fecha uma vez só.
    organiza._FALHARAM.clear()                         # e mesmo com o drenador reiniciado
    semana.recursos = ["guardar", "descartada"]
    organiza._RECURSOS.update(ate=0.0, lista=None)
    for _ in range(3):
        _passo(tmp_path)
    assert semana.estados("descartada") == [c["id"]]
    assert len(semana.estados("pegando")) == 1
    assert len(eventos.ultimos()) == 1                  # o aviso já tinha saído
    url = [u for m, u, x in semana.pedidos if "/pendentes" in u][-1]
    assert c["id"] not in url                           # fechada: fora do ?pular=


def test_com_a_semana_nova_a_vazia_fecha_na_hora(tmp_path, monkeypatch):
    c = _diario_cifrado("vazia-nova-000001", 300)
    semana = Semana([c], recursos=["descartada"])
    monkeypatch.setattr(organiza, "_pedido", semana)
    _passo(tmp_path)
    _passo(tmp_path)
    assert semana.estados("descartada") == [c["id"]]


def test_audio_de_verdade_segue_o_caminho_normal(tmp_path, monkeypatch):
    c = _diario_cifrado("diario-real-00001", 40_000)
    semana = Semana([c], recursos=["descartada"])
    monkeypatch.setattr(organiza, "_pedido", semana)
    assert _passo(tmp_path) == (1, 0)
    assert semana.estados("pc") == [c["id"]] and semana.estados("descartada") == []
    assert not organiza._ler_json(organiza._vazias_path())


def test_toque_sem_fala_numa_anotacao_comum_tambem(tmp_path, monkeypatch):
    c = {"id": "toque-0000000001", "tipo": "nota", "aberta_em": "2026-10-27T14:31:26",
         "partes": [{"id": "a1", "ordem": 0, "tipo": "voz", "tem_audio": 1, "mime": "audio/mp4"}]}
    semana = Semana([c], recursos=["descartada"], audios={"a1": base64.b64encode(b"\x00" * 900).decode()})
    monkeypatch.setattr(organiza, "_pedido", semana)
    _passo(tmp_path)
    assert semana.estados("descartada") == [c["id"]]
    assert eventos.ultimos()[0]["texto"].startswith("Anotação vazia descartada")


def test_texto_com_voz_curta_nao_e_vazia(tmp_path, monkeypatch):
    c = {"id": "texto-000000001", "tipo": "nota", "aberta_em": "2026-10-27T14:31:26",
         "partes": [{"id": "t1", "ordem": 0, "tipo": "texto", "texto": "ligar pro banco"},
                    {"id": "a1", "ordem": 1, "tipo": "voz", "audio": base64.b64encode(b"\x00" * 100).decode()}]}
    semana = Semana([c], recursos=["descartada"])
    monkeypatch.setattr(organiza, "_pedido", semana)
    assert _passo(tmp_path) == (1, 0)


def test_diario_so_com_escalas_vira_check_in(tmp_path, monkeypatch):
    pub = cofre.publica_b64(cofre.carregar_ou_criar())
    dentro = {"tipo": "texto", "texto": "Humor 3", "escalas": {"humor_nota": 3, "energia": 2}}
    env = cofre.selar(json.dumps(dentro).encode(), "e1", pub)
    c = {"id": "escalas-00000001", "tipo": "diario", "aberta_em": "2026-10-27T22:00:00",
         "partes": [{"id": "e1", "ordem": 0, "tipo": "cifrada", "audio": json.dumps(env)}]}
    semana = Semana([c], recursos=["descartada"])
    monkeypatch.setattr(organiza, "_pedido", semana)
    assert _passo(tmp_path) == (1, 0)
    pacote = next((tmp_path / "Inbox").iterdir())
    m = json.loads((pacote / "manifest.json").read_text(encoding="utf-8"))
    assert m["formulario"] == {"humor_nota": 3, "energia": 2} and m["mensagens"] == []


def test_as_duas_falhas_antigas_fecham_como_descartada_uma_vez_so(tmp_path, monkeypatch):
    # Cópia do falhas.json de 28/10 (as duas de "sem conteúdo"), sem abrir nada.
    p = organiza._falhas_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    antigas = {
        "91981c56-aaaa-0000-0000-000000000001": {"nome": "2026-10-15 23-50-00 app 91981c56",
                                                 "motivo": "anotação 91981c56-aaaa sem conteúdo", "desde": 1, "tentativas": 3},
        "b9fb5064-bbbb-0000-0000-000000000002": {"nome": "2026-10-28 02-11-19 app b9fb5064",
                                                 "motivo": "anotação b9fb5064-bbbb sem conteúdo", "desde": 2, "tentativas": 3},
    }
    p.write_text(json.dumps(antigas), encoding="utf-8")
    semana = Semana([])
    monkeypatch.setattr(organiza, "_pedido", semana)
    _passo(tmp_path)                                   # Semana antiga: nada
    assert semana.estados("descartada") == []
    semana.recursos = ["descartada"]
    organiza._RECURSOS.update(ate=0.0, lista=None)
    for _ in range(3):
        _passo(tmp_path)
    assert sorted(semana.estados("descartada")) == sorted(antigas)
    falhas = organiza._ler_falhas()
    assert falhas.keys() == antigas.keys() and all(v.get("descartada") for v in falhas.values())
    evs = eventos.ultimos()
    assert len(evs) == 1 and evs[0]["nivel"] == "info" and "2 anotação" in evs[0]["texto"]
    # Fechadas, saem do ?pular= (a Semana não as lista mais): não ocupam o corte de 50.
    url = [u for m, u, c in semana.pedidos if "/pendentes" in u][-1]
    assert not set(url.split("pular=", 1)[1].split(",") if "pular=" in url else []) & set(antigas)


def test_falha_de_outro_motivo_nao_e_descartada(tmp_path, monkeypatch):
    p = organiza._falhas_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"outra-0000000001": {"nome": "x", "motivo": "Invalid base64"}}), encoding="utf-8")
    semana = Semana([], recursos=["descartada"])
    monkeypatch.setattr(organiza, "_pedido", semana)
    _passo(tmp_path)
    assert semana.estados("descartada") == []
    assert not [u for m, u, c in semana.pedidos if u.endswith("/semana")]  # nem pergunta os recursos


def test_sem_parte_nenhuma_com_a_semana_nova_vira_descartada(tmp_path, monkeypatch):
    c = {"id": "sem-partes-000001", "tipo": "nota", "aberta_em": "2026-10-27T14:31:26", "partes": []}
    semana = Semana([c], recursos=["descartada"])
    monkeypatch.setattr(organiza, "_pedido", semana)
    _passo(tmp_path)
    assert semana.estados("descartada") == [c["id"]] and semana.estados("nota") == []
