"""Straz roli: hook PreToolUse Claude Code (HAR-175).

Claude Code uruchamia ten plik jako `python -I <sciezka> --rola <rola> --worktree <sciezka>`
i podaje zdarzenie na stdin (JSON). Odmowa to JSON `permissionDecision: deny` na stdout i kod 0;
brak odmowy to pusty stdout i kod 0. Hook jest fail-closed: kazdy blad (argumenty, stdin, wyjatek
z `decyzja`) konczy sie odmowa z powodem zaczynajacym sie od "straż roli:".

Fail-closed takze przy awarii startu (HAR-219): bledy importow (`cockpit.services.*`,
`runner.maskowanie`) zapamietuje `_BLAD_STARTU`, a `main` odmawia wtedy kazdego narzedzia
("straż roli: błąd startu straży (<klasa>)"). Awarie, ktorej modul nie obsluzy (blad skladni,
brak interpretera), lapie galaz powloki w poleceniu hooka (`runner.hooks._GALAZ_AWARII_STRAZY`):
kod != 0 to kod 2, czyli blokada narzedzia.

Reguly (niezmienniki z CLAUDE.md):
- no_git_write: w kazdej roli odmowa dla `git commit`, `git push` (sprawdza `waliduj_definicje`),
  podpolecen gita tworzacych commity (`_GIT_TWORZY_COMMIT`, takze `git stash`), aliasow
  ustawianych przez `-c alias.*`, `--config-env alias.*` i `git config alias.*`, `git reset --hard`
  i `git checkout`; komenda jest sprawdzana tez po zdjeciu cudzyslowow, ucieczek i `$` przed
  cudzyslowem (`"g"it commit`, `gi\\t commit`, `$'g'it commit`);
- reviewer_readonly: rola bez prawa zapisu nie uzywa Write/Edit/MultiEdit/NotebookEdit, a Bash
  tylko z bialej listy polecenia odczytu i bez metaznakow powloki; opcje zapisujace albo
  uruchamiajace polecenia (`git diff --output`, `--ext-diff`, `--textconv`, `rg --pre`, `rg -z`)
  sa odmawiane;
- kazda rola: zapis poza worktree jest odmowiony (po `realpath`, wiec `..` i dowiazania nie wyprowadza).

Granica zapisu przez Bash (rola z prawem zapisu): sprawdzamy WYLACZNIE przekierowania powloki
(`>`, `>>`, `>|`, `&>`, `N>`), rozwiazane wzgledem `cwd` zdarzenia; cel poza worktree albo
taki, ktorego nie da sie ocenic (`$`, backtick, `~`, `*`, `?`), jest odmowiony. Inne zapisy Basha
(`tee`, `cp`, `mv`, `dd of=`, `sed -i`, interpreter) NIE sa sprawdzane: Bash nie jest sandboxem
zapisu, a granica to przekierowania powloki.

Odmowa dopisuje jedna linie NDJSON `ToolDenied` do pliku z `COCKPIT_HOOK_FILE` (bez O_CREAT);
blad zapisu nie zmienia decyzji.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    # uruchomienie jako skrypt (`python -I plik`): katalog `src/` nie jest na sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# HAR-219: importy w bloku `try`: zepsuty import nie moze zakonczyc sie tracebackiem i kodem 1
# (Claude Code przepuscilby wtedy narzedzie). Blad zapamietujemy, a `main` odmawia.
_BLAD_STARTU: BaseException | None = None
try:
    from cockpit.services.invariants import Rola, profil_roli  # noqa: E402
    from cockpit.services.walidacja import (  # noqa: E402
        _OPCJE_Z_ARGUMENTEM,
        _ZNAKI_POWLOKI,
        _to_git,
        waliduj_definicje,
    )
    from runner.maskowanie import maskuj_komende  # noqa: E402
except Exception as exc:
    _BLAD_STARTU = exc

ZMIENNA_PLIKU = "COCKPIT_HOOK_FILE"
_PREFIKS = "straż roli: "
_MAKS_OPISU = 200

# Narzedzie zapisu -> klucz sciezki w `tool_input`.
_NARZEDZIA_ZAPISU: Mapping[str, str] = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}

# Biala lista Basha dla roli bez prawa zapisu: pierwszy token polecenia.
_ODCZYT = frozenset({
    "ls",
    "cat",
    "head",
    "tail",
    "grep",
    "rg",
    "wc",
    "pwd",
})
# ... albo `git <podpolecenie>` z tej listy (dokladny token).
_GIT_ODCZYT = frozenset({
    "status",
    "diff",
    "log",
    "show",
})
# Metaznaki powloki: przekierowania, laczenie polecen, podstawienia, nowa linia.
_METAZNAKI = (
    ">",
    "<",
    "|",
    ";",
    "&",
    "`",
    "$(",
    "\n",
)
# HAR-219: podpolecenia gita tworzace commity (`commit` i `push` lapie tez `waliduj_definicje`).
_GIT_TWORZY_COMMIT = frozenset({
    "commit",
    "push",
    "merge",
    "cherry-pick",
    "revert",
    "rebase",
    "am",
    "stash",
})
# Opcje `git diff|log|show` zapisujace plik albo uruchamiajace zewnetrzne polecenie; dopasowanie
# po prefiksie tokenu (git przyjmuje jednoznaczne skroty: `--ou` to `--output`).
_GIT_OPCJE_ZAPISU = frozenset({
    "--ou",
    "--ext-diff",
    "--textconv",
})
# Opcje `rg` uruchamiajace zewnetrzne polecenie (dopasowanie tokenu przed `=`).
_RG_OPCJE_WYKONANIA = frozenset({
    "--pre",
    "--pre-glob",
    "-z",
    "--search-zip",
})
_USUN_CUDZYSLOWY = str.maketrans("", "", "\"'\\")
# `$'git'` i `$"git"` (cytowanie ANSI-C, tlumaczenie): `$` tuz przed cudzyslowem znika razem z nim
_ANSI_C = re.compile(r"\$(?=['\"])")
# opcje gita, ktorych wartosc `klucz=...` stoi w nastepnym tokenie
_OPCJE_KONFIGURACJI = frozenset({
    "-c",
    "--config-env",
})
_ZNAKI_SKLADNI = frozenset("();<>|&")  # na nich shlex (punctuation_chars) tnie tokeny
_DEV_NULL = "/dev/null"
# Cel przekierowania zalezny od powloki (zmienna, podstawienie, tylda, wzorzec): nie do oceny.
_CEL_NIEOCENIANY = re.compile(r"[$`~*?]")
_DESKRYPTOR = re.compile(r"\d+-?|-")  # cel `>&2`, `>&-`: kopia deskryptora, nie plik


class _BladStrazy(Exception):
    """Blad wejscia hooka (argumenty, stdin), ktory konczy sie odmowa."""


def _warianty_komendy(komenda: str) -> tuple[str, str]:
    """Komenda i ta sama bez cudzyslowow i ucieczek: `"g"it commit`, `gi\\t commit` i `$'git' commit`
    skladaja sie w `git commit` dopiero w powloce."""
    return komenda, _ANSI_C.sub("", komenda).translate(_USUN_CUDZYSLOWY)


def _zaczyna_alias(wartosc: str) -> bool:
    return wartosc.lower().startswith("alias.")


def _alias_przez_opcje(tokeny: Sequence[str], j: int) -> str | None:
    """Nazwa opcji gita (`-c`, `--config-env`), ktora ustawia alias, albo None.

    Wartosc w osobnym tokenie (`-c alias.x=...`) albo sklejona (`-calias.x=...`,
    `--config-env=alias.x=...`); `-C <katalog>` to inna opcja.
    """
    opcja = tokeny[j]
    if opcja in _OPCJE_KONFIGURACJI:
        return opcja if j + 1 < len(tokeny) and _zaczyna_alias(tokeny[j + 1]) else None
    if opcja.startswith("-c") and _zaczyna_alias(opcja[2:]):
        return "-c"
    nazwa, _, wartosc = opcja.partition("=")
    return nazwa if nazwa == "--config-env" and _zaczyna_alias(wartosc) else None


def _powod_git(tokeny: Sequence[str], i: int) -> str | None:
    """Powod odmowy dla `git` na pozycji `i` (opcje globalne, potem podpolecenie) albo None."""
    j = i + 1
    while j < len(tokeny) and tokeny[j].startswith("-"):
        przez = _alias_przez_opcje(tokeny, j)
        if przez is not None:
            return f"{_PREFIKS}no_git_write — alias gita przez {przez}"
        # `--config-env k=v` bierze argument w osobnym tokenie, a `_OPCJE_Z_ARGUMENTEM` go nie zna
        j += 2 if tokeny[j].lower() in _OPCJE_Z_ARGUMENTEM or tokeny[j] in _OPCJE_KONFIGURACJI else 1
    if j < len(tokeny):
        podpolecenie = tokeny[j].lower()
        if podpolecenie in _GIT_TWORZY_COMMIT:
            return f"{_PREFIKS}no_git_write — git {podpolecenie} tworzy commit"
        if podpolecenie == "checkout":
            return _powod_niezmiennika_git("git checkout")
        if podpolecenie == "reset" and "--hard" in [t.lower() for t in tokeny[j + 1:]]:
            return _powod_niezmiennika_git("git reset --hard")
        if podpolecenie == "config" and any(_zaczyna_alias(t) for t in tokeny[j + 1:]):
            return f"{_PREFIKS}no_git_write — alias gita przez git config"
    return None


def _zakazany_git(komenda: str) -> str | None:
    """Powod odmowy dla gita zmieniajacego historie albo drzewo, aliasu albo None."""
    for wariant in _warianty_komendy(komenda):
        tokeny = wariant.translate(_ZNAKI_POWLOKI).split()
        for i, token in enumerate(tokeny):
            if _to_git(token.lower()):
                powod = _powod_git(tokeny, i)
                if powod is not None:
                    return powod
    return None


def _powod_niezmiennika_git(git: str) -> str:
    return (
        f"{_PREFIKS}niezmiennik no_git_write: silnik nie zmienia historii "
        f"ani drzewa gita ({git})"
    )


def _tokeny_odczytu(komenda: str) -> list[str] | None:
    """Tokeny komendy z bialej listy odczytu (bez metaznakow) albo None."""
    if any(znak in komenda for znak in _METAZNAKI):
        return None
    try:
        tokeny = shlex.split(komenda)
    except ValueError:  # niezamkniety cudzyslow: nie umiemy ocenic, wiec odmowa
        return None
    if not tokeny:
        return None
    if tokeny[0] == "git":
        return tokeny if len(tokeny) > 1 and tokeny[1] in _GIT_ODCZYT else None
    return tokeny if tokeny[0] in _ODCZYT else None


def _opcja_zapisujaca(tokeny: Sequence[str]) -> str | None:
    """Pierwszy token z opcja zapisujaca albo uruchamiajaca polecenie (tokeny z bialej listy)."""
    if tokeny[0] == "git":
        for token in tokeny[2:]:
            if any(token.startswith(opcja) for opcja in _GIT_OPCJE_ZAPISU):
                return token
    elif tokeny[0] == "rg":
        for token in tokeny[1:]:
            if token.partition("=")[0] in _RG_OPCJE_WYKONANIA:
                return token
    return None


def _tokeny_powloki(komenda: str) -> list[str]:
    """Tokeny z cudzyslowami zdjetymi; znaki skladni (`>`, `&>`, `;`) tna slowa. `ValueError` gdy
    cudzyslow albo ucieczka sa niedomkniete."""
    lekser = shlex.shlex(komenda, posix=True, punctuation_chars=True)
    lekser.whitespace_split = True
    lekser.commenters = ""
    return list(lekser)


def _sama_skladnia(token: str) -> bool:
    """Token z samych znakow skladni powloki; pusty napis tez (brak celu to cel nie do oceny)."""
    return all(znak in _ZNAKI_SKLADNI for znak in token)


def _cele_przekierowan(tokeny: Sequence[str]) -> list[str]:
    """Cele przekierowan wyjscia (`>`, `>>`, `>|`, `&>`, `N>`); kopie deskryptorow pomijamy.

    Brak celu (przekierowanie na koncu komendy) to pusty napis, czyli cel nie do oceny.
    """
    cele: list[str] = []
    i = 0
    while i < len(tokeny):
        token = tokeny[i]
        i += 1
        if ">" not in token or not _sama_skladnia(token):
            continue
        cel = tokeny[i] if i < len(tokeny) else ""
        i += 1
        if token.endswith(">&") and _DESKRYPTOR.fullmatch(cel):
            continue
        cele.append(cel)
    return cele


def _przekierowanie_poza_worktree(komenda: str, baza: Path, worktree: Path) -> str | None:
    """Powod odmowy dla przekierowania poza worktree albo do celu, ktorego nie da sie ocenic."""
    if ">" not in komenda:
        return None
    nieoceniany = f"{_PREFIKS}przekierowanie do celu, którego nie da się ocenić: "
    try:
        tokeny = _tokeny_powloki(komenda)
    except ValueError:
        return nieoceniany + "(komenda nie do rozbioru)"
    korzen = Path(os.path.realpath(worktree))
    for cel in _cele_przekierowan(tokeny):
        if cel == _DEV_NULL:
            continue
        if _sama_skladnia(cel) or _CEL_NIEOCENIANY.search(cel):
            return nieoceniany + cel
        try:
            sciezka = Path(os.path.realpath(baza / cel))
        except (OSError, ValueError):  # np. bajt zerowy w celu: nie umiemy ocenic, wiec odmowa
            return nieoceniany + cel
        if not sciezka.is_relative_to(korzen):
            return f"{_PREFIKS}przekierowanie poza worktree: {cel}"
    return None


def decyzja(zdarzenie: Mapping[str, Any], rola: Rola, worktree: Path) -> str | None:
    """Powod odmowy albo None. Czysta: bez I/O poza `realpath`; nieznane pola to odmowa."""
    narzedzie = zdarzenie.get("tool_name")
    wejscie = zdarzenie.get("tool_input")

    if narzedzie == "Bash":
        komenda = wejscie.get("command") if isinstance(wejscie, Mapping) else None
        if not isinstance(komenda, str):
            return f"{_PREFIKS}polecenie Bash bez komendy w postaci napisu"
        for wariant in _warianty_komendy(komenda):
            naruszenia = waliduj_definicje({"command": wariant})
            if naruszenia:
                return _PREFIKS + str(naruszenia[0])
        git = _zakazany_git(komenda)
        if git is not None:
            return git
        if profil_roli(rola).zapis:
            cwd = zdarzenie.get("cwd")
            baza = Path(cwd) if isinstance(cwd, str) else worktree
            return _przekierowanie_poza_worktree(komenda, baza, worktree)
        tokeny = _tokeny_odczytu(komenda)
        if tokeny is None:
            return (
                f"{_PREFIKS}niezmiennik reviewer_readonly: rola {rola.value} "
                "uruchamia tylko polecenia odczytu bez metaznaków powłoki"
            )
        opcja = _opcja_zapisujaca(tokeny)
        if opcja is not None:
            return f"{_PREFIKS}opcja zapisująca albo uruchamiająca polecenie: {opcja}"
        return None

    klucz = _NARZEDZIA_ZAPISU.get(str(narzedzie))
    if klucz is None:
        return None
    if not profil_roli(rola).zapis:
        return (
            f"{_PREFIKS}niezmiennik reviewer_readonly: rola {rola.value} "
            f"nie ma prawa zapisu ({narzedzie})"
        )
    sciezka = wejscie.get(klucz) if isinstance(wejscie, Mapping) else None
    if not isinstance(sciezka, str) or "\x00" in sciezka:
        return f"{_PREFIKS}{narzedzie} bez poprawnej ścieżki {klucz}"
    cwd = zdarzenie.get("cwd")
    baza = Path(cwd) if isinstance(cwd, str) else worktree
    cel = Path(os.path.realpath(baza / sciezka))
    if not cel.is_relative_to(Path(os.path.realpath(worktree))):
        return f"{_PREFIKS}zapis poza worktree: {sciezka}"
    return None


def _argumenty(argv: Sequence[str]) -> tuple[Rola, Path]:
    if len(argv) != 4 or {argv[0], argv[2]} != {"--rola", "--worktree"}:
        raise _BladStrazy("wymagane argumenty: --rola <rola> --worktree <ścieżka>")
    wartosci = {argv[0]: argv[1], argv[2]: argv[3]}
    try:
        rola = Rola(wartosci["--rola"])
    except ValueError:
        raise _BladStrazy(f"nieznana rola {wartosci['--rola']!r}") from None
    return rola, Path(wartosci["--worktree"])


def _wejscie() -> dict[str, Any]:
    try:
        zdarzenie = json.loads(sys.stdin.buffer.read().decode())
    except (OSError, ValueError, RecursionError):  # ValueError: UnicodeDecodeError i JSONDecodeError
        raise _BladStrazy("nieczytelne wejście hooka (stdin nie jest JSON w UTF-8)") from None
    if not isinstance(zdarzenie, dict):
        raise _BladStrazy("wejście hooka nie jest obiektem JSON")
    return zdarzenie


def _napis(wartosc: object) -> str:
    return wartosc if isinstance(wartosc, str) else ""


def _opis_wywolania(zdarzenie: Mapping[str, Any]) -> str:
    """Komenda Basha albo sciezka zapisu, ucieta; nigdy tresc zapisywanego pliku."""
    narzedzie = zdarzenie.get("tool_name")
    wejscie = zdarzenie.get("tool_input")
    if not isinstance(wejscie, Mapping):
        return ""
    klucz = "command" if narzedzie == "Bash" else _NARZEDZIA_ZAPISU.get(str(narzedzie))
    if klucz is None:
        return ""
    opis = _napis(wejscie.get(klucz))
    if narzedzie == "Bash":
        # HAR-219: maskowanie PRZED ucieciem; sekret przecinany granica 200 znakow wyszedlby w czesci
        opis = maskuj_komende(opis)
    return opis[:_MAKS_OPISU]


def _zapisz_odmowe(zdarzenie: Mapping[str, Any], rola: Rola, powod: str) -> None:
    """Jedna linia NDJSON `ToolDenied` do pliku hookow kroku. Blad zapisu nie zmienia decyzji."""
    sciezka = os.environ.get(ZMIENNA_PLIKU)
    if not sciezka:
        return
    linia = json.dumps({
        "hook_event_name": "ToolDenied",
        "tool_name": _napis(zdarzenie.get("tool_name")),
        "command": _opis_wywolania(zdarzenie),
        "role": rola.value,
        # HAR-219: powod z walidacji cytuje komende, wiec sekret maskujemy tez tu
        "reason": maskuj_komende(powod),
        "session_id": _napis(zdarzenie.get("session_id")),
    })
    try:
        # bez O_CREAT: plik tworzy runner; jeden os.write na O_APPEND nie przeplata linii
        fd = os.open(sciezka, os.O_WRONLY | os.O_APPEND)
        try:
            os.write(fd, linia.encode() + b"\n")
        finally:
            os.close(fd)
    except OSError as exc:
        print(f"{_PREFIKS}nie zapisano zdarzenia odmowy: {type(exc).__name__}", file=sys.stderr)


def _wypisz_odmowe(powod: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": powod,
    }}))
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    if _BLAD_STARTU is not None:
        # HAR-219: straz bez importow nie umie ocenic polecenia, wiec odmawia; bez stdin i pliku hookow
        _wypisz_odmowe(f"{_PREFIKS}błąd startu straży ({type(_BLAD_STARTU).__name__})")
        return 0
    argumenty = sys.argv[1:] if argv is None else argv
    wejscie: tuple[Rola, dict[str, Any]] | None = None
    try:
        rola, worktree = _argumenty(argumenty)
        zdarzenie = _wejscie()
        wejscie = (rola, zdarzenie)
        powod = decyzja(zdarzenie, rola, worktree)
    except _BladStrazy as blad:
        powod = f"{_PREFIKS}{blad}"
    except Exception as blad:  # hook jest fail-closed: kazdy blad to odmowa
        powod = f"{_PREFIKS}nieoczekiwany błąd ({type(blad).__name__})"
    if powod is None:
        return 0
    # HAR-219: powod trafia do transkryptu agenta; cytuje komende, wiec maskujemy go przed wypisaniem
    _wypisz_odmowe(maskuj_komende(powod))
    if wejscie is not None:
        _zapisz_odmowe(wejscie[1], wejscie[0], powod)
    return 0


if __name__ == "__main__":
    sys.exit(main())
