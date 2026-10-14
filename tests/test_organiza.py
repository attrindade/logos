"""Drenador do app Organiza: nome do pacote, manifest, entrega na Inbox e estado devolvido."""
import base64
import json
from datetime import datetime

from logos import config, organiza, pipeline
from logos.triage import canal_from_filename, is_bundle_name, triage

VOZ = b"\x00\x00\x00\x18ftypM4A voz de teste"


def _captura(tipo="ideia", partes=None):
    return {
        "id": "f1cd4982-13f7-40e2-ac8e-a4b43faf06d8",
        "tipo": tipo,
        "aberta_em": "2026-10-14T01:25:11",
        "fechada_em": "2026-10-14T01:25:54",
        "partes": partes if partes is not None else [
            {"id": "p1", "ordem": 0, "tipo": "texto", "texto": "comprar fita", "criada_em": "2026-10-14T01:25:20"},
            {"id": "p2", "ordem": 1, "tipo": "voz", "audio": base64.b64encode(VOZ).decode(),
             "mime": "audio/mp4", "criada_em": "2026-10-14T01:25:40"},
        ],
    }


def test_nome_do_pacote_e_reconhecido_pelo_triage():
    nome = organiza.nome_pacote(_captura())
    assert nome == "2026-10-14 01-25-11 app f1cd4982"
    assert is_bundle_name(nome)
    assert canal_from_filename(nome) == "organiza"
    t = triage(nome, "Ideia: comprar fita")
    assert (t.canal, t.categoria) == ("organiza", "ideia")
    assert t.recorded_at == datetime(2026, 10, 14, 1, 25, 11)


def test_tg_e_arquivos_soltos_continuam_como_antes():
    assert canal_from_filename("2026-09-14 15-02-11 tg 123") == "telegram"
    assert canal_from_filename("2026-10-07 22-30-00 diario.m4a") == "syncthing"
    assert not is_bundle_name("2026-10-14 01-25-11 app f1cd4982.m4a")  # arquivo, não pasta


def test_tipo_vira_primeira_palavra_e_voz_sozinha_ganha_texto():
    m = organiza.manifest_de(_captura("tarefa"), {"p2": "01.m4a"})
    assert m["mensagens"][0] == {"tipo": "texto", "texto": "Tarefa: comprar fita", "quando": "2026-10-14T01:25:20"}
    assert m["mensagens"][1]["arquivo"] == "01.m4a"

    so_voz = _captura("reuniao", [_captura()["partes"][1]])
    m = organiza.manifest_de(so_voz, {"p2": "01.m4a"})
    assert [x["tipo"] for x in m["mensagens"]] == ["texto", "voz"]
    assert m["mensagens"][0]["texto"] == "Reunião"

    m = organiza.manifest_de(_captura("nota"), {"p2": "01.m4a"})
    assert m["mensagens"][0]["texto"] == "comprar fita"  # sem tipo, o texto fica como veio


def test_entrega_monta_fora_e_move_inteiro_sem_duplicar(tmp_path):
    inbox, oficina = tmp_path / "Inbox", tmp_path / "montando"
    destino = organiza.entregar(_captura(), inbox, oficina)
    assert destino == inbox / "2026-10-14 01-25-11 app f1cd4982"
    assert (destino / "01.m4a").read_bytes() == VOZ
    m = json.loads((destino / "manifest.json").read_text(encoding="utf-8"))
    assert m["origem"] == "organiza" and m["mensagens"][0]["texto"] == "Ideia: comprar fita"
    assert list(oficina.iterdir()) == []  # nada sobrou montando

    # Queda entre entregar e avisar a Semana: a volta seguinte recebe a mesma nota de novo.
    assert organiza.entregar(_captura(), inbox, oficina) == destino
    assert len(list(inbox.iterdir())) == 1


def test_pacote_do_app_vai_para_process_bundle(tmp_path, monkeypatch):
    destino = organiza.entregar(_captura(), tmp_path / "Inbox", tmp_path / "m")
    vistos = []
    monkeypatch.setattr(pipeline, "process_bundle", lambda path, ledger: vistos.append(path.name))
    pipeline.process_path(destino, ledger=None)
    assert vistos == [destino.name]


def test_passo_entrega_e_depois_avisa_que_virou_nota(tmp_path, monkeypatch):
    pedidos = []

    def falso(metodo, url, key, corpo=None):
        pedidos.append((metodo, url.rsplit("/", 1)[-1], corpo))
        if url.endswith("/pendentes"):
            return {"capturas": [_captura()]} if len(pedidos) == 1 else {"capturas": []}
        return {"ok": True}

    monkeypatch.setattr(organiza, "_pedido", falso)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    ledger = tmp_path / "ledger.json"
    inbox = tmp_path / "Inbox"

    assert organiza.passo("k", inbox, tmp_path / "m", ledger) == (1, 0)
    assert pedidos[1] == ("PUT", "estado", {"id": _captura()["id"], "estado": "pc"})

    # O watcher processou o pacote: o ledger aponta a nota.
    nota = tmp_path / "nota.md"
    nota.write_text(
        "---\ntitulo: Comprar fita\ncategoria: ideia\n---\n\n## ⚡ Próxima Ação Imediata\n- [ ] Passar na papelaria ⏫\n",
        encoding="utf-8",
    )
    ledger.write_text(json.dumps({"x": {
        "filename": "2026-10-14 01-25-11 app f1cd4982", "state": "enriched", "note_path": str(nota),
    }}), encoding="utf-8")

    assert organiza.passo("k", inbox, tmp_path / "m", ledger) == (0, 1)
    assert pedidos[-1] == ("PUT", "estado", {
        "id": _captura()["id"], "estado": "nota", "titulo": "Comprar fita", "proxima": "Passar na papelaria",
    })
    # Avisado uma vez só.
    assert organiza.passo("k", inbox, tmp_path / "m", ledger) == (0, 0)


def test_voz_vem_num_pedido_proprio_e_nota_com_defeito_nao_segura_a_fila(tmp_path, monkeypatch):
    sem_audio = _captura()
    voz = sem_audio["partes"][1]
    voz["tem_audio"] = 1
    b64 = voz.pop("audio")
    vazia = dict(_captura(), id="vazia-0000000001", partes=[])
    pedidos = []

    def falso(metodo, url, key, corpo=None):
        pedidos.append((metodo, url.rsplit("/", 1)[-1], corpo))
        if url.endswith("/pendentes"):
            return {"capturas": [vazia, sem_audio]}
        if "/partes?id=" in url:
            return {"audio": b64}
        return {"ok": True}

    monkeypatch.setattr(organiza, "_pedido", falso)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    assert organiza.passo("k", tmp_path / "Inbox", tmp_path / "m", tmp_path / "ledger.json") == (1, 0)
    pacote = tmp_path / "Inbox" / "2026-10-14 01-25-11 app f1cd4982"
    assert (pacote / "01.m4a").read_bytes() == VOZ
    # A vazia saiu da fila da Semana em vez de ocupar a janela de pendentes.
    assert ("PUT", "estado", {"id": "vazia-0000000001", "estado": "nota", "titulo": "Sem conteúdo: nada a guardar"}) in pedidos


def test_nota_apagada_na_semana_sai_de_entregues(tmp_path, monkeypatch):
    import urllib.error

    def falso(metodo, url, key, corpo=None):
        if url.endswith("/pendentes"):
            return {"capturas": []}
        raise urllib.error.HTTPError(url, 404, "não existe", {}, None)

    monkeypatch.setattr(organiza, "_pedido", falso)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    nota = tmp_path / "nota.md"
    nota.write_text("---\ntitulo: X\n---\n", encoding="utf-8")
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"x": {"filename": "p", "state": "enriched", "note_path": str(nota)}}), encoding="utf-8")
    organiza._gravar_entregues({"apagada-00000001": "p"})
    assert organiza.passo("k", tmp_path / "Inbox", tmp_path / "m", ledger) == (0, 1)
    assert organiza._ler_entregues() == {}
