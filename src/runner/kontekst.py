"""Manifest kroku (HAR-169): warstwy instrukcji z sha256 w kolejnosci pierwszenstwa.

Manifest niesie sciezki i hashe, nigdy tresc plikow. Hash i rozmiar liczymy z jednego odczytu
bajtow. Plik, ktorego nie ma, jest pomijany; plik, ktorego nie da sie przeczytac, to
`BladManifestu` (brak dowodu nie jest pustym wpisem).
"""
from __future__ import annotations

import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath

from cockpit.services.errors import BladDziedziny
from cockpit.services.lekcje import wpisy_lekcji
from cockpit.services.warstwy_instrukcji import WarstwaInstrukcji
from runner.agent import SKILLE_RUNU, _bez_znakow_sterujacych

PLIK_LEKCJI = ".ai/lessons.md"
PLIK_PROJEKTU = ".cockpit/project.yaml"

# Limity zdarzenia `context_manifest`; MUSZA byc zgodne z `WpisManifestuIn`/`EventIn` w
# cockpit/api/app.py (kokpit odrzuca 422, a runner ponawialby w nieskonczonosc: granica-bledow).
MAX_WPISOW = 1000
MAX_WARSTWA = 64
MAX_SCIEZKA = 255
MAX_ID = 64
MAX_ROZMIAR = 2_147_483_647
_SHA256 = re.compile(r"[0-9a-f]{64}")


class BladManifestu(BladDziedziny):
    """Nie da sie zlozyc manifestu (plik warstwy nieczytelny, wpis ponad limit); tekst to powod."""


@dataclass(frozen=True)
class WpisManifestu:
    warstwa: str
    sciezka: str | None = None
    sha256: str | None = None
    rozmiar: int | None = None
    id: str | None = None


@dataclass(frozen=True)
class Manifest:
    wpisy: tuple[WpisManifestu, ...] = ()

    def jako_listy(self) -> list[dict]:
        return [
            {"warstwa": str(w.warstwa), "sciezka": w.sciezka, "sha256": w.sha256,
             "rozmiar": w.rozmiar, "id": w.id}
            for w in self.wpisy
        ]


def _odczytaj(wt: Path, sciezka: str) -> bytes | None:
    """Bajty pliku wzgledem worktree; brak pliku to None, inny blad dysku to `BladManifestu`."""
    try:
        return (wt / sciezka).read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        raise BladManifestu(f"nie można odczytać pliku warstwy {sciezka}: {e}") from e


def _jest_plikiem(wt: Path, sciezka: str) -> bool:
    """Czy kandydat jest zwyklym plikiem; brak to False, nie umiem ocenic (np. PermissionError) to `BladManifestu`."""
    try:
        return stat.S_ISREG((wt / sciezka).stat().st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as e:
        raise BladManifestu(f"nie można odczytać pliku warstwy {sciezka}: {e}") from e


def _wpis_pliku(wt: Path, warstwa: WarstwaInstrukcji, sciezka: str) -> WpisManifestu | None:
    bajty = _odczytaj(wt, sciezka)
    if bajty is None:
        return None
    return WpisManifestu(
        warstwa=warstwa.value, sciezka=sciezka, sha256=sha256(bajty).hexdigest(),
        rozmiar=len(bajty))


def _claude_md_katalogu(wt: Path, sciezki_planu: Sequence[str]) -> str | None:
    """Najblizszy `CLAUDE.md` w gore od wspolnego katalogu sciezek planu; korzen nie liczy sie."""
    for s in sciezki_planu:
        # sciezka poza worktree czytalaby CLAUDE.md spoza repozytorium runu: brak dowodu, nie pominiecie
        if PurePosixPath(s).is_absolute() or ".." in PurePosixPath(s).parts:
            raise BladManifestu(f"ścieżka planu {s} wskazuje poza worktree")
    katalogi = [PurePosixPath(s).parent.parts for s in sciezki_planu]
    if not katalogi:
        return None
    wspolny = list(katalogi[0])
    for czesci in katalogi[1:]:
        while wspolny and tuple(wspolny) != czesci[:len(wspolny)]:
            wspolny.pop()
    while wspolny:
        kandydat = PurePosixPath(*wspolny, "CLAUDE.md").as_posix()
        if _jest_plikiem(wt, kandydat):
            return kandydat
        wspolny.pop()
    return None


def _glob_na_regex(glob: str) -> re.Pattern[str]:
    """`**/` = dowolna liczba katalogow, `*` i `?` nie przekraczaja `/` (PurePath.full_match od 3.13)."""
    wzorzec = ""
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            wzorzec += "(?:.*/)?"
            i += 3
        elif glob.startswith("**", i):
            wzorzec += ".*"
            i += 2
        elif glob[i] == "*":
            wzorzec += "[^/]*"
            i += 1
        elif glob[i] == "?":
            wzorzec += "[^/]"
            i += 1
        else:
            wzorzec += re.escape(glob[i])
            i += 1
    return re.compile(wzorzec)


def _lekcje(wt: Path, krok: str, sciezki_planu: Sequence[str]) -> list[WpisManifestu]:
    bajty = _odczytaj(wt, PLIK_LEKCJI)
    if bajty is None:
        return []
    try:
        tekst = bajty.decode()
    except UnicodeDecodeError as e:
        raise BladManifestu(f"plik lekcji {PLIK_LEKCJI} nie jest w UTF-8") from e
    wpisy = []
    for lekcja in wpisy_lekcji(tekst):
        pasuje_sciezka = not lekcja.sciezki or any(
            _glob_na_regex(g).fullmatch(s) for g in lekcja.sciezki for s in sciezki_planu)
        if lekcja.krok != krok or not pasuje_sciezka:
            continue
        blok = lekcja.tekst.encode()
        wpisy.append(WpisManifestu(
            warstwa=WarstwaInstrukcji.lekcje.value, sciezka=PLIK_LEKCJI,
            sha256=sha256(blok).hexdigest(), rozmiar=len(blok), id=lekcja.id))
    return wpisy


def sprawdz_limity(manifest: Manifest) -> None:
    """Te same limity co `EventIn.manifest` w kokpicie; naruszenie to `BladManifestu`, nie ponawianie."""
    if len(manifest.wpisy) > MAX_WPISOW:
        raise BladManifestu(f"manifest ma {len(manifest.wpisy)} wpisów, limit {MAX_WPISOW}")
    for w in manifest.wpisy:
        if (
            len(w.warstwa) > MAX_WARSTWA
            or (w.sciezka is not None and len(w.sciezka) > MAX_SCIEZKA)
            or (w.id is not None and len(w.id) > MAX_ID)
            or (w.sha256 is not None and not _SHA256.fullmatch(w.sha256))
            or (w.rozmiar is not None and not 0 <= w.rozmiar <= MAX_ROZMIAR)
        ):
            raise BladManifestu(f"wpis manifestu ponad limit pól: {w.warstwa} {w.sciezka}")


def zloz_manifest(
    worktree: Path, krok: str, sciezki_planu: Sequence[str], *, skill_id: str,
) -> Manifest:
    """Wpisy w kolejnosci `OD_NAJWYZSZEJ`; niezmienniki zawsze, jako wpis bez sciezki i hasha."""
    # znak sterujacy (NUL) w sciezce daje ValueError ze stat()/read_bytes(): ta sama regula co zbuduj_prompt
    try:
        _bez_znakow_sterujacych("skill_id", skill_id)
        for i, s in enumerate(sciezki_planu):
            _bez_znakow_sterujacych(f"sciezki_planu[{i}]", s)
    except ValueError as e:
        raise BladManifestu(str(e)) from e
    wpisy: list[WpisManifestu | None] = [
        WpisManifestu(warstwa=WarstwaInstrukcji.niezmienniki_silnika.value),
        _wpis_pliku(worktree, WarstwaInstrukcji.project_yaml, PLIK_PROJEKTU),
    ]
    katalogu = _claude_md_katalogu(worktree, sciezki_planu)
    if katalogu is not None:
        wpisy.append(_wpis_pliku(worktree, WarstwaInstrukcji.claude_md_katalogu, katalogu))
    wpisy += [
        _wpis_pliku(worktree, WarstwaInstrukcji.claude_md_korzenia, "CLAUDE.md"),
        _wpis_pliku(
            worktree, WarstwaInstrukcji.skill_md, f"{SKILLE_RUNU}/{skill_id}/SKILL.md"),
        *_lekcje(worktree, krok, sciezki_planu),
    ]
    manifest = Manifest(wpisy=tuple(w for w in wpisy if w is not None))
    sprawdz_limity(manifest)
    return manifest
