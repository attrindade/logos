"""Pastas extras observadas além da Inbox própria.

O caso real: a pasta do Google Drive onde o gravador de entrevistas deposita. O Drive for
Desktop a monta como caminho local, então para o watcher é só mais uma pasta — mas é uma
pasta que o dono usa para outras coisas, e que pode sumir quando o Drive não está montado.
"""
from pathlib import Path
from unittest.mock import patch

from watchdog.events import FileCreatedEvent, FileMovedEvent

from logos import config
from logos.ledger import Entry, Ledger, State
from logos.pipeline import retry_failed
from logos.watcher import _InboxHandler, accepts, scan_inbox


def _dirs(monkeypatch, tmp_path):
    inbox = tmp_path / "Inbox"
    drive = tmp_path / "Drive"
    inbox.mkdir()
    drive.mkdir()
    monkeypatch.setattr(config, "INBOX_DIR", inbox)
    monkeypatch.setattr(config, "EXTRA_INBOX_DIRS", [drive])
    return inbox, drive


def test_inbox_dirs_traz_a_propria_primeiro(monkeypatch, tmp_path):
    inbox, drive = _dirs(monkeypatch, tmp_path)
    assert config.inbox_dirs() == [inbox, drive]


def test_inbox_dirs_nao_duplica_se_a_extra_for_a_propria(monkeypatch, tmp_path):
    inbox = tmp_path / "Inbox"
    inbox.mkdir()
    monkeypatch.setattr(config, "INBOX_DIR", inbox)
    monkeypatch.setattr(config, "EXTRA_INBOX_DIRS", [inbox])
    assert config.inbox_dirs() == [inbox]


def test_pasta_extra_aceita_so_audio(monkeypatch, tmp_path):
    inbox, drive = _dirs(monkeypatch, tmp_path)
    assert accepts(drive, "Entrevista Rosalina.m4a")
    assert accepts(drive, "conversa.mp3")
    # Numa pasta do Drive convivem PDF, zip, audiobook e atalhos do próprio Drive.
    assert not accepts(drive, "Contrato 314.pdf")
    assert not accepts(drive, "planilha.gdoc")
    assert not accepts(drive, "backup.zip")


def test_inbox_propria_continua_aceitando_texto(monkeypatch, tmp_path):
    inbox, _ = _dirs(monkeypatch, tmp_path)
    assert accepts(inbox, "2026-09-14 15-02-11 tg.txt")
    assert accepts(inbox, "2026-09-14 15-02-11.m4a")


def test_ignorados_sao_recusados_nas_duas(monkeypatch, tmp_path):
    inbox, drive = _dirs(monkeypatch, tmp_path)
    for d in (inbox, drive):
        assert not accepts(d, ".evr_recently_deleted_(123)2026-09-01 10-37-56.m4a")
        assert not accepts(d, "~syncthing~gravacao.m4a.tmp")


def test_scan_processa_audio_que_chegou_na_extra(monkeypatch, tmp_path):
    _, drive = _dirs(monkeypatch, tmp_path)
    alvo = drive / "Entrevista Rosalina.m4a"
    alvo.write_bytes(b"audio")

    with patch("logos.watcher.process_file") as proc, \
         patch("logos.watcher._wait_for_stable_file", return_value=True):
        scan_inbox(Ledger(tmp_path / "ledger.json"))

    proc.assert_called_once()
    assert proc.call_args.args[0] == alvo


def test_scan_ignora_nao_audio_na_extra(monkeypatch, tmp_path):
    _, drive = _dirs(monkeypatch, tmp_path)
    (drive / "O_manual.pdf").write_bytes(b"pdf")
    (drive / "atalho.gdoc").write_text("{}", encoding="utf-8")

    with patch("logos.watcher.process_file") as proc, \
         patch("logos.watcher._wait_for_stable_file", return_value=True):
        scan_inbox(Ledger(tmp_path / "ledger.json"))

    proc.assert_not_called()


def test_scan_sobrevive_a_extra_inexistente(monkeypatch, tmp_path):
    """Drive desmontado não pode derrubar a varredura da Inbox própria."""
    inbox, _ = _dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "EXTRA_INBOX_DIRS", [tmp_path / "G_nao_montado"])
    alvo = inbox / "2026-09-14 15-02-11.m4a"
    alvo.write_bytes(b"audio")

    with patch("logos.watcher.process_file") as proc, \
         patch("logos.watcher._wait_for_stable_file", return_value=True):
        scan_inbox(Ledger(tmp_path / "ledger.json"))

    proc.assert_called_once()
    assert proc.call_args.args[0] == alvo


def test_evento_na_extra_dispara_processamento(monkeypatch, tmp_path):
    _, drive = _dirs(monkeypatch, tmp_path)
    alvo = drive / "Entrevista Wesley.m4a"
    alvo.write_bytes(b"audio")
    handler = _InboxHandler(Ledger(tmp_path / "ledger.json"))

    with patch("logos.watcher.process_file") as proc, \
         patch("logos.watcher._wait_for_stable_file", return_value=True):
        handler.on_created(FileCreatedEvent(str(alvo)))

    proc.assert_called_once()
    assert proc.call_args.args[0] == alvo


def test_evento_de_nao_audio_na_extra_e_ignorado(monkeypatch, tmp_path):
    _, drive = _dirs(monkeypatch, tmp_path)
    alvo = drive / "Relatorio.pdf"
    alvo.write_bytes(b"pdf")
    handler = _InboxHandler(Ledger(tmp_path / "ledger.json"))

    with patch("logos.watcher.process_file") as proc, \
         patch("logos.watcher._wait_for_stable_file", return_value=True):
        handler.on_created(FileCreatedEvent(str(alvo)))

    proc.assert_not_called()


def test_rename_do_drive_para_a_extra_processa(monkeypatch, tmp_path):
    """Arrastar um arquivo para a pasta chega como 'moved', igual ao Syncthing."""
    _, drive = _dirs(monkeypatch, tmp_path)
    destino = drive / "Entrevista Emily.m4a"
    destino.write_bytes(b"audio")
    handler = _InboxHandler(Ledger(tmp_path / "ledger.json"))

    with patch("logos.watcher.process_file") as proc, \
         patch("logos.watcher._wait_for_stable_file", return_value=True):
        handler.on_moved(FileMovedEvent(str(tmp_path / "Entrevista Emily.m4a"), str(destino)))

    proc.assert_called_once()
    assert proc.call_args.args[0] == destino


def test_retry_encontra_arquivo_que_falhou_na_extra(monkeypatch, tmp_path):
    """O ledger guarda só o nome; o retry precisa procurar nas duas pastas."""
    _, drive = _dirs(monkeypatch, tmp_path)
    alvo = drive / "Entrevista Danilo.m4a"
    alvo.write_bytes(b"audio")
    ledger = Ledger(tmp_path / "ledger.json")
    ledger.upsert(Entry(
        filename=alvo.name, size=alvo.stat().st_size, state=State.FAILED,
        route="Inbox", recorded_at="2026-09-21T12:00:00", error="ollama fora do ar",
    ))

    with patch("logos.pipeline.process_path") as proc:
        retry_failed(ledger)

    proc.assert_called_once()
    assert proc.call_args.args[0] == alvo


def test_arquivo_presente_nas_duas_e_processado_uma_vez(monkeypatch, tmp_path):
    """O gravador escreve na pasta local e no Drive: o ledger é `nome::tamanho`, então a
    segunda cópia é reconhecida como já processada."""
    inbox, drive = _dirs(monkeypatch, tmp_path)
    nome = "2026-09-15 14-14-21.m4a"
    (inbox / nome).write_bytes(b"mesmo audio")
    (drive / nome).write_bytes(b"mesmo audio")
    ledger = Ledger(tmp_path / "ledger.json")

    chamadas = []

    def falso_process(path: Path, lg: Ledger) -> None:
        chamadas.append(path)
        lg.upsert(Entry(
            filename=path.name, size=path.stat().st_size, state=State.ENRICHED,
            route="Inbox", recorded_at="2026-09-15T14:14:21",
        ))

    with patch("logos.watcher.process_file", side_effect=falso_process), \
         patch("logos.watcher._wait_for_stable_file", return_value=True):
        scan_inbox(ledger)

    assert chamadas == [inbox / nome]


def test_arquivo_que_some_durante_a_estabilizacao_devolve_o_status(monkeypatch, tmp_path):
    """Movido, substituído pela sincronização, ou unidade do Drive fora do ar. Sem isto o
    tracker ficava preso em 'estabilizando' e o DevPortal mostrava trabalho inexistente."""
    from logos import watcher
    from logos.status import tracker

    sumido = tmp_path / "2026-09-21 21-47-00 fantasma.m4a"
    chamou_idle = []
    monkeypatch.setattr(tracker, "set_idle", lambda *a, **k: chamou_idle.append(True))

    assert watcher._wait_for_stable_file(sumido) is False
    assert chamou_idle, "o tracker precisa voltar para ocioso quando o arquivo some"
