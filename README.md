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
   - Install Ollama and pull the default model:
     ```bash
     ollama pull gemma4:e4b
     ```
   - Optional: `gemma4:26b` (~18 GB) extracts entities better but is several times slower on CPU. Set `llm_model: "gemma4:26b"` to use it.

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

Only one watcher runs at a time: `main.py` holds `state/logos.lock` with its PID and exits if another live instance owns it.

Maintenance: `python main.py --limpar-fantasmas` removes ledger failures left by partial syncs of a file whose complete version was already processed (a backup of `ledger.json` is written first).

### What happens in the pipeline:
1. **Inbox Scan & Watcher:** Detects incoming voice recordings and `.txt`/`.md` text notes while ensuring write stability. Text skips Whisper; files without a `YYYY-MM-DD HH-MM-SS` name are dated by modification time.
   - **Route vocabulary:** the first word picks the route (`diário`, `planejamento`, anything else → Inbox). In Inbox, `tarefa`, `ideia` and `reunião` set `categoria` by rule, and `tarefa urgente` sets `prioridade: alta`.
   - **Frontmatter:** every note records `fonte_tipo` (`audio`, `texto`) and `canal` (`syncthing`, or `telegram` for files named `… tg.<ext>`).
   - **ActivityWatch:** each finished capture posts a `logos.capture` event to bucket `aw-watcher-logos_<hostname>` (`{rota, canal, tipo, duracao_audio_s, processamento_s}`); silently skipped if `:5600` is down.
2. **STT (Whisper):** Transcribes audio directly to raw verbatim text saved in `Transcripts/`.
3. **LLM Enrichment:** Prompts Ollama with route-specific prompts (`Diario`, `Planejamento`, `Inbox`) to generate clean YAML frontmatter, titles, tags, summaries, and action points.
4. **Vault Writer:** Produces the final Markdown note in `Notes/{Diario, Planejamento, Inbox}/` with the full verbatim transcript preserved.
5. **Audio Archive & Retention:** Copies processed media to `Archive/` and automatically purges audio files older than 60 days while keeping notes and transcripts permanently.

---

## 💬 Telegram Bot (text and voice from anywhere)

The watcher also runs a Telegram bot in a background thread (long polling, no open port). It only writes files into `Inbox/`; the regular pipeline turns them into notes.

1. Create a bot with [@BotFather](https://t.me/BotFather) and store the token **outside the repo** in `<data_root>/state/secrets.yaml`:
   ```yaml
   telegram_token: "123456789:AAH..."
   ```
   or set the `LOGOS_TELEGRAM_TOKEN` environment variable. Never put it in `logos_config.yaml`.
2. Restart the watcher and send `/start` to the bot. The first chat that does so becomes the owner (`telegram_chat_id` is saved in `secrets.yaml`); messages from any other chat are ignored and logged.
3. **Sessions become bundles.** Messages less than `telegram_window_min` (default 10) minutes apart form one capture: text, voice, photos (albums included), PDFs, documents, links and locations. The bundle is assembled in `state/telegram/aberto/`; when it closes (`/nova`, the window expiring, or a watcher restart) the whole folder `YYYY-MM-DD HH-MM-SS tg <message_id>/`, with its `manifest.json`, is renamed into `Inbox/`. The ` tg` suffix sets `canal: telegram`. Each message gets a short reply: session opened, or how many messages it holds.
4. Commands `/tarefa`, `/ideia`, `/planejamento`, `/diario` prefix the matching first word; `/nova` closes the session now. Replying to an older message adds its content to that message's note (a `## 📎 Resposta` section at the end; the frontmatter is not touched). Files above the Bot API's 20 MB download limit are refused with a reply.
5. **One note per bundle.** Route and category come from the first text, voice or caption. Voice is transcribed; photos go through OCR (Tesseract, `ocr_langs: por+eng`) and, only when OCR finds nothing, a description by `vision_model` (default `llm_model`; slow on CPU; empty disables); PDFs use their text layer, or `ocrmypdf` when scanned; `.docx` via `python-docx`; links via `defuddle` when installed, otherwise `trafilatura`; a `.md` sent as a document is the note text. Attachments are copied to `capturas/anexos/YYYY/MM/YYYY-MM-DD_HH-MM-SS_n.<ext>`, embedded with vault-relative paths (`![[…jpg|400]]`, `![[…pdf#page=1]]`), listed in `anexos:`, and get a `<file>.md` sidecar when text was extracted. Locations add `local:` and a map link. Ledger key: `<bundle name>::<sha256 of manifest>`.

OCR prerequisites (optional): Tesseract (`winget install UB-Mannheim.TesseractOCR`) and a `tessdata_dir` (default `<data_root>/models/tessdata`) holding `por.traineddata`, `eng.traineddata`, `osd.traineddata` and the `configs/` folder that `ocrmypdf` needs.

Diário captures sent through Telegram go through Telegram's cloud; the bot reminds you when a message starts with "diário".

Add `* tg*` to `Inbox/.stignore` when the Inbox is a Syncthing receive-only folder: otherwise bot files show up as local changes, and "Revert local changes" deletes them.

### Querying the vault from the chat

The same bot reads the vault, with no LLM and without writing anything:

| Command | Shortcut | What it does |
| :--- | :--- | :--- |
| `/inicio` | `/0` | where to go: projects, briefing and search, already numbered |
| `/ver` | `/v` | lists active projects, most recently touched first |
| `/ver <name>` | | opens the note; long ones come back as a section index |
| `/buscar <term>` | `/b` | matching lines grouped by file (accent- and case-insensitive) |
| `/hoje` | `/h` | the most recent briefing in `briefings/` |
| `/ajuda` | `/a` | the whole system on one screen |

**The numbers in each reply are commands.** That is what keeps the learning curve short: nothing
to memorize, because the next step is already on screen. `/p1` opens the first project of the
last listing, `/s3` the third section of the open note (`/s0` goes back to the index) and `/r2`
the second search result. A full flow is `/0` → `/p1` → `/s3`, typing no names at all.

These indexes live in a per-process `consulta.Contexto`, in memory. They deliberately do not
persist: an index surviving a restart would silently point at a different note, so after a crash
`/p3` says the listing expired instead of opening the wrong thing.

Output goes out as Bot API HTML, with the vault's Markdown reduced to readable text: wikilinks become their alias, pictographs are dropped, table separators disappear, frontmatter stays out of search results. Note bodies go in `<blockquote>`, or `<blockquote expandable>` past `LIMITE_CITACAO`. That is why `partir` closes and reopens the quote at message boundaries: splitting by character would cut a tag in half. Each kind of reply opens with a fixed marker and ends with a hint for the next command; the emoji is semantics, not decoration, and vault text is still stripped of pictographs. `send()` and `notify()` remain plain text — turning HTML on for them would break the briefing, which sends `<` and `&` unescaped.

**Readable folders are an allowlist** in `consulta.ALLOWED_SUBDIRS` (`work/active`, `brain`, `org`, `briefings`, `capturas/Inbox`, `capturas/Planejamento`), relative to `vault_dir`. `capturas/Diario/` and everything else stay out by construction: a new folder in the vault is born invisible to the bot. Changing that tuple changes what Telegram can see — `tests/test_consulta.py::test_diario_nunca_aparece` exists so the change cannot pass unnoticed.

### Sending from scripts

```bash
python -m logos.telegram send --text "Morning briefing ready"
python -m logos.telegram send --text "Activity" --file atividade/2026-09-14.png
python -m logos.telegram check
```

Run from the repo root with the `.venv` Python. Images go as photos (caption = `--text` when ≤ 1024 chars), other files as documents; long text is split at 4096 characters and sent without formatting. Exit codes: `0` sent, `1` send failed, `2` bot not configured (no token or no owner yet). `send` does not touch the watcher's `status.json` or lock.

The bot state (`off`, `polling`, `error`, username, last poll, last error) is published under `bot` in `state/status.json`.

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
├── lock.py               # Single-instance lock file
├── status.py             # Live pipeline status (state/status.json)
├── telegram.py           # Telegram bot (bundles) and send/check CLI
├── consulta.py           # Vault reads from the chat (/ver, /buscar, /hoje)
├── extractors.py         # OCR, vision, PDF, .docx and link text for attachments
├── aw.py                 # ActivityWatch event per capture
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
ollama_keep_alive: "10m"   # keeps the model loaded between notes
llm_model: "gemma4:e4b"    # optional: gemma4:26b
llm_min_words: 40          # shorter notes skip the LLM (title = first 8 words)
archive_retention_days: 60
```
