"""Kontrakt ticket.json i tryb wklejenia ręcznego (HAR-41, BRO-111)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from cockpit.services.errors import BladDziedziny
from runner.tickety import (
    Komentarz,
    PusteWklejenie,
    TicketJson,
    do_pliku,
    z_wklejenia,
)

KOLEJNOSC = [
    "source",
    "id",
    "url",
    "title",
    "description",
    "acceptance_criteria",
    "status",
    "priority",
    "labels",
    "comments",
    "fetched_at",
    "raw",
]
KOLEJNOSC_KOMENTARZA = ["author", "body", "at"]

TERAZ = datetime(2026, 3, 4, 12, 30, 15, tzinfo=timezone.utc)
NAIWNY = datetime(2026, 3, 4, 12, 30, 15)


def _dane(**nadpisz):
    dane = {
        "source": "linear",
        "id": "HAR-41",
        "url": "https://linear.app/x/issue/HAR-41",
        "title": "Kontrakt ticketu",
        "description": "Opis zadania",
        "acceptance_criteria": ["jedno kryterium"],
        "status": "In Review",
        "priority": "P0",
        "labels": ["backend"],
        "comments": [{"author": "Ala", "body": "Zażółć gęślą", "at": TERAZ}],
        "fetched_at": TERAZ,
        "raw": {"oryginal": {"a": 1}},
    }
    dane.update(nadpisz)
    return dane


def _lokalizacja_bledu(dane):
    with pytest.raises(ValidationError) as exc:
        TicketJson(**dane)
    return [e["loc"] for e in exc.value.errors()]


# --- źródło ---------------------------------------------------------------


@pytest.mark.parametrize("zrodlo", ["linear", "jira", "github", "manual"])
def test_zrodlo_ze_stalej_listy_dostawcow_jest_przyjmowane(zrodlo):
    assert TicketJson(**_dane(source=zrodlo)).source == zrodlo


def test_zrodlo_mcp_z_nazwa_serwera_jest_przyjmowane():
    assert TicketJson(**_dane(source="mcp:serwer-1.a_b")).source == "mcp:serwer-1.a_b"


def test_zrodlo_mcp_z_wielka_litera_w_nazwie_serwera_jest_przyjmowane():
    # tylko A-Z w klasie znaków przyjmuje ten napis (bez wielkiej litery nic innego go nie odrzuca)
    assert TicketJson(**_dane(source="mcp:Serwer")).source == "mcp:Serwer"


@pytest.mark.parametrize(
    "zrodlo",
    [
        "jira-cloud",
        "Linear",
        "mcp:",
        " manual",
        "",
    ],
)
def test_nieznane_zrodlo_ticketu_jest_odrzucane(zrodlo):
    assert _lokalizacja_bledu(_dane(source=zrodlo)) == [("source",)]


def test_zrodlo_mcp_z_nazwa_spoza_ascii_jest_odrzucane():
    # tylko zawężenie klasy znaków odrzuca "ą" (\w by je przyjęło)
    assert _lokalizacja_bledu(_dane(source="mcp:serwerą")) == [("source",)]


def test_zrodlo_z_koncowym_znakiem_nowej_linii_jest_odrzucane():
    # tylko pełne dopasowanie odrzuca końcowy \n (Pythonowe $ by go dopuściło)
    assert _lokalizacja_bledu(_dane(source="manual\n")) == [("source",)]


def test_zrodlo_mcp_z_koncowym_znakiem_nowej_linii_jest_odrzucane():
    assert _lokalizacja_bledu(_dane(source="mcp:serwer\n")) == [("source",)]


# --- kształt kontraktu ----------------------------------------------------


def test_nadmiarowe_pole_w_tickecie_jest_odrzucane():
    assert _lokalizacja_bledu(_dane(assignee="Ola")) == [("assignee",)]


def test_nadmiarowe_pole_w_komentarzu_jest_odrzucane():
    komentarz = {"author": "Ala", "body": "tekst", "at": TERAZ, "reakcje": 3}
    assert _lokalizacja_bledu(_dane(comments=[komentarz])) == [("comments", 0, "reakcje")]


def test_komentarz_przyjmuje_tylko_autora_tresc_i_czas():
    k = Komentarz(author="Ala", body="tekst", at=TERAZ)
    assert (k.author, k.body, k.at) == ("Ala", "tekst", TERAZ)


@pytest.mark.parametrize("pole", [p for p in KOLEJNOSC if p != "acceptance_criteria"])
def test_brak_wymaganego_pola_ticketu_jest_bledem_walidacji(pole):
    dane = _dane()
    del dane[pole]
    assert _lokalizacja_bledu(dane) == [(pole,)]


def test_brak_kryteriow_akceptacji_daje_pusta_liste():
    dane = _dane()
    del dane["acceptance_criteria"]
    assert TicketJson(**dane).acceptance_criteria == []


def test_pola_bez_wartosci_ze_zrodla_dopuszczaja_brak_url_statusu_i_priorytetu():
    t = TicketJson(**_dane(url=None, status=None, priority=None))
    assert (t.url, t.status, t.priority) == (None, None, None)


def test_data_pobrania_bez_strefy_czasowej_jest_odrzucana():
    assert _lokalizacja_bledu(_dane(fetched_at=NAIWNY)) == [("fetched_at",)]


def test_data_komentarza_bez_strefy_czasowej_jest_odrzucana():
    komentarz = {"author": "Ala", "body": "x", "at": NAIWNY}
    assert _lokalizacja_bledu(_dane(comments=[komentarz])) == [("comments", 0, "at")]


def test_komentarz_bez_daty_jest_dopuszczony():
    t = TicketJson(**_dane(comments=[{"author": "Ala", "body": "x", "at": None}]))
    assert t.comments[0].at is None


def test_data_ze_strefa_inna_niz_utc_jest_przyjmowana():
    data = datetime(2026, 3, 4, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    assert TicketJson(**_dane(fetched_at=data)).fetched_at == data


@pytest.mark.parametrize(
    "napis", ["In Review", "Do zrobienia", "P0", "Highest", "zażółć", "weird status 42"]
)
def test_status_i_priorytet_przyjmuja_dowolny_napis_ze_zrodla_bez_zmian(napis):
    t = TicketJson(**_dane(status=napis, priority=napis))
    assert (t.status, t.priority) == (napis, napis)
    wczytany = json.loads(do_pliku(t))
    assert (wczytany["status"], wczytany["priority"]) == (napis, napis)


# --- zapis do pliku -------------------------------------------------------


def test_plik_ticketu_ma_klucze_w_kolejnosci_kontraktu_takze_w_komentarzach():
    wczytany = json.loads(do_pliku(TicketJson(**_dane())))
    assert list(wczytany.keys()) == KOLEJNOSC
    assert list(wczytany["comments"][0].keys()) == KOLEJNOSC_KOMENTARZA


def test_plik_ticketu_nie_zalezy_od_kolejnosci_podania_pol():
    dane = _dane()
    odwrocone = dict(reversed(list(dane.items())))
    wczytany = json.loads(do_pliku(TicketJson(**odwrocone)))
    assert list(wczytany.keys()) == KOLEJNOSC


def test_plik_ticketu_zachowuje_polskie_znaki_w_utf8_bez_escapowania():
    t = TicketJson(**_dane(title="Zażółć gęślą jaźń"))
    bajty = do_pliku(t)
    assert isinstance(bajty, bytes)
    assert "Zażółć gęślą jaźń".encode("utf-8") in bajty
    assert b"\\u" not in bajty
    assert json.loads(bajty.decode("utf-8"))["title"] == "Zażółć gęślą jaźń"


def test_plik_ticketu_zapisuje_date_pobrania_jako_iso_8601():
    wczytany = json.loads(do_pliku(TicketJson(**_dane())))
    assert wczytany["fetched_at"] == "2026-03-04T12:30:15Z"
    assert wczytany["comments"][0]["at"] == "2026-03-04T12:30:15Z"


def test_ticket_wczytany_z_pliku_jest_rowny_zapisanemu():
    t = TicketJson(**_dane())
    assert TicketJson.model_validate_json(do_pliku(t)) == t


def test_ticket_z_wklejenia_wczytany_z_pliku_jest_rowny_zapisanemu():
    t = z_wklejenia("Tytuł\nOpis z ą", url="https://x.example/1", teraz=TERAZ)
    assert TicketJson.model_validate_json(do_pliku(t)) == t


def test_plik_ticketu_ma_dokladnie_taki_zapis_bajtow_z_wcieciem_dwoch_spacji():
    # dokładne bajty: tylko format zapisu (wcięcie, brak escapowania, data ze Z) przechodzi
    t = TicketJson(
        source="manual",
        id="X-1",
        url=None,
        title="Zażółć",
        description="Opis",
        acceptance_criteria=["k"],
        status=None,
        priority="P0",
        labels=["a"],
        comments=[Komentarz(author="Ala", body="ą", at=TERAZ)],
        fetched_at=TERAZ,
        raw={"k": "ę"},
    )
    oczekiwany = (
        "{\n"
        '  "source": "manual",\n'
        '  "id": "X-1",\n'
        '  "url": null,\n'
        '  "title": "Zażółć",\n'
        '  "description": "Opis",\n'
        '  "acceptance_criteria": [\n'
        '    "k"\n'
        "  ],\n"
        '  "status": null,\n'
        '  "priority": "P0",\n'
        '  "labels": [\n'
        '    "a"\n'
        "  ],\n"
        '  "comments": [\n'
        "    {\n"
        '      "author": "Ala",\n'
        '      "body": "ą",\n'
        '      "at": "2026-03-04T12:30:15Z"\n'
        "    }\n"
        "  ],\n"
        '  "fetched_at": "2026-03-04T12:30:15Z",\n'
        '  "raw": {\n'
        '    "k": "ę"\n'
        "  }\n"
        "}"
    )
    assert do_pliku(t) == oczekiwany.encode("utf-8")


def test_plik_ticketu_zachowuje_oryginal_ze_zrodla_w_raw():
    raw = {"zagniezdzony": {"lista": [1, 2, {"k": "ą"}]}, "x": None}
    wczytany = json.loads(do_pliku(TicketJson(**_dane(raw=raw))))
    assert wczytany["raw"] == raw


# --- tryb wklejenia ręcznego ---------------------------------------------


def test_ticket_z_wklejenia_ma_zrodlo_manual_i_komplet_kluczy_kontraktu():
    t = z_wklejenia("Tytuł\nOpis", teraz=TERAZ)
    assert t.source == "manual"
    assert list(json.loads(do_pliku(t)).keys()) == KOLEJNOSC


def test_ticket_z_wklejenia_ma_pelna_zawartosc_zgodna_z_decyzja_kontraktu():
    tekst = "Dodaj login\n\nKryteria: działa.\n"
    t = z_wklejenia(tekst, url="https://x.example/t/1", teraz=TERAZ)
    oczekiwany_id = "manual-" + hashlib.sha256(tekst.encode("utf-8")).hexdigest()[:8]
    assert json.loads(do_pliku(t)) == {
        "source": "manual",
        "id": oczekiwany_id,
        "url": "https://x.example/t/1",
        "title": "Dodaj login",
        "description": tekst,
        "acceptance_criteria": [],
        "status": None,
        "priority": None,
        "labels": [],
        "comments": [],
        "fetched_at": "2026-03-04T12:30:15Z",
        "raw": {"tekst": tekst},
    }


def test_ticket_z_wklejenia_bez_adresu_ma_url_pusty():
    assert z_wklejenia("Tytuł", teraz=TERAZ).url is None


def test_ticket_z_wklejenia_bez_podanego_czasu_ma_date_pobrania_ze_strefa_utc():
    przed = datetime.now(timezone.utc)
    t = z_wklejenia("Tytuł")
    po = datetime.now(timezone.utc)
    assert t.fetched_at.utcoffset() == timedelta(0)
    assert przed <= t.fetched_at <= po


def test_ticket_z_wklejenia_ma_date_pobrania_z_podanego_czasu():
    assert z_wklejenia("Tytuł", teraz=TERAZ).fetched_at == TERAZ


def test_tytul_ticketu_z_wklejenia_to_pierwsza_niepusta_linia_pomijajac_puste_i_biale():
    t = z_wklejenia("\n   \n\t\nPrawdziwy tytuł\nDruga linia", teraz=TERAZ)
    assert t.title == "Prawdziwy tytuł"


def test_tytul_ticketu_z_wklejenia_jest_pozbawiony_wiodacych_i_koncowych_spacji():
    t = z_wklejenia("   Tytuł ze spacjami \t\nOpis", teraz=TERAZ)
    assert t.title == "Tytuł ze spacjami"


def test_tytul_ticketu_z_wklejenia_nie_siega_do_drugiej_linii():
    assert z_wklejenia("Pierwsza\nDruga", teraz=TERAZ).title == "Pierwsza"


def test_opis_ticketu_z_wklejenia_to_caly_wklejony_tekst_bez_zmian():
    tekst = "\n  Tytuł  \n\nOpis\n\n"
    t = z_wklejenia(tekst, teraz=TERAZ)
    assert t.description == tekst
    assert t.raw == {"tekst": tekst}


def test_identyfikator_ticketu_z_wklejenia_to_osiem_znakow_skrotu_sha256_tresci():
    tekst = "Zażółć gęślą jaźń"
    t = z_wklejenia(tekst, teraz=TERAZ)
    skrot = hashlib.sha256(tekst.encode("utf-8")).hexdigest()
    assert t.id == "manual-" + skrot[:8]
    assert len(t.id) == len("manual-") + 8


def test_identyfikator_ticketu_z_wklejenia_jest_stabilny_dla_tej_samej_tresci():
    inny_czas = TERAZ + timedelta(days=1)
    a = z_wklejenia("Ten sam tekst", url="https://a.example", teraz=TERAZ)
    b = z_wklejenia("Ten sam tekst", url="https://b.example", teraz=inny_czas)
    assert a.id == b.id


def test_identyfikator_ticketu_z_wklejenia_rozni_sie_dla_roznych_tresci():
    assert z_wklejenia("Tekst A", teraz=TERAZ).id != z_wklejenia("Tekst B", teraz=TERAZ).id


def test_identyfikator_ticketu_uwzglednia_biale_znaki_calego_tekstu_nie_tylko_tytulu():
    # skrót liczony z całego tekstu, nie z title po strip()
    assert z_wklejenia("Tytuł", teraz=TERAZ).id != z_wklejenia("Tytuł\n", teraz=TERAZ).id


def test_ticket_z_wklejenia_ma_puste_kryteria_etykiety_komentarze_status_i_priorytet():
    t = z_wklejenia("Tytuł\n- [ ] kryterium w opisie", teraz=TERAZ)
    assert (t.acceptance_criteria, t.labels, t.comments) == ([], [], [])
    assert (t.status, t.priority) == (None, None)


@pytest.mark.parametrize("tekst", ["", "   ", "\n\n", " \t\n  \n\r\n"])
def test_wklejenie_bez_zadnej_niepustej_linii_jest_odrzucane(tekst):
    with pytest.raises(PusteWklejenie):
        z_wklejenia(tekst, teraz=TERAZ)


def test_puste_wklejenie_jest_bledem_dziedziny():
    assert issubclass(PusteWklejenie, BladDziedziny)
    with pytest.raises(BladDziedziny):
        z_wklejenia("  \n ", teraz=TERAZ)
