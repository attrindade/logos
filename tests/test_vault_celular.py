"""Cópia cifrada do vault para o celular: o que sai, o que nunca sai, e a volta pela Semana."""
import json

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from logos import cofre, config, organiza, vault_celular


def _vault(tmp_path):
    v = tmp_path / "vault"
    for rel in ["brain/Gotchas.md", "work/Index.md", "work/Decisão - X.md", "work/archive/2025/velho.md",
                "work/active/Projeto.md", "capturas/Diario/Diário — 2026-10-14.md", "capturas/Inbox/ideia.md",
                "inbox/sessoes-2026-10-11.md", "perf/Brag.md", "reference/Mapa.md", "thinking/rascunho.md",
                "org/people/Fulano.md", "brain/imagem.png"]:
        f = v / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"conteúdo de {rel}", encoding="utf-8")
    return v


def test_lista_tem_o_combinado_e_nunca_o_diario_nem_o_arquivo(tmp_path):
    rels = set(vault_celular.notas(_vault(tmp_path)))
    assert rels == {"brain/Gotchas.md", "work/Index.md", "work/Decisão - X.md", "work/active/Projeto.md",
                    "capturas/Inbox/ideia.md", "inbox/sessoes-2026-10-11.md", "perf/Brag.md",
                    "reference/Mapa.md", "org/people/Fulano.md"}


def test_diario_fica_de_fora_mesmo_se_alguem_puser_na_lista(tmp_path, monkeypatch):
    v = _vault(tmp_path)
    monkeypatch.setattr(vault_celular, "RECURSIVAS", vault_celular.RECURSIVAS + ("capturas",))
    assert not any("Diario" in r for r in vault_celular.notas(v))


def test_passo_manda_cifrado_so_o_que_mudou_e_apaga_o_que_saiu(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    v = _vault(tmp_path)
    cel = X25519PrivateKey.generate()
    monkeypatch.setattr(organiza, "chave_celular", lambda k: cofre.publica_b64(cel))
    nuvem = {"velho123velho123velho123velho123": "x" * 32}
    enviados = {}

    def falso(metodo, url, key, corpo=None):
        if metodo == "GET":
            return {"itens": [{"id": i, "versao": ver} for i, ver in nuvem.items()]}
        if metodo == "PUT":
            nuvem[corpo["id"]] = corpo["versao"]
            enviados[corpo["id"]] = corpo["env"]
        if metodo == "DELETE":
            nuvem.pop(url.split("id=")[1])
        return {"ok": True}

    monkeypatch.setattr(organiza, "_pedido", falso)
    assert vault_celular.passo("k", v) == (9, 1)
    # nada em claro no que foi para a nuvem; o celular abre e acha o caminho dentro
    for i, env in enviados.items():
        assert "conteúdo" not in env and "brain" not in env
        dentro = json.loads(cofre.abrir_do_pc(json.loads(env), i, cel))
        assert dentro["texto"] == f"conteúdo de {dentro['caminho']}"
    enviados.clear()
    assert vault_celular.passo("k", v) == (0, 0)          # nada mudou
    (v / "brain/Gotchas.md").write_text("mudou", encoding="utf-8")
    assert vault_celular.passo("k", v) == (1, 0)


def test_sem_chave_do_celular_nao_manda_nada(tmp_path, monkeypatch):
    monkeypatch.setattr(organiza, "chave_celular", lambda k: None)
    monkeypatch.setattr(organiza, "_pedido", lambda *a, **k: (_ for _ in ()).throw(AssertionError("não devia chamar")))
    assert vault_celular.passo("k", _vault(tmp_path)) is None
