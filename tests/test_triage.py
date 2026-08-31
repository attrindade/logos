"""Testes de triagem por primeira palavra falada e extração de timestamp."""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logos.triage import Route, is_ignored, triage, triage_route_from_text, triage_from_filename


def check(cond, msg):
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {msg}")
    return cond


def main():
    results = []

    # --- Triagem por primeira palavra da fala ---
    results.append(check(triage_route_from_text("Diário de hoje foi muito produtivo...") == Route.DIARIO, "primeira palavra 'Diário' -> Diario"))
    results.append(check(triage_route_from_text("diario: hoje acordei cedo") == Route.DIARIO, "primeira palavra 'diario:' com pontuação -> Diario"))
    results.append(check(triage_route_from_text("Planejamento para a próxima semana...") == Route.PLANEJAMENTO, "primeira palavra 'Planejamento' -> Planejamento"))
    results.append(check(triage_route_from_text("planejar as metas do mês") == Route.PLANEJAMENTO, "primeira palavra 'planejar' -> Planejamento"))
    results.append(check(triage_route_from_text("Hoje eu preciso comprar pão e leite") == Route.INBOX, "outra primeira palavra ('Hoje') -> Inbox (Nota)"))
    results.append(check(triage_route_from_text("Ideia de projeto para o app") == Route.INBOX, "outra primeira palavra ('Ideia') -> Inbox (Nota)"))
    results.append(check(triage_route_from_text("") == Route.INBOX, "transcrição vazia -> Inbox (Nota)"))

    # --- Combinação com timestamp do nome do arquivo ---
    t1 = triage("2026-08-23 22-46-52.m4a", transcript="Diário pessoal de domingo...")
    results.append(check(t1.route == Route.DIARIO, "triage() com fala 'Diário...' -> Diario"))
    results.append(check(t1.recorded_at == datetime(2026, 8, 23, 22, 46, 52), "timestamp correto extraído do nome"))

    t2 = triage("2026-08-23 22-52-10.m4a", transcript="Planejamento sprint 3")
    results.append(check(t2.route == Route.PLANEJAMENTO, "triage() com fala 'Planejamento...' -> Planejamento"))

    t3 = triage("2026-08-23 23-13-03.m4a", transcript="Lembrar de pagar a conta de luz")
    results.append(check(t3.route == Route.INBOX, "triage() com fala genérica -> Inbox"))

    # --- Nomes especiais e lixo do Syncthing ---
    results.append(check(
        not is_ignored(".evr_recently_deleted_(1787537285279)2026-08-23 23-07-27.m4a"),
        ".evr_recently_deleted_* NÃO é ignorado (é áudio real)",
    ))
    results.append(check(is_ignored(".nomedia"), ".nomedia é ignorado"))
    results.append(check(is_ignored(".stfolder"), ".stfolder é ignorado"))
    results.append(check(is_ignored(".syncthing.tmpfile.tmp"), ".syncthing.*.tmp é ignorado"))

    print()
    total = len(results)
    passed = sum(results)
    print(f"{passed}/{total} testes passaram")
    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    main()
