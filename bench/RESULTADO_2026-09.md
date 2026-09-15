# Bench de modelos · setembro de 2026

Fase 6 do [Plano de Integração Total]. Script: `bench/run_bench_2026_09.py` (fases `llm`, `think`, `keepalive`, `numctx`, `whisper`, `vision`, `summary`).

## Condições

- **Máquina:** Ryzen 7 5700G (8c/16t), 32 GB, tudo em CPU. Disco C com 50,3 GB livres ao fim do bench, não os ~80 GB que o plano registra, e isso já com o Whisper `medium` (1,43 GB) baixado.
- **Versões:** Ollama 0.34.0, faster-whisper 1.2.1, jiwer 4.0.0.
- **Execução:** em 2026-09-14, das 19:09 às 22:46 (o diagnóstico de visão terminou às 22:49), com o watcher do Logos parado e CPU abaixo de 20 % antes de começar.
- **Temperatura:** o Windows não expõe sensor (nem WMI nem contador térmico). A proteção usada foi parar se uma repetição ficasse 3× mais lenta que a anterior, e isso não aconteceu.
- **Corpus:** as 4 transcrições de `results_quick.txt`: diário (724 palavras), planejamento (1267), inbox (148) e inbox (28). A última fica abaixo de `llm_min_words: 40` e, na produção, nem chega à LLM.
- **Prompts e parâmetros:** os reais de `logos/prompts/`, com `num_ctx` e `keep_alive` passados explicitamente (o config não foi tocado). Cada combinação rodou 2 vezes. **Com n = 2, a mediana é a média**, então trate diferenças pequenas como ruído.
- **Carga:** `load_duration` do Ollama. **tok/s:** `eval_count / eval_duration`.
- **Entidades:** comparadas com um gabarito por transcrição, em três classes: esperadas, aceitas (inferência pedida pelo prompt) e proibidas. A proibida é a ambiguidade do Vini registrada em `Gotchas.md`: no diário é a outra pessoa. Contam também os links fora da whitelist e os falsos positivos, isto é, links da whitelist sem menção.
- **Inglês:** detectado só nos valores do YAML. O detector antigo casava a chave `tags:` e por isso deu 4/4 para todos em `results_quick.txt`, um falso alarme.

## 1. LLM com a configuração de produção (raciocínio padrão, `num_ctx` 8192)

| Modelo | Carga | s/nota quente: diário · planej. · inbox 148 · inbox 28 | tok/s | YAML ok | Entidades esperadas: diário · planej. | Links errados | Inglês |
|---|---|---|---|---|---|---|---|
| qwen2.5:3b | 2 s | 29 · 55 · 36 · 10 | 11,8 | 6/8 | 2/6 · 3/9 e 0/9 | proibida 2/2; `[[Nome]]` copiado do molde; whitelist inteira numa inbox | 0/8 |
| gemma4:e2b | 11 s | 168 · 129 · 69 · 57 | 13,8 | 8/8 | 4–5/6 · 9/9 | proibida 4/4 (diário e planej.); ~9 falsos positivos por planejamento | 0/8 |
| **gemma4:e4b** | 15 s | 276 · 291 · 141 · 107 | 6,7 | 7/8¹ | **6/6 · 9/9** | **nenhum** | 0/8 |
| gemma4:26b | 100 s | 360 · 657 · 266 · 249 | 8,5 | 8/8 | 6/6 · 9/9 | nenhum | 0/8 |

¹ A falha foi na inbox de 28 palavras, que a produção não manda à LLM: `titulo: Lembrete: ...` sem aspas (o prompt da inbox não exige aspas) e o fallback recuperou título, resumo e tags.

**O que domina a latência é raciocínio que não aparece na resposta.** Os gemma4 geram tokens de raciocínio mesmo sem `think` no payload. No `/api/generate`, que é o que `llm.py` usa, o campo `thinking` volta vazio e esses tokens só aparecem em `eval_count`. No `/api/chat` o campo volta preenchido. Quem implementar `think` em `llm.py` precisa saber disso. Por nota, o e4b gera cerca de 1 700 tokens e o 26b de 3 000 a 5 000, para cerca de 300 a 450 tokens de YAML visível.

**e4b em mais rodadas.** No planejamento com 8192 foram 5 rodadas (as 2 da matriz e as 3 da seção 4):
- 9/9 entidades em 4 rodadas e 7/9 em uma;
- falsos positivos em 2 das 5: `[[Financas]]` e, uma vez, a equipe inteira do escritório. Isso vem da regra de "inferência ativa" do prompt;
- nenhuma entidade proibida em nenhuma rodada.

## 2. Desligando o raciocínio (`think: false`), medição extra

A produção não envia `think`. Esta fase mede o que acontece se enviar.

| Modelo | Carga fria | s/nota quente: diário · planej. · inbox 148 · inbox 28 | Entidades: diário · planej. | Links errados |
|---|---|---|---|---|
| e2b | 11 s | 29 · 60 · 18 · 11 | 6/6 e **0/6** · 9/9 e **0/9** | proibida 2; numa rodada, 22 falsos positivos; na outra, nenhum link |
| e4b | 15 s | 62 · 88 · 32 · 21 | 3/6 e 5/6 · 8/9 e 8/9 | proibida 1 (a ambiguidade do Vini volta); listas aninhadas 1 |
| **26b** | 67 s | **53 · 90 · 55 · 34** | 5/6 e 6/6 · 8/9 e 8/9 | proibida 0; uma grafia truncada fora da whitelist; um falso positivo |

Desligar o raciocínio deixa tudo de 3 a 5× mais rápido, mas custa entidades.
- **e4b:** é o que mais perde, e volta a errar justamente o caso que o vault registra como perigoso.
- **26b:** perde uma entidade por nota, sem nunca emitir a proibida. É a única configuração medida que cabe na meta do plano (latência por nota abaixo de 90 s) com qualidade próxima da do e4b padrão, **mas só com o modelo quente**. Uma nota isolada, chegando mais de 10 min depois da anterior, paga 67–100 s de carga e fica em 120–190 s. Ainda assim é mais rápida que o e4b padrão (276–291 s).

## 3. `keep_alive` (e4b, sequência das 4 notas)

| Nota | carga com `"0"` | carga com `"10m"` |
|---|---|---|
| 1ª (diário) | 15,4 s | 14,6 s |
| 2ª (planejamento) | 15,2 s | 0 s |
| 3ª (inbox 148) | 14,8 s | 0 s |
| 4ª (inbox 28) | 15,0 s | 0 s |
| **Sequência total** | 805,0 s | 805,0 s |

Com `"10m"`, a recarga de cerca de 15 s some em cada nota depois da primeira. O total empatou porque a rodada com `"10m"` gerou 396 tokens de raciocínio a mais, cerca de 50 s: a variação do raciocínio entre rodadas é maior que o custo de recarga.

**A hipótese do plano não se confirma.** A recarga não é parte relevante dos 229 s de produção. São 15 s de carga contra 110 a 280 s de geração. `"10m"` continua certo, mas só economiza em rajadas.

No 26b a recarga custa de 67 a 100 s, então lá o `keep_alive` longo pesa de verdade.

## 4. `num_ctx` (e4b, planejamento de 1267 palavras, prompt de 2 689 tokens)

| `num_ctx` | Término | Tokens gerados | YAML | Entidades | s/nota quente |
|---|---|---|---|---|---|
| 4096 | `length` nas 3 | 1 407 nas 3 (2 689 + 1 407 = 4 096) | inválido 3/3 | 0/9 nas 3 | 184–188 |
| 8192 | `stop` nas 3 | 1 628–1 802 | válido 3/3 | 9/9, 7/9, 9/9 | 223–241 |

Com 4096, o raciocínio oculto come a janela e a resposta é cortada no meio. Numa das chamadas saíram só 181 caracteres (`titulo`, `tags`, `prioridade`, `status` e `res…`). Na produção isso não levanta erro: o fallback grava uma nota sem entidades.

Com o raciocínio ligado, 4096 não serve para nada acima de ~1 000 palavras. **8192 é necessário, não folga.**

## 5. Whisper: large-v3-turbo × medium (int8, 6 threads, beam 5, VAD)

| Áudio | Duração | turbo: s (tempo ÷ duração) | medium: s (tempo ÷ duração) | Divergência do medium em relação ao turbo | S · D · I |
|---|---|---|---|---|---|
| diário | 310 s | 133 (0,43) | 246 (0,80) | 21,1 % | 55 · 92 · 6 |
| planejamento | 522 s | 245 (0,47) | 430 (0,82) | 11,5 % | 72 · 38 · 36 |
| inbox 148 | 77 s | 29 (0,38) | 36 (0,47) | 5,4 % | 1 · 7 · 0 |
| inbox 28 | 12 s | 9 (0,75) | 7 (0,60) | 14,3 % | 3 · 1 · 0 |
| **Total** | 920 s | **416 (0,45)** | **719 (0,78)** | | |

Carga: turbo 8,3 s, medium 6,3 s.

**Isto não é taxa de erro.** As transcrições salvas foram geradas pelo próprio turbo. O turbo reproduziu as 4 com 0 % de divergência, o que confirma que é determinístico. O que a coluna mede é o quanto o medium se afasta do turbo, depois de normalizar minúsculas e pontuação.

A maior parte da divergência são deleções curtas (a maior tem 8 palavras): o medium pula hesitações e repetições. Sem gabarito humano não dá para dizer qual acerta mais. Ainda assim, o medium é 1,7× mais lento no total e não há sinal de ganho, então **fica o turbo**.

## 6. Visão (gemma4:e4b)

**Ele aceita a imagem, mas não a enxerga.** O `/api/show` lista `vision` entre as capacidades, e a API aceita `images` sem erro. A imagem entra no prompt: com a imagem sintética são 180 tokens, contra 42 sem ela. Mesmo assim, o modelo responde como se nada tivesse sido anexado.

| Teste | Tokens de prompt | Tempo | Resposta |
|---|---|---|---|
| captura de tela 1920×1080, `/api/generate`, fria | 310 | 68 s | "Não foi fornecida nenhuma captura de tela…" |
| mesma imagem, quente | 310 | **1 111 s** | vazia: 7 882 tokens de raciocínio até parar |
| mesma imagem, quente | 310 | 50 s | "Por favor, anexe a captura de tela…" |
| imagem sintética 800×400 ("LOGOS 42" em fundo azul), `/api/generate` | 180 | 76 s | "Por favor, forneça a imagem…" |
| mesma imagem sintética, `/api/chat` | 180 | 61 s | "Por favor, anexe a imagem…" |
| só o texto, sem imagem (controle) | 42 | 37 s | "Por favor, anexe a imagem…" |
| imagem sintética, `/api/generate`, `think: false` | 173 | 24 s | **inventada:** "não há texto legível, o fundo é preto, forma escura no canto" |
| imagem sintética, `/api/chat`, `think: false` | 173 | 6 s | "…esta imagem não foi fornecida…" |

Com raciocínio, a resposta é igual à do controle sem imagem. Sem raciocínio, piora: no `/api/chat` continua dizendo que não recebeu nada, e no `/api/generate` descreve uma imagem que não existe, o que é pior que admitir que não viu. Na combinação Ollama 0.34.0 com `gemma4:e4b`, a visão não funciona. A causa não foi investigada, e nenhum modelo de visão foi baixado.

**Isso afeta a produção.** `config.VISION_MODEL` usa `llm_model` como padrão, e `extractors.describe_image` chama o modelo sempre que o OCR não encontra texto numa foto do Telegram. Há dois resultados possíveis:
- a nota recebe um "anexe a imagem" como descrição;
- o raciocínio dispara, estoura o timeout de 600 s e a descrição volta vazia, depois de travar o pipeline por 10 min.

Nenhuma captura fora do `Diario/` tem esse texto hoje, provavelmente porque ainda não chegou foto sem texto.

## Recomendação

- **Produção:** manter `gemma4:e4b` com raciocínio padrão, `keep_alive: "10m"` e `num_ctx: 8192`, e manter o Whisper `large-v3-turbo`. É a única configuração que não errou link em nenhuma rodada; `num_ctx` 4096 quebra a nota longa.
- **gemma4:26b:** manter os 18 GB por enquanto. Com `think: false`, é o único candidato medido abaixo de 90 s por nota **quente** com entidades quase no nível do e4b: perde uma por nota e nunca emite a proibida. Frio, fica em 120–190 s. Carregado, ocupa 9,7 GB de RAM (os 18 GB são o arquivo em disco), o que cabe nos 32 GB junto com o Whisper. Vale uma semana de teste na produção antes de decidir. Para isso, `llm.py` precisa passar a enviar `think`, mudança que fica fora desta sessão. Se não for adotado, apagar.
- **Whisper medium:** apagar (1,43 GB), porque só serviu ao bench.
- **Visão:** pôr `vision_model: ""` no `logos_config.yaml` até haver um modelo que enxergue de fato. Com o valor vazio, a foto fica só com o OCR, o que é melhor que uma descrição falsa ou 10 min de pipeline travado. Testar `qwen2.5vl:3b` ou `moondream` exige baixar modelo, então decidir antes.
- **Prompts (outra sessão):** a regra de "inferência ativa" é a fonte dos falsos positivos em todos os modelos: equipes inteiras e `[[Financas]]` sem menção. O prompt da inbox não exige aspas em campos escalares, o que causou a única falha de YAML do e4b.

[Plano de Integração Total]: ../../../notes/work/active/Logos/Logos%20%E2%80%94%20Plano%20de%20Integra%C3%A7%C3%A3o%20Total.md
