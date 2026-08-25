# Logos

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)]()

> **Intelligent offline voice-to-vault pipeline:**  
> Audio recorded on mobile (synced via Syncthing) ➔ Local transcription (`faster-whisper`) ➔ Structured enrichment by local LLM (`Ollama`) ➔ Clean categorized Markdown notes.

[🇧🇷 Leia em Português](README.pt-BR.md)

---

## ⚡ Quick Setup (1-Minute TUI Installer)

Logos comes with a clean, interactive Terminal User Interface (TUI) installer that inspects your system, checks dependencies (`ffmpeg`, `Ollama`), configures storage directories, and guides the download of local Whisper models for both **Windows** and **Linux**.

### Windows
Double-click `install.bat` or run in Command Prompt / PowerShell:
```bat
install.bat
```

### Linux / macOS
```bash
chmod +x install.sh
./install.sh
```

---

## 🛠️ System Prerequisites

1. **Python 3.10+** (tested on 3.10, 3.11, 3.12, 3.13, and 3.14).
2. **ffmpeg** (required for high-performance audio conversion):
   - **Windows:** `winget install Gyan.FFmpeg` or download from [ffmpeg.org](https://ffmpeg.org).
   - **Linux (Debian/Ubuntu):** `sudo apt update && sudo apt install ffmpeg`.
   - **Arch Linux:** `sudo pacman -S ffmpeg`.
3. **[Ollama](https://ollama.com)** (for local LLM inference and structuring):
   - Install Ollama and pull the recommended lightweight instruct model:
     ```bash
     ollama pull qwen2.5:3b-instruct-q4_K_M
     ```

---

## 🎙️ Local Whisper & Offline Operation

Logos uses `faster-whisper` (CTranslate2) with optimized CPU quantization (`int8`).
- **Self-Contained:** No dependencies on sibling repos or external paths.
- **Guided Setup:** Choose your model size (`large-v3-turbo`, `medium`, `small`, `base`) directly within the TUI installer, which downloads and tests it automatically.
- **100% Offline-First:** Once cached, `HF_HUB_OFFLINE=1` guarantees instant local execution without network calls or latency.

---

## 🚀 Running Logos

Once setup is complete, start the background resident watcher:

```bash
# Windows
.venv\Scripts\python main.py

# Linux
.venv/bin/python main.py
```

### What happens in the pipeline:
1. **Inbox Scan & Watcher:** Detects incoming voice recordings while ensuring write stability.
2. **STT (Whisper):** Transcribes audio directly to raw verbatim text saved in `Transcripts/`.
3. **LLM Enrichment:** Prompts Ollama with route-specific prompts (`Diario`, `Planejamento`, `Inbox`) to generate clean YAML frontmatter, titles, tags, summaries, and action points.
4. **Vault Writer:** Produces the final Markdown note in `Notes/{Diario, Planejamento, Inbox}/` with the full verbatim transcript preserved.
5. **Audio Archive & Retention:** Copies processed media to `Archive/` and automatically purges audio files older than 60 days while keeping notes and transcripts permanently.

---

## 📱 Mobile Sync Workflow (Syncthing)

For a fully automated mobile-to-desktop capture flow:
1. Install **Syncthing** on your smartphone and computer.
2. On your mobile phone, set your favorite voice recorder app (e.g. *Easy Voice Recorder*, *Samsung Voice Recorder*) to save files to a synced folder.
3. Link the folder to your computer's `Inbox/` directory.
4. **Mobile Configuration:** Set folder type to **Send Only**.
5. **Computer Configuration:** Set folder type to **Receive Only**. Logos operates read-only in `Inbox/` and uses an external idempotent ledger, avoiding any sync conflicts.

---

## 📂 Repository Structure

```
logos/
├── config.py             # Dynamic configuration loader (YAML, env variables, user home)
├── triage.py             # Route classification and timestamp parser
├── ledger.py             # Idempotent state tracker (prevents duplicate work)
├── stt.py                # Headless faster-whisper and ffmpeg integration
├── llm.py                # Ollama client with token chunking and strict YAML parser
├── writer.py             # Markdown note generator and archive manager
├── pipeline.py           # End-to-end orchestrator (STT -> LLM -> Writer)
├── watcher.py            # Real-time directory observer and daily retention manager
└── prompts/              # Route prompt templates (diario.txt, planejamento.txt, inbox.txt)

setup.py                  # Interactive TUI installer & diagnostic assistant
install_startup.py        # Cross-platform background service manager (Startup / systemd)
install.bat / install.sh  # 1-click installation entrypoints
tests/                    # Automated, isolated test suite
```

---

## ⚙️ Custom Configuration

A `logos_config.yaml` file is created in the repository root by the setup tool. You can modify parameters at any time:

```yaml
data_root: "~/Logos"
whisper_models_dir: "~/Logos/models"
whisper_model_size: "large-v3-turbo"
whisper_compute_type: "int8"
whisper_cpu_threads: 6
whisper_language: "pt"
ollama_host: "http://localhost:11434"
llm_model: "qwen2.5:3b-instruct-q4_K_M"
archive_retention_days: 60
```
