#!/usr/bin/env python3
"""Bramka startu epizodu: flagi, ticket, blokery, branch, rozjazd z bazą, baseline - jednym wywołaniem.
Część gitowa PRZETESTOWANA lokalnie na repozytorium tymczasowym; pobranie ticketu przez monolynx_op.py
i preflighty pluginu: UNTESTED (wymagają CLI Monolynx i sieci).

Zastępuje kroki 1, 1c, 1d, 2a-2d, 3a i "Baseline review" starego SKILL.md (ok. 7-12 osobnych wywołań
narzędzi na sesję, w tym 4 komendy gita, które sesja w worktree odrzucała, gdy model je łączył).

Wywołanie (ścieżki absolutne, wpisane literalnie):
  python3 start.py <KEY> --op <katalog pluginu>/scripts/monolynx_op.py --slug <slug>
          [--ticket-json <plik>]      ticket pobrany przez MCP (gdy monolynx_op.py zwrócił kod 3)
          [--krytyk <.../critic_backend.py>] [--transport <.../monolynx_transport.py>]

Co sprawdza, w tej kolejności (pierwszy niespełniony warunek kończy bramkę):
  1. ticket pobrany i ma pola key/title/status          -> inaczej 2
  2. status done/in_review                              -> 13 (decyzja człowieka: przejąć?)
  3. blokery `blocked_by` w statusie innym niż done     -> 10 (czeka na merge; nigdy nie merguj blokera)
  4. branch bazowy: MONOLYNX_BASE_BRANCH > MONOLYNX_MR_TARGET > origin/HEAD; brak -> 11
  5. bieżący branch = bazowy albo detached HEAD         -> 11
  6. MONOLYNX_BRANCH_MODE=ticket i brak numeru ticketu jako osobnego segmentu nazwy brancha -> 11
  7. git fetch origin <baza>; za bazą > 0, własnych 0, czyste drzewo -> git merge --ff-only sam;
     fetch albo fast-forward odrzucony -> 12; własne commity albo brudne drzewo -> praca dalej, rozjazd w stanie
  8. baseline: SHA HEAD i lista brudnych plików (PLIKI_SPRZED_STARTU) - po ewentualnym fast-forward

Wyjście: jedna linia JSON (flagi, ticket, tryb zmiana|defekt, baza, rozjazd, baseline, krytyk, transport),
potem ZAMROŻONA ostatnia linia `START: kod=<n> powod=<tekst>`. Stan trafia do
<git-common-dir>/monolynx/<KEY>/stan.json (znacznik start_tm). Istniejący stan NIE jest nadpisywany:
baseline, przydział, bramki i werdykty zostają, w JSON pojawia się "wznowienie": true,
a powod = "wznowienie - czytaj references/wznowienie.md".

Kody wyjścia:
  0   można pracować
  2   brak dowodu: ticketu nie da się pobrać/odczytać, błąd gita; NIGDY nie traktuj jak 0
  10  niezamknięty bloker (powod zawiera klucze)
  11  branch wymaga decyzji człowieka (bazowy, detached, bez numeru, baza nieustalona)
  12  rozjazd z bazą nie do usunięcia automatycznie (fetch albo --ff-only odrzucony)
  13  ticket done/in_review - decyzja człowieka
  64  błąd użycia
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

FLAGI = {"BRANCH_MODE": "ticket", "AUTOTEST": "false", "AUTOCOMMIT": "false", "AUTOPUSH": "false",
         "CONTRACT": "ask", "SPRINT_RUN": "0", "AUTOMR": "false", "BASE_BRANCH": "", "MR_TARGET": ""}


def git(*a, timeout=120):
    try:
        r = subprocess.run(["git", *a], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def koniec(kod, powod, wynik):
    wynik["kod"] = kod
    print(json.dumps(wynik, ensure_ascii=False))
    print(f"START: kod={kod} powod={powod}")
    sys.exit(kod)


def pobierz_ticket(args):
    if args.ticket_json:
        try:
            with open(args.ticket_json, encoding="utf-8") as f:
                return json.load(f), None
        except (OSError, UnicodeDecodeError, ValueError) as e:
            return None, f"ticket-json nieczytelny: {e}"
    if not args.op:
        return None, "brak --op i --ticket-json"
    try:
        r = subprocess.run([sys.executable, args.op, "get_ticket", "--project_slug", args.slug,
                            "--ticket_id", args.key], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"monolynx_op nie wystartował: {e}"
    if r.returncode == 3:
        return None, "monolynx_op kod 3: pobierz ticket toolem MCP get_ticket, zapisz JSON i podaj --ticket-json"
    if r.returncode != 0:
        return None, f"monolynx_op kod {r.returncode}: {r.stderr.strip()[:200]}"
    try:
        return json.loads(r.stdout), None
    except ValueError:
        return None, "monolynx_op zwrócił nie-JSON"


def ostatnia_linia(skrypt, *a):
    if not skrypt:
        return None
    try:
        r = subprocess.run([sys.executable, skrypt, *a], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"nieuruchomiony: {e}"
    linie = [x for x in r.stdout.splitlines() if x.strip()]
    return linie[-1] if linie else f"brak wyniku (kod {r.returncode})"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("key")
    p.add_argument("--op")
    p.add_argument("--slug", default=os.environ.get("MONOLYNX_PROJECT_SLUG", ""))
    p.add_argument("--ticket-json")
    p.add_argument("--krytyk")
    p.add_argument("--transport")
    try:
        args = p.parse_args()
    except SystemExit:
        sys.exit(64)
    flagi = {k: os.environ.get("MONOLYNX_" + k, v) for k, v in FLAGI.items()}
    if flagi["SPRINT_RUN"] == "1":
        flagi["CONTRACT"] = "auto"
    w = {"flagi": flagi}

    t, err = pobierz_ticket(args)
    if err or not isinstance(t, dict) or not all(k in t for k in ("key", "title", "status")):
        koniec(2, err or "ticket bez pól key/title/status", w)
    labels = [str(x.get("name", x) if isinstance(x, dict) else x).lower() for x in (t.get("labels") or [])]
    defekt = bool({"bug", "defekt"} & set(labels)) or bool(
        re.match(r"(?i)(fix|napraw|bug|b[łl][ąa]d)", t["title"]))
    w["ticket"] = {"key": t["key"], "id": t.get("id"), "title": t["title"], "status": t["status"],
                   "story_points": t.get("story_points"), "spec_page_id": t.get("spec_page_id")}
    w["tryb"] = "defekt" if defekt else "zmiana"
    if t["status"] in ("done", "in_review"):
        koniec(13, f"ticket ma status {t['status']}", w)
    otwarte = [b.get("key", "?") for b in (t.get("blocked_by") or []) if not isinstance(b, dict) or b.get("status") != "done"]
    if otwarte:
        koniec(10, "czeka na merge " + ",".join(otwarte), w)

    kod, baza, _ = 0, flagi["BASE_BRANCH"] or flagi["MR_TARGET"], ""
    if not baza:
        kod, out, _ = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        baza = out.split("/", 1)[1] if kod == 0 and "/" in out else ""
    if not baza:
        koniec(11, "nie ustalono brancha bazowego - ustaw MONOLYNX_BASE_BRANCH", w)
    w["baza"] = baza
    kod, branch, _ = git("branch", "--show-current")
    if kod != 0:
        koniec(2, "git branch --show-current nieudane", w)
    w["branch"] = branch
    if not branch or branch == baza:
        koniec(11, f"praca na branchu bazowym albo detached HEAD ({branch or 'detached'})", w)
    num = re.search(r"(\d+)$", t["key"])
    if flagi["BRANCH_MODE"] == "ticket" and num and not re.search(rf"(^|[-_/]){num.group(1)}($|[-_/])", branch):
        koniec(11, f"branch {branch} bez numeru ticketu {num.group(1)}", w)

    kod, _, e = git("fetch", "origin", baza, timeout=300)
    if kod != 0:
        koniec(12, f"git fetch origin {baza} odrzucony: {e[:150]}", w)
    k1, za, _ = git("rev-list", "--count", f"HEAD..origin/{baza}")
    k2, wlasne, _ = git("rev-list", "--count", f"origin/{baza}..HEAD")
    k3, brudne, _ = git("status", "--porcelain")
    if k1 or k2 or k3:
        koniec(2, "rev-list/status nieudane", w)
    za, wlasne = int(za), int(wlasne)
    rozjazd = {"za_baza": za, "wlasne": wlasne, "czyste": not brudne, "przewinieto": 0}
    if za > 0 and wlasne == 0 and not brudne:
        kod, _, e = git("merge", "--ff-only", f"origin/{baza}")
        if kod != 0:
            koniec(12, f"fast-forward do origin/{baza} odrzucony: {e[:150]}", w)
        rozjazd["przewinieto"] = za
    w["rozjazd"] = rozjazd

    _, sha, _ = git("rev-parse", "HEAD")
    _, brudne, _ = git("status", "--porcelain")
    w["baseline"] = {"sha": sha, "pliki_sprzed_startu": [x[3:] for x in brudne.splitlines() if x.strip()]}
    w["krytyk"] = ostatnia_linia(args.krytyk, "preflight")
    w["transport"] = ostatnia_linia(args.transport)

    kod, common, _ = git("rev-parse", "--path-format=absolute", "--git-common-dir")
    kat = os.path.join(common, "monolynx", t["key"])
    plik = os.path.join(kat, "stan.json")
    try:
        os.makedirs(kat, exist_ok=True)
        if os.path.exists(plik):
            # Wznowienie: baseline, przydział, bramki i werdykty zostają - ponowny pomiar baseline wpisałby
            # pracę zespołu do pliki_sprzed_startu i krytyk wykluczyłby ją z oceny.
            with open(plik, encoding="utf-8") as f:
                stan = json.load(f)
            stan.update(flagi=w["flagi"], ticket=w["ticket"], rozjazd_wznowienia=w["rozjazd"])
            w["baseline"] = stan.get("baseline")
            w["wznowienie"] = True
        else:
            stan = dict(w, key=t["key"], znaczniki={"start_tm": int(time.time())}, poprawki={})
        with open(plik + ".tmp", "w", encoding="utf-8") as f:
            json.dump(stan, f, ensure_ascii=False, indent=1)
        os.replace(plik + ".tmp", plik)
    except (OSError, UnicodeDecodeError, ValueError) as e:
        koniec(2, f"stan nieczytelny albo zapis nieudany: {e}", w)
    w["stan"] = plik
    koniec(0, "wznowienie - czytaj references/wznowienie.md" if w.get("wznowienie") else "ok", w)


if __name__ == "__main__":
    main()
