"""Espelho das gravações do Grava no Google Drive, pelo G: do Drive for Desktop (fase 3, 15/10/2026).

O Syncthing traz do celular Recordings/Grava para `config.GRAVA_DIR` (transcrever/ e
guardar/<apelido>/). Daqui cada gravação é COPIADA para "<GRAVA_DRIVE_DIR>/<apelido>/<mesmo nome>",
com o apelido tirado do nome do arquivo (a primeira palavra depois da hora). Sem projeto no Google
Cloud: o Drive já é uma pasta local nesta máquina (ver Gotchas no vault).

Nunca move nem apaga nada, nem na origem nem no Drive: arquivo que já está lá com outro tamanho
fica como está, com um aviso no log. O Diário nunca vai ao Drive — ele nem passa por esta pasta
(vem por Recordings/Diarios para a Inbox), e mesmo assim nome ou pasta com "diario" é recusado aqui.
O que o Logos transcreve (as pastas observadas, como transcrever/) só vai depois que o ledger
confirma que a fala não o mandou para o Diário: lá a rota vem da primeira palavra dita, não do nome.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

from . import config, eventos
from .ledger import Ledger, State
from .triage import Route, _name_match, _normalize_word, _strip_extension, is_audio_file, is_ignored

logger = logging.getLogger(__name__)

# Abaixo disso o arquivo pode estar sendo escrito. O Syncthing grava num temporário e renomeia, e
# preserva o mtime do celular, então na prática isto mede desde o fim da gravação, não da chegada.
IDADE_MINIMA_S = 120
PASTA_SEM_APELIDO = "outros"
# Começo de palavra: pega "diarios" e apelidos que o Grava aceita criar, como "diariointimo".
_DIARIO = "diario"
# Estados em que a rota do ledger já veio da fala. FAILED não serve: _mark_failed grava a rota
# tirada só do nome, e "entrevista.m4a" que começa com "Diário de hoje" apareceria como Inbox.
_ROTA_PELA_FALA = (State.TRANSCRIBED, State.ENRICHED, State.ARCHIVE_EXPIRED)
# Apelido de tipo no Grava: ASCII minúsculo, sem espaço. O dono pode criar outros além dos
# padrões, então não é a lista de categorias do triage. Sem ponto nem barra: vira nome de pasta.
_APELIDO_RE = re.compile(r"^[a-z0-9_-]{1,40}$")

# Tamanho de cada arquivo na passada anterior: tamanho que mudou entre passadas = ainda chegando.
_vistos: dict[str, int] = {}
# Arquivo que já está no Drive com outro tamanho: avisa uma vez por arquivo, não a cada passada.
_conflitos_avisados: set[str] = set()
# Mesmo contrato para a falha de cópia (no G: uma falha permanente se repetiria a cada 10 min) e
# para as recusas de configuração (origem ausente, pasta do Logos dentro da origem).
_falhas_avisadas: set[str] = set()
_avisos_dados: set[str] = set()
_drive_ausente_avisado = False
_passada_lock = threading.Lock()


def apelido_do_nome(nome: str) -> str:
    """Pasta do Drive para o arquivo: o apelido depois da hora, ou "outros" sem padrão reconhecível."""
    m = _name_match(nome)
    words = (m.group("tag") or "").split() if m else []
    if words and _APELIDO_RE.match(words[0]):
        return words[0]
    return PASTA_SEM_APELIDO


def _palavras(texto: str) -> list[str]:
    # Sem acento e minúsculo; sublinhado e hífen também separam ("2026-10-15_22-21-50_diario").
    return [p for p in re.split(r"[\W_]+", _normalize_word(texto)) if p]


def _excluido(caminho_relativo: Path) -> bool:
    """Diário ou palavra da lista de exclusão em qualquer palavra do nome ou das pastas, com ou
    sem o padrão de data: "Diário.m4a" ou "diario conversa.m4a" dados à mão cairiam em "outros"."""
    # Item que normaliza para vazio ("...") casaria com tudo e pararia o espelho em silêncio.
    proibidas = (_DIARIO, *(w for w in (_normalize_word(str(p)) for p in config.GRAVA_DRIVE_EXCLUIR) if w))
    partes = [*caminho_relativo.parts[:-1], _strip_extension(caminho_relativo.name)]
    return any(p.startswith(proibidas) for parte in partes for p in _palavras(parte))


def _pastas_transcritas() -> set[Path]:
    """Pastas que o watcher observa: o que está nelas tem a rota decidida pela fala."""
    pastas = set()
    for d in config.inbox_dirs():
        try:
            pastas.add(d.resolve())
        except OSError:
            pastas.add(d)
    return pastas


def _liberado_pela_fala(arquivo: Path, tamanho: int, ledger: Ledger | None) -> bool:
    """Gravação transcrita só vai depois que o ledger diz que a fala não a mandou para o Diário."""
    if ledger is None:
        return False
    entrada = ledger.get(arquivo.name, tamanho)
    return entrada is not None and entrada.state in _ROTA_PELA_FALA and entrada.route != Route.DIARIO.value


def _gravacoes(origem: Path):
    """Áudios de origem, recursivo, sem entrar em pasta com ponto (.stfolder, .stversions)."""
    for raiz, pastas, arquivos in os.walk(origem):
        pastas[:] = sorted(p for p in pastas if not p.startswith(".") and not is_ignored(p))
        for nome in sorted(arquivos):
            if is_ignored(nome) or not is_audio_file(nome):
                continue
            yield Path(raiz) / nome


def _copiar(arquivo: Path, alvo: Path) -> None:
    """Cópia para um temporário com ponto ao lado do alvo e troca de uma vez: no Drive nunca
    aparece um arquivo pela metade com o nome final."""
    temporario = alvo.with_name(f".{alvo.name}.copiando")
    try:
        shutil.copyfile(arquivo, temporario)
        try:
            # A data da gravação é bom ter, não condição: se o disco virtual do Drive recusar a
            # cópia da data e dos atributos, os bytes já estão lá e a cópia segue.
            shutil.copystat(arquivo, temporario)
        except OSError as e:
            logger.debug(f"Espelho do Grava: data de {arquivo.name} não copiada: {e}")
        os.replace(temporario, alvo)
    except BaseException:
        # Só o nosso temporário; nada do dono é apagado.
        try:
            temporario.unlink()
        except OSError:
            pass
        raise


def _dentro(caminho: Path, pasta: Path) -> bool:
    return caminho.is_relative_to(pasta)  # inclui a própria pasta


def _recusa_de_configuracao(origem: Path, destino: Path) -> str:
    """Motivo para não espelhar nada nesta configuração, ou "" se está tudo certo."""
    origem_r = origem.resolve()
    # A Inbox recebe o Diário, o Archive guarda cópia dele com outro nome ("..._Diario.m4a") e
    # Transcripts guarda o texto: com qualquer um dentro da origem, o os.walk os levaria ao Drive.
    for nome, pasta in (("Inbox", config.INBOX_DIR), ("Archive", config.ARCHIVE_DIR),
                        ("Transcripts", config.TRANSCRIPTS_DIR)):
        if _dentro(pasta.resolve(), origem_r):
            return f"grava_dir ({origem}) contém a pasta {nome} do Logos ({pasta})"
    # Sem resolve() no destino: com o Drive desmontado a letra nem existe.
    destino_abs = Path(os.path.abspath(destino))
    if _dentro(destino_abs, origem_r):
        return f"grava_drive_dir ({destino}) fica dentro de grava_dir ({origem})"
    for pasta in config.inbox_dirs():
        if _dentro(destino_abs, Path(os.path.abspath(pasta))):
            # O espelho alimentaria a transcrição e geraria notas duplicadas.
            return f"grava_drive_dir ({destino}) fica dentro de uma pasta observada ({pasta})"
    return ""


def _avisar_uma_vez(chave: str, mensagem: str) -> None:
    if chave not in _avisos_dados:
        logger.warning(mensagem)
        _avisos_dados.add(chave)


def espelhar(origem: Path | None = None, destino: Path | None = None, agora: float | None = None,
             ledger: Ledger | None = None) -> dict:
    """Uma passada. Devolve a contagem do que fez; nunca levanta por arquivo problemático.

    `ledger` é o do watcher, em memória: sem ele, nada das pastas observadas é copiado."""
    global _drive_ausente_avisado
    origem = config.GRAVA_DIR if origem is None else origem
    destino = config.GRAVA_DRIVE_DIR if destino is None else destino
    feito = {"copiados": 0, "iguais": 0, "esperando": 0, "recusados": 0, "falhas": 0}
    if not origem or not destino:
        return feito  # desligado
    origem, destino = Path(origem), Path(destino)
    motivo = _recusa_de_configuracao(origem, destino)
    if motivo:
        _avisar_uma_vez(f"config:{motivo}", f"Espelho do Grava recusado: {motivo}.")
        return feito
    try:
        existe = origem.is_dir()
    except OSError:
        existe = False
    if not existe:
        _avisar_uma_vez(f"origem:{origem}", f"Espelho do Grava: pasta do Grava não encontrada ({origem}); nada a copiar.")
        return feito
    _avisos_dados.discard(f"origem:{origem}")

    # A pasta-mãe do destino (G:/Meu Drive) tem de existir: com o Drive desmontado não se cria nada
    # no lugar do ponto de montagem. A própria pasta Grava e as dos apelidos são criadas aqui.
    try:
        montado = destino.parent.is_dir()
    except OSError:
        montado = False
    if not montado:
        if not _drive_ausente_avisado:
            logger.warning(f"Espelho do Grava: Drive indisponível ({destino.parent}); tento de novo na próxima varredura.")
            _drive_ausente_avisado = True
        return feito
    if _drive_ausente_avisado:
        logger.info(f"Espelho do Grava: Drive de volta ({destino.parent}).")
        _drive_ausente_avisado = False

    agora = time.time() if agora is None else agora
    transcritas = _pastas_transcritas()
    presentes = set()
    for arquivo in _gravacoes(origem):
        chave = str(arquivo)
        presentes.add(chave)
        try:
            st = arquivo.stat()
            if _excluido(arquivo.relative_to(origem)):
                feito["recusados"] += 1
                continue
            anterior = _vistos.get(chave)
            _vistos[chave] = st.st_size
            if agora - st.st_mtime < IDADE_MINIMA_S or (anterior is not None and anterior != st.st_size):
                feito["esperando"] += 1
                continue
            if arquivo.parent.resolve() in transcritas and not _liberado_pela_fala(arquivo, st.st_size, ledger):
                # Ainda não transcrita, falhou, ou a fala a mandou para o Diário (aí espera para sempre).
                feito["esperando"] += 1
                continue
            alvo = destino / apelido_do_nome(arquivo.name) / arquivo.name
            if alvo.exists():
                if alvo.stat().st_size == st.st_size:
                    feito["iguais"] += 1
                elif chave not in _conflitos_avisados:
                    logger.warning(f"Espelho do Grava: {alvo} já existe com outro tamanho; deixei como está.")
                    _conflitos_avisados.add(chave)
                continue
            alvo.parent.mkdir(parents=True, exist_ok=True)
            _copiar(arquivo, alvo)
            feito["copiados"] += 1
            _falhas_avisadas.discard(chave)
            logger.info(f"Espelho do Grava: {arquivo.name} -> {alvo.parent}")
        except Exception as e:
            feito["falhas"] += 1
            if chave not in _falhas_avisadas:
                logger.warning(f"Espelho do Grava: falha ao copiar {arquivo.name}: {e}")
                _falhas_avisadas.add(chave)
            else:
                logger.debug(f"Espelho do Grava: falha de novo ao copiar {arquivo.name}: {e}")

    for chave in set(_vistos) - presentes:
        del _vistos[chave]
    if feito["copiados"]:
        n = feito["copiados"]
        eventos.registrar("logos", f"Grava: {n} gravaç{'ão copiada' if n == 1 else 'ões copiadas'} para o Drive.")
    return feito


def _passada_segura(ledger: Ledger | None) -> None:
    try:
        espelhar(ledger=ledger)
    except Exception:
        logger.exception("Espelho do Grava: passada falhou; tento de novo na próxima varredura.")
    finally:
        _passada_lock.release()


def iniciar_passada(ledger: Ledger | None = None) -> bool:
    """Dispara uma passada numa thread própria, para nunca atrasar a transcrição. Se a anterior
    ainda está copiando (Drive lento, gravação de horas), esta não começa. Nunca levanta."""
    if not config.GRAVA_DIR or not config.GRAVA_DRIVE_DIR:
        return False
    if not _passada_lock.acquire(blocking=False):
        return False
    try:
        threading.Thread(target=_passada_segura, args=(ledger,), name="grava-drive", daemon=True).start()
    except Exception:
        _passada_lock.release()
        logger.exception("Espelho do Grava: não consegui abrir a thread.")
        return False
    return True
