"""A caixa da captura (LOG 9, 28/10/2026): spec-captura §7, critérios 3 a 7 e 10, com o Claude e o
Ollama simulados. Nada aqui chama o claude.exe, o Ollama ou a Semana de verdade."""
import json
import logging
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from logos import acoes_celular, caixa, cofre, config, organiza
from logos.ledger import Ledger
from logos.organiza import write_manifest
from logos.pipeline import process_bundle
from logos.stt import Transcription
from logos.triage import Route

CID = "0b6abafe-caixa-1"
NOME = "2026-10-28 14-10-00 app 0b6abafe"
FALADO = datetime(2026, 10, 28, 14, 10)
AGORA = datetime(2026, 10, 28, 14, 12)


@pytest.fixture
def amb(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    monkeypatch.setattr(config, "CAIXA_LIGADA", True)
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "Inbox")
    monkeypatch.setattr(config, "TRANSCRIPTS_DIR", tmp_path / "Transcripts")
    monkeypatch.setattr(config, "NOTES_DIR", vault / "capturas")
    monkeypatch.setattr(config, "ARCHIVE_DIR", tmp_path / "Archive")
    monkeypatch.setattr(config, "VAULT_DIR", None)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    config.INBOX_DIR.mkdir()
    return Ledger(tmp_path / "ledger.json")


class Modelo:
    """Claude ou Ollama simulado: conta as chamadas e guarda o prompt."""

    def __init__(self, resposta=None, erro=None, nome="claude-opus-5-5"):
        self.resposta, self.erro, self.nome, self.prompts = resposta, erro, nome, []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if self.erro:
            raise self.erro
        return dict(self.resposta), self.nome


def _resp(destino="nota", confianca=0.9, quando="", entendi="Algo que você disse.", titulo="Título da caixa"):
    return {"destino": destino, "confianca": confianca, "entendi": entendi, "texto_limpo": "texto limpo",
            "quando": quando, "titulo": titulo}


def _manifest(**extra):
    return {"origem": "organiza", "captura_id": CID, "aberto_em": "2026-10-28T14:10:00", **extra}


def _pacote(mensagens, **extra) -> Path:
    d = config.INBOX_DIR / NOME
    d.mkdir()
    (d / "01.m4a").write_bytes(b"\x00" * 9000)
    write_manifest(d, {**_manifest(**extra), "mensagens": mensagens})
    return d


def _rodar(d, ledger, fala="", claude=None, ollama=None):
    claude = claude or Modelo(_resp())
    ollama = ollama or Modelo(erro=RuntimeError("não devia"))
    with patch("logos.pipeline.transcribe_file", return_value=Transcription(fala, 3.0)), \
         patch("logos.pipeline.enrich", return_value={"titulo": "x", "tags": [], "resumo": "."}), \
         patch("logos.pipeline.aw.post_capture"), \
         patch.object(caixa, "chamar_claude", claude), patch.object(caixa, "chamar_ollama", ollama):
        process_bundle(d, ledger)
    return claude


def _notas():
    return list(config.NOTES_DIR.rglob("*.md")) if config.NOTES_DIR.exists() else []


class Semana:
    """A Semana simulada: guarda cada pedido e responde como a de verdade, no essencial."""

    def __init__(self, recados=None):
        self.pedidos, self.recados = [], list(recados or [])

    def __call__(self, metodo, url, key, corpo=None):
        self.pedidos.append((metodo, url.rsplit("/api/", 1)[1], corpo))
        if metodo == "GET" and url.endswith("/api/recados"):
            return {"recados": self.recados}
        if metodo == "POST" and url.endswith("/api/recados"):
            self.recados.append({"id": len(self.recados) + 1, "contexto": corpo["contexto"]})
            return {"ok": True, "id": len(self.recados)}
        return {"ok": True}

    def de(self, rota, metodo=None):
        return [c for m, r, c in self.pedidos if r == rota and (metodo is None or m == metodo)]


# ---- critério 3: privado é privado (o Diário nunca chega ao classificador) ----

@pytest.mark.parametrize("manifest,route,texto", [
    (_manifest(rota="diario"), Route.DIARIO, "hoje foi um dia bom"),     # o cadeado / Diário da noite
    (_manifest(), Route.DIARIO, "Diário, hoje eu fiquei pensando"),        # 1ª palavra "Diário"
    (_manifest(), Route.INBOX, "[voz 0:12] Diário hoje eu fiquei"),        # com a marca da voz na frente
    (_manifest(), Route.INBOX, "Ideia: Diário, uma coisa"),                # com a palavra do tipo na frente
    ({"origem": "telegram", "captura_id": CID}, Route.INBOX, "ligar pro Osmar"),
    ({"origem": "organiza"}, Route.INBOX, "sem captura_id"),
])
def test_diario_e_o_que_nao_e_do_organiza_nunca_chamam_o_classificador(amb, manifest, route, texto):
    claude = Modelo(_resp())
    assert caixa.decidir(manifest, texto, FALADO, route=route, agora=AGORA, claude=claude, ollama=claude) is None
    assert claude.prompts == [] and not caixa.pasta().exists()


def test_pacote_do_diario_vira_nota_do_diario_sem_classificador_nem_decisao(amb):
    d = _pacote([{"tipo": "texto", "texto": "Hoje foi difícil, mas consegui terminar"}], rota="diario")
    claude = _rodar(d, amb)
    assert claude.prompts == []
    assert not caixa.pasta().exists()
    assert all("Inbox" not in str(n) for n in _notas())


def test_caixa_desligada_nao_chama_nada(amb, monkeypatch):
    monkeypatch.setattr(config, "CAIXA_LIGADA", False)
    claude = Modelo(_resp())
    assert caixa.decidir(_manifest(), "ligar pro Osmar", FALADO, route=Route.INBOX, claude=claude) is None
    assert claude.prompts == []


# ---- o caminho normal no pipeline ----

def test_nota_curta_passa_pelo_classificador_e_leva_titulo_e_destino(amb, caplog):
    d = _pacote([{"tipo": "texto", "texto": "lembrar de ligar pro Osmar amanhã cedo"}])
    claude = Modelo(_resp("plano", 0.93, "amanha", "Ligar para o Osmar amanhã cedo.", "Ligar para o Osmar"))
    with caplog.at_level(logging.INFO, logger="logos.caixa"):
        _rodar(d, amb, claude=claude)
    assert len(claude.prompts) == 1 and "ligar pro Osmar" in claude.prompts[0]
    dec = caixa.ler(CID)
    assert dec["destino"] == "plano" and dec["alvo"] == "2026-10-29" and dec["pacote"] == NOME
    assert dec["modelo"] == "claude-opus-5-5" and isinstance(dec["segundos"], float)
    [nota] = _notas()
    texto = nota.read_text(encoding="utf-8")
    assert "destino: plano" in texto and "titulo: Ligar para o Osmar" in texto
    assert "lembrar de ligar pro Osmar amanhã cedo" in texto  # a transcrição literal fica
    # o log diz o modelo que respondeu e o tempo, sem a fala
    assert "por claude-opus-5-5 em" in caplog.text and "Osmar" not in caplog.text


def test_planejamento_cai_no_destino_plano(amb):
    claude = Modelo(_resp("nota", 0.2))
    dec = caixa.decidir(_manifest(), "Planejamento: amanhã começo pelo relatório", FALADO,
                        route=Route.PLANEJAMENTO, agora=AGORA, claude=claude)
    assert dec["destino"] == "plano" and "planejamento" in claude.prompts[0]


# ---- critério 5: na dúvida, Nota; sem o Claude, Ollama; sem os dois, Nota ----

def test_claude_falha_e_o_ollama_responde(amb, caplog):
    claude = Modelo(erro=TimeoutError("passou de 60 s"))
    ollama = Modelo(_resp("fim_de_semana", 0.8), nome="gemma4:e4b")
    with caplog.at_level(logging.INFO, logger="logos.caixa"):
        dec = caixa.decidir(_manifest(), "para a semana que vem", FALADO, route=Route.INBOX, agora=AGORA,
                            claude=claude, ollama=ollama)
    assert dec["destino"] == "fim_de_semana" and dec["modelo"] == "gemma4:e4b"
    assert "o Claude não respondeu (TimeoutError" in caplog.text and "por gemma4:e4b" in caplog.text


def test_os_dois_falham_e_fica_nota(amb):
    falha = Modelo(erro=RuntimeError("fora"))
    dec = caixa.decidir(_manifest(), "qualquer coisa", FALADO, route=Route.INBOX, agora=AGORA, claude=falha, ollama=falha)
    assert dec["destino"] == "nota" and dec["entendi"] == caixa.FRASE_FALHOU and dec["modelo"] == "nenhum"
    assert caixa.ler(CID)["destino"] == "nota"  # gravada: o drenador conta à Semana


def test_confianca_baixa_vira_nota(amb):
    dec = caixa.decidir(_manifest(), "talvez eu ligue", FALADO, route=Route.INBOX, agora=AGORA,
                        claude=Modelo(_resp("plano", 0.4, "hoje")))
    assert dec["destino"] == "nota" and dec["motivo"] == "confiança baixa"


def test_fala_velha_nao_vira_plano(amb):
    ontem = datetime(2026, 10, 27, 15, 0)
    dec = caixa.decidir(_manifest(), "hoje à tarde ligo pro Osmar", ontem, route=Route.INBOX, agora=AGORA,
                        claude=Modelo(_resp("plano", 0.95, "hoje")))
    assert dec["destino"] == "nota" and dec["entendi"] == caixa.FRASE_VELHA


def test_fala_da_madrugada_ainda_e_do_dia_anterior(amb):
    # 01h30 de quarta, dia lógico ainda é terça: "amanhã" é quarta, que é hoje às 14h12 — vale
    madrugada = datetime(2026, 10, 28, 1, 30)
    dec = caixa.decidir(_manifest(), "amanhã ligo pro Osmar", madrugada, route=Route.INBOX, agora=AGORA,
                        claude=Modelo(_resp("plano", 0.9, "amanha")))
    assert dec["destino"] == "plano" and dec["alvo"] == "2026-10-28"


def test_destino_fora_da_lista_e_entendi_longo(amb):
    dec = caixa.decidir(_manifest(), "x", FALADO, route=Route.INBOX, agora=AGORA,
                        claude=Modelo(_resp("pedido", 0.99, entendi="a" * 300)))
    assert dec["destino"] == "nota" and len(dec["entendi"]) <= caixa.MAX_ENTENDI


# ---- critério 6: idempotência ----

def test_decisao_gravada_nao_chama_o_claude_de_novo(amb):
    claude = Modelo(_resp("recado", 0.9))
    a = caixa.decidir(_manifest(), "o botão está pequeno", FALADO, route=Route.INBOX, agora=AGORA, claude=claude)
    b = caixa.decidir(_manifest(), "o botão está pequeno", FALADO, route=Route.INBOX, agora=AGORA, claude=claude)
    assert len(claude.prompts) == 1 and a["destino"] == b["destino"] == "recado"


@pytest.mark.parametrize("destino,rota", [("plano", "dia"), ("fim_de_semana", "complementos"), ("recado", "recados")])
def test_mesmo_pacote_duas_vezes_gera_um_efeito(amb, monkeypatch, destino, rota):
    caixa.decidir(_manifest(), "frase", FALADO, route=Route.INBOX, agora=AGORA,
                  claude=Modelo(_resp(destino, 0.9, "hoje")))
    semana = Semana()
    monkeypatch.setattr(organiza, "_pedido", semana)
    organiza.aplicar_caixa("k", CID)
    organiza.aplicar_caixa("k", CID)  # a próxima volta do drenador
    # mesmo com a decisão relida do disco sem `efeito` (queda antes de gravar), o recado não duplica
    dec = caixa.ler(CID)
    dec["efeito"] = None
    caixa.aplicar("k", dec, semana, agora=AGORA)
    escritas = [c for m, r, c in semana.pedidos if r == rota and m in ("PUT", "POST")]
    if destino == "recado":
        assert len(escritas) == 1
    else:
        assert len({c["id"] for c in escritas}) == 1  # o mesmo id: a Semana ignora a repetição
    estados = semana.de("capturas/estado")
    assert len(estados) == 1 and estados[0]["destino"] == destino and estados[0]["estado"] == "pc"


# ---- critério 7: o plano só muda com o Aceitar ----

def test_plano_vira_fala_do_dia_sem_mexer_no_plano(amb, monkeypatch):
    caixa.decidir(_manifest(), "amanhã cedo ligo pro Osmar", FALADO, route=Route.INBOX, agora=AGORA,
                  claude=Modelo(_resp("plano", 0.95, "amanha")))
    semana = Semana()
    caixa.aplicar("k", caixa.ler(CID), semana, agora=AGORA)
    [fala] = semana.de("dia")
    assert fala["id"] == f"caixa-{CID}" and fala["tipo"] == "dia" and fala["dia"] == "2026-10-28"
    assert fala["texto"].startswith("Para quinta, 29/10:")
    assert fala["contexto"] == {"origem": "caixa", "captura_id": CID, "falado_em": "2026-10-28T14:10:00"}
    assert not any("acao" in (c or {}) or "ops" in (c or {}) for _, _, c in semana.pedidos)


# ---- o drenador manda destino e entendi junto com o "nota" ----

def test_drenador_manda_destino_no_pc_e_no_nota(amb, monkeypatch, tmp_path):
    caixa.decidir(_manifest(), "frase", FALADO, route=Route.INBOX, agora=AGORA, claude=Modelo(_resp("nota", 0.9)))
    semana = Semana()
    monkeypatch.setattr(organiza, "_pedido", lambda m, u, k, c=None: (
        {"capturas": []} if u.endswith("/pendentes") else semana(m, u, k, c)))
    monkeypatch.setattr(organiza, "caminho_chave_existe", lambda: False)
    monkeypatch.setattr(organiza, "fechar_descartadas", lambda key: 0)
    nota = tmp_path / "n.md"
    nota.write_text("---\ntitulo: T\n---\n", encoding="utf-8")
    organiza._gravar_entregues({CID: NOME})
    monkeypatch.setattr(organiza, "nota_do_pacote", lambda nome, lp: nota)
    organiza.passo("k", config.INBOX_DIR, tmp_path / "of", tmp_path / "ledger.json")
    estados = semana.de("capturas/estado")
    assert [e["estado"] for e in estados] == ["pc", "nota"]
    assert all(e["destino"] == "nota" and e["entendi"] for e in estados)
    [linha] = semana.de("linhas")
    assert linha == {"chave": "caixa", "dados": {"entendeu": 1, "esperando": 0, "por_destino": {"nota": 1}}}


def test_efeito_que_falha_fica_para_a_proxima_volta_sem_mandar_o_nota(amb, monkeypatch, tmp_path):
    """Revisão da onda 2: com a nota já escrita, um /api/dia que falha não pode virar "nota" com
    destino "plano" sem a fala; a captura fica em `entregues` e o efeito sai na volta seguinte."""
    import urllib.error
    caixa.decidir(_manifest(), "ligar pro Osmar amanhã", FALADO, route=Route.INBOX, agora=AGORA,
                  claude=Modelo(_resp("plano", 0.95, quando="amanha")))
    semana, falhas = Semana(), {"dia": 1}

    def pedido(m, u, k, c=None):
        if u.endswith("/pendentes"):
            return {"capturas": []}
        if u.endswith("/api/dia") and falhas["dia"]:
            falhas["dia"] -= 1
            raise urllib.error.URLError("sem rede")
        return semana(m, u, k, c)

    monkeypatch.setattr(organiza, "_pedido", pedido)
    monkeypatch.setattr(organiza, "caminho_chave_existe", lambda: False)
    monkeypatch.setattr(organiza, "fechar_descartadas", lambda key: 0)
    nota = tmp_path / "n.md"
    nota.write_text("---\ntitulo: T\n---\n", encoding="utf-8")
    organiza._gravar_entregues({CID: NOME})
    monkeypatch.setattr(organiza, "nota_do_pacote", lambda nome, lp: nota)
    organiza.passo("k", config.INBOX_DIR, tmp_path / "of", tmp_path / "ledger.json")
    assert semana.de("dia") == [] and semana.de("capturas/estado") == []
    assert CID in organiza._ler_entregues()
    organiza.passo("k", config.INBOX_DIR, tmp_path / "of", tmp_path / "ledger.json")
    assert len(semana.de("dia")) == 1
    assert [e["estado"] for e in semana.de("capturas/estado")] == ["pc", "nota"]
    assert CID not in organiza._ler_entregues()


def test_cadeado_do_celular_vira_rota_diario_e_nao_passa_pela_caixa(amb):
    """O caminho de verdade do cadeado, desde a captura da Semana: parte `cifrada` → `eh_diario` →
    o drenador põe `tipo: diario` → `manifest_de` fixa `rota: diario` → `elegivel` recusa."""
    c = {"id": CID, "tipo": "nota", "aberta_em": "2026-10-28T14:10:00",
         "partes": [{"id": "p1", "ordem": 1, "tipo": "cifrada", "criada_em": "2026-10-28T14:10:05"}]}
    assert organiza.eh_diario(c)
    m = organiza.manifest_de(dict(c, tipo="diario", partes=[
        {"id": "p1", "ordem": 1, "tipo": "texto", "texto": "ligar pro Osmar amanhã"}]), {})
    assert m["rota"] == "diario"
    claude = Modelo(_resp("plano", 0.99))
    assert caixa.elegivel(m, Route.INBOX, "ligar pro Osmar amanhã") is False
    assert caixa.decidir(m, "ligar pro Osmar amanhã", FALADO, route=Route.INBOX, agora=AGORA,
                         claude=claude, ollama=claude) is None
    assert claude.prompts == [] and not caixa.pasta().exists()


# ---- critério 10: a linha `caixa` só tem números e só sai quando muda ----

def test_linha_caixa_so_numeros_e_so_quando_muda(amb):
    for i, d in enumerate(["plano", "nota", "nota"]):
        caixa.gravar({"captura_id": f"cap-0000{i}", "destino": d, "decidido_em": 1_000_000})
    caixa.gravar({"captura_id": "cap-velha1", "destino": "recado", "decidido_em": 1_000_000 - 13 * 3600})
    semana = Semana()
    assert caixa.publicar_linha("k", 2, semana, agora=1_000_100)
    assert not caixa.publicar_linha("k", 2, semana, agora=1_000_200)  # nada mudou
    [linha] = semana.de("linhas")
    dados = linha["dados"]
    assert dados == {"entendeu": 3, "esperando": 2, "por_destino": {"plano": 1, "nota": 2}}
    # a regra da Semana (lib/caixa.mjs): só estas chaves, só inteiros
    assert set(dados) <= {"entendeu", "esperando", "por_destino"}
    assert all(isinstance(v, int) for v in [dados["entendeu"], dados["esperando"], *dados["por_destino"].values()])
    assert caixa.publicar_linha("k", 2, semana, agora=1_000_000 + 13 * 3600)  # passaram 12 h: zera
    assert semana.de("linhas")[-1]["dados"]["entendeu"] == 0


# ---- critério 8: a correção em 1 toque ----

def _nota_com_destino(tmp_path, destino):
    n = tmp_path / "nota.md"
    n.write_text(f"---\ndate: 2026-10-28\ntitulo: T\ndestino: {destino}\nfonte: {NOME}\n---\n\nfala literal\n", encoding="utf-8")
    return n


def test_correcao_desfaz_aplica_e_troca_o_destino_da_nota(amb, tmp_path):
    caixa.decidir(_manifest(), "amanhã ligo pro Osmar", FALADO, route=Route.INBOX, agora=AGORA,
                  claude=Modelo(_resp("plano", 0.9, "amanha")))
    semana = Semana()
    caixa.aplicar("k", caixa.ler(CID), semana, agora=AGORA)
    nota = _nota_com_destino(tmp_path, "plano")
    ok, frase = caixa.corrigir("k", CID, "fim_de_semana", semana, nota)
    assert ok and frase == "Mudei para Fim de semana."
    assert {"id": f"caixa-{CID}", "acao": "recusar"} in semana.de("dia")
    assert semana.de("complementos") == [{"id": CID, "texto": "texto limpo"}]
    assert semana.de("capturas/estado")[-1]["destino"] == "fim_de_semana"
    texto = nota.read_text(encoding="utf-8")
    assert "destino: fim_de_semana" in texto and "correcoes:\n  - plano → fim_de_semana" in texto
    assert texto.endswith("---\n\nfala literal\n")  # a transcrição não muda
    assert caixa.ultimas_correcoes()[-1]["para"] == "fim_de_semana"
    # de volta ao plano: id novo, porque a fala antiga foi recusada
    ok, _ = caixa.corrigir("k", CID, "plano", semana, nota, agora=AGORA)
    assert ok and semana.de("dia", "PUT")[-1]["id"] == f"caixa-{CID}-2"
    assert {"id": CID, "apagar": True} in semana.de("complementos")
    assert nota.read_text(encoding="utf-8").count("  - ") == 2


def test_correcao_de_recado_avisa_que_ele_fica(amb):
    caixa.decidir(_manifest(), "o botão", FALADO, route=Route.INBOX, agora=AGORA, claude=Modelo(_resp("recado", 0.9)))
    semana = Semana()
    caixa.aplicar("k", caixa.ler(CID), semana, agora=AGORA)
    ok, frase = caixa.corrigir("k", CID, "nota", semana)
    assert ok and "recado já tinha ido" in frase and caixa.ler(CID)["destino"] == "nota"


def test_correcao_recusa_diario_e_captura_sem_decisao(amb):
    assert caixa.corrigir("k", CID, "diario", Semana()) == (False, "Esse destino não existe na caixa.")
    assert caixa.corrigir("k", "nao-passou-1", "nota", Semana())[0] is False


def test_acao_caixa_mudar_pelo_celular(amb, monkeypatch, tmp_path):
    caixa.decidir(_manifest(), "frase", FALADO, route=Route.INBOX, agora=AGORA, claude=Modelo(_resp("nota", 0.9)),
                  pacote=NOME)
    nota = _nota_com_destino(tmp_path, "nota")
    monkeypatch.setattr(organiza, "nota_do_pacote", lambda nome, lp: nota if nome == NOME else None)
    semana = Semana()
    monkeypatch.setattr(acoes_celular, "_pedido", semana)
    chave = X25519PrivateKey.generate()
    aid = "acao-caixa-1"
    dados = {"tipo": "caixa_mudar", "captura_id": CID, "destino": "recado"}
    a = {"id": aid, "tipo": "caixa_mudar",
         "env": json.dumps(cofre.selar(json.dumps(dados).encode(), aid, cofre.publica_b64(chave)))}
    estado, frase = acoes_celular._executar("k", a, chave, tmp_path)
    assert estado == "ok" and frase == "Mudei para Recado."
    assert "destino: recado" in nota.read_text(encoding="utf-8")
    assert len(semana.de("recados", "POST")) == 1


# ---- LOG 12: a linha `manha` ----

def test_manha_publica_uma_vez_e_so_a_noite_de_hoje(amb, monkeypatch):
    from logos import sono
    semana = Semana()
    monkeypatch.setattr(organiza, "_pedido", semana)
    monkeypatch.setattr(organiza, "_MANHA", {"proxima": 0.0})
    monkeypatch.setattr(sono, "manha_de_hoje", lambda agora=None: {"dia": "2026-10-28", "sono_min": 412})
    assert organiza.manha_ao_celular("k")
    assert not organiza.manha_ao_celular("k")  # já foi hoje
    assert semana.de("linhas") == [{"chave": "manha", "dados": {"sono_min": 412}}]
    monkeypatch.setattr(sono, "manha_de_hoje", lambda agora=None: None)  # sem noite de hoje
    (config.STATE_DIR / "organiza" / "manha.json").unlink()
    assert not organiza.manha_ao_celular("k") and len(semana.de("linhas")) == 1


def test_manha_com_a_semana_antiga_tenta_depois_sem_gravar(amb, monkeypatch):
    import urllib.error
    from logos import sono

    def recusa(m, u, k, c=None):
        raise urllib.error.HTTPError(u, 400, "chave desconhecida", None, None)
    monkeypatch.setattr(organiza, "_pedido", recusa)
    monkeypatch.setattr(organiza, "_MANHA", {"proxima": 0.0})
    monkeypatch.setattr(sono, "manha_de_hoje", lambda agora=None: {"dia": "2026-10-28", "sono_min": 412})
    assert not organiza.manha_ao_celular("k")
    assert not (config.STATE_DIR / "organiza" / "manha.json").exists()
    assert organiza._MANHA["proxima"] > 0


def test_manha_de_hoje_le_so_o_dia_certo(amb):
    from logos import sono
    sono._gravar_estado({"manha": {"dia": "2026-10-22", "sono_min": 400}})
    assert sono.manha_de_hoje(datetime(2026, 10, 28, 9).astimezone()) is None  # noite velha não é a de hoje
    sono._gravar_estado({"manha": {"dia": "2026-10-28", "sono_min": 400}})
    assert sono.manha_de_hoje(datetime(2026, 10, 28, 9).astimezone()) == {"dia": "2026-10-28", "sono_min": 400}
    sono._gravar_estado({"manha": {"dia": "2026-10-28", "sono_min": "seis horas"}})
    assert sono.manha_de_hoje(datetime(2026, 10, 28, 9).astimezone()) is None


# ---- onda 4 (28/10/2026): LOG 6a, 6b e 6c ----

DEPOIS = datetime(2026, 10, 31, 10, 0)  # sábado, dois dias depois do "amanhã" (quinta, 29/10)


def test_correcao_tardia_para_plano_fica_nota_sem_fala_de_hoje(amb, tmp_path):
    """6a: a correção para Plano respeita a fala velha. Era Recado; ele toca em Plano no sábado, mas
    a fala era para quinta: o recado não volta, nenhuma fala vai a /api/dia e o destino fica Nota."""
    caixa.decidir(_manifest(), "amanhã ligo pro Osmar", FALADO, route=Route.INBOX, agora=AGORA,
                  claude=Modelo(_resp("fim_de_semana", 0.9, "amanha")))
    semana = Semana()
    caixa.aplicar("k", caixa.ler(CID), semana, agora=AGORA)
    nota = _nota_com_destino(tmp_path, "fim_de_semana")
    ok, frase = caixa.corrigir("k", CID, "plano", semana, nota, agora=DEPOIS)
    assert ok and frase == "Isto era para quinta, 29/10, que já passou: ficou como Nota."
    assert semana.de("dia") == []  # nenhuma fala de hoje
    assert {"id": CID, "apagar": True} in semana.de("complementos")  # o efeito anterior saiu
    dec = caixa.ler(CID)
    assert dec["destino"] == "nota" and dec["entendi"] == frase and dec["alvo"] == ""
    estado = semana.de("capturas/estado")[-1]
    assert estado["destino"] == "nota" and estado["entendi"] == frase and len(frase) <= caixa.MAX_ENTENDI
    assert "destino: nota" in nota.read_text(encoding="utf-8")
    assert caixa.ultimas_correcoes()[-1]["para"] == "plano"  # o que ele disse vai para o prompt


def test_correcao_tardia_de_nota_para_plano_nao_muda_nada(amb, tmp_path):
    caixa.decidir(_manifest(), "hoje à tarde ligo pro Osmar", FALADO, route=Route.INBOX, agora=AGORA,
                  claude=Modelo(_resp("nota", 0.9, "hoje")))
    semana = Semana()
    caixa.aplicar("k", caixa.ler(CID), semana, agora=AGORA)
    nota = _nota_com_destino(tmp_path, "nota")
    antes = nota.read_text(encoding="utf-8")
    ok, frase = caixa.corrigir("k", CID, "plano", semana, nota, agora=DEPOIS)
    assert ok and frase == "Isto era para quarta, 28/10, que já passou: ficou como Nota."
    assert semana.pedidos == [] and nota.read_text(encoding="utf-8") == antes
    assert caixa.ler(CID)["destino"] == "nota" and caixa.ler(CID)["correcoes"] == []


def test_correcao_para_plano_ainda_valida_leva_o_dia_na_frente(amb):
    """Dentro do prazo: vira fala de hoje, com o "Para quinta, 29/10:" (antes da correção, o alvo
    ficava vazio quando a decisão original não era Plano)."""
    caixa.decidir(_manifest(), "amanhã ligo pro Osmar", FALADO, route=Route.INBOX, agora=AGORA,
                  claude=Modelo(_resp("nota", 0.9, "amanha")))
    semana = Semana()
    caixa.aplicar("k", caixa.ler(CID), semana, agora=AGORA)
    ok, frase = caixa.corrigir("k", CID, "plano", semana, agora=AGORA)
    assert ok and frase == "Mudei para Plano."
    [fala] = semana.de("dia")
    assert fala["dia"] == "2026-10-28" and fala["texto"].startswith("Para quinta, 29/10:")


def test_correcao_para_plano_sem_dia_dito_vale_para_hoje(amb):
    """Sem `quando`, ele não disse dia nenhum: dias depois, o toque em Plano põe no plano de hoje."""
    caixa.decidir(_manifest(), "ligar pro encanador", FALADO, route=Route.INBOX, agora=AGORA,
                  claude=Modelo(_resp("nota", 0.9, "")))
    semana = Semana()
    caixa.aplicar("k", caixa.ler(CID), semana, agora=AGORA)
    ok, frase = caixa.corrigir("k", CID, "plano", semana, agora=DEPOIS)
    assert ok and frase == "Mudei para Plano."
    [fala] = semana.de("dia")
    assert fala["dia"] == "2026-10-31" and fala["texto"].startswith("Para sábado, 31/10:")


def test_acao_caixa_mudar_tardia_registra_o_destino_que_ficou(amb, monkeypatch, tmp_path):
    caixa.decidir(_manifest(), "frase", FALADO, route=Route.INBOX, agora=AGORA,
                  claude=Modelo(_resp("recado", 0.9, "hoje")), pacote=NOME)
    monkeypatch.setattr(organiza, "nota_do_pacote", lambda nome, lp: None)
    monkeypatch.setattr(caixa, "datetime", type("D", (datetime,), {"now": staticmethod(lambda: DEPOIS)}))
    semana = Semana()
    monkeypatch.setattr(acoes_celular, "_pedido", semana)
    vistos = []
    monkeypatch.setattr(acoes_celular.eventos, "registrar", lambda *a, **k: vistos.append(a[1]))
    chave = X25519PrivateKey.generate()
    aid = "acao-caixa-2"
    dados = {"tipo": "caixa_mudar", "captura_id": CID, "destino": "plano"}
    a = {"id": aid, "tipo": "caixa_mudar",
         "env": json.dumps(cofre.selar(json.dumps(dados).encode(), aid, cofre.publica_b64(chave)))}
    estado, frase = acoes_celular._executar("k", a, chave, tmp_path)
    assert estado == "ok" and "ficou como Nota" in frase
    assert vistos and vistos[-1].endswith("(Nota).")


def test_linha_caixa_nao_conta_como_entendeu_quando_nenhum_modelo_respondeu(amb):
    """6b: os dois modelos fora não é "PC entendeu". O formato fica o do contrato (só as 3 chaves)."""
    caixa.gravar({"captura_id": "cap-ok-0001", "destino": "plano", "modelo": "claude-opus-5-5", "decidido_em": 1_000_000})
    caixa.gravar({"captura_id": "cap-fora-001", "destino": "nota", "modelo": "nenhum", "decidido_em": 1_000_000})
    # corrigida por ele depois: conta (o destino foi dele)
    caixa.gravar({"captura_id": "cap-fora-002", "destino": "recado", "modelo": "nenhum", "decidido_em": 1_000_000,
                  "correcoes": [{"de": "nota", "para": "recado"}]})
    dados = caixa.contagens(0, agora=1_000_100)
    assert dados == {"entendeu": 2, "esperando": 0, "por_destino": {"plano": 1, "recado": 1}}
    assert set(dados) == {"entendeu", "esperando", "por_destino"}


def test_drenador_conta_nenhum_como_esperando_ate_virar_nota(amb, monkeypatch, tmp_path):
    falha = Modelo(erro=RuntimeError("fora"))
    caixa.decidir(_manifest(), "qualquer coisa", FALADO, route=Route.INBOX, agora=AGORA, claude=falha, ollama=falha)
    semana = Semana()
    monkeypatch.setattr(organiza, "_pedido", lambda m, u, k, c=None: (
        {"capturas": []} if u.endswith("/pendentes") else semana(m, u, k, c)))
    monkeypatch.setattr(organiza, "caminho_chave_existe", lambda: False)
    monkeypatch.setattr(organiza, "fechar_descartadas", lambda key: 0)
    organiza._gravar_entregues({CID: NOME})
    monkeypatch.setattr(organiza, "nota_do_pacote", lambda nome, lp: None)  # a nota ainda não saiu
    organiza.passo("k", config.INBOX_DIR, tmp_path / "of", tmp_path / "ledger.json")
    assert semana.de("linhas")[-1]["dados"] == {"entendeu": 0, "esperando": 1, "por_destino": {}}
    nota = tmp_path / "n.md"
    nota.write_text("---\ntitulo: T\n---\n", encoding="utf-8")
    monkeypatch.setattr(organiza, "nota_do_pacote", lambda nome, lp: nota)
    organiza.passo("k", config.INBOX_DIR, tmp_path / "of", tmp_path / "ledger.json")  # vira nota
    organiza.passo("k", config.INBOX_DIR, tmp_path / "of", tmp_path / "ledger.json")
    assert semana.de("linhas")[-1]["dados"] == {"entendeu": 0, "esperando": 0, "por_destino": {}}


def test_tempo_no_log_e_o_total_quando_o_claude_falha(amb, caplog, monkeypatch):
    """6c: o Claude gasta 60 s e falha, o Ollama responde em 3 s: o log diz 63 s, com a reserva."""
    relogio = iter([100.0, 160.0, 163.0, 163.0])
    monkeypatch.setattr(caixa.time, "monotonic", lambda: next(relogio))
    claude = Modelo(erro=TimeoutError("passou de 60 s"))
    ollama = Modelo(_resp("nota", 0.8), nome="gemma4:e4b")
    with caplog.at_level(logging.INFO, logger="logos.caixa"):
        dec = caixa.decidir(_manifest(), "uma ideia", FALADO, route=Route.INBOX, agora=AGORA,
                            claude=claude, ollama=ollama)
    assert dec["segundos"] == 63.0 and dec["segundos_reserva"] == 3.0
    assert "por gemma4:e4b em 63,0 s (reserva 3,0 s)" in caplog.text
