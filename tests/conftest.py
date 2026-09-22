"""Isola os testes dos dados reais.

Precisa rodar antes de qualquer `import logos...`: `logos.status` instancia um StatusTracker
no import e gravaria o status.json de produção (lido pelo DevPortal). As variáveis de ambiente
têm precedência sobre o logos_config.yaml em `logos/config.py`.
"""
import os
import sys
import tempfile
from pathlib import Path

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="logos-tests-"))
os.environ["LOGOS_DATA_ROOT"] = str(_TEST_ROOT)
os.environ["LOGOS_INBOX_DIR"] = str(_TEST_ROOT / "Inbox")
os.environ["LOGOS_NOTES_DIR"] = str(_TEST_ROOT / "Notes")
# A consulta do bot lê o vault a partir daqui. Sem isto, a suíte herdaria o vault_dir do
# logos_config.yaml e /buscar rodaria contra as notas reais — inclusive as que a allowlist
# existe para manter fora.
os.environ["LOGOS_VAULT_DIR"] = str(_TEST_ROOT / "vault")
# Vazio = nenhuma pasta extra. Sem isto a suíte herdaria o extra_inbox_dirs do
# logos_config.yaml e varreria a pasta real do Google Drive do dono.
os.environ["LOGOS_EXTRA_INBOX_DIRS"] = ""
# Porta fechada: process_file real nos testes não pode postar capturas falsas no ActivityWatch.
os.environ["LOGOS_AW_PORT"] = "1"
# Nenhum teste carrega modelo de visão no Ollama; os de extração ligam o que precisam.
os.environ["LOGOS_VISION_MODEL"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
