"""Drenador do app Organiza: nome do pacote, manifest, entrega na Inbox e estado devolvido."""
import pytest
import base64
import json
from datetime import datetime

from logos import config, organiza, pipeline
from logos.triage import canal_from_filename, is_bundle_name, triage

VOZ = b"\x00\x00\x00\x18ftypM4A voz de teste"




@pytest.fixture(autouse=True)
def _sem_falhas_antigas():
    organiza._FALHARAM.clear()
    organiza._TENTATIVAS.clear()
    organiza._PROXIMA.clear()
    organiza._RECURSOS.update(ate=0.0, lista=None)

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


def test_foto_pdf_e_link_do_app_viram_as_entradas_do_bot(tmp_path):
    foto, pdf = b"\xff\xd8\xff foto", b"%PDF-1.4 pdf"
    partes = [
        {"id": "t", "ordem": 0, "tipo": "texto", "texto": "ver https://exemplo.org/a?b=1, e (https://x.io).",
         "criada_em": "2026-10-14T01:25:20"},
        {"id": "f", "ordem": 1, "tipo": "arquivo", "texto": "IMG_1.jpg", "mime": "image/jpeg",
         "audio": base64.b64encode(foto).decode(), "criada_em": "2026-10-14T01:25:30"},
        {"id": "d", "ordem": 2, "tipo": "arquivo", "texto": "contrato.pdf", "mime": "application/pdf",
         "audio": base64.b64encode(pdf).decode(), "criada_em": "2026-10-14T01:25:40"},
    ]
    destino = organiza.entregar(_captura("nota", partes), tmp_path / "Inbox", tmp_path / "montando")
    m = json.loads((destino / "manifest.json").read_text(encoding="utf-8"))["mensagens"]
    assert m[0]["links"] == ["https://exemplo.org/a?b=1", "https://x.io"]
    assert m[1] == {"tipo": "foto", "arquivo": "01.jpg", "mime": "image/jpeg", "quando": "2026-10-14T01:25:30",
                    "nome_original": "IMG_1.jpg"}
    assert m[2]["tipo"] == "documento" and m[2]["arquivo"] == "02.pdf" and m[2]["nome_original"] == "contrato.pdf"
    assert (destino / "01.jpg").read_bytes() == foto and (destino / "02.pdf").read_bytes() == pdf


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
    # "pegando" antes de baixar (o celular não reabre mais), depois "pc".
    assert pedidos[1] == ("PUT", "estado", {"id": _captura()["id"], "estado": "pegando"})
    assert pedidos[2] == ("PUT", "estado", {"id": _captura()["id"], "estado": "pc"})

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


def test_diario_cifrado_abre_no_pc_vai_com_rota_e_volta_sem_titulo(tmp_path, monkeypatch):
    from logos import cofre
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    chave = cofre.carregar_ou_criar()
    pub = cofre.publica_b64(chave)
    texto = cofre.selar(json.dumps({"tipo": "texto", "texto": "o que pesou hoje"}).encode(), "d1", pub)
    voz = cofre.selar(json.dumps({"tipo": "voz", "audio": base64.b64encode(VOZ).decode(), "mime": "audio/mp4"}).encode(), "d2", pub)
    captura = dict(_captura("diario"), id="diario-000000001", partes=[
        {"id": "d1", "ordem": 0, "tipo": "cifrada", "audio": json.dumps(texto), "criada_em": "2026-10-14T22:00:00"},
        {"id": "d2", "ordem": 1, "tipo": "cifrada", "tem_audio": 1, "criada_em": "2026-10-14T22:01:00"},
    ])
    pedidos = []

    def falso(metodo, url, key, corpo=None):
        pedidos.append((metodo, url.rsplit("/", 1)[-1], corpo))
        if url.endswith("/pendentes"):
            return {"capturas": [captura]} if sum(1 for p in pedidos if p[1] == "pendentes") == 1 else {"capturas": []}
        if "/partes?id=d2" in url:
            return {"audio": json.dumps(voz)}
        return {"ok": True}

    monkeypatch.setattr(organiza, "_pedido", falso)
    ledger = tmp_path / "ledger.json"
    assert organiza.passo("k", tmp_path / "Inbox", tmp_path / "m", ledger) == (1, 0)
    pacote = next((tmp_path / "Inbox").iterdir())
    m = json.loads((pacote / "manifest.json").read_text(encoding="utf-8"))
    assert m["rota"] == "diario"
    assert m["mensagens"][0] == {"tipo": "texto", "texto": "o que pesou hoje", "quando": "2026-10-14T22:00:00"}
    assert (pacote / m["mensagens"][1]["arquivo"]).read_bytes() == VOZ

    nota = tmp_path / "Diário — 2026-10-14.md"
    nota.write_text("---\ntitulo: Diário\n---\n## ⚡ Próxima Ação Imediata\n- [ ] algo pessoal ⏫\n", encoding="utf-8")
    ledger.write_text(json.dumps({"x": {"filename": pacote.name, "state": "enriched", "note_path": str(nota)}}), encoding="utf-8")
    assert organiza.passo("k", tmp_path / "Inbox", tmp_path / "m", ledger) == (0, 1)
    assert pedidos[-1] == ("PUT", "estado", {"id": "diario-000000001", "estado": "nota"})  # nada do conteúdo


def test_diario_com_outra_chave_fica_na_nuvem(tmp_path, monkeypatch):
    from logos import cofre
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    cofre.carregar_ou_criar()
    outra = cofre.publica_b64(cofre.X25519PrivateKey.generate())
    env = cofre.selar(b'{"tipo": "texto", "texto": "x"}', "d1", outra)
    captura = dict(_captura("diario"), id="diario-000000002", partes=[
        {"id": "d1", "ordem": 0, "tipo": "cifrada", "audio": json.dumps(env), "criada_em": "2026-10-14T22:00:00"}])
    pedidos = []

    def falso(metodo, url, key, corpo=None):
        pedidos.append((metodo, url, corpo))
        return {"capturas": [captura]} if url.endswith("/pendentes") else {"ok": True}

    monkeypatch.setattr(organiza, "_pedido", falso)
    assert organiza.passo("k", tmp_path / "Inbox", tmp_path / "m", tmp_path / "l.json") == (0, 0)
    assert not any(m == "PUT" and c.get("estado") == "pc" for m, _, c in pedidos)  # nem "pc": o envelope continua lá


def test_diario_com_tipo_trocado_na_nuvem_continua_diario_e_sem_parte_em_claro(tmp_path, monkeypatch):
    from logos import cofre
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    pub = cofre.publica_b64(cofre.carregar_ou_criar())
    env = cofre.selar(json.dumps({"tipo": "texto", "texto": "pessoal"}).encode(), "d1", pub)
    captura = dict(_captura("nota"), id="diario-000000003", partes=[  # alguém trocou o tipo para "nota"
        {"id": "d1", "ordem": 0, "tipo": "cifrada", "audio": json.dumps(env), "criada_em": "2026-10-14T22:00:00"},
        {"id": "x1", "ordem": 1, "tipo": "texto", "texto": "injetado", "criada_em": "2026-10-14T22:01:00"},
    ])
    pedidos = []

    def falso(metodo, url, key, corpo=None):
        pedidos.append((metodo, url.rsplit("/", 1)[-1], corpo))
        if url.endswith("/pendentes"):
            return {"capturas": [captura]} if sum(1 for p in pedidos if p[1] == "pendentes") == 1 else {"capturas": []}
        if url.endswith("/linhas"):
            return {"linhas": {"cofre": {"dados": {"pub": pub}}}}
        return {"ok": True}

    monkeypatch.setattr(organiza, "_pedido", falso)
    ledger = tmp_path / "ledger.json"
    assert organiza.passo("k", tmp_path / "Inbox", tmp_path / "m", ledger) == (1, 0)
    pacote = next((tmp_path / "Inbox").iterdir())
    m = json.loads((pacote / "manifest.json").read_text(encoding="utf-8"))
    assert m["rota"] == "diario" and [x.get("texto") for x in m["mensagens"]] == ["pessoal"]
    nota = tmp_path / "n.md"
    nota.write_text("---\ntitulo: pessoal\n---\n", encoding="utf-8")
    ledger.write_text(json.dumps({"x": {"filename": pacote.name, "state": "enriched", "note_path": str(nota)}}), encoding="utf-8")
    organiza.passo("k", tmp_path / "Inbox", tmp_path / "m", ledger)
    assert pedidos[-1] == ("PUT", "estado", {"id": "diario-000000003", "estado": "nota"})


def test_chave_publicada_diferente_e_avisada(tmp_path, monkeypatch, caplog):
    from logos import cofre
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    cofre.carregar_ou_criar()
    outra = cofre.publica_b64(cofre.X25519PrivateKey.generate())
    monkeypatch.setattr(organiza, "_pedido", lambda m, u, k, c=None: {"linhas": {"cofre": {"dados": {"pub": outra}}}})
    assert organiza.conferir_chave_publicada("k") is False
    assert "NÃO É A DESTE PC" in caplog.text


def test_diario_curto_leva_escalas_ao_formulario_e_pergunta_a_cada_resposta(tmp_path, monkeypatch):
    from logos import cofre
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    chave = cofre.carregar_ou_criar()
    pub = cofre.publica_b64(chave)

    def selada(pid, dentro):
        return {"id": pid, "ordem": int(pid[1:]), "tipo": "cifrada", "criada_em": f"2026-10-14T21:0{pid[1:]}:00",
                "audio": json.dumps(cofre.selar(json.dumps(dentro).encode(), pid, pub))}

    escalas = {"tipo": "texto", "texto": "Humor 4 · Energia 2 · Estresse médio",
               "escalas": {"humor_nota": 4, "energia": 2, "estresse": "médio", "sono": "<5h", "x": 1}}
    captura = dict(_captura("diario"), id="diario-curto-01", partes=[
        selada("d0", escalas),
        selada("d1", {"tipo": "texto", "texto": "a reunião rendeu", "pergunta": 1}),
        selada("d2", {"tipo": "texto", "texto": "cansaço", "pergunta": 2}),
        selada("d3", {"tipo": "texto", "texto": "solto", "pergunta": 9}),  # fora da lista: vai sem título
    ])
    abertas = organiza.abrir_partes(captura, None, chave)
    m = organiza.manifest_de(dict(abertas, tipo="diario"), {})
    # só os campos e valores que o /diario aceita; o texto das escalas não vira resposta
    assert m["formulario"] == {"humor_nota": 4, "energia": 2, "estresse": "médio"}
    assert [x.get("pergunta") for x in m["mensagens"]] == [1, 2, None]
    assert m["perguntas"] == list(organiza.PERGUNTAS_DIARIO)
    assert "Humor 4" not in json.dumps(m["mensagens"], ensure_ascii=False)


def test_n_a12_diario_antigo_com_resposta_4_ainda_ganha_titulo(tmp_path, monkeypatch):
    # O app pergunta só as 3 primeiras desde 10/2026; um Diário antigo com a 4ª continua com título no PC.
    from logos import cofre
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    chave = cofre.carregar_ou_criar()
    pub = cofre.publica_b64(chave)
    parte = {"id": "d4", "ordem": 4, "tipo": "cifrada", "criada_em": "2026-10-14T21:04:00",
             "audio": json.dumps(cofre.selar(json.dumps({"tipo": "texto", "texto": "às 9h, o relatório",
                                                          "pergunta": 4}).encode(), "d4", pub))}
    abertas = organiza.abrir_partes(dict(_captura("diario"), id="diario-antigo-4", partes=[parte]), None, chave)
    m = organiza.manifest_de(dict(abertas, tipo="diario"), {})
    assert [x.get("pergunta") for x in m["mensagens"]] == [4]
    assert len(m["perguntas"]) == 4 and m["perguntas"][3].startswith("Amanhã, a primeira coisa")
    texto = pipeline._agrupar(["às 9h, o relatório"], [4], "diario", "###", m["perguntas"])
    assert texto == "### " + organiza.PERGUNTAS_DIARIO[3] + "\n\nàs 9h, o relatório"


def test_drenador_antigo_le_as_escalas_como_texto():
    # O envelope das escalas tem "texto": quem não conhece "escalas" ainda entrega algo legível.
    m = organiza.manifest_de(dict(_captura("diario"), partes=[
        {"id": "d0", "ordem": 0, "tipo": "texto", "texto": "Humor 4 · Energia 2 · Estresse médio"}]), {})
    assert m["mensagens"][0]["texto"].startswith("Humor 4")
    assert "formulario" not in m and "perguntas" not in m


def test_agrupar_usa_as_perguntas_do_pacote_quando_vem():
    secoes = ["livre", "boa", "pesou"]
    texto = pipeline._agrupar(secoes, [None, 1, 2], "diario", "###", ["Pergunta A", "Pergunta B"])
    assert texto == "livre\n\n### Pergunta A\n\nboa\n\n### Pergunta B\n\npesou"
    # sem lista própria (ou com lixo), ficam as do bot
    bot = pipeline._agrupar(["boa"], [1], "diario", "###", [3])
    assert bot.startswith("### " + pipeline.PERGUNTAS_POR_ROTA["diario"][0])


def test_chave_do_celular_fixada_na_primeira_vez_e_troca_recusada(tmp_path, monkeypatch, caplog):
    from logos import cofre
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    a = cofre.publica_b64(X25519PrivateKey.generate())
    b = cofre.publica_b64(X25519PrivateKey.generate())
    publicada = {"pub": a}
    monkeypatch.setattr(organiza, "_pedido", lambda m, u, k, c=None: {"linhas": {"celular": {"dados": dict(publicada)}}})
    monkeypatch.setattr(organiza, "_CEL_CACHE", {"pub": None, "ate": 0.0})
    assert organiza.chave_celular("k") == a
    assert (tmp_path / "state/organiza/celular.pub").read_text(encoding="utf-8") == a
    publicada["pub"] = b
    assert organiza.chave_celular("k") == a  # cache de 5 min: a Semana não é consultada de novo
    organiza._CEL_CACHE["ate"] = 0.0
    assert organiza.chave_celular("k") is None
    assert "não é a fixada" in caplog.text
    for ruim in ("curta==", base64.b64encode(bytes(32)).decode()):  # curta e ponto de ordem baixa
        publicada["pub"] = ruim
        assert organiza.chave_celular("k") is None


def test_diario_que_nao_abre_sai_da_janela_das_pendentes(tmp_path, monkeypatch):
    urls = []
    organiza._FALHARAM.add("diario-ruim-0001")
    monkeypatch.setattr(organiza, "_pedido", lambda m, u, k, c=None: (urls.append(u), {"capturas": []})[1])
    monkeypatch.setattr(organiza, "caminho_chave_existe", lambda: False)
    organiza.passo("k", tmp_path / "Inbox", tmp_path / "m", tmp_path / "l.json")
    assert urls[0].endswith("/pendentes?pular=diario-ruim-0001")


def test_nota_ilegivel_vira_falha_definitiva_com_um_evento_so(tmp_path, monkeypatch):
    """A captura de 16/10/2026 (91981c56): partes que não abrem voltavam a cada minuto para sempre,
    sem aviso. Agora: N tentativas, espaçadas (28/10: as 3 em 2 min davam falha a um Diário que só
    estava atrasado), depois sai da janela (também após reiniciar) e avisa uma vez."""
    from logos import eventos
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(organiza, "caminho_chave_existe", lambda: False)
    relogio = [1_800_000_000.0]
    monkeypatch.setattr(organiza.time, "time", lambda: relogio[0])
    # A voz diz ter áudio e ele não vem: não é vazia (pode ser a Semana), é ilegível.
    ilegivel = dict(_captura(), id="91981c56-aaaa-bbbb-cccc-000000000001", aberta_em="2026-10-16T06:10:05",
                    partes=[{"id": "p9", "ordem": 0, "tipo": "voz", "mime": "audio/mp4", "tem_audio": 1}])
    pedidos = []

    def falso(metodo, url, key, corpo=None):
        pedidos.append((metodo, url, corpo))
        if "/pendentes" in url:
            pulados = url.split("pular=", 1)[1].split(",") if "pular=" in url else []
            return {"capturas": [] if ilegivel["id"] in pulados else [ilegivel]}
        if "/partes?id=" in url:
            return {"audio": ""}
        return {"ok": True}

    def voltas(n, passo_s=60):
        for _ in range(n):
            assert organiza.passo("k", tmp_path / "Inbox", tmp_path / "m", tmp_path / "l.json") == (0, 0)
            relogio[0] += passo_s

    def pegando():
        return len([c for m, u, c in pedidos if c and c.get("estado") == "pegando"])

    monkeypatch.setattr(organiza, "_pedido", falso)
    voltas(5)                       # 1ª tentativa e depois 4 voltas esperando: não tenta de novo
    assert pegando() == 1
    relogio[0] += organiza.ESPERA_ILEGIVEL_S[0]
    voltas(5)                       # 2ª
    assert pegando() == 2
    relogio[0] += 3600
    voltas(3)                       # 1 h depois: ainda esperando as 6 h
    assert pegando() == 2 and not [e for e in eventos.ultimos() if e["nivel"] == "falha"]
    relogio[0] += organiza.ESPERA_ILEGIVEL_S[1]
    voltas(5)                       # 3ª: falha definitiva
    assert pegando() == organiza.TENTATIVAS_ILEGIVEL  # depois disso nem pede mais
    falhas = [e for e in eventos.ultimos() if e["nivel"] == "falha"]
    assert len(falhas) == 1 and falhas[0]["origem"] == "organiza"
    assert "16/10 06:10" in falhas[0]["texto"] and "91981c56" in falhas[0]["texto"]
    # Nada foi apagado nem marcado como nota na nuvem.
    assert not [c for m, u, c in pedidos if c and c.get("estado") in ("nota", "descartada")]
    assert not (tmp_path / "Inbox").exists() or not list((tmp_path / "Inbox").iterdir())

    # Drenador reiniciado: a memória some, o arquivo de falhas não. Continua fora e sem novo evento.
    organiza._FALHARAM.clear()
    organiza._TENTATIVAS.clear()
    organiza._PROXIMA.clear()
    pedidos.clear()
    organiza.passo("k", tmp_path / "Inbox", tmp_path / "m", tmp_path / "l.json")
    assert ilegivel["id"] in pedidos[0][1]  # vai no ?pular=
    assert not [c for m, u, c in pedidos if c and c.get("estado") == "pegando"]
    assert len([e for e in eventos.ultimos() if e["nivel"] == "falha"]) == 1


def test_falha_definitiva_de_diario_nao_leva_conteudo(tmp_path, monkeypatch):
    from logos import eventos
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    c = dict(_captura("diario"), id="d1a2b3c4-0000")
    organiza._TENTATIVAS[c["id"]] = 3
    organiza._falha_definitiva(c, "anotação d1a2b3c4-0000 sem conteúdo", diario=True)
    texto = eventos.ultimos()[0]["texto"]
    assert texto.startswith("Um Diário do celular de 14/10 01:25") and "comprar fita" not in texto
    assert "d1a2b3c4-0000" in organiza._ler_falhas()


def test_pular_manda_as_definitivas_primeiro(tmp_path, monkeypatch):
    # Revisão de 25/10, B1: com mais de 50, o corte alfabético deixava uma definitiva de fora.
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(organiza, "caminho_chave_existe", lambda: False)
    p = organiza._falhas_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"zz-definitiva-01": {"nome": "x"}}), encoding="utf-8")
    organiza._FALHARAM.update(f"aa-temporaria-{n:02d}" for n in range(60))
    urls = []
    monkeypatch.setattr(organiza, "_pedido", lambda m, u, k, c=None: (urls.append(u), {"capturas": []})[1])
    organiza.passo("k", tmp_path / "Inbox", tmp_path / "m", tmp_path / "l.json")
    pulados = urls[0].split("pular=", 1)[1].split(",")
    assert pulados[0] == "zz-definitiva-01" and len(pulados) == organiza.PULAR_MAX


def test_resposta_que_nao_e_json_nao_vira_falha_definitiva(tmp_path, monkeypatch):
    # Uma página de erro no pedido da parte (json.loads levanta) é rede, não nota ilegível.
    from logos import eventos
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(organiza, "caminho_chave_existe", lambda: False)
    c = dict(_captura(), partes=[{"id": "p9", "ordem": 0, "tipo": "voz", "mime": "audio/mp4", "tem_audio": True}])

    def falso(metodo, url, key, corpo=None):
        if "/pendentes" in url:
            return {"capturas": [] if c["id"] in url else [c]}
        if "/partes" in url:
            raise json.JSONDecodeError("Expecting value", "<html>", 0)
        return {"ok": True}

    monkeypatch.setattr(organiza, "_pedido", falso)
    for _ in range(organiza.TENTATIVAS_ILEGIVEL + 3):
        organiza.passo("k", tmp_path / "Inbox", tmp_path / "m", tmp_path / "l.json")
    assert organiza._ler_falhas() == {}
    assert not [e for e in eventos.ultimos() if e["nivel"] == "falha"]
    assert c["id"] in organiza._FALHARAM  # 5 temporárias: fora da janela só até reiniciar
