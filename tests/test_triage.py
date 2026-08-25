"""Testes de triagem contra os nomes reais observados na Inbox."""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logos.triage import Route, is_ignored, triage


def check(cond, msg):
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {msg}")
    return cond


def main():
    results = []

    # --- padrões reais de tag ---
    t = triage("2026-08-23 22-46-52 D.m4a")
    results.append(check(t.route == Route.DIARIO, "tag ' D' -> Diario"))
    results.append(check(t.recorded_at == datetime(2026, 8, 23, 22, 46, 52), "timestamp do nome (D)"))

    t = triage("2026-08-23 22-52-10 P.m4a")
    results.append(check(t.route == Route.PLANEJAMENTO, "tag ' P' -> Planejamento"))

    t = triage("2026-08-23 23-07-27 Diário.m4a")
    results.append(check(t.route == Route.DIARIO, "tag ' Diário' -> Diario"))

    t = triage("2026-08-23 23-11-41 D4.m4a")
    results.append(check(t.route == Route.DIARIO, "tag ' D4' -> Diario"))

    t = triage("2026-08-23 23-13-03.m4a")
    results.append(check(t.route == Route.INBOX, "sem tag -> Inbox"))

    t = triage("2026-08-23 23-13-03 X.m4a")
    results.append(check(t.route == Route.INBOX, "tag desconhecida ' X' -> Inbox (nunca descarta)"))

    t = triage("arquivo_totalmente_fora_do_padrao.m4a")
    results.append(check(t.route == Route.INBOX, "nome fora do padrão -> Inbox, nunca exceção"))

    # --- .evr_recently_deleted_* são áudio real, não devem ser ignorados ---
    results.append(check(
        not is_ignored(".evr_recently_deleted_(1787537285279)2026-08-23 23-07-27 Diário.m4a"),
        ".evr_recently_deleted_* NÃO é ignorado (é áudio real)",
    ))
    t = triage(".evr_recently_deleted_(1787537285279)2026-08-23 23-07-27 Diário.m4a")
    results.append(check(t.route == Route.DIARIO, ".evr_recently_deleted_* com tag Diário resolve rota corretamente"))

    t = triage(".evr_recently_deleted_(1787537526895)2026-08-23 23-11-41 D4.m4a")
    results.append(check(t.route == Route.DIARIO, ".evr_recently_deleted_* com tag D4 resolve rota corretamente"))

    t = triage(".evr_recently_deleted_(1787537542021)2026-08-23 23-10-10.m4a")
    results.append(check(t.route == Route.INBOX, ".evr_recently_deleted_* sem tag -> Inbox"))

    # --- lixo do Syncthing/app deve ser ignorado ---
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

