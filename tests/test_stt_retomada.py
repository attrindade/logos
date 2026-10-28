"""Transcrição retomável (LOG 7 da onda 4, 28/10/2026), com o Whisper e o ffmpeg simulados: o
progresso vai por segmento para state/stt/<hash>.jsonl e, se o mesmo áudio voltar, continua do fim
do último segmento, com o mesmo modelo e os mesmos parâmetros."""
import json

import pytest

from logos import config, stt


class Seg:
    def __init__(self, inicio, fim, texto):
        self.start, self.end, self.text = inicio, fim, texto


class Info:
    def __init__(self, duracao):
        self.duration = duracao


class Queda(Exception):
    pass


class Whisper:
    """Devolve os segmentos do trecho pedido (relativos ao começo do trecho, como o de verdade) e
    pode cair depois de N segmentos, no meio do gerador."""

    def __init__(self, segmentos, total, cai_depois=None):
        self.segmentos, self.total, self.cai_depois = segmentos, total, cai_depois
        self.chamadas, self.inicio = [], 0.0

    def transcribe(self, caminho, **kw):
        self.chamadas.append(kw)
        inicio = self.inicio
        resto = [(a - inicio, b - inicio, t) for a, b, t in self.segmentos if a >= inicio - 1e-6]
        cai = self.cai_depois

        def gerar():
            for i, (a, b, t) in enumerate(resto):
                if cai is not None and i == cai:
                    raise Queda("o watcher caiu")
                yield Seg(a, b, t)

        return gerar(), Info(self.total - inicio)


SEGS = [(0.0, 20.0, " Primeira parte."), (20.0, 41.5, " Segunda parte."), (41.5, 63.0, " Terceira parte."),
        (63.0, 88.0, " Quarta parte."), (88.0, 120.0, " Quinta parte.")]
TUDO = "Primeira parte. Segunda parte. Terceira parte. Quarta parte. Quinta parte."


@pytest.fixture
def amb(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "WHISPER_ANTI_ALUCINACAO", False)
    monkeypatch.setattr(config, "WHISPER_VOCABULARIO", False)
    audio = tmp_path / "reuniao.m4a"
    audio.write_bytes(b"audio de 2 minutos" * 1000)
    cortes = []

    def extrair(p, inicio_s=0.0):
        cortes.append(inicio_s)
        w = tmp_path / f"t{len(cortes)}.wav"
        w.write_bytes(b"wav")
        return w

    monkeypatch.setattr(stt, "extract_audio_to_wav", extrair)
    return audio, cortes


def _usar(monkeypatch, w):
    monkeypatch.setattr(stt, "get_model", lambda: w)


def _parciais():
    pasta = config.STATE_DIR / "stt"
    return sorted(pasta.glob("*.jsonl")) if pasta.exists() else []


def test_sem_queda_transcreve_inteiro_e_nao_deixa_parcial(amb, monkeypatch):
    audio, cortes = amb
    _usar(monkeypatch, Whisper(SEGS, 120.0))
    t = stt.transcribe_file(audio)
    assert t.text == TUDO and t.duration_s == 120.0
    assert cortes == [0.0] and _parciais() == []


def test_queda_no_meio_grava_o_progresso_e_a_volta_continua_de_onde_parou(amb, monkeypatch, caplog):
    audio, cortes = amb
    w = Whisper(SEGS, 120.0, cai_depois=3)
    _usar(monkeypatch, w)
    with pytest.raises(Queda):
        stt.transcribe_file(audio)
    [p] = _parciais()
    linhas = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]
    assert linhas[0]["duracao_s"] == 120.0 and linhas[0]["assinatura"]["modelo"] == config.WHISPER_MODEL_SIZE
    assert [l["fim"] for l in linhas[1:]] == [20.0, 41.5, 63.0]

    # O watcher volta: o mesmo áudio continua de 63 s, com os mesmos parâmetros.
    w.cai_depois, w.inicio = None, 63.0
    import logging
    with caplog.at_level(logging.INFO, logger="logos.stt"):
        t = stt.transcribe_file(audio)
    assert cortes == [0.0, 63.0]
    assert t.text == TUDO and t.duration_s == 120.0  # a duração é a do áudio inteiro
    # beam, língua, VAD: nada muda na retomada; só o texto já pronto vai de contexto (como o
    # condition_on_previous_text faria sem a queda; no teste real de 28/10, o texto saiu idêntico)
    assert w.chamadas[1] == {**w.chamadas[0], "initial_prompt": "Primeira parte. Segunda parte. Terceira parte."}
    assert _parciais() == []
    assert "retomando de 63.0 s de 120.0 s (3 segmentos já prontos)" in caplog.text


def test_com_o_vocabulario_ligado_o_prompt_dele_fica(amb, monkeypatch):
    audio, cortes = amb
    monkeypatch.setattr(config, "WHISPER_VOCABULARIO", True)
    monkeypatch.setattr(stt, "vocabulario", lambda: ["Igor"])
    w = Whisper(SEGS, 120.0, cai_depois=3)
    _usar(monkeypatch, w)
    with pytest.raises(Queda):
        stt.transcribe_file(audio)
    w.cai_depois, w.inicio = None, 63.0
    assert stt.transcribe_file(audio).text == TUDO
    assert w.chamadas[1]["initial_prompt"] == w.chamadas[0]["initial_prompt"] == "Nomes: Igor."


def test_o_mesmo_audio_com_outro_nome_tambem_retoma(amb, monkeypatch):
    audio, cortes = amb
    w = Whisper(SEGS, 120.0, cai_depois=2)
    _usar(monkeypatch, w)
    with pytest.raises(Queda):
        stt.transcribe_file(audio)
    copia = audio.with_name("2026-10-27 reuniao (1).m4a")
    copia.write_bytes(audio.read_bytes())
    w.cai_depois, w.inicio = None, 41.5
    assert stt.transcribe_file(copia).text == TUDO and cortes[-1] == 41.5


def test_outro_audio_nao_usa_o_parcial(amb, monkeypatch):
    audio, cortes = amb
    w = Whisper(SEGS, 120.0, cai_depois=2)
    _usar(monkeypatch, w)
    with pytest.raises(Queda):
        stt.transcribe_file(audio)
    outro = audio.with_name("outro.m4a")
    outro.write_bytes(b"outra gravacao" * 1000)
    w.cai_depois = None
    assert stt.transcribe_file(outro).text == TUDO and cortes[-1] == 0.0
    assert len(_parciais()) == 1  # o do primeiro áudio continua esperando por ele


def test_modelo_trocado_descarta_o_parcial(amb, monkeypatch):
    audio, cortes = amb
    w = Whisper(SEGS, 120.0, cai_depois=3)
    _usar(monkeypatch, w)
    with pytest.raises(Queda):
        stt.transcribe_file(audio)
    monkeypatch.setattr(config, "WHISPER_MODEL_SIZE", "outro-modelo")
    w.cai_depois = None
    assert stt.transcribe_file(audio).text == TUDO and cortes[-1] == 0.0
    assert _parciais() == []


def test_linha_pela_metade_e_ignorada(amb, monkeypatch):
    audio, cortes = amb
    w = Whisper(SEGS, 120.0, cai_depois=3)
    _usar(monkeypatch, w)
    with pytest.raises(Queda):
        stt.transcribe_file(audio)
    [p] = _parciais()
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"inicio": 63.0, "fim": 8')  # a queda no meio da escrita
    w.cai_depois, w.inicio = None, 63.0
    assert stt.transcribe_file(audio).text == TUDO and cortes[-1] == 63.0


def test_queda_depois_do_ultimo_segmento_nao_chama_o_whisper(amb, monkeypatch):
    audio, cortes = amb
    w = Whisper(SEGS, 120.0)
    _usar(monkeypatch, w)
    # Simula: os 5 segmentos gravados, e a queda antes de apagar o parcial.
    apagar = stt.Parcial.apagar
    monkeypatch.setattr(stt.Parcial, "apagar", lambda self: self.fechar())
    stt.transcribe_file(audio)
    assert len(_parciais()) == 1
    monkeypatch.setattr(stt.Parcial, "apagar", apagar)
    monkeypatch.setattr(stt, "get_model", lambda: pytest.fail("não devia carregar o modelo"))
    monkeypatch.setattr(stt, "extract_audio_to_wav", lambda *a, **k: pytest.fail("não devia cortar o áudio"))
    t = stt.transcribe_file(audio)
    assert t.text == TUDO and t.duration_s == 120.0 and _parciais() == []


def test_audio_curto_nao_grava_progresso(amb, monkeypatch):
    audio, cortes = amb
    w = Whisper([(0.0, 10.0, " Curta."), (10.0, 30.0, " Ainda curta.")], 30.0, cai_depois=1)
    _usar(monkeypatch, w)
    with pytest.raises(Queda):
        stt.transcribe_file(audio)
    assert _parciais() == []


def test_frase_inventada_sai_das_duas_metades(amb, monkeypatch):
    audio, cortes = amb
    monkeypatch.setattr(config, "WHISPER_ANTI_ALUCINACAO", True)
    segs = [(0.0, 30.0, " Bom dia."), (30.0, 60.0, " Obrigado por assistir."), (60.0, 90.0, " Vamos lá."),
            (90.0, 120.0, " Obrigado por assistir.")]
    w = Whisper(segs, 120.0, cai_depois=2)
    _usar(monkeypatch, w)
    with pytest.raises(Queda):
        stt.transcribe_file(audio)
    w.cai_depois, w.inicio = None, 60.0
    assert stt.transcribe_file(audio).text == "Bom dia. Vamos lá."


def test_parcial_velho_sai_depois_de_7_dias(amb, monkeypatch):
    import os
    import time
    pasta = config.STATE_DIR / "stt"
    pasta.mkdir(parents=True)
    velho = pasta / "abc.jsonl"
    velho.write_text("{}\n", encoding="utf-8")
    antigo = time.time() - 8 * 24 * 3600
    os.utime(velho, (antigo, antigo))
    novo = pasta / "def.jsonl"
    novo.write_text("{}\n", encoding="utf-8")
    stt._limpar_parciais_velhos()
    assert not velho.exists() and novo.exists()


def test_corte_vai_depois_do_i_no_ffmpeg(monkeypatch, tmp_path):
    vistos = []

    class R:
        returncode = 0
        stderr = b""

    monkeypatch.setattr(stt, "check_ffmpeg", lambda: True)
    monkeypatch.setattr(stt.subprocess, "run", lambda cmd, **kw: (vistos.append(cmd), R())[1])
    stt.extract_audio_to_wav(tmp_path / "a.m4a")
    stt.extract_audio_to_wav(tmp_path / "a.m4a", inicio_s=63.25)
    assert "-ss" not in vistos[0]
    c = vistos[1]
    assert c.index("-ss") > c.index("-i") and c[c.index("-ss") + 1] == "63.250"
