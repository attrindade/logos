"""Orquestra STT (ou texto direto) -> LLM -> Writer para um arquivo, e o mesmo para um pacote do Telegram."""
import hashlib
import json
import logging
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path

from . import aw, config, extractors, formulario
from . import dia as dia_mod
from .ledger import Entry, Ledger, State
from .llm import enrich, is_long_recording, recording_enrichment, short_enrichment
from .status import PipelineStage, tracker
from .stt import transcribe_file
from .triage import Route, Triage, _normalize_word, is_bundle_name, is_ignored, is_text_file, triage
from .writer import (
    IMAGE_EXTS,
    append_to_note,
    archive_audio,
    store_attachment,
    write_link_sidecar,
    write_note,
    write_sidecar,
)

logger = logging.getLogger(__name__)

EXCERPT_WORDS = 1500  # por anexo, no texto enviado à LLM; o sidecar guarda o texto inteiro
MAX_LINKS_PER_MESSAGE = 3
TEXT_DOC_EXTS = {".md", ".txt"}


def is_incomplete_file_error(error: Exception) -> bool:
    """MP4/M4A sem o átomo moov: o arquivo ainda não terminou de ser escrito/sincronizado."""
    return "moov atom not found" in str(error)


def _mark_failed(path: Path, size: int, mtime: datetime, ledger: Ledger, stage: str, error: Exception) -> None:
    t_initial = triage(path.name, fallback=mtime)
    ledger.upsert(Entry(
        filename=path.name, size=size, state=State.FAILED,
        route=t_initial.route.value, recorded_at=t_initial.recorded_at.isoformat(), error=str(error),
    ))
    tracker.set_failed(path.name, stage, str(error))


def _degradation_reason(error: Exception) -> str:
    """Frase curta para o dono, não o traceback. Recusa de conexão é o caso comum."""
    text = str(error)
    if "10061" in text or "Max retries exceeded" in text or "NewConnectionError" in text:
        return f"O Ollama não respondeu em {config.OLLAMA_HOST} — o serviço provavelmente está parado."
    return f"A etapa de enriquecimento falhou: {_one_line(text, 300)}"


def _notify_degraded(filename: str, reason: str) -> None:
    """Avisa o dono pelo canal que ele já usa. Import local: telegram importa o pipeline."""
    try:
        from . import telegram
        aviso = f"Logos: {filename} virou nota sem enriquecimento."
        telegram.notify(aviso + chr(10) + reason)
    except Exception as e:
        logger.warning(f"Não deu para avisar a degradação de {filename}: {e}")


def _notify_note_ready(route: str, note_path: Path, enrichment: dict, tipo: str | None) -> None:
    """Devolve a nota pronta no mesmo chat. Sem isto o dono fala, recebe um "registrado" e
    nunca vê o resultado: a nota existe e fica no vault, invisível.

    O Diário é a exceção deliberada. A decisão de exposição do diário o mantém 100% local, e
    tudo que passa pelo bot passa pela nuvem do Telegram — então dele vai só a confirmação de
    que existe, sem título, sem ação, sem uma linha do conteúdo."""
    try:
        from . import telegram
        if route.lower().startswith("diario") or route.lower().startswith("diário"):
            telegram.notify("Logos: diário registrado.")
            return
        partes = [f"Logos: {enrichment.get('titulo') or note_path.stem}"]
        if tipo == "gravacao":
            partes.append("(gravação longa — transcrita, sem enriquecimento)")
        proxima = enrichment.get("proxima_acao")
        if proxima:
            partes.append(f"Próxima ação: {proxima}")
        acoes = enrichment.get("acoes") or []
        if isinstance(acoes, list) and acoes:
            partes.append("Ações: " + "; ".join(str(a) for a in acoes[:3]))
        partes.append(note_path.name)
        telegram.notify(chr(10).join(partes))
    except Exception as e:
        logger.warning(f"Não deu para devolver a nota de {note_path.name}: {e}")


# ---------------------------------------------------------------------------
# Nota por dia (Diário e Planejamento)
# ---------------------------------------------------------------------------

_ROTAS_DO_DIA = (Route.DIARIO, Route.PLANEJAMENTO)
_ROTA_DO_MANIFEST = {"diario": Route.DIARIO, "planejamento": Route.PLANEJAMENTO}
_CAMPOS_DA_ENTRADA = (
    "titulo", "tags", "humor", "resumo", "proxima_acao", "acoes", "decisoes", "bloqueios",
    "vitorias", "gratidao", "tensoes", "pontos", "takeaways", "foco", "intencao", "blocos",
    "fica_de_fora", "amanha", "pessoas", "projetos",
)


def _fala_diz_amanha(fala: str) -> bool:
    """'planejamento amanhã ...' ou 'planejamento para amanhã ...' é plano do dia seguinte."""
    return any(_normalize_word(p) == "amanha" for p in fala.split()[1:3])


def _dia_da_entrada(t: Triage, manifest: dict | None = None, fala: str = "") -> date:
    valor = (manifest or {}).get("para") or (manifest or {}).get("dia")
    if valor:
        try:
            return date.fromisoformat(valor)
        except ValueError:
            pass
    dia = dia_mod.dia_logico(t.recorded_at)
    if t.route == Route.PLANEJAMENTO and _fala_diz_amanha(fala):
        dia += timedelta(days=1)
    return dia


def _registrar_no_dia(
    t: Triage, dia: date, fonte: str, origem: str, texto_md: str, enrichment: dict, llm_model: str,
    tipo: str | None, degradado: str, respostas: dict | None = None, carregar: dict | None = None,
    anexos: list[dict] | None = None, local: dict | None = None,
) -> tuple[dia_mod.Registro, dict]:
    dados = {
        "fonte": fonte,
        "momento": t.recorded_at.isoformat(timespec="seconds"),
        "hora": f"{t.recorded_at:%H:%M}",
        "origem": origem,
        "formulario": respostas or {},
        "carregar": carregar or {},
        "enriquecimento": {k: enrichment[k] for k in _CAMPOS_DA_ENTRADA if enrichment.get(k) not in (None, "", [], {})},
        "modelo_llm": llm_model,
        "degradado": degradado,
        "tipo": tipo or "",
    }
    dados = {k: v for k, v in dados.items() if v not in ("", {}, None) or k in ("fonte", "hora")}
    registro = dia_mod.registrar_entrada(t.route, dia, dados, texto_md, anexos, local)
    if t.route == Route.DIARIO and (respostas or {}).get("plano_cumprido"):
        try:
            dia_mod.marcar_cumprido(dia, respostas["plano_cumprido"])
        except Exception as e:
            logger.warning(f"Não deu para marcar o plano de {dia} como conferido: {e}")
    return registro, dados


def _notify_dia(route: Route, dia: date, registro: dia_mod.Registro, dados: dict) -> None:
    """Devolutiva da entrada. Do diário sai só a confirmação: nem título, nem uma linha da fala."""
    try:
        from . import telegram
        rotulo = dia_mod.rotulo_dia(dia)
        if route == Route.DIARIO:
            texto = "Logos: diário registrado."
            if registro.total > 1:
                texto = f"Logos: diário registrado (entrada {registro.numero} de {rotulo})."
            if registro.aviso:
                texto += chr(10) + registro.aviso
            telegram.notify(texto)
            return
        enr = dados.get("enriquecimento") or {}
        partes = [f"Logos: plano para {rotulo}, entrada {registro.numero}"]
        foco = (dados.get("formulario") or {}).get("foco")
        if not foco or foco == "outro":
            foco = enr.get("foco") or ""
        if foco:
            partes.append(f"Foco: {foco}")
        if enr.get("intencao"):
            partes.append(f"A UMA coisa: {enr['intencao']}")
        if enr.get("proxima_acao"):
            partes.append(f"Próxima ação: {enr['proxima_acao']}")
        acoes = enr.get("acoes") or []
        if isinstance(acoes, list) and acoes:
            partes.append("Ações: " + "; ".join(str(a) for a in acoes[:3]))
        partes.append(registro.caminho.name)
        if registro.aviso:
            partes.append(registro.aviso)
        telegram.notify(chr(10).join(partes))
    except Exception as e:
        logger.warning(f"Não deu para devolver a entrada de {registro.caminho.name}: {e}")


def process_path(path: Path, ledger: Ledger) -> None:
    """Arquivo solto -> process_file; pasta de pacote do bot -> process_bundle; outras pastas, nada."""
    if path.is_dir():
        if is_bundle_name(path.name):
            process_bundle(path, ledger)
        return
    process_file(path, ledger)


def process_file(path: Path, ledger: Ledger) -> None:
    if is_ignored(path.name):
        return

    try:
        stat = path.stat()
    except FileNotFoundError:
        return  # arquivo sumiu entre a detecção e o processamento
    size = stat.st_size
    # Nome fora do padrão "YYYY-MM-DD HH-MM-SS" (ex.: texto salvo à mão) usa o mtime.
    mtime = datetime.fromtimestamp(stat.st_mtime)

    if ledger.is_processed(path.name, size):
        logger.debug(f"Já processado (ledger): {path.name}")
        return

    start_time = time.time()
    started_at = datetime.now().astimezone()
    fonte_tipo = "texto" if is_text_file(path.name) else "audio"
    duracao_audio_s = 0.0
    existing = ledger.get(path.name, size)

    if existing is not None and existing.state == State.TRANSCRIBED and existing.transcript_path:
        # Retomando após queda entre transcrição e enriquecimento — não retranscreve.
        transcript_path = Path(existing.transcript_path)
        transcript = transcript_path.read_text(encoding="utf-8")
        t = triage(path.name, transcript, fallback=mtime)
        tracker.set_stage(
            PipelineStage.TRIAGING,
            filename=path.name,
            file_size=size,
            route=t.route.value,
            message=f"Retomando: classificado como '{t.route.value}'",
            details=f"Transcrição reaproveitada de {transcript_path.name}",
        )
    else:
        if fonte_tipo == "texto":
            try:
                # utf-8-sig: o Bloco de Notas e alguns apps gravam BOM
                transcript = path.read_text(encoding="utf-8-sig").strip()
            except (OSError, UnicodeDecodeError) as e:
                logger.error(f"Falha ao ler texto de {path.name}: {e}")
                _mark_failed(path, size, mtime, ledger, PipelineStage.TRANSCRIBING, e)
                return
        else:
            # Etapa 2: Transcrição STT (Whisper)
            tracker.set_stage(
                PipelineStage.TRANSCRIBING,
                filename=path.name,
                file_size=size,
                message=f"Transcrevendo áudio com Whisper ({config.WHISPER_MODEL_SIZE})...",
                details=f"Arquivo: {path.name} ({round(size / 1024, 1)} KB)",
            )
            try:
                transcription = transcribe_file(path)
            except Exception as e:
                if is_incomplete_file_error(e):
                    # Syncthing entregou uma versão parcial enquanto o gravador ainda escrevia.
                    # Não é falha: quando o tamanho mudar, o watcher chama process_file de novo.
                    logger.info(f"Arquivo incompleto (sincronização parcial), aguardando nova versão: {path.name} ({size} bytes)")
                    tracker.set_idle()
                    return
                logger.error(f"Falha na transcrição de {path.name}: {e}")
                _mark_failed(path, size, mtime, ledger, PipelineStage.TRANSCRIBING, e)
                return
            transcript = transcription.text
            duracao_audio_s = transcription.duration_s

        # Etapa 3: Triagem & Roteamento
        t = triage(path.name, transcript, fallback=mtime)
        tracker.set_stage(
            PipelineStage.TRIAGING,
            filename=path.name,
            file_size=size,
            route=t.route.value,
            message=f"Triagem concluída: Rota '{t.route.value}'",
            details=f"Data identificada: {t.recorded_at.strftime('%Y-%m-%d %H:%M:%S')}",
        )

        config.TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        transcript_name = f"{t.recorded_at.strftime('%Y-%m-%d_%H-%M-%S')}_{t.route.value}.txt"
        transcript_path = config.TRANSCRIPTS_DIR / transcript_name
        transcript_path.write_text(transcript, encoding="utf-8")

        ledger.upsert(Entry(
            filename=path.name, size=size, state=State.TRANSCRIBED,
            route=t.route.value, recorded_at=t.recorded_at.isoformat(),
            transcript_path=str(transcript_path),
        ))

    # Etapa 4: Enriquecimento com IA (LLM) — notas curtas pulam a LLM
    tipo = None
    degradado = ""
    if len(transcript.split()) < config.LLM_MIN_WORDS:
        enrichment = short_enrichment(transcript)
        llm_model = ""
    elif is_long_recording(transcript):
        # Gravação (reunião, sessão de RPG, aula): só transcrita e nomeada. O contrato de
        # nota de voz não descreve esse material; a leitura fica para a promoção em sessão.
        tracker.set_stage(
            PipelineStage.ENRICHING,
            filename=path.name,
            file_size=size,
            route=t.route.value,
            message="Gravação longa: transcrita e nomeada, sem enriquecimento.",
            details="Acima do limiar de contexto — rota de gravação.",
        )
        tipo = "gravacao"
        enrichment = recording_enrichment(transcript)
        llm_model = config.LLM_MODEL if enrichment.pop("_llm_usado", False) else ""
    else:
        tracker.set_stage(
            PipelineStage.ENRICHING,
            filename=path.name,
            file_size=size,
            route=t.route.value,
            message=f"Enriquecendo com IA via Ollama ({config.LLM_MODEL})...",
            details=f"Gerando resumo estruturado, ações e metadados",
        )
        llm_model = config.LLM_MODEL
        try:
            enrichment = enrich(transcript, t.route)
        except Exception as e:
            logger.warning(f"Falha na etapa LLM de {path.name}: {e}. Nota será gerada só com o transcrito.")
            degradado = _degradation_reason(e)
            enrichment = short_enrichment(transcript)
            _notify_degraded(path.name, degradado)

    # Etapa 5: Geração de Nota & Arquivamento
    tracker.set_stage(
        PipelineStage.WRITING,
        filename=path.name,
        file_size=size,
        route=t.route.value,
        message="Salvando nota no Obsidian e arquivando original...",
        details=f"Destino: Notes/{t.route.value}",
    )

    registro = None
    if t.route in _ROTAS_DO_DIA:
        dia = _dia_da_entrada(t, fala=transcript)
        origem = "telegram" if t.canal == "telegram" else "gravador"
        registro, dados = _registrar_no_dia(t, dia, path.name, origem, transcript, enrichment, llm_model, tipo, degradado)
        note_path = registro.caminho
    else:
        note_path = write_note(
            path.name, t, transcript, enrichment, llm_model,
            fonte_tipo=fonte_tipo, tipo=tipo, degradado=degradado,
        )
    archive_path = archive_audio(path, t)

    ledger.upsert(Entry(
        filename=path.name, size=size, state=State.ENRICHED,
        route=t.route.value, recorded_at=t.recorded_at.isoformat(),
        transcript_path=str(transcript_path), note_path=str(note_path),
        archive_path=str(archive_path),
    ))
    logger.info(f"Nota gerada: {note_path}")
    if registro is not None:
        _notify_dia(t.route, dia, registro, dados)
    else:
        _notify_note_ready(t.route.value, note_path, enrichment, tipo)

    elapsed = time.time() - start_time
    tracker.set_completed(
        filename=path.name,
        route=t.route.value,
        note_path=str(note_path),
        archive_path=str(archive_path),
        elapsed_s=elapsed,
    )
    aw.post_capture(
        started_at,
        rota=t.route.value,
        canal=t.canal,
        tipo=fonte_tipo,
        duracao_audio_s=duracao_audio_s,
        processamento_s=elapsed,
    )


# ---------------------------------------------------------------------------
# Pacotes do Telegram (Etapa 4)
# ---------------------------------------------------------------------------

def read_manifest(path: Path) -> tuple[dict, str] | None:
    """(manifest, hash). O hash dos bytes do manifest compõe a chave do pacote no ledger."""
    try:
        raw = (path / "manifest.json").read_bytes()
        manifest = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    return manifest, hashlib.sha256(raw).hexdigest()[:16]


@dataclass
class _Anexo:
    source: Path
    text: str = ""  # texto extraído; vira sidecar quando houver


@dataclass
class _Link:
    url: str
    title: str = ""
    text: str = ""


def _excerpt(text: str) -> str:
    words = text.split()
    return text if len(words) <= EXCERPT_WORDS else " ".join(words[:EXCERPT_WORDS]) + " […]"


def _one_line(text: str, limit: int = 200) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[:limit].rstrip() + "…"


def _mmss(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def _parse_iso(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _bundle_size(path: Path) -> int:
    try:
        return sum(p.stat().st_size for p in path.iterdir() if p.is_file())
    except OSError:
        return 0


def process_bundle(path: Path, ledger: Ledger) -> None:
    loaded = read_manifest(path)
    if loaded is None:
        logger.warning(f"Pacote sem manifest legível, ignorado por ora: {path.name}")
        return
    manifest, digest = loaded
    if ledger.is_processed_bundle(path.name, digest):
        logger.debug(f"Pacote já processado (ledger): {path.name}")
        return
    message_ids = [m["message_id"] for m in manifest.get("mensagens", []) if "message_id" in m]
    # O comando e a tela do formulário também são desta nota: responder ao "Diário fechado" é o
    # gesto natural de "mais uma coisa", e sem isto a resposta viraria nota nova no Inbox.
    form = manifest.get("form") or {}
    message_ids += [i for i in (form.get("sid"), form.get("msg_id")) if isinstance(i, int) and i not in message_ids]
    try:
        _process_bundle(path, manifest, digest, message_ids, ledger)
    except Exception as e:
        logger.exception(f"Falha ao processar o pacote {path.name}")
        ledger.upsert(Entry(
            filename=path.name, size=_bundle_size(path), state=State.FAILED,
            manifest_hash=digest, message_ids=message_ids, error=str(e),
        ))
        tracker.set_failed(path.name, PipelineStage.WRITING, str(e))


def _process_bundle(path: Path, manifest: dict, digest: str, message_ids: list[int], ledger: Ledger) -> None:
    start_time = time.time()
    started_at = datetime.now().astimezone()
    mensagens = manifest.get("mensagens", [])
    size = _bundle_size(path)
    tracker.set_stage(
        PipelineStage.TRIAGING, filename=path.name, file_size=size,
        message=f"Pacote do Telegram: {len(mensagens)} mensagem(ns)",
    )

    sections: list[str] = []
    perguntas: list[int | None] = []  # a pergunta do formulário que cada seção responde
    anexos: list[_Anexo] = []
    links: list[_Link] = []
    triage_text = ""  # primeiro texto, voz ou legenda: decide rota e categoria
    duracao_audio_s = 0.0
    local = None
    fotos = 0

    for m in mensagens:
        antes = len(sections)
        tipo = m.get("tipo", "")
        legenda = (m.get("legenda") or "").strip()
        arquivo = path / m["arquivo"] if m.get("arquivo") else None
        ext = arquivo.suffix.lower() if arquivo else ""
        conteudo = ""

        if tipo == "texto":
            conteudo = (m.get("texto") or "").strip()
            sections.append(conteudo)
        elif tipo in ("voz", "audio") and arquivo:
            tracker.set_stage(
                PipelineStage.TRANSCRIBING, filename=path.name, file_size=size,
                message=f"Transcrevendo {arquivo.name} do pacote com Whisper...",
            )
            try:
                transcription = transcribe_file(arquivo)
                conteudo = transcription.text
                duracao_audio_s += transcription.duration_s
                sections.append(f"[voz {_mmss(transcription.duration_s)}] {conteudo}".rstrip())
            except Exception as e:
                logger.error(f"Falha na transcrição de {arquivo.name} em {path.name}: {e}")
                sections.append("[voz: falha na transcrição]")
        elif arquivo and (tipo == "foto" or ext in IMAGE_EXTS):
            fotos += 1
            texto = extractors.image_text(arquivo)
            anexos.append(_Anexo(arquivo, texto))
            sections.append(f"[foto {fotos}: {_one_line(texto) or 'sem texto reconhecido'}] {legenda}".rstrip())
        elif arquivo and ext in TEXT_DOC_EXTS:
            conteudo = extractors.plain_text(arquivo)  # .md/.txt enviado como documento é a própria nota
            sections.append(conteudo)
        elif arquivo:
            nome = m.get("nome_original") or arquivo.name
            texto = extractors.document_text(arquivo)
            anexos.append(_Anexo(arquivo, texto))
            marcador = "pdf" if ext == ".pdf" else "arquivo"
            sections.append("\n".join(filter(None, [f"[{marcador}: {nome}] {legenda}".rstrip(), _excerpt(texto)])))
        elif tipo == "localizacao" and m.get("local"):
            local = m["local"]
            titulo = f" · {local['titulo']}" if local.get("titulo") else ""
            sections.append(f"[local: {local['lat']}, {local['lon']}{titulo}] {legenda}".rstrip())

        for url in m.get("links", [])[:MAX_LINKS_PER_MESSAGE]:
            title, text = extractors.link_text(url)
            links.append(_Link(url, title, text))
            sections.append("\n".join(filter(None, [f"[link: {title or url}] {url}", _excerpt(text)])))

        perguntas.extend([m.get("pergunta")] * (len(sections) - antes))
        if not triage_text:
            triage_text = conteudo or legenda

    t = triage(path.name, triage_text, fallback=_parse_iso(manifest.get("aberto_em")))
    rota_fixa = _ROTA_DO_MANIFEST.get(manifest.get("rota") or "")
    if rota_fixa is not None:
        # O comando já disse a rota; a primeira palavra não decide nada aqui.
        t = replace(t, route=rota_fixa, tag=rota_fixa.value, categoria="", prioridade="")
    transcript = _agrupar(sections, perguntas, manifest.get("rota"), "###")

    config.TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    transcript_path = config.TRANSCRIPTS_DIR / f"{t.recorded_at:%Y-%m-%d_%H-%M-%S}_{t.route.value}_tg.txt"
    transcript_path.write_text(transcript, encoding="utf-8")

    tracker.set_stage(
        PipelineStage.WRITING, filename=path.name, file_size=size, route=t.route.value,
        message="Guardando anexos e sidecars no vault...",
    )
    reply_to = manifest.get("reply_to")
    target = ledger.find_by_message_id(reply_to) if reply_to is not None else None
    if target is not None and not Path(target.note_path).exists():
        target = None
    # Anexo de resposta mora junto da nota que ele complementa: uma foto respondendo ao diário,
    # guardada pela rota da própria resposta (quase sempre Inbox), iria para o git.
    rota_dos_anexos = t.route
    if target is not None:
        try:
            rota_dos_anexos = Route(target.route)
        except ValueError:
            pass
    stored: list[dict] = []
    n = 0
    for anexo in anexos:
        n += 1
        dest = store_attachment(anexo.source, t.recorded_at, n, rota_dos_anexos)
        long_image_text = dest.suffix.lower() in IMAGE_EXTS and len(anexo.text) > 200
        needs_sidecar = anexo.text.strip() and (dest.suffix.lower() not in IMAGE_EXTS or long_image_text)
        stored.append({"path": dest, "sidecar": write_sidecar(dest, anexo.text) if needs_sidecar else None})
    for link in links:
        n += 1
        sidecar = (write_link_sidecar(link.url, link.title, link.text, t.recorded_at, n, rota_dos_anexos)
                   if link.text.strip() else None)
        stored.append({"url": link.url, "titulo": link.title, "sidecar": sidecar})

    if target is not None:
        # Resposta a uma mensagem que já virou nota: complementa aquela nota.
        note_path = Path(target.note_path)
        append_to_note(note_path, stored, transcript, t.recorded_at, local=local)
        route = target.route or t.route.value
        logger.info(f"Pacote {path.name} anexado à nota existente: {note_path}")
    else:
        route = t.route.value
        base = triage_text or next((a.text for a in anexos if a.text.strip()), "")
        tipo = None
        degradado = ""
        do_dia = t.route in _ROTAS_DO_DIA
        if do_dia and not transcript.strip() and not anexos:
            enrichment, llm_model = {}, ""  # só o check-in do formulário
        elif len(transcript.split()) < config.LLM_MIN_WORDS:
            enrichment = short_enrichment(base) if base.strip() else {
                "titulo": _fallback_title(anexos, local), "tags": [], "resumo": "",
            }
            llm_model = ""
        elif is_long_recording(transcript):
            tracker.set_stage(
                PipelineStage.ENRICHING, filename=path.name, file_size=size, route=route,
                message="Gravação longa no pacote: transcrita e nomeada, sem enriquecimento.",
            )
            tipo = "gravacao"
            enrichment = recording_enrichment(transcript)
            llm_model = config.LLM_MODEL if enrichment.pop("_llm_usado", False) else ""
        else:
            tracker.set_stage(
                PipelineStage.ENRICHING, filename=path.name, file_size=size, route=route,
                message=f"Enriquecendo pacote com IA via Ollama ({config.LLM_MODEL})...",
            )
            llm_model = config.LLM_MODEL
            texto_llm = "\n\n".join(filter(None, [formulario.contexto_llm(manifest), transcript]))
            try:
                enrichment = enrich(texto_llm, t.route)
            except Exception as e:
                logger.warning(f"Falha na etapa LLM do pacote {path.name}: {e}. Nota sem enriquecimento.")
                degradado = _degradation_reason(e)
                enrichment = short_enrichment(base) if base.strip() else {"titulo": path.name, "tags": [], "resumo": ""}
                _notify_degraded(path.name, degradado)
        if do_dia:
            dia = _dia_da_entrada(t, manifest, fala=triage_text)
            registro, dados = _registrar_no_dia(
                t, dia, path.name, "telegram", _agrupar(sections, perguntas, manifest.get("rota"), "####"),
                enrichment, llm_model, tipo, degradado,
                respostas=manifest.get("formulario"), carregar=manifest.get("carregar"),
                anexos=stored, local=local,
            )
            note_path = registro.caminho
            logger.info(f"Pacote {path.name} somado à nota do dia: {note_path} (entrada {registro.numero})")
            _notify_dia(t.route, dia, registro, dados)
        else:
            note_path = write_note(
                path.name, t, transcript, enrichment, llm_model,
                fonte_tipo="pacote", anexos=stored, local=local,
                tipo=tipo, degradado=degradado,
            )
            logger.info(f"Nota gerada a partir do pacote {path.name}: {note_path}")
            _notify_note_ready(route, note_path, enrichment, tipo)

    ledger.upsert(Entry(
        filename=path.name, size=size, state=State.ENRICHED,
        route=route, recorded_at=t.recorded_at.isoformat(),
        transcript_path=str(transcript_path), note_path=str(note_path),
        manifest_hash=digest, message_ids=message_ids,
    ))
    elapsed = time.time() - start_time
    tracker.set_completed(filename=path.name, route=route, note_path=str(note_path), elapsed_s=elapsed)
    aw.post_capture(
        started_at, rota=route, canal=t.canal, tipo="pacote",
        duracao_audio_s=duracao_audio_s, processamento_s=elapsed,
    )


def _agrupar(sections: list[str], perguntas: list[int | None], rota: str | None, nivel: str) -> str:
    """Texto do pacote com as respostas sob a pergunta do formulário que estava na tela.
    O que chegou fora das perguntas vem antes, sem título."""
    textos = PERGUNTAS_POR_ROTA.get(rota or "")
    if not textos or not any(perguntas):
        return "\n\n".join(s for s in sections if s).strip()
    livres = [s for s, p in zip(sections, perguntas) if s and not p]
    partes = ["\n\n".join(livres)] if livres else []
    for n in sorted({p for p in perguntas if p}):
        respostas = [s for s, p in zip(sections, perguntas) if s and p == n]
        if respostas:
            titulo = f"{nivel} {textos[n - 1]}\n\n" if 1 <= n <= len(textos) else ""
            partes.append(titulo + "\n\n".join(respostas))
    return "\n\n".join(partes).strip()


PERGUNTAS_POR_ROTA = formulario.PERGUNTAS


def _fallback_title(anexos: list[_Anexo], local) -> str:
    if anexos and all(a.source.suffix.lower() in IMAGE_EXTS for a in anexos):
        return "Foto do Telegram" if len(anexos) == 1 else "Fotos do Telegram"
    if local and not anexos:
        return f"Localização {local.get('titulo') or ''}".strip()
    return "Anexo do Telegram"


def retry_failed(ledger: Ledger) -> None:
    """Reenfileira entradas com estado FAILED, exceto as já superadas por uma versão maior
    do mesmo arquivo que chegou a ENRICHED (fantasmas de sincronização parcial)."""
    for entry in ledger.failed_entries():
        if ledger.is_superseded(entry):
            logger.debug(f"Ignorando falha obsoleta ({entry.size} bytes): {entry.filename}")
            continue
        # O ledger guarda só o nome, não a pasta de origem: procura em todas as observadas.
        for directory in config.inbox_dirs():
            path = directory / entry.filename
            try:
                existe = path.exists()
            except OSError:
                continue  # unidade do Drive fora do ar
            if existe:
                logger.info(f"Reprocessando arquivo que falhou antes: {entry.filename}")
                process_path(path, ledger)
                break
