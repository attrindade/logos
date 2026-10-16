"""Espelho do Grava no Drive: só cópia, nunca o Diário, nunca arquivo pela metade."""
import logging
import os
import threading
import time

import pytest

from logos import config, grava_drive, watcher
from logos.grava_drive import apelido_do_nome, espelhar
from logos.ledger import Entry, Ledger, State

VELHO = time.time() - 3600


@pytest.fixture(autouse=True)
def _estado_limpo(monkeypatch):
    monkeypatch.setattr(grava_drive, "_vistos", {})
    monkeypatch.setattr(grava_drive, "_conflitos_avisados", set())
    monkeypatch.setattr(grava_drive, "_falhas_avisadas", set())
    monkeypatch.setattr(grava_drive, "_avisos_dados", set())
    monkeypatch.setattr(grava_drive, "_drive_ausente_avisado", False)
    # Lock novo por teste: um teste que falhe com o lock preso não trava os outros.
    monkeypatch.setattr(grava_drive, "_passada_lock", threading.Lock())


@pytest.fixture
def pastas(tmp_path):
    grava = tmp_path / "Grava"
    (grava / "transcrever").mkdir(parents=True)
    (grava / "guardar").mkdir()
    drive = tmp_path / "MeuDrive" / "Grava"
    drive.parent.mkdir()
    return grava, drive


def _audio(caminho, conteudo=b"audio", mtime=VELHO):
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_bytes(conteudo)
    os.utime(caminho, (mtime, mtime))
    return caminho


def test_copia_transcrever_e_guardar(pastas):
    grava, drive = pastas
    a = _audio(grava / "transcrever" / "2026-10-15 22-21-50 reuniao Conselho.m4a")
    b = _audio(grava / "guardar" / "rpg" / "2026-10-15 23-00-00 rpg.m4a", b"rpg longo")
    c = _audio(grava / "transcrever" / "2026-10-15 23-10-00.m4a")  # sem apelido

    feito = espelhar(grava, drive)

    assert feito["copiados"] == 3
    assert (drive / "reuniao" / a.name).read_bytes() == b"audio"
    assert (drive / "rpg" / b.name).read_bytes() == b"rpg longo"
    assert (drive / "outros" / c.name).exists()
    # Cópia, não movimento; e nenhum temporário sobra no destino.
    assert a.exists() and b.exists() and c.exists()
    assert not [p for p in drive.rglob(".*")]
    # mtime preservado (copy2): a data da gravação continua a do celular.
    assert abs((drive / "reuniao" / a.name).stat().st_mtime - VELHO) < 2


def test_nunca_copia_diario(pastas):
    grava, drive = pastas
    _audio(grava / "transcrever" / "2026-10-15 22-00-00 diario.m4a")
    _audio(grava / "transcrever" / "2026-10-15 22-00-01 diarios.m4a")
    _audio(grava / "guardar" / "diario" / "2026-10-15 22-00-02 ideia.m4a")  # pela pasta
    _audio(grava / "transcrever" / "2026-10-15 22-00-03 Diário.m4a")  # nome dado à mão
    _audio(grava / "transcrever" / "2026-10-15 22-00-04 DIARIO extra.m4a")
    # Sem o padrão de data, postos à mão em guardar/: antes caíam em "outros".
    _audio(grava / "guardar" / "x" / "Diário.m4a")
    _audio(grava / "guardar" / "x" / "diario conversa.m4a")
    _audio(grava / "guardar" / "x" / "2026-10-15_22-21-50_Diario.m4a")  # nome do Archive
    # Apelidos que o Grava aceita criar, porque só reserva "diario" e "diarios".
    _audio(grava / "guardar" / "diariointimo" / "2026-10-15 22-00-05 diariointimo.m4a")
    _audio(grava / "transcrever" / "2026-10-15 22-00-06 diario2.m4a")

    feito = espelhar(grava, drive)

    assert feito["copiados"] == 0
    assert feito["recusados"] == 10
    assert not drive.exists() or not any(drive.rglob("*"))


def test_pula_ponto_temporario_e_nao_audio(pastas):
    grava, drive = pastas
    t = grava / "transcrever"
    _audio(t / ".pending-2026-10-15 22-00-00 ideia.m4a")
    _audio(t / ".2026-10-15 22-00-00 ideia.marcas.txt")
    _audio(t / "~syncthing~2026-10-15 22-00-00 ideia.m4a.tmp")
    _audio(t / ".syncthing.2026-10-15 22-00-00 ideia.m4a.tmp")
    _audio(t / "2026-10-15 22-00-00 ideia.txt")
    _audio(grava / ".stfolder" / "2026-10-15 22-00-00 ideia.m4a")
    _audio(grava / ".stversions" / "2026-10-15 22-00-00 ideia.m4a")

    feito = espelhar(grava, drive)

    assert feito["copiados"] == 0
    assert not drive.exists() or not any(drive.rglob("*"))


def test_pula_arquivo_recente_e_copia_depois(pastas):
    grava, drive = pastas
    agora = time.time()
    a = _audio(grava / "transcrever" / "2026-10-15 22-21-50 ideia.m4a", mtime=agora - 30)

    assert espelhar(grava, drive, agora=agora)["esperando"] == 1
    assert not (drive / "ideia" / a.name).exists()

    assert espelhar(grava, drive, agora=agora + 300)["copiados"] == 1
    assert (drive / "ideia" / a.name).exists()


def test_pula_quando_o_tamanho_mudou_desde_a_passada_anterior(pastas):
    grava, drive = pastas
    a = _audio(grava / "transcrever" / "2026-10-15 22-21-50 ideia.m4a", mtime=VELHO)
    grava_drive._vistos[str(a)] = 1  # na passada anterior era menor

    assert espelhar(grava, drive)["esperando"] == 1
    assert espelhar(grava, drive)["copiados"] == 1  # tamanho estável agora


def test_pula_existente_igual_e_nao_sobrescreve_diferente(pastas, caplog):
    grava, drive = pastas
    a = _audio(grava / "transcrever" / "2026-10-15 22-21-50 ideia.m4a", b"12345")
    b = _audio(grava / "transcrever" / "2026-10-15 22-30-00 ideia.m4a", b"12345")
    _audio(drive / "ideia" / a.name, b"12345")
    _audio(drive / "ideia" / b.name, b"outro tamanho")

    with caplog.at_level(logging.WARNING, logger="logos.grava_drive"):
        feito = espelhar(grava, drive)
        espelhar(grava, drive)

    assert feito["iguais"] == 1 and feito["copiados"] == 0
    assert (drive / "ideia" / b.name).read_bytes() == b"outro tamanho"
    assert sum("outro tamanho" in r.message for r in caplog.records) == 1


def test_drive_ausente_avisa_uma_vez_e_nao_cria_nada(tmp_path, caplog):
    grava = tmp_path / "Grava"
    _audio(grava / "transcrever" / "2026-10-15 22-21-50 ideia.m4a")
    drive = tmp_path / "G_desmontado" / "Grava"

    with caplog.at_level(logging.WARNING, logger="logos.grava_drive"):
        assert espelhar(grava, drive)["copiados"] == 0
        assert espelhar(grava, drive)["copiados"] == 0

    assert not drive.parent.exists()
    assert sum("Drive indisponível" in r.message for r in caplog.records) == 1


def test_desligado_e_recusa_a_inbox(pastas, monkeypatch):
    grava, drive = pastas
    _audio(grava / "transcrever" / "2026-10-15 22-21-50 ideia.m4a")
    assert espelhar(grava, None)["copiados"] == 0
    monkeypatch.setattr(config, "INBOX_DIR", grava)
    assert espelhar(grava, drive)["copiados"] == 0
    assert not drive.exists()


def test_falha_na_copia_nao_deixa_temporario(pastas, monkeypatch):
    grava, drive = pastas
    _audio(grava / "transcrever" / "2026-10-15 22-21-50 ideia.m4a")

    def quebra(origem, destino):
        destino.write_bytes(b"pela met")
        raise OSError("disco cheio")

    monkeypatch.setattr(grava_drive.shutil, "copyfile", quebra)
    feito = espelhar(grava, drive)

    assert feito["falhas"] == 1
    assert list((drive / "ideia").iterdir()) == []


def test_iniciar_passada_desligada_nao_abre_thread():
    # A suíte desliga o espelho pela variável vazia (conftest).
    assert config.GRAVA_DRIVE_DIR is None
    assert grava_drive.iniciar_passada() is False


@pytest.mark.parametrize("nome, apelido", [
    ("2026-10-15 22-21-50 reuniao Conselho.m4a", "reuniao"),
    ("2026-10-15 22-21-50 culinaria.m4a", "culinaria"),   # apelido criado pelo dono
    ("2026-10-15 22-21-50 Conselho.m4a", "outros"),        # maiúscula não é apelido
    ("2026-10-15 22-21-50.m4a", "outros"),
    ("Entrevista Rosalina.m4a", "outros"),
    ("2026-10-15 22-21-50 ...m4a", "outros"),
])
def test_apelido_do_nome(nome, apelido):
    assert apelido_do_nome(nome) == apelido


# --- Rota pela fala: o ledger decide o que das pastas observadas pode ir ---

@pytest.fixture
def observada(pastas, monkeypatch, tmp_path):
    """transcrever/ como pasta observada, igual ao logos_config.yaml, e um ledger vazio."""
    grava, drive = pastas
    monkeypatch.setattr(config, "EXTRA_INBOX_DIRS", [grava / "transcrever"])
    return grava, drive, Ledger(tmp_path / "ledger.json")


def _no_ledger(ledger, arquivo, route, state=State.ENRICHED):
    ledger.upsert(Entry(filename=arquivo.name, size=arquivo.stat().st_size, state=state, route=route))


def test_fala_com_diario_em_arquivo_de_outro_tipo_nao_vai_ao_drive(observada):
    # O caso de test_triage_grava::test_rota_nao_muda_pelo_apelido: o nome diz "entrevista", a
    # fala começa com "Diário de hoje", a nota foi para o Diário. O áudio não pode ir ao Drive.
    grava, drive, ledger = observada
    a = _audio(grava / "transcrever" / "2026-10-15 22-21-50 entrevista.m4a")
    _no_ledger(ledger, a, "Diario")

    feito = espelhar(grava, drive, ledger=ledger)

    assert feito["copiados"] == 0 and feito["esperando"] == 1
    assert not drive.exists() or not any(drive.rglob("*"))


def test_transcrita_so_vai_depois_da_rota_pela_fala(observada):
    grava, drive, ledger = observada
    a = _audio(grava / "transcrever" / "2026-10-15 22-21-50 entrevista.m4a")

    assert espelhar(grava, drive, ledger=ledger)["esperando"] == 1  # ainda não transcrita
    # FAILED tem a rota tirada só do nome (Inbox), não da fala: continua esperando.
    _no_ledger(ledger, a, "Inbox", State.FAILED)
    assert espelhar(grava, drive, ledger=ledger)["esperando"] == 1
    _no_ledger(ledger, a, "Inbox", State.ENRICHED)
    assert espelhar(grava, drive, ledger=ledger)["copiados"] == 1
    assert (drive / "entrevista" / a.name).exists()


def test_sem_ledger_nada_da_pasta_observada_vai(observada):
    grava, drive, _ = observada
    _audio(grava / "transcrever" / "2026-10-15 22-21-50 entrevista.m4a")
    b = _audio(grava / "guardar" / "rpg" / "2026-10-15 23-00-00 rpg.m4a")

    feito = espelhar(grava, drive)

    # guardar/ não passa pela transcrição: segue só pela regra do nome.
    assert feito["copiados"] == 1 and feito["esperando"] == 1
    assert (drive / "rpg" / b.name).exists()


def test_ledger_de_outro_tamanho_nao_libera(observada):
    grava, drive, ledger = observada
    a = _audio(grava / "transcrever" / "2026-10-15 22-21-50 entrevista.m4a", b"versao final")
    ledger.upsert(Entry(filename=a.name, size=3, state=State.ENRICHED, route="Inbox"))

    assert espelhar(grava, drive, ledger=ledger)["esperando"] == 1


# --- Guardas de configuração ---

@pytest.mark.parametrize("pasta", ["INBOX_DIR", "ARCHIVE_DIR", "TRANSCRIPTS_DIR"])
def test_recusa_origem_que_contem_pasta_do_logos(tmp_path, monkeypatch, caplog, pasta):
    # grava_dir apontando para o data_root: a Inbox tem Diários cuja rota veio da fala e o
    # Archive guarda "..._Diario.m4a"; nenhum dos dois pode ser percorrido.
    raiz = tmp_path / "Logos"
    drive = tmp_path / "MeuDrive" / "Grava"
    drive.parent.mkdir()
    _audio(raiz / "Inbox" / "2026-10-15 22-21-50.m4a")
    _audio(raiz / "Archive" / "2026-10-15_22-21-50_Inbox.m4a")
    _audio(raiz / "Grava" / "transcrever" / "2026-10-15 22-30-00 ideia.m4a")
    monkeypatch.setattr(config, pasta, raiz / pasta.split("_")[0].capitalize())

    with caplog.at_level(logging.WARNING, logger="logos.grava_drive"):
        assert espelhar(raiz, drive)["copiados"] == 0
        assert espelhar(raiz, drive)["copiados"] == 0

    assert not drive.exists()
    assert sum("recusado" in r.message for r in caplog.records) == 1  # aviso único


def test_recusa_destino_dentro_da_origem_ou_de_pasta_observada(pastas, monkeypatch):
    grava, _ = pastas
    _audio(grava / "transcrever" / "2026-10-15 22-21-50 ideia.m4a")
    assert espelhar(grava, grava / "espelho")["copiados"] == 0
    assert not (grava / "espelho").exists()

    observada = grava.parent / "Drive" / "Inbox"
    observada.mkdir(parents=True)
    monkeypatch.setattr(config, "EXTRA_INBOX_DIRS", [observada])
    assert espelhar(grava, observada / "Grava")["copiados"] == 0
    assert not (observada / "Grava").exists()


def test_origem_ausente_avisa_uma_vez(tmp_path, caplog):
    drive = tmp_path / "MeuDrive" / "Grava"
    drive.parent.mkdir()
    with caplog.at_level(logging.WARNING, logger="logos.grava_drive"):
        espelhar(tmp_path / "nao_existe", drive)
        espelhar(tmp_path / "nao_existe", drive)
    assert sum("não encontrada" in r.message for r in caplog.records) == 1


# --- Lista de exclusão (terapia) ---

def test_lista_de_exclusao_pega_nome_e_apelido(pastas, monkeypatch):
    grava, drive = pastas
    monkeypatch.setattr(config, "GRAVA_DRIVE_EXCLUIR", ["terapia", "..."])  # "..." não recusa tudo
    _audio(grava / "transcrever" / "2026-10-20 18-00-00 reuniao Terapia.m4a")
    _audio(grava / "guardar" / "terapia" / "2026-10-20 18-00-01 terapia.m4a")
    _audio(grava / "guardar" / "x" / "Terapias de casal.m4a")
    _audio(grava / "transcrever" / "2026-10-20 18-00-02 reuniao Conselho.m4a")

    feito = espelhar(grava, drive)

    assert feito["recusados"] == 3 and feito["copiados"] == 1


def test_lista_de_exclusao_vazia_nao_libera_o_diario(pastas, monkeypatch):
    grava, drive = pastas
    monkeypatch.setattr(config, "GRAVA_DRIVE_EXCLUIR", [])
    _audio(grava / "transcrever" / "2026-10-15 22-00-00 diario.m4a")
    _audio(grava / "transcrever" / "2026-10-20 18-00-00 reuniao Terapia.m4a")

    feito = espelhar(grava, drive)

    assert feito["recusados"] == 1 and feito["copiados"] == 1


# --- Cópia no G: ---

def test_falha_na_data_nao_impede_a_copia(pastas, monkeypatch):
    grava, drive = pastas
    a = _audio(grava / "transcrever" / "2026-10-15 22-21-50 ideia.m4a")

    def recusa(origem, destino, **kw):
        raise OSError("atributo não suportado")

    monkeypatch.setattr(grava_drive.shutil, "copystat", recusa)
    feito = espelhar(grava, drive)

    assert feito["copiados"] == 1
    assert (drive / "ideia" / a.name).read_bytes() == b"audio"
    assert not [p for p in drive.rglob(".*")]


def test_falha_na_copia_avisa_uma_vez_por_arquivo(pastas, monkeypatch, caplog):
    grava, drive = pastas
    _audio(grava / "transcrever" / "2026-10-15 22-21-50 ideia.m4a")

    def quebra(origem, destino):
        raise OSError("nome inválido")

    monkeypatch.setattr(grava_drive.shutil, "copyfile", quebra)
    with caplog.at_level(logging.WARNING, logger="logos.grava_drive"):
        assert espelhar(grava, drive)["falhas"] == 1
        assert espelhar(grava, drive)["falhas"] == 1
    assert sum("falha ao copiar" in r.message for r in caplog.records) == 1


# --- Thread, lock e ligação no watcher ---

@pytest.fixture
def ligado(pastas, monkeypatch):
    grava, drive = pastas
    monkeypatch.setattr(config, "GRAVA_DIR", grava)
    monkeypatch.setattr(config, "GRAVA_DRIVE_DIR", drive)


def test_lock_e_liberado_quando_a_passada_levanta(ligado, monkeypatch):
    def explode(**kw):
        raise RuntimeError("bug no espelho")

    monkeypatch.setattr(grava_drive, "espelhar", explode)
    lock = grava_drive._passada_lock
    for _ in range(2):
        assert grava_drive.iniciar_passada() is True
        assert lock.acquire(timeout=5)  # a thread soltou o lock mesmo com o erro
        lock.release()


def test_segunda_passada_nao_comeca_com_a_primeira_copiando(ligado, monkeypatch):
    comecou, solta = threading.Event(), threading.Event()
    recebido = []

    def demora(**kw):
        recebido.append(kw.get("ledger"))
        comecou.set()
        solta.wait(5)

    monkeypatch.setattr(grava_drive, "espelhar", demora)
    marcador = object()
    assert grava_drive.iniciar_passada(marcador) is True
    assert comecou.wait(5)
    assert grava_drive.iniciar_passada(marcador) is False
    solta.set()
    lock = grava_drive._passada_lock
    assert lock.acquire(timeout=5)
    lock.release()
    assert recebido == [marcador]  # o ledger do watcher chega ao espelho


def test_watcher_nao_cai_se_o_espelho_nao_comeca(monkeypatch):
    chamadas = []

    def quebra(ledger=None):
        chamadas.append(ledger)
        raise RuntimeError("thread não abriu")

    monkeypatch.setattr(grava_drive, "iniciar_passada", quebra)
    watcher._espelhar_grava("ledger")  # não levanta
    assert chamadas == ["ledger"]
