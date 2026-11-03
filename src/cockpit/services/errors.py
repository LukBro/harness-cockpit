"""Bledy dziedziny. Warstwa api/ tlumaczy je na kody HTTP."""


class BladDziedziny(Exception):
    """Baza. Niesie komunikat dla czlowieka, nie slad stosu."""


class BrakDostepu(BladDziedziny):
    """Brak sesji, zly token, brak czlonkostwa w zespole."""


class NieprawidloweObecneHaslo(BladDziedziny):
    """Zle obecne haslo przy zmianie hasla: blad formularza, nie wygasla sesja (HAR-145)."""


class ZbytWieleProb(BladDziedziny):
    """Logowanie wstrzymane po serii nieudanych prob; komunikat podaje czas oczekiwania (HAR-46)."""


class NieZnaleziono(BladDziedziny):
    pass


class NaruszonyNiezmiennik(BladDziedziny):
    """Proba zlamania reguly z listy niezmiennikow (BRO-74)."""

    def __init__(self, nazwa: str, szczegol: str = "") -> None:
        self.nazwa = nazwa
        super().__init__(f"niezmiennik {nazwa}: {szczegol}" if szczegol else f"niezmiennik {nazwa}")


class BlednySkill(BladDziedziny):
    """SKILL.md nie da sie wczytac: nieznana rola, brak pola, id inne niz katalog (HAR-5)."""


class BlednyPlikLekcji(BladDziedziny):
    """Plik `.ai/lessons.md` nie da sie odczytac: brak pliku, prawa, nie UTF-8 (HAR-87)."""


class StanNiemozliwy(BladDziedziny):
    """Np. runner offline przy probie uruchomienia."""


class Zabronione(BladDziedziny):
    """Czlonek zespolu bez uprawnienia do operacji (HAR-38)."""


class PlikPozaZakresem(Zabronione):
    """Runner odmowil dostepu do sciezki: poza zakresem, sekret, .git przy zapisie (HAR-55)."""


class RunnerOffline(StanNiemozliwy):
    """Zespol nie ma runnera online: kokpit dziala w trybie tylko do odczytu (HAR-55)."""


class WymagaPotwierdzenia(StanNiemozliwy):
    """Na tickecie trwa juz run; drugi startuje dopiero po jawnym potwierdzeniu (HAR-58)."""

    def __init__(self, aktywny_run_id: str) -> None:
        self.aktywny_run_id = aktywny_run_id
        super().__init__(
            f"na tym tickecie trwa już run {aktywny_run_id}, potwierdź, jeśli drugi run jest celowy"
        )


class NiespojneDane(BladDziedziny):
    """Dane wejsciowe wzajemnie sprzeczne, np. pelna dlugosc krotsza niz przyslany tekst (HAR-56)."""
