"""Hook PreToolUse (AskUserQuestion): sesja w tle nie pyta człowieka, tylko strażnika decyzji.

W sesji w tle (CLAUDE_CODE_SESSION_KIND=bg) AskUserQuestion blokuje sesję na prośbie, której nikt nie
widzi (HAR-219, HAR-180: 49 min). Hook odmawia i podaje protokół: sesja kończy turę wiadomością
z linią `DECYZJA-POTRZEBNA`, a strażnik decyzji (.git/monolynx/straznik) odpowiada przez SendMessage.
W sesji interaktywnej hook przepuszcza narzędzie bez zmian.
Awaria odczytu wejścia w sesji w tle = odmowa (fail-closed, reguła granica-bledow).
"""
import json
import os
import sys

PROTOKOL = (
    "Sesja w tle nie pyta człowieka. Zamiast AskUserQuestion zakończ turę wiadomością, której pierwsza "
    "linia to `DECYZJA-POTRZEBNA: <jedno zdanie, czego dotyczy decyzja>`, a dalej: kontekst z dowodami "
    "(plik:linia, wynik komendy), opcje jako lista `- <etykieta>: <skutek>` i Twoja rekomendacja. "
    "Nie wykonuj żadnej z opcji przed odpowiedzią. Strażnik decyzji odpowie wiadomością zaczynającą się "
    "od „Decyzja strażnika”; wykonaj ją. Zanim zapytasz, sprawdź, czy odpowiedź nie wynika z ticketu, "
    "CLAUDE.md albo .claude/rules — wtedy działaj bez pytania."
)


def main():
    if os.environ.get("CLAUDE_CODE_SESSION_KIND") != "bg":
        return 0
    try:
        dane = json.load(sys.stdin)
        narzedzie = dane.get("tool_name")
    except (ValueError, OSError, AttributeError):
        narzedzie = "AskUserQuestion"  # nie umiem ocenić -> traktuję jak pytanie (fail-closed)
    if narzedzie != "AskUserQuestion":
        return 0
    json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                      "permissionDecisionReason": PROTOKOL}}, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
