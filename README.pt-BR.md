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
   - Instale o Ollama e baixe o modelo padrão:
     ```bash
     ollama pull gemma4:e4b
     ```
   - Opcional: `gemma4:26b` (~18 GB) extrai entidades melhor, mas é várias vezes mais lento em CPU. Para usar, `llm_model: "gemma4:26b"`.

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

Só um watcher roda por vez: o `main.py` guarda o PID em `state/logos.lock` e sai se outra instância viva já detém o lock.

Manutenção: `python main.py --limpar-fantasmas` remove do ledger as falhas deixadas por sincronizações parciais de um arquivo cuja versão completa já foi processada (grava antes um backup do `ledger.json`).

O Logos iniciará o processo residente (*watcher*):
1. Faz varredura da pasta `Inbox/` inicial e retoma pendências se houver.
2. Monitora a chegada de novas gravações e de notas de texto `.txt`/`.md` continuamente. Texto pula o Whisper; arquivo sem nome `AAAA-MM-DD HH-MM-SS` é datado pelo horário de modificação.
   - **Vocabulário de rotas:** a primeira palavra escolhe a rota (`diário`, `planejamento`, qualquer outra → Inbox). Na Inbox, `tarefa`, `ideia` e `reunião` preenchem `categoria` por regra, e `tarefa urgente` define `prioridade: alta`.
   - **Frontmatter:** toda nota registra `fonte_tipo` (`audio`, `texto`) e `canal` (`syncthing`, ou `telegram` para arquivos `… tg.<ext>`).
   - **ActivityWatch:** cada captura concluída posta um evento `logos.capture` no bucket `aw-watcher-logos_<hostname>` (`{rota, canal, tipo, duracao_audio_s, processamento_s}`); ignorado em silêncio se o `:5600` não responder.
3. Transcreve com Whisper e salva o texto integral verbatim em `Transcripts/`.
4. Envia para a LLM local gerar resumo estruturado, pontos principais e tags em YAML.
5. Grava a nota final categorizada em `Notes/{Diario, Planejamento, Inbox}/`.
6. Copia o áudio processado para `Archive/` e gerencia a retenção automática (60 dias).

---

## 💬 Bot Telegram (texto e voz de qualquer lugar)

O watcher também roda um bot Telegram numa thread (long polling, sem porta aberta). O bot só grava arquivos na `Inbox/`; o pipeline normal os transforma em notas.

1. Crie o bot no [@BotFather](https://t.me/BotFather) e guarde o token **fora do repo**, em `<data_root>/state/secrets.yaml`:
   ```yaml
   telegram_token: "123456789:AAH..."
   ```
   ou na variável de ambiente `LOGOS_TELEGRAM_TOKEN`. Nunca no `logos_config.yaml`.
2. Relance o watcher e mande `/start` ao bot. O primeiro chat que fizer isso vira o dono (`telegram_chat_id` gravado no `secrets.yaml`); mensagens de qualquer outro chat são ignoradas e logadas.
3. Texto vira `Inbox/AAAA-MM-DD HH-MM-SS tg <message_id>.txt`; voz vira `… tg <message_id>.oga`. O sufixo ` tg` define `canal: telegram`. Cada mensagem recebe uma resposta com o nome do arquivo.
4. Os comandos `/tarefa`, `/ideia`, `/planejamento` e `/diario` prefixam a primeira palavra correspondente. `/nova` fica reservado para a janela de sessão. Arquivos acima do limite de 20 MB de download da Bot API são recusados com resposta.

Capturas de Diário mandadas pelo Telegram passam pela nuvem do Telegram; o bot lembra disso quando a mensagem começa com "diário".

Quando a Inbox é uma pasta receive-only do Syncthing, acrescente `* tg*` ao `Inbox/.stignore`: sem isso, os arquivos do bot aparecem como alterações locais, e "Revert local changes" os apaga.

### Envio a partir de scripts

```bash
python -m logos.telegram send --text "Briefing da manhã pronto"
python -m logos.telegram send --text "Atividade" --file atividade/2026-09-14.png
python -m logos.telegram check
```

Rode da raiz do repo com o Python do `.venv`. Imagens vão como foto (legenda = `--text` quando tiver até 1024 caracteres); os demais arquivos, como documento. Texto longo é dividido em partes de 4096 caracteres e enviado sem formatação. Códigos de saída: `0` enviado, `1` falha no envio, `2` bot não configurado (sem token ou ainda sem dono). O `send` não toca no `status.json` nem no lock do watcher.

O estado do bot (`off`, `polling`, `error`, usuário, último poll, último erro) sai em `bot` no `state/status.json`.

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
├── lock.py               # Lock de instância única
├── status.py             # Status do pipeline em tempo real (state/status.json)
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
ollama_keep_alive: "10m"   # mantém o modelo carregado entre notas
llm_model: "gemma4:e4b"    # opcional: gemma4:26b
llm_min_words: 40          # notas mais curtas pulam a LLM (título = 8 primeiras palavras)
archive_retention_days: 60
```
