"""Bledy dziedziny. Warstwa api/ tlumaczy je na kody HTTP."""


class BladDziedziny(Exception):
    """Baza. Niesie komunikat dla czlowieka, nie slad stosu."""


class BrakDostepu(BladDziedziny):
    """Brak sesji, zly token, brak czlonkostwa w zespole."""


class NieZnaleziono(BladDziedziny):
    pass


class NaruszonyNiezmiennik(BladDziedziny):
    """Proba zlamania reguly z listy niezmiennikow (BRO-74)."""

    def __init__(self, nazwa: str, szczegol: str = "") -> None:
        self.nazwa = nazwa
        super().__init__(f"niezmiennik {nazwa}: {szczegol}" if szczegol else f"niezmiennik {nazwa}")


class BlednySkill(BladDziedziny):
    """SKILL.md nie da sie wczytac: nieznana rola, brak pola, id inne niz katalog (HAR-5)."""


class StanNiemozliwy(BladDziedziny):
    """Np. runner offline przy probie uruchomienia."""
