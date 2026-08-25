"""Valida que o modelo Whisper configurado carrega e funciona corretamente."""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logos import config
from logos.stt import get_model

print(f"Carregando Whisper '{config.WHISPER_MODEL_SIZE}' de '{config.WHISPER_MODELS_DIR}' ...")
t0 = time.time()
model = get_model()
print(f"Modelo carregado com sucesso em {time.time() - t0:.1f}s")
print("OK — modelo Whisper pronto para uso.")

