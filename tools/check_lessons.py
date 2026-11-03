"""Pilnuje, by kazdy wpis .ai/lessons.md mial zakres (HAR-87).

Regula bez sprawdzenia to zyczenie. To uruchamia sie w CI i w testach.
"""
from __future__ import annotations

import sys
from pathlib import Path

KORZEN = Path(__file__).resolve().parent.parent
LESSONS = KORZEN / ".ai" / "lessons.md"


def sprawdz(plik: Path = LESSONS) -> list[str]:
    # src/ tego checkoutu na poczatku: editable install moze wskazywac na inny checkout.
    sys.path.insert(0, str(KORZEN / "src"))
    from cockpit.services.lekcje import wczytaj_lekcje

    return [
        f"{plik}:{p.wiersz}: {p.wpis}: {p.komunikat} ({p.kod})" for p in wczytaj_lekcje(plik)
    ]


def main(argv: list[str]) -> int:
    sys.path.insert(0, str(KORZEN / "src"))
    from cockpit.services.errors import BlednyPlikLekcji

    plik = Path(argv[0]) if argv else LESSONS
    try:
        problemy = sprawdz(plik)
    except BlednyPlikLekcji as exc:
        print(exc, file=sys.stderr)
        return 2
    if problemy:
        print("Lekcje bez pełnego zakresu:")
        for x in problemy:
            print("  ", x)
        return 1
    print("lekcje mają zakres - w porządku")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
