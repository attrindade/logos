"""Ações do app Organiza: marcar tarefa no vault, analisar conversa, lista de contatos, e as marcas."""
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from logos import acoes_celular, cofre, config, organiza, pipeline


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "work" / "active").mkdir(parents=True)
    (tmp_path / "capturas" / "Diario").mkdir(parents=True)
    return tmp_path


def test_concluir_marca_a_linha_pelo_texto_e_mantem_crlf(vault):
    f = vault / "work" / "active" / "Organiza.md"
    f.write_bytes("# Organiza\r\n- [ ] Baixar  o botão\r\n  - [ ] outra coisa\r\n".encode("utf-8"))
    ok, frase = acoes_celular.concluir(vault, "work/active/Organiza.md", "Baixar o botão")
    assert ok, frase
    assert f.read_bytes() == "# Organiza\r\n- [x] Baixar  o botão\r\n  - [ ] outra coisa\r\n".encode("utf-8")


def test_concluir_tarefa_aninhada_e_numerada(vault):
    f = vault / "work" / "active" / "X.md"
    f.write_text("1. [ ] primeira\n    - [ ] filha\n", encoding="utf-8")
    assert acoes_celular.concluir(vault, "work/active/X.md", "filha")[0]
    assert f.read_text(encoding="utf-8") == "1. [ ] primeira\n    - [x] filha\n"


def test_concluir_nao_mexe_quando_nao_acha_ou_acha_duas(vault):
    f = vault / "work" / "active" / "X.md"
    original = "- [ ] a\n- [ ] a\n- [x] b\n"
    f.write_text(original, encoding="utf-8")
    ok, frase = acoes_celular.concluir(vault, "work/active/X.md", "a")
    assert not ok and "2 tarefas" in frase
    ok, frase = acoes_celular.concluir(vault, "work/active/X.md", "b")
    assert not ok and "Não achei" in frase
    assert f.read_text(encoding="utf-8") == original


def test_concluir_so_em_nota_que_vai_ao_celular(vault):
    d = vault / "capturas" / "Diario" / "D.md"
    d.write_text("- [ ] segredo\n", encoding="utf-8")
    for caminho in ("capturas/Diario/D.md", "work/active/../../capturas/Diario/D.md"):
        ok, _ = acoes_celular.concluir(vault, caminho, "segredo")
        assert not ok
    assert d.read_text(encoding="utf-8") == "- [ ] segredo\n"


def test_contatos_validos_e_limite(tmp_path, monkeypatch):
    monkeypatch.setattr(acoes_celular, "contatos_path", lambda: tmp_path / "c.json")
    n = acoes_celular.guardar_contatos([
        {"jid": "5551@s.whatsapp.net", "nome": "Clara", "dias": 14},
        {"jid": "x", "dias": 0},           # frequência fora
        {"jid": "y", "dias": True},        # bool não é número
        {"nome": "sem jid", "dias": 7},
    ])
    assert n == 1
    assert acoes_celular.contatos() == [{"jid": "5551@s.whatsapp.net", "nome": "Clara", "dias": 14}]


def test_analisar_monta_prompt_com_audio_transcrito_e_limpa_saida(monkeypatch):
    from logos import whatsapp_celular
    monkeypatch.setattr(whatsapp_celular, "_conversa", lambda jid: {"mensagens": [
        {"de_mim": False, "autor": "", "ts": 1, "tipo": "audio", "transcricao": "oi, sumido"},
        {"de_mim": True, "ts": 2, "tipo": "texto", "texto": "oi!"},
    ]})
    visto = {}

    def gerar(prompt):
        visto["p"] = prompt
        return {"situacao": "Ela mandou áudio.", "caminhos": [{"rotulo": "Ligar", "como": "x", "rascunho": "y"}] * 5,
                "atencao": ["", "tom"], "extra": "fora"}
    a = acoes_celular.analisar("j", "Clara", "se respondo hoje", gerar=gerar)
    assert "[áudio] oi, sumido" in visto["p"] and "Eu: oi!" in visto["p"] and "se respondo hoje" in visto["p"]
    assert len(a["caminhos"]) == 3 and a["atencao"] == ["tom"] and "extra" not in a


def test_marcas_abrem_e_entram_no_manifest():
    chave = X25519PrivateKey.generate()
    pub = cofre.publica_b64(chave)
    dentro = {"projetos": ["Organiza", ""], "pessoas": ["Clara"], "responde": {"titulo": "Organiza", "trecho": "botão"}}
    env = json.dumps(cofre.selar(json.dumps(dentro).encode(), "nota-123", pub))
    c = {"id": "nota-123", "tipo": "nota", "marcas": env,
         "partes": [{"id": "p1", "tipo": "texto", "texto": "oi", "ordem": 0}]}
    m = organiza.abrir_marcas(c, chave)
    assert m == {"projetos": ["Organiza"], "pessoas": ["Clara"],
                 "responde": {"titulo": "Organiza", "caminho": "", "trecho": "botão"}}
    man = organiza.manifest_de(dict(c, marcas_abertas=m), {})
    assert man["marcas"]["projetos"] == ["Organiza"]
    # envelope de outra nota (dado associado diferente) não abre e não segura a nota
    assert organiza.abrir_marcas(dict(c, id="outra-123"), chave) == {}


def test_aplicar_marcas_poe_as_escolhidas_primeiro_sem_repetir():
    e = {"projetos": ["[[organiza]]", "[[Logos]]"], "pessoas": []}
    pipeline.aplicar_marcas(e, {"projetos": ["Organiza"], "pessoas": ["Clara"], "times": ["PPJ"],
                                "responde": {"titulo": "Organiza", "trecho": "x"}})
    assert e["projetos"] == ["[[Organiza]]", "[[Logos]]"]
    assert e["pessoas"] == ["[[Clara]]"] and e["times"] == ["[[PPJ]]"]
    assert e["responde_a"] == "[[Organiza]]" and e["responde_trecho"] == "x"
    pipeline.aplicar_marcas(e, None)  # sem marcas, nada muda


def _acao(chave_pub, aid, tipo, dados):
    return {"id": aid, "tipo": tipo, "env": json.dumps(cofre.selar(json.dumps(dados).encode(), aid, chave_pub))}


def test_tipo_de_fora_precisa_bater_com_o_de_dentro(tmp_path, monkeypatch):
    monkeypatch.setattr(acoes_celular, "contatos_path", lambda: tmp_path / "c.json")
    chave = X25519PrivateKey.generate()
    a = _acao(cofre.publica_b64(chave), "acao-0001", "contatos", {"tipo": "analisar", "jid": "x"})
    estado, frase = acoes_celular._executar("k", a, chave, tmp_path)
    assert estado == "falha" and "tipo" in frase
    assert not (tmp_path / "c.json").exists()  # a lista não foi apagada


def test_concluir_espera_o_job_do_vault(tmp_path, monkeypatch):
    from logos import jobs
    monkeypatch.setattr(jobs, "lock_dono", lambda: {"pid": 1})
    chave = X25519PrivateKey.generate()
    a = _acao(cofre.publica_b64(chave), "acao-0002", "concluir", {"tipo": "concluir", "caminho": "work/active/X.md", "texto": "a"})
    with pytest.raises(acoes_celular.Ocupado):
        acoes_celular._executar("k", a, chave, tmp_path)


def test_acao_feita_nao_e_refeita_quando_o_ok_nao_chegou(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    chave = X25519PrivateKey.generate()
    monkeypatch.setattr(cofre, "carregar_ou_criar", lambda: chave)
    a = _acao(cofre.publica_b64(chave), "acao-0003", "contatos", {"tipo": "contatos", "lista": []})
    puts, rodou = [], []
    monkeypatch.setattr(acoes_celular, "_pedido", lambda m, u, k, c=None: (
        {"acoes": [dict(a)]} if m == "GET" else (puts.append(c), (_ for _ in ()).throw(OSError("rede")))[1]))
    orig = acoes_celular._executar
    monkeypatch.setattr(acoes_celular, "_executar", lambda *x: (rodou.append(1), orig(*x))[1])
    acoes_celular.passo("k", raiz=tmp_path)
    acoes_celular.passo("k", raiz=tmp_path)
    assert len(rodou) == 1 and len(puts) == 2  # executou uma vez, reenviou o estado na segunda


def test_pacote_nao_e_processado_por_duas_threads(monkeypatch, tmp_path):
    assert pipeline._pegar("p1") and not pipeline._pegar("p1")
    pipeline._soltar("p1")
    assert pipeline._pegar("p1")
    pipeline._soltar("p1")
