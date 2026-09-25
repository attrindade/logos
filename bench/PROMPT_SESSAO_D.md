# Sessão D · Bench de modelos (rodar à noite ou com o watcher parado)

Como usar: abrir o Claude Code em `C:\Workspace\repo-personal\logos` e colar tudo abaixo da linha de corte. Esta sessão só lê `bench/` e escreve resultados; convive com a sessão B no mesmo repositório, mas **briga por CPU** com o watcher do Logos, com o Whisper e com qualquer outra sessão pesada. Fonte de verdade: `C:\Workspace\notes\work\active\Logos\Logos — Plano de Integração Total.md` (Fase 6).

---

Você está em `C:\Workspace\repo-personal\logos`. Esta sessão mede, não muda nada do pipeline. Leia `bench/run_llm_bench.py`, `bench/run_llm_bench_quick.py`, `bench/run_stt.py`, `bench/validate_model.py`, `bench/results_quick.txt` e `logos_config.yaml`. Use o advisor antes de começar e antes de concluir. Sem commits até o relatório final.

## Hardware e estado

- Ryzen 7 5700G (8c/16t), 32 GB, GPU integrada sem CUDA: tudo roda em CPU. Disco com ~80 GB livres.
- Modelos no Ollama: `gemma4:e2b` (7,2 GB), `gemma4:e4b` (9,6 GB, é o que roda em produção), `gemma4:26b` (18 GB, nunca usado em produção), `qwen2.5:3b-instruct-q4_K_M` (1,9 GB). Whisper `large-v3-turbo` int8 em `C:\Users\andre\Logos\models`, 6 threads.
- Produção: `ollama_keep_alive: "0"` até a sessão B trocar para `"10m"`; `num_ctx 8192`; 229,5 s para um áudio de ~2 min, um único ponto de dado.
- Transcrições de referência em `C:\Users\andre\Logos\Transcripts\` (26 arquivos). As 4 usadas em `results_quick.txt` são a base de comparação.

## Estado depois das sessões A, B e C (atualizado em 2026-09-14, à noite)

- A sessão B já mudou `ollama_keep_alive` para `"10m"` no `logos_config.yaml` local (gitignored) e no default de `config.py`. Para medir `"0"` contra `"10m"`, passe o valor explicitamente na chamada do Ollama dentro do bench; não edite o config.
- O bot Telegram (`logos/telegram.py`) roda junto do watcher. Parar o watcher pausa o bot; as mensagens ficam na fila do Telegram e chegam quando ele voltar. Nada se perde.
- Há vários `pythonw.exe` na máquina: além do Logos (pai e filho do `.venv`), a sessão C instalou watchers do ActivityWatch que também rodam como `pythonw.exe`. **Nunca use `Get-Process pythonw | Stop-Process`.** Só o filtro por linha de comando abaixo.
- `C:\Users\andre\Logos\models` tem só `large-v3-turbo` e a pasta `tessdata` do OCR. O `medium` ainda não existe.
- `gemma4:26b` continua instalado (18 GB).

## Antes de medir

1. Verificar se o watcher do Logos está rodando (`Get-Content $HOME\Logos\state\logos.lock` mostra o PID). Se estiver, me avisar e PERGUNTAR se pode parar. Não pare sozinho. Quando eu autorizar, parar **só** os processos do Logos:
   ```powershell
   Get-CimInstance Win32_Process -Filter "name='pythonw.exe'" |
     Where-Object CommandLine -like '*logos\main.py*' |
     ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
   ```
   Relançar no fim com `wscript "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\Logos.vbs"` e conferir `logos.lock` e as últimas linhas de `C:\Users\andre\Logos\logs\watcher.log`.
2. Verificar que não há outra sessão pesada: `ollama ps` vazio, CPU abaixo de 20 % por 30 s.
3. Registrar temperatura ambiente: versão do Ollama, versão do `faster-whisper`, hora de início.

## Medições

1. **LLM, latência e qualidade**: para cada modelo (`e2b`, `e4b`, `26b`; `qwen2.5:3b` como piso), rodar as 4 transcrições de referência com os prompts reais de `logos/prompts/`. Medir: tempo de carga do modelo (primeira chamada), tempo por nota com modelo quente, tokens/s (`eval_count / eval_duration` da resposta do Ollama), YAML válido, campos de lista corretos, vazamento de inglês, entidades corretas contra o contexto de referência (`[[Governa Clima]]`, `[[Eduardo]]` etc.). Rodar cada combinação 2 vezes; reportar a mediana.
2. **`keep_alive`**: com `e4b`, a sequência de 4 notas com `keep_alive: "0"` e com `"10m"`. A diferença é o custo de recarga por nota.
3. **`num_ctx`**: `e4b` com 4096 e 8192 na transcrição mais longa (`2026-08-23_22-52-10_Planejamento.txt`, 1267 palavras).
4. **Whisper**: `large-v3-turbo` int8 contra `medium` int8 (baixar `medium` se não existir, ~1,5 GB; PERGUNTAR antes) em 4 áudios do `Archive/` de tamanhos diferentes. Medir tempo real × duração do áudio e comparar o texto com as transcrições já salvas (taxa de erro de palavra aproximada com `jiwer`, instalar no `.venv` se faltar).
5. **Visão, só se der tempo**: testar se `gemma4:e4b` aceita imagem na API do Ollama com uma captura de tela; se aceitar, tempo por imagem. Não baixar modelo novo sem PERGUNTAR.

## Relatório

Escrever `bench/RESULTADO_2026-09.md` com: tabela por modelo (carga, s/nota quente, tok/s, YAML ok, entidades ok, inglês), efeito do `keep_alive`, efeito do `num_ctx`, Whisper turbo × medium, e uma recomendação em uma frase para produção (modelo, `keep_alive`, `num_ctx`) e para o `26b` (manter ou apagar os 18 GB). Fazer um commit só, `bench: resultados de setembro de 2026`. Eu levo a recomendação para o vault.

## Regras

- Não alterar `logos_config.yaml`, `logos/` nem o ledger. Qualquer arquivo temporário vai para `bench/tmp/` e é removido no fim.
- Se a máquina passar de 90 °C ou a medição ficar 3× mais lenta que a anterior, parar e me avisar.
