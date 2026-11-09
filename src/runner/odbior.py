"""Odbior runu (HAR-89): diff worktree runu wzgledem `base_commit`, wylacznie do odczytu.

Niezmienniki: `no_git_write` (zbieranie diffu - `collect`, `krok_diff` - to tylko `git diff-index`,
`git diff --no-index` i `git ls-files`, zero zapisu w indeksie; `GIT_OPTIONAL_LOCKS=0` chroni
pozostale wywolania; zapisuja wylacznie `integruj` i `commituj`, opisane nizej). Diff plikow
sledzonych idzie przez plumbing `diff-index -p`, nie przez porcelane `git diff`.
WHY: porcelana `git diff` odswieza indeks (stat-cache plikow o zmienionym mtime) mimo
`GIT_OPTIONAL_LOCKS=0`; `diff-index` indeksu nie zmienia i nie pokazuje plikow tylko
stat-dirty. `run_dir_outside_repo` (pliki z `.cockpit/_run/` to artefakty
runu, nie zmiany do odbioru: wykluczenie jawne, niezalezne od `.gitignore`), `evidence_required`
(blad gita to blad, nie pusty diff). Komunikaty bledow sa stale: bez sciezek i stderr gita.

HAR-179: dwie osobne akcje czlowieka po odbiorze. `integruj` przenosi zmiany runu do drzewa
roboczego uzytkownika (`git apply`, bez indeksu i bez commita), a `commituj` zapisuje je jednym
commitem. To jedyne miejsce w `src/`, ktore zapisuje w gicie; push nie istnieje.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from cockpit.services.errors import BladDziedziny

SHA_PELNY = re.compile(r"[0-9a-f]{40}")

# Format diffu nie zalezy od konfiguracji uzytkownika (kolor, zewnetrzny program, textconv, prefiksy).
_FORMAT_DIFFU = [
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--src-prefix=a/",
    "--dst-prefix=b/",
]
_WYKLUCZ_RUN = ":(exclude).cockpit/_run"
# WHY stala modulu: polskie nazwy plikow doslownie w naglowku diffu. W funkcji mutmut zmienia
# literal na wielkie litery - mutant rownowazny (git czyta klucz i bool bez rozroznienia), ktory
# oblewalby bramke; linie chroni reczny mutant w tests/mutanty_reczne/HAR-89.toml.
_BEZ_CYTOWANIA_NAZW = "core.quotepath=false"
# Komunikaty bledow sa stale: nie niosa stderr gita ani sciezek.
_BLAD_DIFFU = "git nie zebral diffu runu"
_BLAD_INTEGRACJI = "git nie wykonal integracji runu"
_BLAD_COMMITU = "git nie wykonal commita"
_BLAD_REPOZYTORIUM = "git nie wskazal repozytorium uzytkownika"
_BLAD_NAZWY = "zmiany runu zawieraja nazwe pliku, ktorej nie da sie przeniesc"
_NAGLOWEK_DIFFU = re.compile(rb"^diff --git ([^\n]*)$", re.MULTILINE)
# git czyta sciezki doslownie: `[ab].txt` nie moze zabrac `a.txt`
_SCIEZKI_DOSLOWNIE = "--literal-pathspecs"


class BladOdbioru(BladDziedziny):
    """Git nie zebral diffu runu (brak binarki, nie-repozytorium, zagniezdzone repozytorium,
    zly commit bazowy) albo nie wykonal integracji lub commita."""


class BrakZmianRunu(BladOdbioru):
    """Diff runu jest pusty: nie ma czego integrowac ani commitowac."""


class BladIntegracji(BladOdbioru):
    """`git apply --check` odmowil: zmiany runu koliduja z lokalnymi zmianami uzytkownika.

    Komunikat niesie wylacznie nazwy plikow (wzgledne od korzenia repozytorium), nigdy stderr gita."""

    def __init__(self, pliki: list[str]) -> None:
        self.pliki = sorted(pliki)
        super().__init__(f"zmiany runu koliduja z lokalnymi zmianami: {', '.join(self.pliki)}")


def _git(worktree: Path, argumenty: list[str], dozwolone: tuple[int, ...]) -> bytes:
    return _git_z_kodem(worktree, argumenty, dozwolone)[0]


def _git_z_kodem(
    worktree: Path, argumenty: list[str], dozwolone: tuple[int, ...],
    wejscie: bytes | None = None, komunikat: str = _BLAD_DIFFU,
) -> tuple[bytes, int]:
    """Jedyne miejsce uruchamiajace gita: (stdout, kod) albo BladOdbioru (bez stderr w komunikacie).

    `wejscie` trafia na stdin w dokladnie tych bajtach (diff dla `git apply`)."""
    try:
        wynik = subprocess.run(
            ["git", "-c", _BEZ_CYTOWANIA_NAZW, "-C", str(worktree), *argumenty],
            capture_output=True,
            input=wejscie,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except OSError:
        raise BladOdbioru("git nie jest dostepny") from None
    if wynik.returncode not in dozwolone:
        raise BladOdbioru(komunikat)
    return wynik.stdout, wynik.returncode


def _tekst(surowe: bytes) -> str:
    return surowe.decode(errors="replace")


def zbierz_diff(worktree: Path, base_commit: str) -> str:
    return _tekst(_zbierz_diff_bajty(worktree, base_commit))


def _zbierz_diff_bajty(worktree: Path, base_commit: str, binarny: bool = False) -> bytes:
    """Diff runu w bajtach wiernych plikom: tylko taki trafia do `git apply` (tekst z
    zamiana bajtow spoza UTF-8 na znak zastepczy zepsulaby plik).

    `binarny` dodaje `--binary`: pliki binarne dostaja latke, ktora `git apply` stosuje (bez
    niej git pisze tylko "Binary files differ"). Diff do odbioru (`zbierz_diff`) go nie ma."""
    format_diffu = [*_FORMAT_DIFFU, "--binary"] if binarny else _FORMAT_DIFFU
    if not SHA_PELNY.fullmatch(base_commit):
        raise BladOdbioru("base_commit nie jest SHA")
    diff = _git(
        worktree,
        ["diff-index", "-p", *format_diffu, base_commit, "--", ".", _WYKLUCZ_RUN],
        (0,),
    )
    nowe = _git(
        worktree,
        ["ls-files", "--others", "--exclude-standard", "-z", ".", _WYKLUCZ_RUN],
        (0,),
    )
    for surowa in nowe.split(b"\0"):
        sciezka = os.fsdecode(surowa)
        if not sciezka or sciezka.startswith(".cockpit/_run/"):
            continue
        if sciezka.endswith("/"):
            # wpis zakonczony `/` to zagniezdzone repozytorium: git nie poda jego tresci
            raise BladOdbioru("worktree zawiera zagniezdzone repozytorium")
        # kod 1 = rozni sie (nowy plik), 0 = brak wpisu dla pliku (np. pusty), >=2 = blad
        wynik, kod = _git_z_kodem(
            worktree,
            ["diff", "--no-index", *format_diffu, "--", "/dev/null", sciezka],
            (0, 1),
        )
        if kod == 1 and not wynik:
            # kod 1 ma znaczyc "rozni sie", wiec musi byc lata; pusty stdout to blad gita
            raise BladOdbioru("git nie zebral diffu runu")
        diff += wynik
    return diff


def _nazwa_z_naglowka(reszta: bytes) -> str:
    """Nazwa pliku z `a/NAZWA b/NAZWA`: obie nazwy sa rowne (diff bez wykrywania zmian nazw),
    wiec dlugosc nazwy wynika z dlugosci naglowka i nazwa moze zawierac spacje oraz ` b/`.
    Nazwa w cudzyslowie (znaki sterujace) albo niezgodna z formatem to blad, nie pominiecie."""
    dlugosc = len(reszta) // 2 - 2
    nazwa = reszta[2:2 + dlugosc]
    if dlugosc <= 0 or reszta != b"a/" + nazwa + b" b/" + nazwa:
        raise BladOdbioru(_BLAD_NAZWY)
    try:
        # mutant rownowazny: "UTF-8" i inne zapisy to alias tego samego kodeka; kodek inny
        # niz utf-8 chroni wiersz recznego mutanta HAR-179 (nazwa nie-UTF-8 to blad)
        return nazwa.decode("utf-8")  # pragma: no mutate
    except UnicodeDecodeError:
        raise BladOdbioru(_BLAD_NAZWY) from None


def _podziel_diff(diff: bytes) -> list[tuple[str, bytes]]:
    """(nazwa pliku, kawalek diffu tego pliku) w kolejnosci diffu; kawalki sklejone dadza diff."""
    naglowki = list(_NAGLOWEK_DIFFU.finditer(diff))
    kawalki: list[tuple[str, bytes]] = []
    for i, naglowek in enumerate(naglowki):
        koniec = naglowki[i + 1].start() if i + 1 < len(naglowki) else len(diff)
        kawalki.append((_nazwa_z_naglowka(naglowek.group(1)), diff[naglowek.start():koniec]))
    return kawalki


def pliki_runu(worktree: Path, base_commit: str) -> list[str]:
    """Posortowane pliki zmienione w runie (bez `.cockpit/_run/`): te same co w diffie do odbioru."""
    return sorted(nazwa for nazwa, _ in _podziel_diff(_zbierz_diff_bajty(worktree, base_commit)))


def repozytorium_docelowe(worktree: Path) -> Path:
    """Korzen repozytorium uzytkownika, z ktorego powstal worktree runu.

    Wynika z samego worktree (wspolny katalog gita); sciezki od kokpitu runner nie przyjmuje.
    Repozytorium bez drzewa roboczego (bare) nie ma dokad przenosic zmian."""
    wynik, _ = _git_z_kodem(
        worktree, ["rev-parse", "--path-format=absolute", "--git-common-dir"], (0,),
        komunikat=_BLAD_REPOZYTORIUM,
    )
    wspolny = Path(os.fsdecode(wynik.rstrip(b"\n")))
    if not wspolny.is_absolute() or wspolny.name != ".git":
        raise BladOdbioru(_BLAD_REPOZYTORIUM)
    return wspolny.parent


def _kolidujace(repo: Path, kawalki: list[tuple[str, bytes]]) -> BladOdbioru:
    """Blad do rzucenia po odmowie `apply --check`: pliki, ktorych kawalek diffu nie przechodzi
    sam. Stderr gita nie jest czytany (zalezy od jezyka i brzmienia komunikatow)."""
    kolidujace = [
        nazwa for nazwa, kawalek in kawalki
        if _git_z_kodem(repo, ["apply", "--check"], (0, 1), kawalek, _BLAD_INTEGRACJI)[1] == 1
    ]
    if not kolidujace:
        return BladOdbioru(_BLAD_INTEGRACJI)
    return BladIntegracji(kolidujace)


def integruj(worktree: Path, base_commit: str) -> list[str]:
    """Przenosi zmiany runu do drzewa roboczego uzytkownika: `git apply` bez `--index`, bez
    commita. Najpierw `--check`: konflikt to odmowa z lista plikow, drzewo zostaje nietkniete."""
    diff = _zbierz_diff_bajty(worktree, base_commit, True)
    kawalki = _podziel_diff(diff)
    if not kawalki:
        raise BrakZmianRunu("run nie ma zmian do przeniesienia")
    repo = repozytorium_docelowe(worktree)
    _, kod = _git_z_kodem(repo, ["apply", "--check"], (0, 1), diff, _BLAD_INTEGRACJI)
    if kod == 1:
        raise _kolidujace(repo, kawalki)
    _git_z_kodem(repo, ["apply"], (0,), diff, _BLAD_INTEGRACJI)
    return sorted(nazwa for nazwa, _ in kawalki)


def commituj(worktree: Path, pliki: list[str], opis: str) -> str:
    """Commit w repozytorium uzytkownika, wylacznie z plikami runu: `add` i `commit -- <pliki>`
    (zmiany, ktore uzytkownik juz dodal do indeksu, nie wchodza do commita). Tozsamosc z
    konfiguracji gita repozytorium, bez `--no-verify`: hooki uzytkownika dzialaja. Zwraca SHA."""
    if not pliki:
        # `commit --` bez sciezek zabralby cale to, co uzytkownik ma w indeksie
        raise BrakZmianRunu("brak plikow runu do zapisania")
    repo = repozytorium_docelowe(worktree)
    # usuniecie pliku nie ma czego dodawac (po nieudanym commicie bylo juz w indeksie, a
    # `add` na nieistniejacej sciezce konczy sie bledem); commit ponizej zapisze je wszystkie
    istniejace = [p for p in pliki if os.path.lexists(repo / p)]
    if istniejace:
        _git_z_kodem(
            repo, [_SCIEZKI_DOSLOWNIE, "add", "--", *istniejace], (0,), komunikat=_BLAD_COMMITU)
    _git_z_kodem(
        repo, [_SCIEZKI_DOSLOWNIE, "commit", "-m", opis, "--", *pliki], (0,),
        komunikat=_BLAD_COMMITU)
    wynik, _ = _git_z_kodem(repo, ["rev-parse", "HEAD"], (0,), komunikat=_BLAD_COMMITU)
    try:
        # mutant rownowazny: "ASCII" i inne zapisy to alias tego samego kodeka; wynik
        # `rev-parse HEAD` sprawdza dalej SHA_PELNY, wiec bajt spoza ASCII i tak jest bledem
        sha = wynik.decode("ascii").strip()  # pragma: no mutate
    except UnicodeDecodeError:
        raise BladOdbioru(_BLAD_COMMITU) from None
    if not SHA_PELNY.fullmatch(sha):
        raise BladOdbioru(_BLAD_COMMITU)
    return sha
