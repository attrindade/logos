# Sessão B · Logos (pipeline + bot Telegram)

Como usar: abrir o Claude Code em `C:\Workspace\repo-personal\logos` e colar tudo abaixo da linha de corte. Fonte de verdade: `C:\Workspace\notes\work\active\Logos\Logos — Plano de Integração Total.md` (Fase 0, Fase 1 e o Complemento) e `C:\Workspace\notes\work\Decisão - Canal Telegram para Captura Fora do Diário.md`. As outras frentes: sessão A em `C:\Workspace\notes\thinking\2026-09-14-sessao-A-vault.md`, C em `C:\Workspace\repo-tools\aw-tools\PROMPT_SESSAO_C.md`, D em `bench\PROMPT_SESSAO_D.md`.

---

Você está em `C:\Workspace\repo-personal\logos`, um pipeline Python offline de voz para vault: áudio chega por Syncthing em `C:\Users\andre\Logos\Inbox`, `faster-whisper` transcreve, Ollama enriquece com prompts por rota e `writer.py` grava notas em `C:\Workspace\notes\capturas\<Rota>\`. Esta sessão faz o **lado do pipeline** do plano de integração. Leia primeiro: `README.md`, `logos_config.yaml`, todos os módulos em `logos/`, `tests/`, e as três notas do vault: `C:\Workspace\notes\work\active\Logos\Logos — Plano de Integração Total.md`, `C:\Workspace\notes\work\active\Logos\Logos — Diagnóstico 2026-09-14.md` (seções 3 e 4) e `C:\Workspace\notes\work\Decisão - Canal Telegram para Captura Fora do Diário.md`.

Use o advisor antes de começar cada etapa e antes de declarar cada etapa concluída. Rode `pytest` depois de cada etapa. Commits por etapa, Conventional Commits em português, sem push até eu pedir. Pare para me perguntar só onde está escrito PERGUNTAR.

## Fatos medidos em 2026-09-14

- Ledger em `C:\Users\andre\Logos\state\ledger.json`, chave `nome::tamanho`. 26 entradas `enriched`, 7 `failed`: todas do arquivo `2026-09-01 10-38-03.m4a` em tamanhos parciais (passos de ~0,84 MB), com erro ffmpeg `moov atom not found`. A versão final (7053774 bytes) foi enriquecida. `retry_failed` reprocessa os 7 fantasmas a cada reinício.
- `logos_config.yaml`: `llm_model: gemma4:e4b`, `ollama_keep_alive: "0"`, `ollama_num_ctx: 8192`, `notes_dir: C:/Workspace/notes/capturas`. README e o North Star do vault dizem `gemma4:26b`; o que roda é `e4b`.
- Há dois `pythonw.exe` rodando `main.py`, criados no mesmo segundo (`Logos.vbs` na pasta Startup). Pode ser instância duplicada.
- `triage.is_ignored` deixa passar `.evr_recently_deleted_*` de propósito; isso faz áudio apagado no celular virar nota.
- Bug de saída: tags aninhadas (`- - - PPJ`) na captura de Planejamento de 2026-09-01; wikilink em minúsculas `[[renato barcelos advogados]]`.
- Última conclusão registrada: 229,5 s para um `.m4a` de 1,7 MB. Um ponto de dado só.
- O celular está fora do Syncthing desde 2026-09-07. Não é problema seu; é meu.
- Contrato com a sessão A (vault): os jobs do vault vão chamar `python -m logos.telegram send --text "<texto>" [--file <caminho>]` a partir deste diretório com o `.venv`. Mantenha esse CLI estável.
- Contrato com a sessão C (ActivityWatch): ao concluir cada captura, o pipeline posta um evento no bucket `aw-watcher-logos_att` via `aw-client` (`pip install aw-client`), tipo `logos.capture`, dados `{rota, canal, tipo, duracao_audio_s, processamento_s}`. Se o servidor `:5600` não responder, ignore silenciosamente. Crie isso em `logos/aw.py` na Etapa 2.

## Etapa 1 — Fase 0 do pipeline

1. **Fantasmas de sincronização parcial.** Em `pipeline.py`, tratar erro do ffmpeg contendo `moov atom not found` como *arquivo incompleto*: não gravar `failed`, registrar `INFO` e deixar o watcher tentar de novo quando o tamanho mudar. Em `retry_failed`, pular entradas `failed` cujo `filename` já tem uma entrada `enriched` com tamanho maior. Um comando `python main.py --limpar-fantasmas` (ou função em `ledger.py`) que remove essas entradas obsoletas; rodar uma vez e me mostrar o antes e depois. Teste novo em `tests/`.
2. **Lixeira do gravador.** Remover a exceção de `.evr_recently_deleted_` em `triage.is_ignored` e criar `C:\Users\andre\Logos\Inbox\.stignore` com `.evr_recently_deleted_*`. PERGUNTAR antes de criar o `.stignore`, porque a pasta é receive-only e o Syncthing pode reagir.
3. **Instância única.** Lock file em `state/logos.lock` com PID em `main.py`; se já existir um processo vivo com aquele PID, sair com log. Depois me dizer como encerrar os dois `pythonw.exe` atuais e relançar um só (não faça você: eu relanço).
4. **`ollama_keep_alive`**: mudar para `"10m"` no `logos_config.yaml` e no default de `config.py`. Não medir aqui; a sessão D mede.
5. **YAML das tags e links.** Em `llm.py`, após `yaml.safe_load`, normalizar: achatar listas aninhadas, `strip`, tags em minúsculas sem `#`; em `pessoas`/`projetos`, preservar a grafia do contexto de referência dos prompts (`[[Renato Barcelos Advogados]]`, não minúsculas). Teste com o YAML defeituoso real.
6. **Documentação.** README (os dois idiomas) passa a dizer `gemma4:e4b` como modelo padrão e cita o `26b` como opcional. Não toque no vault; a sessão A cuida do North Star.
7. Critério de aceite: `pytest` verde; `ledger.json` sem os 7 fantasmas; log do watcher sem "Reprocessando" repetido.

## Etapa 2 — Texto e vocabulário de rotas

1. `pipeline.py` aceita `.txt` e `.md`: se a extensão for texto, `transcript = path.read_text(encoding="utf-8")` e pula o Whisper. Timestamp pelo nome, como hoje; se o nome não bater no padrão, `mtime` do arquivo.
2. Frontmatter novo em `writer.py`: `fonte_tipo: audio|texto|pacote` e `canal: syncthing|telegram`. Não remover nenhum campo existente.
3. Vocabulário de rotas em `triage.py`: além de "diário" e "planejamento", a primeira palavra "tarefa", "ideia" e "reunião" mantém a rota Inbox mas preenche `categoria` (`tarefa`, `ideia`, `reuniao`) e, para "tarefa", `prioridade: alta` se a segunda palavra for "urgente". Testes.
4. Notas curtas: se o texto tiver menos de 40 palavras, pular a LLM e gerar título das primeiras 8 palavras, resumo = texto. Configurável em `logos_config.yaml` (`llm_min_words: 40`).
5. `logos/aw.py` com o contrato da sessão C; chamado no fim de `process_file`.
6. Critério de aceite: um `.txt` colocado na Inbox vira nota em `capturas/Inbox/` em menos de 10 s.

## Etapa 3 — Bot Telegram, texto e voz

1. PERGUNTAR: me peça o token do bot (eu crio no BotFather). Guardar em `C:\Users\andre\Logos\state\secrets.yaml` (fora do repo) ou variável `LOGOS_TELEGRAM_TOKEN`; nunca no `logos_config.yaml` versionado.
2. `logos/telegram.py`: long polling (`getUpdates`, sem webhook, sem porta aberta), biblioteca `python-telegram-bot` ou `requests` puro, à sua escolha, justificando. Só aceita o `chat_id` do dono: o primeiro `/start` grava o `chat_id` em `secrets.yaml`; qualquer outro chat é ignorado e logado.
3. Texto vira `Inbox/YYYY-MM-DD HH-MM-SS.txt`; voz vira `.oga` (ffmpeg já lê). Comandos `/tarefa`, `/ideia`, `/planejamento`, `/diario` prefixam a primeira palavra correspondente. `/nova` fecha a janela de sessão (Etapa 4). Cada mensagem recebida ganha uma reação ou resposta curta de confirmação com o nome do arquivo.
4. Subcomando CLI `python -m logos.telegram send --text "..." [--file caminho]` para os jobs do vault. Documentar no README.
5. O bot roda dentro do mesmo processo do watcher (thread) ou como segundo processo lançado por `main.py`; escolha e justifique. Status do bot no `status.json`.
6. Se a mensagem começar com "diário", processar normalmente e responder uma linha lembrando que o Diário via Telegram passa pela nuvem; a regra do vault mantém o Diário fora do `om` de qualquer forma.
7. Critério de aceite: mandar "tarefa urgente testar o bot" pelo celular e a nota aparecer em `capturas/Inbox/` com `canal: telegram`, `categoria: tarefa`, `prioridade: alta`.

## Etapa 4 — Pacotes multimodais

1. A unidade vira pacote: `Inbox/YYYY-MM-DD HH-MM-SS tg/` com `manifest.json` (mensagens em ordem, ids, legendas, `reply_to`, `media_group_id`) e os arquivos. Regras de agrupamento, nesta ordem: legenda é o texto da nota; álbum é uma nota; resposta anexa à nota da mensagem original (achar pelo `message_id` guardado no ledger); janela de 10 min agrupa; anexo sozinho fora de janela vira nota própria.
2. `logos/extractors.py`: imagem → OCR (`rapidocr-onnxruntime` ou Tesseract por+eng; PERGUNTAR qual instalar), e descrição por modelo de visão do Ollama só se o OCR vier vazio (testar se `gemma4:e4b` aceita imagem; senão `qwen2.5vl:3b`, PERGUNTAR antes de baixar); PDF → `pypdf`, e `ocrmypdf` se não houver camada de texto (PERGUNTAR antes de instalar Tesseract/Ghostscript); `.docx` → `python-docx`; link → `defuddle` se estiver instalado, senão `trafilatura`; localização → `local:` no frontmatter; `.md` enviado como documento é a própria nota.
3. `process_bundle` em `pipeline.py`: transcreve áudios, extrai anexos, monta um transcript único com marcadores `[foto 1: …]`, `[pdf: …]`, triagem pela primeira palavra do primeiro texto ou voz, enriquecimento com o texto todo, writer embute anexos com `![[arquivo|400]]` e `![[arquivo.pdf#page=1]]`, sidecars `arquivo.pdf.md` ao lado.
4. Anexos vão para `C:\Workspace\notes\capturas\anexos\YYYY\MM\YYYY-MM-DD_HH-MM-SS_n.<ext>`; frontmatter `anexos:` com a lista. A sessão A registra a pasta no `CLAUDE.md` do vault; você só cria.
5. Ledger: chave `nome-do-pacote::hash-do-manifest`. Limite de 20 MB por arquivo da Bot API: responder ao usuário quando exceder.
6. Critério de aceite: mandar texto, depois uma foto com legenda, depois um PDF em 3 minutos; sair uma nota só com os três embutidos e o texto do PDF no sidecar.

## Regras

- Nunca apagar nem mover arquivos de `C:\Users\andre\Logos\Inbox` (receive-only). Nunca apagar `Archive/` ou `Transcripts/`.
- Não mudar `notes_dir`. Não escrever fora de `capturas/` no vault.
- Ao terminar cada etapa, escrever um resumo curto em `RELATORIO_SESSAO_B.md` neste repo, com o que mudou, comandos para relançar o watcher e o que ficou pendente.
