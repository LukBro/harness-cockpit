#!/usr/bin/env python3
"""Straz projektu: hook PreToolUse dla sesji Claude Code w tym repozytorium.

Odmawia (kod 2 + powod na stderr), gdy narzedzie probuje:
- wypchnac cokolwiek poza `origin` = github.com/LukBro/harness-cockpit,
  na galaz spoza `worktree-HAR-*`, `retro-*`, `fix-*`, na `main`/`master`,
  z wymuszeniem albo kasowaniem, albo commity innego autora niz LukBro
  lub z linia wspolautora (CLAUDE.md, sekcja Commity);
- uzyc `gh` poza tym repozytorium albo w trybie zmieniajacym repozytorium,
  sekrety, workflow, uwierzytelnienie; scalic PR z pominieciem ochrony (`--admin`)
  albo bez gwarancji zielonego CI: w sesji sprintu (MONOLYNX_SPRINT_RUN=1) tylko
  `gh pr merge --auto` (GitHub scala po zielonych checkach), w innych sesjach
  `--auto` albo `--match-head-commit <sha>` (forma `/monolynx:mr-queue`);
- zmienic ustawienia albo hooki Claude Code w `.claude/` (sesja nie zmienia sobie
  uprawnien). Odblokowanie wylacznie przez czlowieka: zmienna
  COCKPIT_STRAZ_ODBLOKUJ=1 w srodowisku procesu `claude`, nie w komendzie.

Brak dowodu to odmowa (`.claude/rules/falszywie-zielone.md`): gdy czegos nie da sie
sprawdzic (nieczytelna komenda, brak `origin/main`), komenda nie przechodzi.
Kazda decyzja o push / gh trafia do dziennika audytu poza repozytorium.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = "LukBro/harness-cockpit"
AUTOR = "lukaszbrowar@gmail.com"
GALEZIE = re.compile(r"(worktree-HAR-\d+|retro-[\w.-]+|fix-[\w.-]+)")
CHRONIONE = {"main", "master"}
FLAGI_ZAKAZANE_PUSH = {
    "-f", "--force", "--force-with-lease", "--force-if-includes",
    "-d", "--delete", "--mirror", "--all", "--prune", "--repo",
}
GH_ZAKAZANE = {"repo", "secret", "variable", "workflow", "ruleset", "auth", "release",
               "ssh-key", "gpg-key", "extension", "alias", "codespace", "gist"}
SCIEZKI_STRAZY = (".claude/settings.json", ".claude/settings.local.json", ".claude/hooks/")
TYLKO_ODCZYT = {"cat", "head", "tail", "less", "grep", "rg", "wc", "ls", "diff"}
GIT_ODCZYT = {"show", "diff", "log", "status", "blame"}


class Odmowa(Exception):
    pass


def _git(katalog: str, *argv: str) -> str:
    wynik = subprocess.run(["git", "-C", katalog, *argv], capture_output=True, text=True,
                           timeout=20)
    if wynik.returncode != 0:
        raise Odmowa(f"nie da sie sprawdzic: git {' '.join(argv)} ({wynik.stderr.strip()})")
    return wynik.stdout.strip()


def _repo_z_url(url: str) -> str | None:
    m = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                     r"([^/]+/[^/]+?)(?:\.git)?/?", url.strip())
    return m.group(1) if m else None


HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
POWLOKI = {"bash", "sh", "zsh", "dash", "ksh"}


def _bez_heredoc(komenda: str) -> str:
    """Usuwa tresc heredoc (dane, nie polecenia). Gdy w linii wystepuje powloka
    (`bash <<EOF`), tresc jest poleceniami i zostaje do oceny. Heredoc bez
    zamkniecia to odmowa."""
    linie, wynik, i = komenda.split("\n"), [], 0
    while i < len(linie):
        linia = linie[i]
        ograniczniki = [m.group(2) for m in HEREDOC.finditer(linia)]
        i += 1
        if not ograniczniki:
            wynik.append(linia)
            continue
        do_powloki = any(Path(t).name in POWLOKI for t in linia.split())
        wynik.append(HEREDOC.sub("", linia))
        for ogr in ograniczniki:
            cialo = []
            while i < len(linie) and linie[i].strip() != ogr:
                cialo.append(linie[i])
                i += 1
            if i >= len(linie):
                raise Odmowa(f"heredoc bez zamkniecia ({ogr})")
            i += 1
            if do_powloki:
                wynik.extend(cialo)
    return "\n".join(wynik)


def _bez_przekierowan(slowa: list[str]) -> list[str]:
    """Argumenty polecenia bez przekierowan: operator (`>`, `2>&`, `>>`), jego cel
    i numer deskryptora tuz przed nim (`2>&1` -> nic)."""
    wynik, pomin = [], False
    for t in slowa:
        if pomin:
            pomin = False
            continue
        if t and set(t) <= set("<>&") and ("<" in t or ">" in t):
            if wynik and wynik[-1].isdigit():
                wynik.pop()
            pomin = True
            continue
        wynik.append(t)
    return wynik


def _segmenty(komenda: str) -> list[list[str]]:
    """Dzieli komende na proste polecenia po `;`, `&&`, `||`, `|`, `&` i nowej linii -
    tylko POZA cudzyslowami (`jq '... | ...'`, `git commit -m "a; b"` to jedno polecenie)."""
    lekser = shlex.shlex(_bez_heredoc(komenda), posix=True, punctuation_chars=";&|\n<>")
    lekser.whitespace = " \t\r"
    lekser.whitespace_split = True
    try:
        tokeny = list(lekser)
    except ValueError as e:
        raise Odmowa(f"nieczytelna komenda ({e})") from e
    segmenty, slowa = [], []
    for t in tokeny + [";"]:
        if t and set(t) <= set(";&|\n"):
            while slowa and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", slowa[0]):
                slowa = slowa[1:]      # przypisania zmiennych przed komenda
            if slowa:
                segmenty.append(slowa)
            slowa = []
        else:
            slowa.append(t)
    return segmenty


def _podkomenda_git(slowa: list[str]) -> tuple[int, list[str]]:
    """Indeks podkomendy gita i opcje globalne przed nia (`git -C x -c k=v push`)."""
    i, globalne = 1, []
    while i < len(slowa) and slowa[i].startswith("-"):
        globalne.append(slowa[i])
        if slowa[i] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") \
                and i + 1 < len(slowa):
            globalne.append(slowa[i + 1])
            i += 2
        else:
            i += 1
    return i, globalne


def _git_push(slowa: list[str], cwd: str, git=_git) -> None:
    slowa = _bez_przekierowan(slowa)
    i, globalne = _podkomenda_git(slowa)
    katalog = cwd
    j = 0
    while j < len(globalne):
        if globalne[j] == "-C" and j + 1 < len(globalne):
            katalog = str(Path(cwd, globalne[j + 1]))
            j += 2
            continue
        raise Odmowa(f"git push z opcja globalna {globalne[j]} (np. -c, --git-dir) "
                     "jest zabroniony")
    argumenty = slowa[i + 1:]
    for a in argumenty:
        nazwa = a.split("=", 1)[0]
        if nazwa in FLAGI_ZAKAZANE_PUSH or (a.startswith("-") and not a.startswith("--")
                                             and "f" in a[1:]):
            raise Odmowa(f"git push {a} jest zabroniony (wymuszenie, kasowanie, inne repo)")
    pozycyjne = [a for a in argumenty if not a.startswith("-")]
    zdalne = pozycyjne[0] if pozycyjne else "origin"
    if zdalne != "origin":
        raise Odmowa(f"push tylko do origin, nie do {zdalne!r}")
    for adres in (git(katalog, "remote", "get-url", "origin"),
                  git(katalog, "remote", "get-url", "--push", "origin")):
        if _repo_z_url(adres) != REPO:
            raise Odmowa(f"origin wskazuje na {adres!r}, a push jest dozwolony tylko do {REPO}")
    refspece = pozycyjne[1:] or [git(katalog, "rev-parse", "--abbrev-ref", "HEAD")]
    for ref in refspece:
        if ref.startswith("+"):
            raise Odmowa(f"refspec z '+' (wymuszenie) jest zabroniony: {ref}")
        zrodlo, _, cel = ref.partition(":")
        cel = (cel or zrodlo).removeprefix("refs/heads/")
        if cel in CHRONIONE or not GALEZIE.fullmatch(cel):
            raise Odmowa(f"push na galaz {cel!r} jest zabroniony; dozwolone: "
                         "worktree-HAR-<n>, retro-*, fix-*")
        commity = git(katalog, "log", "--format=%H%x00%ae%x00%B%x1e",
                      f"origin/main..{zrodlo or cel}")
        for wpis in filter(None, (c.strip() for c in commity.split("\x1e"))):
            sha, autor, tresc = (wpis.split("\x00") + ["", ""])[:3]
            if autor != AUTOR:
                raise Odmowa(f"commit {sha[:7]} ma autora {autor}, dozwolony tylko {AUTOR}")
            if re.search(r"(?im)^co-authored-by:", tresc):
                raise Odmowa(f"commit {sha[:7]} ma linie Co-Authored-By (CLAUDE.md)")


def _gh_merge(slowa: list[str], srodowisko) -> None:
    if "--admin" in slowa:
        raise Odmowa("gh pr merge --admin (z pominieciem ochrony) robi tylko czlowiek")
    if "--auto" in slowa or "--disable-auto" in slowa:
        return
    if srodowisko.get("MONOLYNX_SPRINT_RUN") == "1":
        raise Odmowa("w sesji sprintu gh pr merge tylko z --auto (merge po zielonym CI)")
    if not any(a == "--match-head-commit" or a.startswith("--match-head-commit=")
               for a in slowa):
        raise Odmowa("gh pr merge tylko z --auto albo --match-head-commit <sha>: konto ma"
                     " prawa admina i zwykly merge moglby ominac czerwone checki")


def _gh(slowa: list[str], srodowisko=os.environ) -> None:
    slowa = _bez_przekierowan(slowa)
    for j, a in enumerate(slowa):
        if a in ("-R", "--repo") and j + 1 < len(slowa) and slowa[j + 1] != REPO:
            raise Odmowa(f"gh tylko dla {REPO}, nie {slowa[j + 1]}")
        if a.startswith("--repo=") and a.split("=", 1)[1] != REPO:
            raise Odmowa(f"gh tylko dla {REPO}")
    polecenie = [a for a in slowa[1:] if not a.startswith("-")]
    if not polecenie:
        return
    if polecenie[0] in GH_ZAKAZANE and polecenie[:2] != ["auth", "status"]:
        raise Odmowa(f"gh {polecenie[0]} jest zabronione w sesjach agentow")
    if polecenie[0] == "pr" and len(polecenie) > 1 and polecenie[1] == "merge":
        _gh_merge(slowa, srodowisko)
    if polecenie[0] == "api":
        metoda = next((slowa[j + 1] for j, a in enumerate(slowa)
                       if a in ("-X", "--method") and j + 1 < len(slowa)), "GET").upper()
        zapis = any(a in ("-f", "-F", "--field", "--raw-field", "--input") for a in slowa)
        if metoda != "GET" or zapis:
            raise Odmowa("gh api tylko do odczytu (GET bez pol)")


def _sciezka_strazy(sciezka: str) -> bool:
    s = sciezka.replace("\\", "/")
    return any(w in s for w in SCIEZKI_STRAZY)


def ocen(zdarzenie: dict, git=_git, srodowisko=os.environ) -> str | None:
    """Zwraca opis decyzji do dziennika (None = nic do zapisania); rzuca Odmowa."""
    narzedzie = zdarzenie.get("tool_name", "")
    wejscie = zdarzenie.get("tool_input") or {}
    odblokowane = srodowisko.get("COCKPIT_STRAZ_ODBLOKUJ") == "1"
    if narzedzie in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        sciezka = str(wejscie.get("file_path") or wejscie.get("notebook_path") or "")
        if _sciezka_strazy(sciezka) and not odblokowane:
            raise Odmowa(f"zmiana {sciezka} (ustawienia/hooki Claude Code) tylko przez czlowieka")
        return None
    if narzedzie != "Bash":
        return None
    komenda = str(wejscie.get("command", ""))
    cwd = str(zdarzenie.get("cwd") or os.getcwd())
    opis = None
    for slowa in _segmenty(komenda):
        program = Path(slowa[0]).name
        podkomenda = None
        if program == "git":
            i, _ = _podkomenda_git(slowa)
            podkomenda = slowa[i] if i < len(slowa) else None
        przekierowanie = any(">" in s for s in slowa)
        odczyt = (program in TYLKO_ODCZYT or podkomenda in GIT_ODCZYT) and not przekierowanie
        if any(_sciezka_strazy(s) for s in slowa) and not odczyt and not odblokowane:
            raise Odmowa("komenda dotyka ustawien/hookow Claude Code - tylko czlowiek")
        if podkomenda == "push":
            _git_push(slowa, cwd, git)
            opis = f"git push: {' '.join(slowa)}"
        elif podkomenda == "remote" and len(slowa) > i + 1 \
                and slowa[i + 1] in ("add", "set-url", "rename", "remove", "rm"):
            raise Odmowa("zmiana zdalnych repozytoriow jest zabroniona")
        elif program == "gh":
            _gh(slowa, srodowisko)
            if any(a in slowa for a in ("create", "merge", "rerun", "update-branch")):
                opis = f"gh: {' '.join(slowa)}"
    return opis


def _dziennik(wpis: dict) -> None:
    katalog = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"),
                   "harness-cockpit")
    try:
        katalog.mkdir(parents=True, exist_ok=True)
        with open(katalog / "audyt-git.ndjson", "a", encoding="utf-8") as f:
            f.write(json.dumps(wpis, ensure_ascii=False) + "\n")
    except OSError:
        pass  # dziennik pomocniczy; decyzja nie zalezy od jego zapisu


def main() -> int:
    try:
        zdarzenie = json.load(sys.stdin)
    except (ValueError, OSError) as e:
        print(f"straz: nieczytelne wejscie hooka ({e}) - odmowa", file=sys.stderr)
        return 2
    teraz = datetime.now(timezone.utc).isoformat()
    try:
        opis = ocen(zdarzenie)
    except Odmowa as e:
        _dziennik({"czas": teraz, "decyzja": "odmowa", "powod": str(e),
                   "komenda": (zdarzenie.get("tool_input") or {}).get("command"),
                   "cwd": zdarzenie.get("cwd")})
        print(f"straz projektu: {e}", file=sys.stderr)
        return 2
    if opis:
        _dziennik({"czas": teraz, "decyzja": "zgoda", "opis": opis, "cwd": zdarzenie.get("cwd")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
