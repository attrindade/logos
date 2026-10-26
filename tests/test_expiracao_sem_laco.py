"""O áudio que expirou do Archive não volta a virar nota (laço de 23 a 25/10/2026).

O original fica na Inbox para sempre (Syncthing só-recebe; o watcher nunca apaga nada lá). Antes,
`expire_archive` marcava ARCHIVE_EXPIRED, `is_processed` só aceitava ENRICHED e a varredura
seguinte transcrevia de novo e gravava outra nota, com outro título; o reprocessamento copiava o
áudio de volta ao Archive e, na expiração seguinte, tudo se repetia.
"""
from datetime import datetime, timedelta

from logos import config, pipeline, watcher
from logos.ledger import CONCLUIDOS, Entry, Ledger, State


def _gravacao_antiga(tmp_path, nome: str, dias: int) -> tuple[Ledger, object, object]:
    inbox = tmp_path / "Inbox"
    archive = tmp_path / "Archive"
    inbox.mkdir(exist_ok=True)
    archive.mkdir(exist_ok=True)
    original = inbox / nome
    original.write_bytes(b"audio-" + nome.encode())
    copia = archive / (nome.replace(" ", "_") + "_Inbox.m4a")
    copia.write_bytes(original.read_bytes())
    ledger = Ledger(tmp_path / "ledger.json")
    ledger.upsert(Entry(
        filename=nome, size=original.stat().st_size, state=State.ENRICHED, route="Inbox",
        recorded_at=(datetime.now() - timedelta(days=dias)).isoformat(),
        archive_path=str(copia), note_path=str(tmp_path / "nota.md"),
    ))
    return ledger, original, copia


def test_archive_expired_conta_como_processado(tmp_path):
    ledger, original, _ = _gravacao_antiga(tmp_path, "2026-08-23 23-13-03.m4a", 63)
    entry = ledger.get(original.name, original.stat().st_size)
    entry.state = State.ARCHIVE_EXPIRED
    ledger.upsert(entry)
    assert ledger.is_processed(original.name, original.stat().st_size)
    assert State.TRANSCRIBED not in CONCLUIDOS and State.FAILED not in CONCLUIDOS


def test_expirar_e_varrer_de_novo_nao_reprocessa(tmp_path, monkeypatch):
    """O ciclo inteiro do watcher, várias vezes: expira, varre a Inbox, expira de novo."""
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "Inbox")
    monkeypatch.setattr(config, "ARCHIVE_RETENTION_DAYS", 60)
    monkeypatch.setattr(config, "STABILITY_CHECK_INTERVAL_S", 0)
    monkeypatch.setattr(config, "STABILITY_CHECK_COUNT", 1)
    # As de 31/08–24/09 que ainda vão expirar e as de agosto que já expiraram, juntas.
    ledger, velha, copia_velha = _gravacao_antiga(tmp_path, "2026-08-23 23-13-03.m4a", 63)
    _, nova, copia_nova = _gravacao_antiga(tmp_path, "2026-09-01 10-38-03.m4a", 54)
    ledger = Ledger(tmp_path / "ledger.json")  # recarrega com as duas entradas
    processados = []
    monkeypatch.setattr(watcher, "process_file", lambda path, led: processados.append(path.name))
    monkeypatch.setattr(pipeline, "process_file", lambda path, led: processados.append(path.name))

    for dias_depois in (0, 1, 7, 30):
        # Simula o relógio andando: a de setembro também passa dos 60 dias no meio do caminho.
        monkeypatch.setattr(config, "ARCHIVE_RETENTION_DAYS", 60 - dias_depois)
        watcher.expire_archive(ledger)
        watcher.scan_inbox(ledger)
        watcher.expire_archive(ledger)

    assert processados == [], f"reprocessou: {processados}"
    assert not copia_velha.exists() and not copia_nova.exists()  # a retenção continua valendo
    assert velha.exists() and nova.exists()  # e o original na Inbox nunca é tocado
    for f in (velha, nova):
        e = ledger.get(f.name, f.stat().st_size)
        assert e.state == State.ARCHIVE_EXPIRED and e.note_path  # a nota segue referenciada


def test_process_file_nao_refaz_nota_de_entrada_expirada(tmp_path, monkeypatch):
    """A outra porta de entrada (evento do watchdog, retry): process_file também respeita o ledger."""
    ledger, original, _ = _gravacao_antiga(tmp_path, "2026-08-24 10-18-28.m4a", 62)
    entry = ledger.get(original.name, original.stat().st_size)
    entry.state = State.ARCHIVE_EXPIRED
    ledger.upsert(entry)

    def nao_pode(*a, **k):
        raise AssertionError("transcreveu de novo um áudio já concluído")

    monkeypatch.setattr(pipeline, "transcribe_file", nao_pode)
    pipeline.process_file(original, ledger)
    assert ledger.get(original.name, original.stat().st_size).state == State.ARCHIVE_EXPIRED


def test_pacote_expirado_tambem_conta(tmp_path):
    ledger = Ledger(tmp_path / "ledger.json")
    ledger.upsert(Entry(filename="2026-09-14 16-10-00 tg 2", size=10, state=State.ARCHIVE_EXPIRED,
                        manifest_hash="abc"))
    assert ledger.is_processed_bundle("2026-09-14 16-10-00 tg 2", "abc")
