"""Instância única do watcher."""
import os
import subprocess
import sys
from unittest.mock import patch

from logos import lock


def test_adquire_e_libera(tmp_path):
    path = tmp_path / "logos.lock"
    assert lock.acquire(path) is None
    assert path.read_text(encoding="utf-8") == str(os.getpid())
    lock.release(path)
    assert not path.exists()


def test_recusa_quando_outra_instancia_viva(tmp_path):
    path = tmp_path / "logos.lock"
    path.write_text("424242", encoding="utf-8")
    with patch.object(lock, "_is_live_logos", return_value=True):
        assert lock.acquire(path) == 424242
    assert path.read_text(encoding="utf-8") == "424242"


def test_lock_orfao_de_pid_morto_e_retomado(tmp_path):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    path = tmp_path / "logos.lock"
    path.write_text(str(dead.pid), encoding="utf-8")

    assert lock.acquire(path) is None
    assert path.read_text(encoding="utf-8") == str(os.getpid())


def test_pid_vivo_que_nao_e_logos_nao_segura_o_lock(tmp_path):
    path = tmp_path / "logos.lock"
    path.write_text(str(os.getppid()), encoding="utf-8")  # processo vivo sem main.py na linha de comando
    with patch.object(lock.psutil, "Process") as proc:
        proc.return_value.cmdline.return_value = ["pytest"]
        assert lock.acquire(path) is None


def test_lixo_no_lock_e_tratado_como_orfao(tmp_path):
    path = tmp_path / "logos.lock"
    path.write_text("não é pid", encoding="utf-8")
    assert lock.acquire(path) is None
