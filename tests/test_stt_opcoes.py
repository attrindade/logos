"""N1 e N5 do Whisper (rodada de 25/10/2026): desligados de fábrica; ligados, mudam só a chamada."""
from logos import config, stt


def test_desligados_de_fabrica_a_chamada_fica_como_antes(monkeypatch):
    monkeypatch.setattr(config, "WHISPER_ANTI_ALUCINACAO", False)
    monkeypatch.setattr(config, "WHISPER_VOCABULARIO", False)
    assert stt.opcoes_transcribe() == {}


def test_n5_liga_o_limiar_de_silencio(monkeypatch):
    monkeypatch.setattr(config, "WHISPER_ANTI_ALUCINACAO", True)
    monkeypatch.setattr(config, "WHISPER_VOCABULARIO", False)
    op = stt.opcoes_transcribe()
    assert op == {"hallucination_silence_threshold": 2.0, "word_timestamps": True}


def test_frases_inventadas_sem_acento_caixa_ou_pontuacao():
    assert stt.inventada(" Legendas pela comunidade Amara.org ")
    assert stt.inventada("Obrigado por assistir!")
    assert stt.inventada("INSCREVA-SE NO CANAL.")
    assert not stt.inventada("Obrigado por assistir à reunião de ontem, Igor.")
    assert not stt.inventada("mandar o roteiro ao Renato")


def test_n1_vocabulario_do_arquivo_e_dos_nomes_das_notas(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "org/people").mkdir(parents=True)
    (vault / "work/active").mkdir(parents=True)
    (vault / "org/people/Igor Tal.md").write_text("conteúdo que não é lido", encoding="utf-8")
    (vault / "work/active/Governa Clima.md").write_text("x", encoding="utf-8")
    extra = tmp_path / "vocabulario.txt"
    extra.write_text("# comentário\nSyncthing\nmLauncher\n\nigor tal\n", encoding="utf-8")
    assert stt.vocabulario(vault, extra) == ["Syncthing", "mLauncher", "igor tal", "Governa Clima"]
    monkeypatch.setattr(config, "WHISPER_ANTI_ALUCINACAO", False)
    monkeypatch.setattr(config, "WHISPER_VOCABULARIO", True)
    monkeypatch.setattr(stt, "vocabulario", lambda: ["Igor", "Planeja"])
    assert stt.opcoes_transcribe() == {"initial_prompt": "Nomes: Igor, Planeja.", "hotwords": "Igor Planeja"}


def test_n1_vocabulario_cabe_no_prompt(tmp_path):
    extra = tmp_path / "v.txt"
    extra.write_text("\n".join(f"Nome{i:03d}" for i in range(300)), encoding="utf-8")
    nomes = stt.vocabulario(tmp_path / "nada", extra)
    assert 0 < len(nomes) < 300 and sum(len(n) + 2 for n in nomes) <= stt.VOCAB_MAX_CARACTERES


def test_transcribe_file_filtra_o_segmento_inventado_so_com_a_chave(tmp_path, monkeypatch):
    class Seg:
        def __init__(self, t):
            self.text = t

    class Info:
        duration = 10.0

    chamadas = []

    class Modelo:
        def transcribe(self, caminho, **kw):
            chamadas.append(kw)
            return iter([Seg(" Bom dia."), Seg(" Obrigado por assistir.")]), Info()

    wav = tmp_path / "a.wav"
    wav.write_bytes(b"")
    monkeypatch.setattr(stt, "extract_audio_to_wav", lambda p: wav)
    monkeypatch.setattr(stt, "get_model", lambda: Modelo())
    monkeypatch.setattr(config, "WHISPER_VOCABULARIO", False)
    monkeypatch.setattr(config, "WHISPER_ANTI_ALUCINACAO", False)
    assert stt.transcribe_file(tmp_path / "x.m4a").text == "Bom dia. Obrigado por assistir."
    wav.write_bytes(b"")
    monkeypatch.setattr(config, "WHISPER_ANTI_ALUCINACAO", True)
    assert stt.transcribe_file(tmp_path / "x.m4a").text == "Bom dia."
    assert "hallucination_silence_threshold" not in chamadas[0] and chamadas[1]["hallucination_silence_threshold"] == 2.0


def test_n1_satelite_do_projeto_vira_so_o_nome_do_projeto(tmp_path):
    vault = tmp_path / "vault"
    (vault / "work/active").mkdir(parents=True)
    for nome in ("Governa Clima.md", "Governa Clima — Reuniões Log.md", "Logos — Arquitetura.md"):
        (vault / "work/active" / nome).write_text("x", encoding="utf-8")
    assert stt.vocabulario(vault, tmp_path / "sem.txt") == ["Governa Clima", "Logos"]


def test_n5_frases_medidas_no_ruido_sem_vad():
    # 25/10/2026, 5 trechos de ruído sem VAD, chaves desligadas: o Whisper escreveu estas.
    assert stt.inventada("Se inscreva no canal.") and stt.inventada("Legenda Adriana Zanotto")
