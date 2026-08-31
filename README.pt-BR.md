# Logos (Português)

> **Pipeline offline inteligente de voz para notas estruturadas:**  
> Áudio gravado no celular (sincronizado via Syncthing) ➔ Transcrição local (faster-whisper) ➔ Enriquecimento por LLM local (Ollama) ➔ Notas Markdown categorizadas.

---

## ⚡ Instalação Rápida (1 Minuto)

O Logos inclui um assistente interativo em TUI (Terminal User Interface) que diagnostica seu ambiente, verifica dependências, configura pastas e gerencia modelos locais tanto no **Windows** quanto no **Linux**.

### Windows
Basta dar dois cliques em `install.bat` ou rodar no terminal:
```bat
install.bat
```

### Linux / macOS
```bash
chmod +x install.sh
./install.sh
```

---

## 🛠️ Pré-requisitos do Sistema

1. **Python 3.10+** (incluindo 3.12, 3.13 e 3.14).
2. **ffmpeg** (necessário para extração de áudio):
   - **Windows:** `winget install Gyan.FFmpeg` ou via [ffmpeg.org](https://ffmpeg.org).
   - **Linux (Debian/Ubuntu):** `sudo apt update && sudo apt install ffmpeg`.
   - **Arch Linux:** `sudo pacman -S ffmpeg`.
3. **[Ollama](https://ollama.com)** (para inferência e sumarização local):
   - Instale o Ollama e baixe o modelo sugerido:
     ```bash
     ollama pull gemma4:26b
     ```

---

## 🎙️ Como o Whisper Local Funciona

O Logos utiliza o `faster-whisper` com inferência otimizada em CPU (int8/quantizada).
- **Independência:** O Logos não depende de pastas externas ou modelos compartilhados de outros repositórios.
- **Download Guiado:** O instalador interativo permite selecionar o tamanho do modelo desejado (`large-v3-turbo`, `medium`, `small`, `base`) e baixa diretamente para a sua pasta de modelos local.
- **Offline First:** Uma vez baixado, a flag `HF_HUB_OFFLINE=1` garante funcionamento 100% desconectado da internet sem atrasos de checagem remota.

---

## 🚀 Uso e Execução

Após rodar o instalador:

```bash
# Windows
.venv\Scripts\python main.py

# Linux
.venv/bin/python main.py
```

O Logos iniciará o processo residente (*watcher*):
1. Faz varredura da pasta `Inbox/` inicial e retoma pendências se houver.
2. Monitora a chegada de novas gravações continuamente.
3. Transcreve com Whisper e salva o texto integral verbatim em `Transcripts/`.
4. Envia para a LLM local gerar resumo estruturado, pontos principais e tags em YAML.
5. Grava a nota final categorizada em `Notes/{Diario, Planejamento, Inbox}/`.
6. Copia o áudio processado para `Archive/` e gerencia a retenção automática (60 dias).

---

## 📱 Integração com o Celular (Syncthing)

Para um fluxo 100% automático:
1. Instale o **Syncthing** no celular e no computador.
2. No celular, use qualquer gravador de voz (ex: *Easy Voice Recorder*, *Voice Recorder*).
3. Aponte a pasta de gravações do celular para sincronizar com a pasta `Inbox/` do Logos.
4. **Modo no celular:** *Send Only* (Enviar apenas).
5. **Modo no PC:** *Receive Only* (Receber apenas). O Logos é desenhado para nunca apagar nada diretamente na Inbox, evitando conflitos de sincronização.

---

## 📂 Estrutura de Arquivos

```
logos/
├── config.py             # Carregamento dinâmico de configurações (.yaml, env)
├── triage.py             # Classificação automática por nome de arquivo / tags
├── ledger.py             # Registro idempotente de estado (evita reprocessamento)
├── stt.py                # Transcrição headless com faster-whisper e ffmpeg
├── llm.py                # Integração com Ollama (prompting, chunking, yaml)
├── writer.py             # Criação da nota Markdown e cópia para Archive
├── pipeline.py           # Orquestração do fluxo STT -> LLM -> Writer
├── watcher.py            # Monitor residente de pastas e ciclo de retenção
└── prompts/              # Prompts por rota (Diario, Planejamento, Inbox)

setup.py                  # Instalador e configurador TUI interativo
install_startup.py        # Gerenciador de inicialização (Windows Startup / Linux systemd)
install.bat / install.sh  # Scripts de 1 clique para instalação rápida
tests/                    # Suíte de testes isolados e automatizados
```

---

## ⚙️ Configuração Personalizada

O arquivo de configuração `logos_config.yaml` é gerado automaticamente na raiz do projeto durante o `setup.py`. Você pode editá-lo diretamente a qualquer momento:

```yaml
data_root: "~/Logos"
whisper_models_dir: "~/Logos/models"
whisper_model_size: "large-v3-turbo"
whisper_compute_type: "int8"
whisper_cpu_threads: 6
whisper_language: "pt"
ollama_host: "http://localhost:11434"
llm_model: "gemma4:26b"
archive_retention_days: 60
```
