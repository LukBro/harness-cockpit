"""Krok systemowy `system: diff`: dowod dla recenzentow, bez LLM (HAR-176).

Zbiera diff worktree wzgledem `base_commit` (`runner.odbior.zbierz_diff`, tylko odczyt) i
zapisuje w katalogu kroku w katalogu runu, poza repozytorium: `zmiana.diff` oraz
`zmienione-linie.json` (zakresy FAKTYCZNIE dodanych linii kazdego pliku, numeracja nowej
wersji). Skille recenzji dostaja te pliki przez `consumes`, a `kotwice` sprawdzaja kotwice
`plik:linia` notatki wzgledem drugiego z nich. Pusty diff to brak dowodu (`evidence_required`):
krok nieudany, a recenzenci pominieci z `brak_wejscia` (graf).
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from cockpit.services.kroki_systemowe import PLIK_DIFFU, PLIK_LINII
from cockpit.services.walidacja import Naruszenie, Niezmiennik, sprawdz_katalog_runu
from runner.kotwice import KODOWANIE
from runner.krok import WynikKroku, _zapisz_atomowo
from runner.odbior import BladOdbioru, zbierz_diff

__all__ = ["PLIK_DIFFU", "PLIK_LINII", "wykonaj_diff", "zmienione_linie"]

BRAK_ZMIAN = "brak zmian do recenzji"
# Opcje zapisu zmienione-linie.json: stala modulu (polskie znaki doslownie, wciecie 2).
OPCJE_JSON = {"ensure_ascii": False, "indent": 2}
NAGLOWEK_PLIKU = re.compile(r"diff --git a/(?P<a>.+) b/(?P<b>.+)")
NAGLOWEK_HUNKA = re.compile(r"@@ -\d+(?:,(?P<stare>\d+))? \+(?P<od>\d+)(?:,(?P<nowe>\d+))? @@")


def _dodaj_linie(zakresy: list[list[int]], numer: int) -> None:
    """Dodaje linie do zakresow; kolejna linia za ostatnim zakresem go rozszerza."""
    if zakresy and zakresy[-1][1] == numer - 1:
        zakresy[-1][1] = numer
    else:
        zakresy.append([numer, numer])


def zmienione_linie(diff: str) -> list[dict]:
    """`[{"plik": ..., "linie": [[od, do], ...]}]` posortowane po pliku.

    Zakresy liczone z linii `+` wzgledem naglowkow hunkow (`@@ -a,b +c,d @@`), z licznikami
    hunka: tresc wygladajaca jak naglowek pliku (`--- x`) jest wtedy zwykla linia. Nazwa pliku
    pochodzi z `diff --git a/<plik> b/<plik>`; plik usuniety i binarny nie maja hunka z dodanymi
    liniami, wiec zostaja z `linie: []`."""
    pliki: list[dict] = []
    biezacy: dict | None = None
    nowe = numer = 0      # pozostale nowe linie hunka i numer nastepnej nowej linii
    # split, nie splitlines: tresc linii moze zawierac \x0c czy \u2028, ktore splitlines tnie
    for linia in diff.split("\n"):
        if nowe > 0:
            # hunk trwa, dopoki zostaly nowe linie (dodane i kontekst); linie `-` i marker
            # braku konca linii nie przesuwaja licznika
            znak = linia[:1]
            if znak == "+":
                _dodaj_linie(biezacy["linie"], numer)
            if znak not in ("-", "\\"):
                numer += 1
                nowe -= 1
            continue
        naglowek = NAGLOWEK_PLIKU.fullmatch(linia)
        if naglowek is not None:
            biezacy = {"plik": naglowek["b"], "linie": []}
            pliki.append(biezacy)
        elif biezacy is not None and (hunk := NAGLOWEK_HUNKA.match(linia)) is not None:
            nowe = int(hunk["nowe"]) if hunk["nowe"] is not None else 1
            numer = int(hunk["od"])
    return sorted(pliki, key=lambda wpis: wpis["plik"])


def _zapisz_artefakty(katalog_kroku: Path, diff: str, linie: str) -> None:
    _zapisz_atomowo(katalog_kroku / PLIK_LINII, linie.encode(KODOWANIE))
    _zapisz_atomowo(katalog_kroku / PLIK_DIFFU, diff.encode(KODOWANIE))


async def wykonaj_diff(worktree: Path, base_commit: str, katalog_kroku: Path) -> WynikKroku:
    """Zapisuje `zmiana.diff` i `zmienione-linie.json` w `katalog_kroku` (atomowo, w watku).

    Blad gita i pusty diff to porazka bez zadnego pliku; blad zapisu tez."""
    try:
        diff = await asyncio.to_thread(zbierz_diff, worktree, base_commit)
    except BladOdbioru as blad:
        return WynikKroku(status="nieudany", powod=str(blad), kod_wyjscia=None)
    if not diff.strip():
        return WynikKroku(
            status="nieudany",
            powod=str(Naruszenie(Niezmiennik.evidence_required, BRAK_ZMIAN)),
            kod_wyjscia=None)
    naruszenie = sprawdz_katalog_runu(katalog_kroku, worktree)
    if naruszenie is not None:
        return WynikKroku(status="nieudany", powod=str(naruszenie), kod_wyjscia=None)
    linie = json.dumps(zmienione_linie(diff), **OPCJE_JSON)
    try:
        await asyncio.to_thread(_zapisz_artefakty, katalog_kroku, diff, linie)
    except OSError as blad:
        return WynikKroku(
            status="nieudany", powod=f"nie można zapisać artefaktów kroku diff: {blad}",
            kod_wyjscia=None)
    return WynikKroku(status="zakonczony", powod=None, kod_wyjscia=0)
