"""Reguła pokrycia bramkami: zaplanowane, wykonane, pominięte (HAR-64).

Nazwy testów w języku dziedziny, nie technologii. Funkcja jest czysta, więc
testy nie potrzebują bazy. Asercje porównują cały `PokrycieBramek` albo pełny
słownik powodów; granice stanu (0/0, 1/1, 1 z 2, 0 z 2, 2 z 3) mają osobne testy.
"""

import dataclasses

import pytest

from cockpit.services.pokrycie import (
    PokrycieBramek,
    PowodPominiecia,
    StanPokrycia,
    WynikBramki,
    WynikKrokuWeryfikujacego,
    pokrycie_bramek,
)

WYKONANA = WynikBramki.wykonana
NIEUDANA = WynikBramki.nieudana
POMINIETA = WynikBramki.pominieta

BRAK_CAPABILITY = PowodPominiecia.brak_capability
PUSTY_FOREACH = PowodPominiecia.pusty_foreach
BRAK_WEJSCIA = PowodPominiecia.brak_wejscia
ZALEZNOSC_NIEUDANA = PowodPominiecia.zaleznosc_nieudana

# Fragmenty komunikatów ValueError, których oczekuje test.
BRAK_POWODU = r"^pominieta wymaga powodu$"
POWOD_SPOZA_LISTY = "spoza listy"
POWOD_ZBEDNY = r"^wykonana ani nieudana nie moze miec powodu$"
POWTORZONY_KROK = "krok_id"

# Powody, które są pominięciem bramki (zaleznosc_nieudana nim nie jest).
POWODY_POMINIECIA = [BRAK_CAPABILITY, PUSTY_FOREACH, BRAK_WEJSCIA]


def wykonany(krok_id: str) -> WynikKrokuWeryfikujacego:
    return WynikKrokuWeryfikujacego(krok_id, WYKONANA)


def nieudany(krok_id: str) -> WynikKrokuWeryfikujacego:
    return WynikKrokuWeryfikujacego(krok_id, NIEUDANA)


def pominiety(
    krok_id: str, powod: PowodPominiecia = BRAK_CAPABILITY
) -> WynikKrokuWeryfikujacego:
    return WynikKrokuWeryfikujacego(krok_id, POMINIETA, powod)


# --- stan: gałęzie wyliczania ---------------------------------------------


def test_workflow_z_trzema_bramkami_bez_skonfigurowanych_capability_jest_niesprawdzony():
    kroki = [pominiety("lint"), pominiety("typy"), pominiety("testy")]

    assert pokrycie_bramek(kroki) == PokrycieBramek(
        zaplanowane=3,
        wykonane=0,
        pominiete=3,
        powody={
            "lint": PowodPominiecia.brak_capability,
            "typy": PowodPominiecia.brak_capability,
            "testy": PowodPominiecia.brak_capability,
        },
        stan=StanPokrycia.niesprawdzony,
    )


def test_run_bez_krokow_weryfikujacych_nie_jest_sprawdzony():
    assert pokrycie_bramek([]) == PokrycieBramek(
        zaplanowane=0,
        wykonane=0,
        pominiete=0,
        powody={},
        stan=StanPokrycia.niesprawdzony,
    )


def test_jedna_bramka_wykonana_i_jedna_pominieta_daja_stan_czesciowy():
    wynik = pokrycie_bramek([wykonany("lint"), pominiety("testy", PUSTY_FOREACH)])

    assert wynik == PokrycieBramek(
        zaplanowane=2,
        wykonane=1,
        pominiete=1,
        powody={"testy": PowodPominiecia.pusty_foreach},
        stan=StanPokrycia.czesciowo,
    )


def test_dwie_z_trzech_bramek_wykonane_to_stan_czesciowy():
    wynik = pokrycie_bramek([wykonany("a"), wykonany("b"), pominiety("c")])

    assert (wynik.zaplanowane, wynik.wykonane, wynik.pominiete) == (3, 2, 1)
    assert wynik.stan == StanPokrycia.czesciowo


def test_dwie_pominiete_bramki_bez_zadnej_wykonanej_to_niesprawdzony():
    wynik = pokrycie_bramek([pominiety("a"), pominiety("b")])

    assert (wynik.zaplanowane, wynik.wykonane, wynik.pominiete) == (2, 0, 2)
    assert wynik.stan == StanPokrycia.niesprawdzony


def test_jedna_bramka_wykonana_to_run_sprawdzony():
    assert pokrycie_bramek([wykonany("lint")]) == PokrycieBramek(
        zaplanowane=1,
        wykonane=1,
        pominiete=0,
        powody={},
        stan=StanPokrycia.sprawdzony,
    )


def test_wszystkie_bramki_wykonane_to_run_sprawdzony():
    wynik = pokrycie_bramek([wykonany("a"), wykonany("b"), wykonany("c")])

    assert (wynik.zaplanowane, wynik.wykonane, wynik.pominiete) == (3, 3, 0)
    assert wynik.powody == {}
    assert wynik.stan == StanPokrycia.sprawdzony


def test_nieudana_bramka_liczy_sie_jako_wykonana():
    assert pokrycie_bramek([nieudany("testy")]) == PokrycieBramek(
        zaplanowane=1,
        wykonane=1,
        pominiete=0,
        powody={},
        stan=StanPokrycia.sprawdzony,
    )


def test_nieudana_obok_pominietej_daje_stan_czesciowy_a_nie_niesprawdzony():
    wynik = pokrycie_bramek([nieudany("testy"), pominiety("lint")])

    assert (wynik.zaplanowane, wynik.wykonane, wynik.pominiete) == (2, 1, 1)
    assert wynik.stan == StanPokrycia.czesciowo


def test_wykonana_nieudana_i_pominieta_razem_licza_sie_osobno():
    wynik = pokrycie_bramek([wykonany("a"), nieudany("b"), pominiety("c", BRAK_WEJSCIA)])

    assert wynik == PokrycieBramek(
        zaplanowane=3,
        wykonane=2,
        pominiete=1,
        powody={"c": PowodPominiecia.brak_wejscia},
        stan=StanPokrycia.czesciowo,
    )


def test_powody_dotycza_tylko_pominietych_bramek():
    wynik = pokrycie_bramek([wykonany("a"), nieudany("b"), pominiety("c", PUSTY_FOREACH)])

    assert dict(wynik.powody) == {"c": PowodPominiecia.pusty_foreach}


@pytest.mark.parametrize("powod", POWODY_POMINIECIA)
def test_kazdy_powod_pominiecia_bramki_liczy_sie_jako_pominiecie(powod):
    assert pokrycie_bramek([wykonany("a"), pominiety("b", powod)]) == PokrycieBramek(
        zaplanowane=2,
        wykonane=1,
        pominiete=1,
        powody={"b": powod},
        stan=StanPokrycia.czesciowo,
    )


# --- zaleznosc_nieudana: nie jest pominięciem bramki ----------------------


def test_krok_z_nieudana_zaleznoscia_nie_jest_pominieciem_bramki():
    wynik = pokrycie_bramek([wykonany("lint"), pominiety("testy", ZALEZNOSC_NIEUDANA)])

    assert wynik == PokrycieBramek(
        zaplanowane=2,
        wykonane=1,
        pominiete=0,
        powody={},
        stan=StanPokrycia.czesciowo,
    )


def test_sama_nieudana_zaleznosc_jest_zaplanowana_ale_run_niesprawdzony():
    assert pokrycie_bramek([pominiety("testy", ZALEZNOSC_NIEUDANA)]) == PokrycieBramek(
        zaplanowane=1,
        wykonane=0,
        pominiete=0,
        powody={},
        stan=StanPokrycia.niesprawdzony,
    )


def test_nieudana_zaleznosc_obok_prawdziwego_pominiecia_nie_wchodzi_do_powodow():
    wynik = pokrycie_bramek(
        [
            pominiety("a", BRAK_CAPABILITY),
            pominiety("b", ZALEZNOSC_NIEUDANA),
            wykonany("c"),
        ]
    )

    assert wynik == PokrycieBramek(
        zaplanowane=3,
        wykonane=1,
        pominiete=1,
        powody={"a": PowodPominiecia.brak_capability},
        stan=StanPokrycia.czesciowo,
    )


# --- walidacja wejścia ----------------------------------------------------


def test_pominiecie_bramki_bez_powodu_jest_odrzucone():
    with pytest.raises(ValueError, match=BRAK_POWODU):
        WynikKrokuWeryfikujacego("lint", POMINIETA, None)


def test_pominiecie_bramki_bez_powodu_domyslnie_jest_odrzucone():
    with pytest.raises(ValueError, match=BRAK_POWODU):
        WynikKrokuWeryfikujacego("lint", POMINIETA)


@pytest.mark.parametrize(
    "powod",
    ["brak capability lint", "", "   ", "\t\n ", "BRAK_CAPABILITY", "inny_powod"],
)
def test_pominiecie_z_powodem_spoza_listy_jest_odrzucone(powod):
    with pytest.raises(ValueError, match=POWOD_SPOZA_LISTY):
        WynikKrokuWeryfikujacego("lint", POMINIETA, powod)  # type: ignore[arg-type]


@pytest.mark.parametrize("powod", list(PowodPominiecia))
def test_powod_podany_jako_napis_z_listy_jest_zamieniany_na_czlonka_listy(powod):
    krok = WynikKrokuWeryfikujacego("lint", POMINIETA, powod.value)  # type: ignore[arg-type]

    assert type(krok.powod) is PowodPominiecia
    assert krok.powod is powod


def test_powod_podany_jako_napis_z_listy_trafia_do_pokrycia_jako_czlonek_listy():
    krok = WynikKrokuWeryfikujacego("lint", POMINIETA, "pusty_foreach")  # type: ignore[arg-type]

    wynik = pokrycie_bramek([krok])

    assert type(wynik.powody["lint"]) is PowodPominiecia
    assert wynik.powody == {"lint": PowodPominiecia.pusty_foreach}


@pytest.mark.parametrize("wynik", ["skipped", None, "pominięta", "WYKONANA"])
def test_nieznany_wynik_bramki_jest_odrzucony(wynik):
    with pytest.raises(ValueError, match=r"^wynik spoza listy"):
        WynikKrokuWeryfikujacego("lint", wynik)  # type: ignore[arg-type]


def test_nieznany_wynik_bramki_z_powodem_jest_odrzucony_jako_wynik_spoza_listy():
    with pytest.raises(ValueError, match=r"^wynik spoza listy"):
        WynikKrokuWeryfikujacego("lint", "skipped", "brak_capability")  # type: ignore[arg-type]


def test_wynik_podany_napisem_z_listy_jest_zamieniany_na_czlonka_listy():
    krok = WynikKrokuWeryfikujacego("lint", "nieudana")  # type: ignore[arg-type]

    assert type(krok.wynik) is WynikBramki
    assert krok.wynik is WynikBramki.nieudana


def test_nieudana_bramka_podana_napisem_liczy_sie_jako_wykonana():
    wynik = pokrycie_bramek([WynikKrokuWeryfikujacego("lint", "nieudana")])  # type: ignore[arg-type]

    assert (wynik.zaplanowane, wynik.wykonane, wynik.pominiete) == (1, 1, 0)
    assert wynik.stan == StanPokrycia.sprawdzony


def test_pominiecie_podane_napisami_liczy_sie_jako_pominiecie_bramki():
    krok = WynikKrokuWeryfikujacego("lint", "pominieta", "brak_capability")  # type: ignore[arg-type]

    assert pokrycie_bramek([krok]) == PokrycieBramek(
        zaplanowane=1,
        wykonane=0,
        pominiete=1,
        powody={"lint": PowodPominiecia.brak_capability},
        stan=StanPokrycia.niesprawdzony,
    )


def test_powod_przy_wykonanej_bramce_jest_odrzucony():
    with pytest.raises(ValueError, match=POWOD_ZBEDNY):
        WynikKrokuWeryfikujacego("lint", WYKONANA, BRAK_CAPABILITY)


def test_powod_przy_nieudanej_bramce_jest_odrzucony():
    with pytest.raises(ValueError, match=POWOD_ZBEDNY):
        WynikKrokuWeryfikujacego("lint", NIEUDANA, BRAK_CAPABILITY)


def test_powtorzony_krok_jest_odrzucony():
    with pytest.raises(ValueError, match=POWTORZONY_KROK):
        pokrycie_bramek([wykonany("lint"), wykonany("lint")])


def test_ten_sam_krok_raz_wykonany_raz_pominiety_jest_odrzucony():
    with pytest.raises(ValueError, match=POWTORZONY_KROK):
        pokrycie_bramek([wykonany("lint"), pominiety("lint")])


# --- niezmienność i wejście -----------------------------------------------


def test_powody_pokrycia_sa_tylko_do_odczytu():
    wynik = pokrycie_bramek([pominiety("lint", BRAK_CAPABILITY)])

    with pytest.raises(TypeError):
        wynik.powody["x"] = BRAK_WEJSCIA  # type: ignore[index]
    assert dict(wynik.powody) == {"lint": PowodPominiecia.brak_capability}


def test_powody_pokrycia_nie_zmieniaja_sie_gdy_ktos_zmieni_dane_zrodlowe():
    kroki = [pominiety("lint", BRAK_CAPABILITY)]
    wynik = pokrycie_bramek(kroki)

    kroki.append(pominiety("typy"))

    assert dict(wynik.powody) == {"lint": PowodPominiecia.brak_capability}


def test_wynik_pokrycia_jest_niezmienny():
    wynik = pokrycie_bramek([wykonany("lint")])

    with pytest.raises(dataclasses.FrozenInstanceError):
        wynik.stan = StanPokrycia.niesprawdzony  # type: ignore[misc]


def test_wynik_kroku_jest_niezmienny():
    krok = wykonany("lint")

    with pytest.raises(dataclasses.FrozenInstanceError):
        krok.wynik = POMINIETA  # type: ignore[misc]


def test_pokrycie_liczy_kroki_podane_jednorazowo():
    def generuj():
        yield wykonany("a")
        yield pominiety("b", BRAK_WEJSCIA)
        yield nieudany("c")

    assert pokrycie_bramek(generuj()) == PokrycieBramek(
        zaplanowane=3,
        wykonane=2,
        pominiete=1,
        powody={"b": PowodPominiecia.brak_wejscia},
        stan=StanPokrycia.czesciowo,
    )


def test_wartosci_stanow_wynikow_i_powodow_sa_stabilnymi_napisami():
    assert [s.value for s in StanPokrycia] == ["sprawdzony", "czesciowo", "niesprawdzony"]
    assert [w.value for w in WynikBramki] == ["wykonana", "nieudana", "pominieta"]
    assert [p.value for p in PowodPominiecia] == [
        "brak_capability",
        "pusty_foreach",
        "brak_wejscia",
        "zaleznosc_nieudana",
    ]
