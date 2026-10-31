"""Walidator niezmiennikow na czystych funkcjach (HAR-6).

Bez bazy i bez I/O poza migawka katalogu. Funkcje zwracaja Naruszenie albo liste
Naruszen; rzucanie wyjatku (NaruszonyNiezmiennik) to decyzja wywolujacego.
Model workflow powstaje w BRO-102 - stale kluczy ponizej moga sie zmienic.

Egzekwowane na sciezce wykonania sa wylacznie sprawdzenia wskazane w EGZEKWOWANE.
Pozostale istnieja, ale nie sa podlaczone do punktow wejscia (cockpit.__main__,
runner.__main__): waliduj_workflow (walidator workflow poza zakresem M1.0, BRO-99)
oraz runner/krok.py i runner/worktree.py, ktore wolaja sprawdzenia, ale nie sa
osiagalne z runner.__main__.
"""
from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .invariants import Niezmiennik, Rola, Tier, profil_roli

KLUCZ_ROLI = "role"
KLUCZ_TIERU = "tier"
KLUCZ_MODELU = "model"
KLUCZE_ZAPISU = ("write", "zapis", "can_write")
KLUCZE_KOMENDY = ("command", "run")  # klucz komendy nie jest jeszcze ustalony
KLUCZE_DEFINICJI = frozenset({KLUCZ_ROLI, KLUCZ_TIERU, KLUCZ_MODELU, *KLUCZE_ZAPISU, *KLUCZE_KOMENDY})

_PRAWDA = frozenset({"true", "yes", "tak", "on", "1"})
# opcje globalne gita, ktore biora argument w osobnym tokenie
_OPCJE_Z_ARGUMENTEM = frozenset({"-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"})
_ZAPIS_GITA = frozenset({"commit", "push"})
_ZNAKI_POWLOKI = str.maketrans({c: " " for c in ";&|()`'\"\n\r"})


@dataclass(frozen=True)
class Naruszenie:
    niezmiennik: Niezmiennik
    szczegol: str

    def __str__(self) -> str:
        nazwa = f"niezmiennik {self.niezmiennik.value}"
        return f"{nazwa}: {self.szczegol}" if self.szczegol else nazwa


def _daje_zapis(wartosc: Any) -> bool:
    if wartosc is True:
        return True
    if isinstance(wartosc, str):
        return wartosc.strip().lower() in _PRAWDA
    if isinstance(wartosc, (int, float)) and not isinstance(wartosc, bool):
        return wartosc != 0
    return False


def _tekst_komendy(wartosc: Any) -> str | None:
    if isinstance(wartosc, str):
        return wartosc
    if isinstance(wartosc, (list, tuple)) and all(isinstance(e, str) for e in wartosc):
        return " ".join(wartosc)
    return None


def _to_git(token: str) -> bool:
    return token.rsplit("/", 1)[-1] == "git"


def _zapis_do_gita(komenda: str) -> bool:
    # WHY: komenda to wejscie uzytkownika; `bash -c "git push"` czy `cd r&&git push`
    # kleja git z cudzyslowem lub separatorem, wiec znaki powloki traktujemy jak spacje.
    tokeny = komenda.lower().translate(_ZNAKI_POWLOKI).split()
    for i, token in enumerate(tokeny):
        if not _to_git(token):
            continue
        j = i + 1
        while j < len(tokeny) and tokeny[j].startswith("-"):
            j += 2 if tokeny[j] in _OPCJE_Z_ARGUMENTEM else 1
        if j < len(tokeny) and tokeny[j] in _ZAPIS_GITA:
            return True
    return False


def waliduj_definicje(definicja: Mapping[str, Any]) -> list[Naruszenie]:
    """Sprawdza definicje kroku: role_binding, reviewer_readonly, no_git_write. Nie rzuca."""
    wynik: list[Naruszenie] = []

    rola: Rola | None = None
    if KLUCZ_ROLI in definicja:
        surowa = definicja[KLUCZ_ROLI]
        try:
            if not isinstance(surowa, str):
                raise ValueError
            rola = Rola(surowa)
        except ValueError:
            wynik.append(Naruszenie(
                Niezmiennik.role_binding,
                f"nieznana rola {surowa!r} - dozwolone: {', '.join(r.value for r in Rola)}",
            ))

    obecne = [k for k in KLUCZE_ZAPISU if k in definicja]
    for klucz in obecne:
        wynik.append(Naruszenie(
            Niezmiennik.role_binding,
            f"klucz '{klucz}' jest niedozwolony - prawo zapisu wynika z roli, nie z osobnego klucza",
        ))
    if (
        rola is not None
        and rola == Rola.reviewer
        and not profil_roli(rola).zapis
        and any(_daje_zapis(definicja[k]) for k in obecne)
    ):
        wynik.append(Naruszenie(
            Niezmiennik.reviewer_readonly,
            "recenzent nie dostaje prawa zapisu w zadnym backendzie",
        ))

    wskazowka = "model ustawia sie w profilu backendu u runnera (BRO-98), nie w workflow"
    if KLUCZ_MODELU in definicja:
        wynik.append(Naruszenie(
            Niezmiennik.role_binding, f"klucz '{KLUCZ_MODELU}' jest niedozwolony - {wskazowka}"
        ))
    if KLUCZ_TIERU in definicja:
        surowy = definicja[KLUCZ_TIERU]
        try:
            if not isinstance(surowy, str):
                raise ValueError
            tier = Tier(surowy)
        except ValueError:
            wynik.append(Naruszenie(
                Niezmiennik.role_binding,
                f"tier {surowy!r} nie jest tierem ({', '.join(t.value for t in Tier)}) - {wskazowka}",
            ))
        else:
            if rola is not None and tier != profil_roli(rola).tier:
                wynik.append(Naruszenie(
                    Niezmiennik.role_binding,
                    f"tier {tier.value} nie pasuje do roli {rola.value} "
                    f"(rola ustawia tier {profil_roli(rola).tier.value})",
                ))

    for klucz in KLUCZE_KOMENDY:
        if klucz not in definicja:
            continue
        komenda = _tekst_komendy(definicja[klucz])
        if komenda is not None and _zapis_do_gita(komenda):
            wynik.append(Naruszenie(
                Niezmiennik.no_git_write,
                f"silnik nie commituje ani nie pushuje: git w komendzie '{komenda}'",
            ))
    return wynik


# --- reviewer_readonly: migawka katalogu ------------------------------------

Migawka = Mapping[str, str]


def _wyjatek(rel: Path, wyjatki: Sequence[Path]) -> bool:
    return any(rel.parts[: len(w.parts)] == w.parts for w in wyjatki if w.parts)


def migawka(katalog: Path, wyjatki: Sequence[Path] = ()) -> Migawka:
    """Sciezka wzgledna (POSIX) -> sha256 tresci. `.git` pomijany zawsze.

    git status przepisuje .git/index, wiec .git/ nie jest zmiana pracy recenzenta.
    Dowiazania symboliczne: hash tekstu celu, nie sledzimy ich.
    """
    wynik: dict[str, str] = {}
    for korzen, katalogi, pliki in os.walk(katalog, followlinks=False):
        katalogi[:] = [d for d in katalogi if d != ".git"]
        dowiazane = [d for d in katalogi if os.path.islink(os.path.join(korzen, d))]
        katalogi[:] = [d for d in katalogi if d not in dowiazane]
        for nazwa in [*pliki, *dowiazane]:
            if nazwa == ".git":
                continue
            pelna = Path(korzen, nazwa)
            rel = pelna.relative_to(katalog)
            if _wyjatek(rel, wyjatki):
                continue
            if pelna.is_symlink():
                dane = os.readlink(pelna).encode()
            elif pelna.is_file():
                dane = pelna.read_bytes()
            else:
                continue
            wynik[rel.as_posix()] = hashlib.sha256(dane).hexdigest()
    return wynik


def sprawdz_tylko_odczyt(
    przed: Migawka, katalog: Path, wyjatki: Sequence[Path] = ()
) -> Naruszenie | None:
    """Katalog po kroku recenzenta ma byc taki jak przed. Podpiecie: BRO-104/BRO-108."""
    po = migawka(katalog, wyjatki)
    grupy = (
        ("nowe", sorted(set(po) - set(przed))),
        ("usuniete", sorted(set(przed) - set(po))),
        ("zmienione", sorted(k for k in set(przed) & set(po) if przed[k] != po[k])),
    )
    opisy = [f"{etykieta}: {', '.join(sciezki)}" for etykieta, sciezki in grupy if sciezki]
    if not opisy:
        return None
    return Naruszenie(Niezmiennik.reviewer_readonly, "; ".join(opisy))


# --- pozostale niezmienniki -------------------------------------------------

def sprawdz_decyzje(krok_ma_approval: bool, decyzja: str | None) -> Naruszenie | None:
    if krok_ma_approval and (decyzja is None or not decyzja.strip()):
        return Naruszenie(
            Niezmiennik.approval_required, "krok z approval czeka na decyzje czlowieka"
        )
    return None


def sprawdz_katalog_runu(run_dir: Path, repo: Path) -> Naruszenie | None:
    """run_dir (logi, wyniki, werdykty runu) nie lezy w repozytorium uzytkownika.

    To nie dotyczy `.cockpit/_run/skills/` - materializacja skilli z HAR-5 jest
    swiadomym wyjatkiem i nie jest run_dir.
    """
    r, k = run_dir.resolve(), repo.resolve()
    if r == k or k in r.parents:
        return Naruszenie(
            Niezmiennik.run_dir_outside_repo,
            f"katalog runu {run_dir} lezy w repozytorium {repo}",
        )
    return None


def sprawdz_pomiar(cost_usd: float | None, cost_source: str) -> Naruszenie | None:
    if cost_usd == 0.0 and cost_source == "unavailable":
        # BRO-87: zero znaczy ZMIERZONE zero. Brak pomiaru to None.
        return Naruszenie(
            Niezmiennik.measured_or_null,
            "koszt 0 przy zrodle 'unavailable' - brak pomiaru zapisz jako None",
        )
    return None


def dowod_z_komendy(stdout: str) -> Naruszenie | None:
    if not stdout.strip():
        return Naruszenie(Niezmiennik.evidence_required, "komenda nie zwrocila zadnego wyniku")
    return None


def dowod_z_pliku(plik: Path) -> Naruszenie | None:
    if not plik.is_file():
        return Naruszenie(Niezmiennik.evidence_required, f"brak pliku dowodu: {plik}")
    if not plik.read_text(errors="replace").strip():
        return Naruszenie(Niezmiennik.evidence_required, f"plik dowodu jest pusty: {plik}")
    return None


SPRAWDZENIA: Mapping[Niezmiennik, tuple[Callable[..., Any], ...]] = MappingProxyType({
    Niezmiennik.role_binding: (waliduj_definicje,),
    Niezmiennik.reviewer_readonly: (waliduj_definicje, sprawdz_tylko_odczyt),
    Niezmiennik.approval_required: (sprawdz_decyzje,),
    Niezmiennik.no_git_write: (waliduj_definicje,),
    Niezmiennik.run_dir_outside_repo: (sprawdz_katalog_runu,),
    Niezmiennik.measured_or_null: (sprawdz_pomiar,),
    Niezmiennik.evidence_required: (dowod_z_komendy, dowod_z_pliku),
})

# Punkty wywolania "modul:funkcja" osiagalne z cockpit.__main__ i runner.__main__.
# Test w tests/test_warstwy.py pilnuje, ze punkt faktycznie wola sprawdzenie.
EGZEKWOWANE: Mapping[Niezmiennik, tuple[str, ...]] = MappingProxyType({
    Niezmiennik.measured_or_null: ("cockpit.services.runs:zapisz_zdarzenie",),
})
