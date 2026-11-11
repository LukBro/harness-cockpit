"""Hook PreToolUse (AskUserQuestion): sesja w tle nie pyta człowieka, tylko strażnika decyzji.

W sesji w tle (CLAUDE_CODE_SESSION_KIND=bg) AskUserQuestion blokuje sesję na prośbie, której nikt nie
widzi (HAR-219, HAR-180: 49 min). Hook odmawia i podaje protokół: sesja kończy turę wiadomością
z linią `DECYZJA-POTRZEBNA`, a strażnik decyzji (.git/monolynx/straznik) odpowiada przez SendMessage.
W sesji interaktywnej hook przepuszcza narzędzie bez zmian.
Nieczytelne wejście w sesji w tle (nie JSON, bajty spoza UTF-8, błąd odczytu stdin, nie obiekt, brak `tool_name`
albo nie napis) = odmowa
(fail-closed, reguła granica-bledow).
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


def narzedzie(tekst):
    """Nazwa narzędzia z wejścia hooka; None, gdy wejścia nie da się odczytać."""
    try:
        nazwa = json.loads(tekst)["tool_name"]
    except (ValueError, KeyError, TypeError):
        return None
    return nazwa if isinstance(nazwa, str) else None


def main():
    if os.environ.get("CLAUDE_CODE_SESSION_KIND") != "bg":
        return 0
    try:
        tekst = sys.stdin.buffer.read()  # bajty: spoza UTF-8 -> ValueError w narzedzie(), nie traceback
    except OSError:
        tekst = b""
    if narzedzie(tekst) not in (None, "AskUserQuestion"):
        return 0
    json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                      "permissionDecisionReason": PROTOKOL}}, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
