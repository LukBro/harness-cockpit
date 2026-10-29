"""Adapter Linear: pobranie zgloszenia i lista po typie stanu (HAR-53)."""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from cockpit.services.errors import BladDziedziny
from runner import secrets
from runner.tickety import TicketJson, do_pliku, linear
from runner.tickety.bledy import KODY, BladIntegracji, PrzekroczonyLimit, z_ponowieniem

TOKEN = "lin_api_SEKRET_HAR53"
TERAZ = datetime(2026, 3, 4, 12, 30, 15, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures" / "tickety" / "linear_issue.json"

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

KOMUNIKAT_TOKENU = (
    "Linear: token odrzucony. Popraw go w ~/.config/cockpit/credentials.json "
    '(klucz "linear") albo w zmiennej COCKPIT_LINEAR_TOKEN.'
)


# --- pomocnicy -------------------------------------------------------------


def _odpowiedz_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _odp(status=200, *, json_=None, tresc=None, naglowki=None):
    """Fabryka odpowiedzi: swieza odpowiedz przy kazdym zadaniu."""

    def fabryka(_zadanie):
        if tresc is not None:
            return httpx.Response(status, content=tresc, headers=naglowki)
        return httpx.Response(status, json=json_, headers=naglowki)

    return fabryka


class Serwer:
    """Zaslepka transportu: kolejne kroki, ostatni powtarza sie w nieskonczonosc."""

    def __init__(self, *kroki):
        self.kroki = list(kroki)
        self.zadania: list[httpx.Request] = []

    def __call__(self, zadanie: httpx.Request) -> httpx.Response:
        self.zadania.append(zadanie)
        krok = self.kroki[min(len(self.zadania), len(self.kroki)) - 1]
        if isinstance(krok, Exception):
            raise krok
        return krok(zadanie)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def tresc(self, i: int) -> dict:
        return json.loads(self.zadania[i].content)


class Odczekania:
    """Wstrzykiwane `odczekaj`: zapisuje argumenty, nigdy nie spi."""

    def __init__(self):
        self.wartosci: list[float] = []

    async def __call__(self, sekundy: float) -> None:
        self.wartosci.append(sekundy)


def _lista(wezly, *, ma_nastepna=False, kursor=None):
    return {
        "data": {
            "issues": {
                "nodes": wezly,
                "pageInfo": {"hasNextPage": ma_nastepna, "endCursor": kursor},
            }
        }
    }


def _wezel(identyfikator, tytul="Tytul", stan="Do zrobienia"):
    return {"identifier": identyfikator, "title": tytul, "state": {"name": stan}}


def _blad_graphql(komunikat, kod=None):
    blad = {"message": komunikat}
    if kod is not None:
        blad["extensions"] = {"code": kod}
    return {"errors": [blad]}


@pytest.fixture(autouse=True)
def _token_i_pusty_plik_sekretow(monkeypatch, tmp_path):
    monkeypatch.setenv("COCKPIT_LINEAR_TOKEN", TOKEN)
    monkeypatch.setattr(secrets, "CONFIG", tmp_path / "brak.json")


@pytest.fixture
def bez_tokenu(monkeypatch, tmp_path):
    monkeypatch.delenv("COCKPIT_LINEAR_TOKEN", raising=False)
    monkeypatch.setattr(secrets, "CONFIG", tmp_path / "brak.json")


async def _pobierz(serwer, odczekania=None, id="HAR-53"):
    return await linear.pobierz(
        id,
        transport=serwer.transport,
        odczekaj=odczekania if odczekania is not None else Odczekania(),
        teraz=TERAZ,
    )


async def _listuj(serwer, odczekania=None, typy=("unstarted",)):
    return await linear.listuj(
        list(typy),
        transport=serwer.transport,
        odczekaj=odczekania if odczekania is not None else Odczekania(),
    )


async def _kod_bledu(korutyna) -> BladIntegracji:
    with pytest.raises(BladIntegracji) as exc:
        await korutyna
    return exc.value


# --- BladIntegracji --------------------------------------------------------


def test_kody_bledow_integracji_to_pieciu_znanych_przyczyn():
    assert KODY == ("brak_tokenu", "token_odrzucony", "limit", "nie_znaleziono", "blad_dostawcy")


@pytest.mark.parametrize("kod", KODY)
def test_blad_integracji_niesie_dostawce_kod_i_komunikat_a_jako_tekst_sam_komunikat(kod):
    blad = BladIntegracji("Linear", kod, "cos poszlo nie tak")
    assert (blad.dostawca, blad.kod, blad.komunikat) == ("Linear", kod, "cos poszlo nie tak")
    assert str(blad) == "cos poszlo nie tak"


def test_blad_integracji_jest_bledem_dziedziny():
    assert issubclass(BladIntegracji, BladDziedziny)


@pytest.mark.parametrize("kod", ["limt", "LIMIT", "", "token", "blad"])
def test_blad_integracji_z_kodem_spoza_listy_to_blad_programisty(kod):
    with pytest.raises(ValueError) as exc:
        BladIntegracji("Linear", kod, "komunikat")
    assert str(exc.value) == f"nieznany kod bledu integracji: {kod!r}"


def test_przekroczony_limit_pamieta_zadane_odczekanie_albo_jego_brak():
    assert PrzekroczonyLimit(12.5).odczekanie_s == 12.5
    assert PrzekroczonyLimit().odczekanie_s is None


def test_przekroczony_limit_niesie_zadane_odczekanie_tez_jako_argument_wyjatku():
    assert PrzekroczonyLimit(2.5).args == (2.5,)


# --- z_ponowieniem ---------------------------------------------------------


class Skrypt:
    """Wywolanie zwracajace albo rzucajace kolejne wartosci; liczy wywolania."""

    def __init__(self, *kroki):
        self.kroki = list(kroki)
        self.wywolania = 0

    async def __call__(self):
        krok = self.kroki[min(self.wywolania, len(self.kroki) - 1)]
        self.wywolania += 1
        if isinstance(krok, Exception):
            raise krok
        return krok


async def test_pierwsze_udane_wywolanie_nie_czeka_i_nie_ponawia():
    skrypt, czekanie = Skrypt("wynik"), Odczekania()
    wynik = await z_ponowieniem(skrypt, dostawca="Linear", odczekaj=czekanie)
    assert (wynik, skrypt.wywolania, czekanie.wartosci) == ("wynik", 1, [])


async def test_limit_dwa_razy_a_potem_sukces_zwraca_wynik_po_trzech_wywolaniach():
    skrypt = Skrypt(PrzekroczonyLimit(), PrzekroczonyLimit(), "wynik")
    czekanie = Odczekania()
    wynik = await z_ponowieniem(skrypt, dostawca="Linear", odczekaj=czekanie)
    assert (wynik, skrypt.wywolania, len(czekanie.wartosci)) == ("wynik", 3, 2)


async def test_ciagly_limit_wola_dokladnie_trzy_razy_i_czeka_tylko_miedzy_probami():
    skrypt, czekanie = Skrypt(PrzekroczonyLimit()), Odczekania()
    with pytest.raises(BladIntegracji) as exc:
        await z_ponowieniem(skrypt, 3, dostawca="Linear", odczekaj=czekanie)
    assert skrypt.wywolania == 3
    assert len(czekanie.wartosci) == 2  # po ostatniej probie nie czekamy
    assert (exc.value.kod, exc.value.dostawca) == ("limit", "Linear")


async def test_liczba_prob_jest_laczna_liczba_wywolan_takze_gdy_inna_niz_domyslna():
    skrypt, czekanie = Skrypt(PrzekroczonyLimit()), Odczekania()
    with pytest.raises(BladIntegracji):
        await z_ponowieniem(skrypt, 2, dostawca="Linear", odczekaj=czekanie)
    assert (skrypt.wywolania, len(czekanie.wartosci)) == (2, 1)


async def test_jedna_proba_nie_ponawia_i_nie_czeka_nawet_przy_limicie():
    skrypt, czekanie = Skrypt(PrzekroczonyLimit()), Odczekania()
    with pytest.raises(BladIntegracji) as exc:
        await z_ponowieniem(skrypt, 1, dostawca="Linear", odczekaj=czekanie)
    assert (skrypt.wywolania, czekanie.wartosci, exc.value.kod) == (1, [], "limit")


@pytest.mark.parametrize("proby", [0, -1])
async def test_zero_lub_ujemna_liczba_prob_to_blad_programisty_bez_wywolania(proby):
    skrypt = Skrypt("wynik")
    with pytest.raises(ValueError, match=r"^liczba prob musi byc co najmniej 1$"):
        await z_ponowieniem(skrypt, proby, dostawca="Linear", odczekaj=Odczekania())
    assert skrypt.wywolania == 0


async def test_odczekanie_bierze_sekundy_z_odpowiedzi_dostawcy():
    skrypt = Skrypt(PrzekroczonyLimit(7.0), PrzekroczonyLimit(2.5), "ok")
    czekanie = Odczekania()
    await z_ponowieniem(skrypt, dostawca="Linear", odczekaj=czekanie)
    assert czekanie.wartosci == [7.0, 2.5]


async def test_bez_wskazania_dostawcy_odczekanie_jest_domyslne_jedna_sekunda():
    skrypt = Skrypt(PrzekroczonyLimit(), "ok")
    czekanie = Odczekania()
    await z_ponowieniem(skrypt, dostawca="Linear", odczekaj=czekanie)
    assert czekanie.wartosci == [1.0]


async def test_domyslne_odczekanie_mozna_zmienic():
    skrypt = Skrypt(PrzekroczonyLimit(), "ok")
    czekanie = Odczekania()
    await z_ponowieniem(
        skrypt, dostawca="Linear", odczekaj=czekanie, domyslne_odczekanie_s=0.25
    )
    assert czekanie.wartosci == [0.25]


async def test_zadanie_zerowego_odczekania_przez_dostawce_nie_jest_zastepowane_domyslnym():
    skrypt = Skrypt(PrzekroczonyLimit(0.0), "ok")
    czekanie = Odczekania()
    await z_ponowieniem(skrypt, dostawca="Linear", odczekaj=czekanie)
    assert czekanie.wartosci == [0.0]


async def test_zbyt_dlugie_odczekanie_zadane_przez_dostawce_jest_ucinane_do_minuty():
    skrypt = Skrypt(PrzekroczonyLimit(3600.0), "ok")
    czekanie = Odczekania()
    await z_ponowieniem(skrypt, dostawca="Linear", odczekaj=czekanie)
    assert czekanie.wartosci == [60.0]


async def test_gorny_limit_odczekania_mozna_zmienic():
    skrypt = Skrypt(PrzekroczonyLimit(30.0), "ok")
    czekanie = Odczekania()
    await z_ponowieniem(skrypt, dostawca="Linear", odczekaj=czekanie, maks_odczekanie_s=10.0)
    assert czekanie.wartosci == [10.0]


async def test_odczekanie_krotsze_niz_gorny_limit_nie_jest_zmieniane():
    skrypt = Skrypt(PrzekroczonyLimit(59.0), "ok")
    czekanie = Odczekania()
    await z_ponowieniem(skrypt, dostawca="Linear", odczekaj=czekanie)
    assert czekanie.wartosci == [59.0]


async def test_komunikat_o_limicie_wskazuje_dostawce_i_liczbe_prob():
    with pytest.raises(BladIntegracji) as exc:
        await z_ponowieniem(
            Skrypt(PrzekroczonyLimit()), 2, dostawca="Jira", odczekaj=Odczekania()
        )
    assert "Jira" in exc.value.komunikat
    assert "2" in exc.value.komunikat
    assert exc.value.dostawca == "Jira"


@pytest.mark.parametrize(
    "wyjatek", [ValueError("zle dane"), BladIntegracji("Linear", "blad_dostawcy", "awaria")]
)
async def test_inny_wyjatek_niz_limit_przechodzi_od_razu_bez_ponowienia_i_czekania(wyjatek):
    skrypt, czekanie = Skrypt(wyjatek), Odczekania()
    with pytest.raises(type(wyjatek)) as exc:
        await z_ponowieniem(skrypt, 3, dostawca="Linear", odczekaj=czekanie)
    assert exc.value is wyjatek
    assert (skrypt.wywolania, czekanie.wartosci) == (1, [])


async def test_inny_wyjatek_po_limicie_przerywa_ponawianie():
    skrypt = Skrypt(PrzekroczonyLimit(), ValueError("zle"), "ok")
    czekanie = Odczekania()
    with pytest.raises(ValueError):
        await z_ponowieniem(skrypt, 3, dostawca="Linear", odczekaj=czekanie)
    assert (skrypt.wywolania, len(czekanie.wartosci)) == (2, 1)


# --- pobierz: odwzorowanie na TicketJson ----------------------------------


async def test_zgloszenie_z_odpowiedzi_linear_trafia_do_kontraktu_ticketu_ze_wszystkimi_polami():
    odpowiedz = _odpowiedz_fixture()
    ticket = await _pobierz(Serwer(_odp(json_=odpowiedz)))

    assert isinstance(ticket, TicketJson)
    assert ticket.source == "linear"
    assert ticket.id == "HAR-53"
    assert ticket.url == "https://linear.app/harness/issue/HAR-53/adapter-linear"
    assert ticket.title == "Adapter Linear: pobranie zgłoszenia"
    assert ticket.description == (
        "Zażółć gęślą jaźń.\n\nRunner pobiera zgłoszenie przez API GraphQL."
    )
    assert ticket.acceptance_criteria == []
    assert ticket.status == "W recenzji"
    assert ticket.priority == "High"
    assert ticket.labels == ["backend", "integracje"]
    assert [(k.author, k.body) for k in ticket.comments] == [
        ("Łukasz Żółć", "Sprawdź limit zapytań — żądań jest dużo."),
        ("nieznany", "Komentarz automatu bez autora."),
    ]
    assert ticket.comments[0].at == datetime(2026, 3, 1, 9, 15, tzinfo=timezone.utc)
    assert ticket.comments[1].at == datetime(
        2026, 3, 2, 18, 45, 30, tzinfo=timezone(timedelta(hours=2))
    )
    assert ticket.comments[0].at.utcoffset() is not None
    assert ticket.comments[1].at.utcoffset() == timedelta(hours=2)
    assert ticket.fetched_at == TERAZ
    assert ticket.raw == odpowiedz["data"]["issue"]


async def test_identyfikator_ticketu_to_czytelny_numer_a_nie_wewnetrzne_uuid_ani_podane_zapytanie():
    # zapytanie malymi literami, odpowiedz niesie identifier "HAR-53" i uuid w polu id
    ticket = await _pobierz(Serwer(_odp(json_=_odpowiedz_fixture())), id="har-53")
    assert ticket.id == "HAR-53"


async def test_status_to_nazwa_stanu_zespolu_a_nie_jego_typ():
    ticket = await _pobierz(Serwer(_odp(json_=_odpowiedz_fixture())))
    assert ticket.status == "W recenzji"
    assert ticket.status != "started"


async def test_priorytet_to_napis_ze_zrodla_a_nie_liczba():
    odpowiedz = _odpowiedz_fixture()
    odpowiedz["data"]["issue"]["priority"] = 1
    odpowiedz["data"]["issue"]["priorityLabel"] = "Pilny"
    ticket = await _pobierz(Serwer(_odp(json_=odpowiedz)))
    assert ticket.priority == "Pilny"


async def test_surowa_odpowiedz_w_raw_to_sam_wezel_zgloszenia_bez_koperty_data():
    odpowiedz = _odpowiedz_fixture()
    ticket = await _pobierz(Serwer(_odp(json_=odpowiedz)))
    assert "data" not in ticket.raw
    assert ticket.raw["identifier"] == "HAR-53"
    assert ticket.raw["priority"] == 2  # oryginal bez normalizacji


async def test_brak_opisu_w_linear_daje_pusty_opis_a_nie_null():
    odpowiedz = _odpowiedz_fixture()
    odpowiedz["data"]["issue"]["description"] = None
    ticket = await _pobierz(Serwer(_odp(json_=odpowiedz)))
    assert ticket.description == ""
    assert ticket.raw["description"] is None


async def test_zgloszenie_bez_stanu_ma_status_pusty():
    odpowiedz = _odpowiedz_fixture()
    odpowiedz["data"]["issue"]["state"] = None
    ticket = await _pobierz(Serwer(_odp(json_=odpowiedz)))
    assert ticket.status is None


async def test_zgloszenie_bez_etykiet_i_komentarzy_ma_puste_listy():
    odpowiedz = _odpowiedz_fixture()
    odpowiedz["data"]["issue"]["labels"] = {"nodes": []}
    odpowiedz["data"]["issue"]["comments"] = {"nodes": []}
    ticket = await _pobierz(Serwer(_odp(json_=odpowiedz)))
    assert (ticket.labels, ticket.comments) == ([], [])


async def test_kolejnosc_etykiet_i_komentarzy_jest_taka_jak_w_zrodle():
    odpowiedz = _odpowiedz_fixture()
    odpowiedz["data"]["issue"]["labels"]["nodes"].reverse()
    odpowiedz["data"]["issue"]["comments"]["nodes"].reverse()
    ticket = await _pobierz(Serwer(_odp(json_=odpowiedz)))
    assert ticket.labels == ["integracje", "backend"]
    assert [k.author for k in ticket.comments] == ["nieznany", "Łukasz Żółć"]


async def test_linear_nie_ma_kryteriow_akceptacji_wiec_lista_jest_pusta_nawet_gdy_opis_ma_checklist():
    odpowiedz = _odpowiedz_fixture()
    odpowiedz["data"]["issue"]["description"] = "Opis\n- [ ] kryterium w tresci"
    ticket = await _pobierz(Serwer(_odp(json_=odpowiedz)))
    assert ticket.acceptance_criteria == []


async def test_bez_wstrzyknietego_czasu_data_pobrania_to_teraz_w_utc():
    przed = datetime.now(timezone.utc)
    ticket = await linear.pobierz(
        "HAR-53", transport=Serwer(_odp(json_=_odpowiedz_fixture())).transport
    )
    po = datetime.now(timezone.utc)
    assert ticket.fetched_at.utcoffset() == timedelta(0)
    assert przed <= ticket.fetched_at <= po


async def test_pobrane_zgloszenie_zapisuje_sie_do_poprawnego_ticket_json():
    ticket = await _pobierz(Serwer(_odp(json_=_odpowiedz_fixture())))
    bajty = do_pliku(ticket)
    wczytany = json.loads(bajty)
    assert list(wczytany.keys()) == KOLEJNOSC
    assert wczytany["source"] == "linear"
    assert wczytany["status"] == "W recenzji"
    assert wczytany["acceptance_criteria"] == []
    assert "Zażółć gęślą jaźń".encode("utf-8") in bajty
    assert TicketJson.model_validate_json(bajty) == ticket


# --- pobierz: wysylane zadanie --------------------------------------------


async def test_zadanie_pobrania_idzie_do_linear_z_samym_kluczem_api_bez_przedrostka():
    serwer = Serwer(_odp(json_=_odpowiedz_fixture()))
    await _pobierz(serwer)
    assert len(serwer.zadania) == 1
    zadanie = serwer.zadania[0]
    assert zadanie.method == "POST"
    assert str(zadanie.url) == "https://api.linear.app/graphql"
    assert zadanie.headers["authorization"] == TOKEN  # bez przedrostka Bearer
    assert "bearer" not in zadanie.headers["authorization"].lower()
    assert zadanie.headers["content-type"].startswith("application/json")


async def test_zadanie_pobrania_niesie_identyfikator_osobno_od_tresci_zapytania():
    serwer = Serwer(_odp(json_=_odpowiedz_fixture()))
    await _pobierz(serwer, id="HAR-777")
    tresc = serwer.tresc(0)
    assert set(tresc) == {"query", "variables"}
    assert tresc["variables"] == {"id": "HAR-777"}
    assert "HAR-777" not in tresc["query"]
    assert "issue(id: $id)" in tresc["query"]


async def test_token_z_pliku_sekretow_jest_uzywany_gdy_nie_ma_zmiennej_srodowiskowej(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("COCKPIT_LINEAR_TOKEN")
    plik = tmp_path / "credentials.json"
    plik.write_text(json.dumps({"linear": "lin_api_Z_PLIKU"}), encoding="utf-8")
    monkeypatch.setattr(secrets, "CONFIG", plik)
    serwer = Serwer(_odp(json_=_odpowiedz_fixture()))
    await _pobierz(serwer)
    assert serwer.zadania[0].headers["authorization"] == "lin_api_Z_PLIKU"


async def test_token_jest_czytany_przy_kazdym_wywolaniu_a_nie_raz_przy_imporcie(monkeypatch):
    serwer = Serwer(_odp(json_=_odpowiedz_fixture()))
    await _pobierz(serwer)
    monkeypatch.setenv("COCKPIT_LINEAR_TOKEN", "lin_api_NOWY")
    await _pobierz(serwer)
    assert [z.headers["authorization"] for z in serwer.zadania] == [TOKEN, "lin_api_NOWY"]


# --- brak tokenu ----------------------------------------------------------


async def test_brak_tokenu_przerywa_pobranie_bez_zadnego_zadania_i_wskazuje_gdzie_go_ustawic(
    bez_tokenu,
):
    serwer = Serwer(_odp(json_=_odpowiedz_fixture()))
    blad = await _kod_bledu(_pobierz(serwer))
    assert (blad.dostawca, blad.kod) == ("Linear", "brak_tokenu")
    assert "credentials.json" in blad.komunikat
    assert "COCKPIT_LINEAR_TOKEN" in blad.komunikat
    assert serwer.zadania == []


async def test_brak_tokenu_przerywa_liste_bez_zadnego_zadania(bez_tokenu):
    serwer = Serwer(_odp(json_=_lista([])))
    blad = await _kod_bledu(_listuj(serwer))
    assert (blad.dostawca, blad.kod) == ("Linear", "brak_tokenu")
    assert serwer.zadania == []


async def test_pusty_token_w_pliku_sekretow_to_brak_tokenu_bez_zadania(monkeypatch, tmp_path):
    monkeypatch.delenv("COCKPIT_LINEAR_TOKEN")
    plik = tmp_path / "credentials.json"
    plik.write_text(json.dumps({"linear": ""}), encoding="utf-8")
    monkeypatch.setattr(secrets, "CONFIG", plik)
    serwer = Serwer(_odp(json_=_odpowiedz_fixture()))
    blad = await _kod_bledu(_pobierz(serwer))
    assert blad.kod == "brak_tokenu"
    assert serwer.zadania == []


async def test_plik_sekretow_bez_klucza_linear_to_brak_tokenu_bez_zadania(monkeypatch, tmp_path):
    monkeypatch.delenv("COCKPIT_LINEAR_TOKEN")
    plik = tmp_path / "credentials.json"
    plik.write_text(json.dumps({"github": "x"}), encoding="utf-8")
    monkeypatch.setattr(secrets, "CONFIG", plik)
    serwer = Serwer(_odp(json_=_odpowiedz_fixture()))
    blad = await _kod_bledu(_pobierz(serwer))
    assert blad.kod == "brak_tokenu"
    assert serwer.zadania == []


async def test_plik_sekretow_z_uszkodzona_trescia_to_brak_tokenu_bez_ujawniania_tresci(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("COCKPIT_LINEAR_TOKEN")
    plik = tmp_path / "credentials.json"
    plik.write_text('{"linear": "lin_api_SEKRET_HAR53" <<<', encoding="utf-8")
    monkeypatch.setattr(secrets, "CONFIG", plik)
    serwer = Serwer(_odp(json_=_odpowiedz_fixture()))
    blad = await _kod_bledu(_pobierz(serwer))
    assert blad.kod == "brak_tokenu"
    assert "nieczytelny" in blad.komunikat
    assert "lin_api_SEKRET_HAR53" not in blad.komunikat
    assert serwer.zadania == []


async def test_plik_sekretow_w_zlym_kodowaniu_to_brak_tokenu_a_nie_awaria(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("COCKPIT_LINEAR_TOKEN")
    plik = tmp_path / "credentials.json"
    plik.write_bytes(b"\xff\xfe\x00\x80")
    monkeypatch.setattr(secrets, "CONFIG", plik)
    serwer = Serwer(_odp(json_=_odpowiedz_fixture()))
    blad = await _kod_bledu(_pobierz(serwer))
    assert blad.kod == "brak_tokenu"
    assert "nieczytelny" in blad.komunikat
    assert serwer.zadania == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root czyta kazdy plik")
async def test_plik_sekretow_bez_praw_odczytu_to_brak_tokenu_a_nie_awaria(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("COCKPIT_LINEAR_TOKEN")
    plik = tmp_path / "credentials.json"
    plik.write_text(json.dumps({"linear": TOKEN}), encoding="utf-8")
    plik.chmod(0o000)
    monkeypatch.setattr(secrets, "CONFIG", plik)
    try:
        serwer = Serwer(_odp(json_=_odpowiedz_fixture()))
        blad = await _kod_bledu(_pobierz(serwer))
    finally:
        plik.chmod(0o600)
    assert blad.kod == "brak_tokenu"
    assert "nieczytelny" in blad.komunikat
    assert serwer.zadania == []


@pytest.mark.parametrize("tresc", ["[]", "null", '"tekst"', "5"], ids=["lista", "null", "napis", "liczba"])
@pytest.mark.parametrize("wywolanie", [_pobierz, _listuj], ids=["pobierz", "listuj"])
async def test_plik_sekretow_bedacy_czytelnym_zapisem_ale_nie_zbiorem_kluczy_to_brak_tokenu_bez_zadania(
    monkeypatch, tmp_path, wywolanie, tresc
):
    monkeypatch.delenv("COCKPIT_LINEAR_TOKEN")
    plik = tmp_path / "credentials.json"
    plik.write_text(tresc, encoding="utf-8")
    monkeypatch.setattr(secrets, "CONFIG", plik)
    serwer = Serwer(_odp(json_=_odpowiedz_fixture()))
    blad = await _kod_bledu(wywolanie(serwer))
    assert (blad.dostawca, blad.kod) == ("Linear", "brak_tokenu")
    assert "nieczytelny" in blad.komunikat
    assert serwer.zadania == []


@pytest.mark.parametrize("wartosc", [123, ["x"], {"a": "b"}], ids=["liczba", "lista", "obiekt"])
@pytest.mark.parametrize("wywolanie", [_pobierz, _listuj], ids=["pobierz", "listuj"])
async def test_token_w_pliku_sekretow_ktory_nie_jest_napisem_to_brak_tokenu_bez_zadania(
    monkeypatch, tmp_path, wywolanie, wartosc
):
    monkeypatch.delenv("COCKPIT_LINEAR_TOKEN")
    plik = tmp_path / "credentials.json"
    plik.write_text(json.dumps({"linear": wartosc}), encoding="utf-8")
    monkeypatch.setattr(secrets, "CONFIG", plik)
    serwer = Serwer(_odp(json_=_odpowiedz_fixture()))
    blad = await _kod_bledu(wywolanie(serwer))
    assert (blad.dostawca, blad.kod) == ("Linear", "brak_tokenu")
    assert "brak tokenu" in blad.komunikat
    assert serwer.zadania == []


async def test_nieczytelny_plik_sekretow_przerywa_tez_liste(monkeypatch, tmp_path):
    monkeypatch.delenv("COCKPIT_LINEAR_TOKEN")
    plik = tmp_path / "credentials.json"
    plik.write_text("to nie jest json", encoding="utf-8")
    monkeypatch.setattr(secrets, "CONFIG", plik)
    serwer = Serwer(_odp(json_=_lista([])))
    blad = await _kod_bledu(_listuj(serwer))
    assert blad.kod == "brak_tokenu"
    assert serwer.zadania == []


# --- pobierz: bledy dostawcy ----------------------------------------------


async def test_blad_polaczenia_to_blad_dostawcy_z_nazwa_klasy_wyjatku_bez_ponowien():
    serwer = Serwer(httpx.ConnectError("nie polaczono"))
    czekanie = Odczekania()
    blad = await _kod_bledu(_pobierz(serwer, czekanie))
    assert (blad.dostawca, blad.kod) == ("Linear", "blad_dostawcy")
    assert blad.komunikat == "Linear: błąd połączenia (ConnectError)"
    assert (len(serwer.zadania), czekanie.wartosci) == (1, [])


async def test_przekroczenie_czasu_oczekiwania_to_blad_dostawcy_a_nie_surowa_awaria():
    blad = await _kod_bledu(_pobierz(Serwer(httpx.ReadTimeout("wolno"))))
    assert blad.kod == "blad_dostawcy"
    assert "ReadTimeout" in blad.komunikat


async def test_odrzucony_token_daje_blad_z_nazwa_dostawcy_i_miejscem_poprawki():
    serwer = Serwer(_odp(401, json_={"error": "Unauthorized"}))
    blad = await _kod_bledu(_pobierz(serwer))
    assert (blad.dostawca, blad.kod) == ("Linear", "token_odrzucony")
    assert blad.komunikat == KOMUNIKAT_TOKENU
    assert "Linear" in blad.komunikat
    assert "credentials.json" in blad.komunikat
    assert "COCKPIT_LINEAR_TOKEN" in blad.komunikat
    assert len(serwer.zadania) == 1  # token nie poprawi sie sam: bez ponowien


async def test_odrzucony_token_jest_rozpoznawany_takze_gdy_odpowiedz_jest_nieczytelna():
    blad = await _kod_bledu(_pobierz(Serwer(_odp(401, tresc=b"<html>nie json</html>"))))
    assert blad.kod == "token_odrzucony"


async def test_limit_zapytan_z_czasem_odczekania_od_dostawcy_czeka_tyle_sekund_i_ponawia():
    serwer = Serwer(
        _odp(429, tresc=b"", naglowki={"Retry-After": "7"}),
        _odp(json_=_odpowiedz_fixture()),
    )
    czekanie = Odczekania()
    ticket = await _pobierz(serwer, czekanie)
    assert ticket.id == "HAR-53"
    assert czekanie.wartosci == [7.0]
    assert len(serwer.zadania) == 2


async def test_limit_zapytan_bez_retry_after_czeka_domyslna_sekunde():
    serwer = Serwer(_odp(429, tresc=b""), _odp(json_=_odpowiedz_fixture()))
    czekanie = Odczekania()
    await _pobierz(serwer, czekanie)
    assert czekanie.wartosci == [1.0]


@pytest.mark.parametrize("retry_after", ["jutro", "-5", "", "inf", "nan", "Infinity"])
async def test_nieliczbowy_nieskonczony_lub_ujemny_czas_odczekania_jest_ignorowany_i_czeka_domyslna_sekunde(
    retry_after,
):
    serwer = Serwer(
        _odp(429, tresc=b"", naglowki={"Retry-After": retry_after}),
        _odp(json_=_odpowiedz_fixture()),
    )
    czekanie = Odczekania()
    await _pobierz(serwer, czekanie)
    assert czekanie.wartosci == [1.0]


async def test_zerowy_czas_odczekania_od_dostawcy_jest_respektowany_a_nie_zastepowany_domyslnym():
    serwer = Serwer(
        _odp(429, tresc=b"", naglowki={"Retry-After": "0"}),
        _odp(json_=_odpowiedz_fixture()),
    )
    czekanie = Odczekania()
    await _pobierz(serwer, czekanie)
    assert czekanie.wartosci == [0.0]


async def test_bardzo_dlugi_czas_odczekania_od_dostawcy_jest_ucinany_do_minuty():
    serwer = Serwer(
        _odp(429, tresc=b"", naglowki={"Retry-After": "3600"}),
        _odp(json_=_odpowiedz_fixture()),
    )
    czekanie = Odczekania()
    await _pobierz(serwer, czekanie)
    assert czekanie.wartosci == [60.0]


async def test_limit_zapytan_dwa_razy_a_potem_sukces_daje_zgloszenie_po_trzech_zadaniach():
    serwer = Serwer(
        _odp(429, tresc=b"", naglowki={"Retry-After": "2"}),
        _odp(429, tresc=b"<html>zwolnij</html>"),
        _odp(json_=_odpowiedz_fixture()),
    )
    czekanie = Odczekania()
    ticket = await _pobierz(serwer, czekanie)
    assert ticket.id == "HAR-53"
    assert len(serwer.zadania) == 3
    assert czekanie.wartosci == [2.0, 1.0]


async def test_ciagly_limit_zapytan_daje_blad_limit_po_trzech_zadaniach_z_nazwa_dostawcy():
    serwer = Serwer(_odp(429, tresc=b""))
    czekanie = Odczekania()
    blad = await _kod_bledu(_pobierz(serwer, czekanie))
    assert (blad.dostawca, blad.kod) == ("Linear", "limit")
    assert "Linear" in blad.komunikat
    assert "3" in blad.komunikat
    assert len(serwer.zadania) == 3
    assert len(czekanie.wartosci) == 2


async def test_limit_zgloszony_w_tresci_odpowiedzi_jest_ponawiany_po_czasie_podanym_przez_dostawce():
    serwer = Serwer(
        _odp(400, json_=_blad_graphql("Rate limit exceeded", "RATELIMITED"),
             naglowki={"Retry-After": "3"}),
        _odp(json_=_odpowiedz_fixture()),
    )
    czekanie = Odczekania()
    ticket = await _pobierz(serwer, czekanie)
    assert ticket.id == "HAR-53"
    assert czekanie.wartosci == [3.0]
    assert len(serwer.zadania) == 2


async def test_limit_zgloszony_obok_innego_bledu_dostawcy_tez_jest_rozpoznany():
    odpowiedz = {
        "errors": [
            {"message": "cos innego", "extensions": {"code": "INNE"}},
            {"message": "Rate limit", "extensions": {"code": "RATELIMITED"}},
        ]
    }
    serwer = Serwer(_odp(400, json_=odpowiedz), _odp(json_=_odpowiedz_fixture()))
    ticket = await _pobierz(serwer)
    assert ticket.id == "HAR-53"
    assert len(serwer.zadania) == 2


async def test_ciagly_limit_zgloszony_w_tresci_odpowiedzi_daje_blad_limit_po_trzech_zadaniach():
    serwer = Serwer(_odp(400, json_=_blad_graphql("Rate limit exceeded", "RATELIMITED")))
    blad = await _kod_bledu(_pobierz(serwer))
    assert blad.kod == "limit"
    assert len(serwer.zadania) == 3


@pytest.mark.parametrize("status", [200, 400])
async def test_odrzucenie_tokenu_zgloszone_w_tresci_odpowiedzi_to_odrzucony_token_bez_ponowien(status):
    serwer = Serwer(
        _odp(status, json_=_blad_graphql("You need to authenticate", "AUTHENTICATION_ERROR"))
    )
    blad = await _kod_bledu(_pobierz(serwer))
    assert (blad.dostawca, blad.kod) == ("Linear", "token_odrzucony")
    assert blad.komunikat == KOMUNIKAT_TOKENU
    assert len(serwer.zadania) == 1


async def test_nieistniejace_zgloszenie_zgloszone_w_tresci_odpowiedzi_to_nie_znaleziono_z_numerem():
    serwer = Serwer(_odp(200, json_=_blad_graphql("Entity not found: Issue - HAR-999")))
    blad = await _kod_bledu(_pobierz(serwer, id="HAR-999"))
    assert (blad.dostawca, blad.kod) == ("Linear", "nie_znaleziono")
    assert blad.komunikat == "Linear: nie znaleziono zgłoszenia HAR-999."
    assert len(serwer.zadania) == 1


async def test_komunikat_o_braku_encji_w_srodku_zdania_nie_oznacza_braku_zgloszenia():
    serwer = Serwer(_odp(200, json_=_blad_graphql("Blad wewnetrzny: Entity not found w cache")))
    blad = await _kod_bledu(_pobierz(serwer))
    assert blad.kod == "blad_dostawcy"


async def test_zgloszenie_ktorego_dostawca_nie_zwrocil_to_nie_znaleziono_z_numerem():
    serwer = Serwer(_odp(200, json_={"data": {"issue": None}}))
    blad = await _kod_bledu(_pobierz(serwer, id="HAR-404"))
    assert blad.kod == "nie_znaleziono"
    assert blad.komunikat == "Linear: nie znaleziono zgłoszenia HAR-404."


@pytest.mark.parametrize(
    "odpowiedz",
    [
        _blad_graphql("Argument Validation Error"),
        _blad_graphql("Internal server error", "INTERNAL_SERVER_ERROR"),
    ],
    ids=["bez_rozszerzen", "z_nieznanym_kodem"],
)
async def test_inny_blad_zapytania_to_blad_dostawcy_nawet_gdy_zgloszenia_nie_ma_w_danych(odpowiedz):
    # `issue: null` przy bledzie nie moze zostac uznane za "nie znaleziono"
    pelna = {**odpowiedz, "data": {"issue": None}}
    serwer = Serwer(_odp(200, json_=pelna))
    blad = await _kod_bledu(_pobierz(serwer))
    assert (blad.dostawca, blad.kod) == ("Linear", "blad_dostawcy")
    assert len(serwer.zadania) == 1


async def test_blad_zapytania_przy_pelnych_danych_nie_jest_zielonym_wynikiem():
    odpowiedz = _odpowiedz_fixture()
    odpowiedz["errors"] = [{"message": "Czesciowa awaria"}]
    blad = await _kod_bledu(_pobierz(Serwer(_odp(200, json_=odpowiedz))))
    assert blad.kod == "blad_dostawcy"


async def test_pusta_lista_bledow_od_dostawcy_nie_przeszkadza_w_pobraniu():
    odpowiedz = _odpowiedz_fixture()
    odpowiedz["errors"] = []
    ticket = await _pobierz(Serwer(_odp(200, json_=odpowiedz)))
    assert ticket.id == "HAR-53"


@pytest.mark.parametrize("status", [403, 500, 502])
async def test_status_odpowiedzi_inny_niz_sukces_to_blad_dostawcy_nawet_gdy_tresc_wyglada_poprawnie(
    status,
):
    serwer = Serwer(_odp(status, json_=_odpowiedz_fixture()))
    blad = await _kod_bledu(_pobierz(serwer))
    assert (blad.dostawca, blad.kod) == ("Linear", "blad_dostawcy")
    assert str(status) in blad.komunikat
    assert len(serwer.zadania) == 1


@pytest.mark.parametrize(
    "tresc", [b"<html>to nie json</html>", b"", b"[]", b'"tekst"', b"null", b"42"]
)
async def test_odpowiedz_bez_obiektu_danych_to_blad_dostawcy(tresc):
    blad = await _kod_bledu(_pobierz(Serwer(_odp(200, tresc=tresc))))
    assert (blad.dostawca, blad.kod) == ("Linear", "blad_dostawcy")


def _bez(klucz_sciezka):
    def zmien(odpowiedz):
        wezel = odpowiedz
        for klucz in klucz_sciezka[:-1]:
            wezel = wezel[klucz]
        del wezel[klucz_sciezka[-1]]
        return odpowiedz

    return zmien


def _ustaw(klucz_sciezka, wartosc):
    def zmien(odpowiedz):
        wezel = odpowiedz
        for klucz in klucz_sciezka[:-1]:
            wezel = wezel[klucz]
        wezel[klucz_sciezka[-1]] = wartosc
        return odpowiedz

    return zmien


@pytest.mark.parametrize(
    "zmiana",
    [
        lambda o: {},
        _ustaw(["data"], None),
        _bez(["data", "issue", "state"]),
        _ustaw(["data", "issue", "state"], "W recenzji"),
        _bez(["data", "issue", "state", "name"]),
        _bez(["data", "issue", "identifier"]),
        _bez(["data", "issue", "title"]),
        _bez(["data", "issue", "labels"]),
        _ustaw(["data", "issue", "labels"], ["backend"]),
        _bez(["data", "issue", "comments"]),
        _ustaw(["data", "issue", "comments", "nodes"], [{"body": "x", "user": None}]),
        _ustaw(["data", "issue", "comments", "nodes"], [{"body": "x", "createdAt": "wczoraj",
                                                          "user": None}]),
        _ustaw(["data", "issue", "title"], 123),
        _ustaw(["data"], "tekst"),
        _ustaw(["data", "issue"], "tekst"),
        _ustaw(["data", "issue", "labels", "nodes"], None),
        _ustaw(["data", "issue", "comments", "nodes"], None),
        _ustaw(["data", "issue", "comments", "nodes"],
               [{"body": "x", "createdAt": "2026-03-01T09:15:00Z", "user": "tekst"}]),
    ],
    ids=[
        "pusty_obiekt",
        "data_null",
        "brak_stanu",
        "stan_jako_napis",
        "stan_bez_nazwy",
        "brak_identyfikatora",
        "brak_tytulu",
        "brak_etykiet",
        "etykiety_jako_lista",
        "brak_komentarzy",
        "komentarz_bez_daty_utworzenia",
        "komentarz_z_data_spoza_formatu",
        "tytul_jako_liczba",
        "dane_jako_napis",
        "zgloszenie_jako_napis",
        "etykiety_bez_listy_wezlow",
        "komentarze_bez_listy_wezlow",
        "autor_komentarza_jako_napis",
    ],
)
async def test_zgloszenie_o_niespodziewanej_budowie_to_czytelny_blad_dostawcy(
    zmiana,
):
    odpowiedz = zmiana(_odpowiedz_fixture())
    blad = await _kod_bledu(_pobierz(Serwer(_odp(200, json_=odpowiedz))))
    assert (blad.dostawca, blad.kod) == ("Linear", "blad_dostawcy")


# --- nic nie wycieka -------------------------------------------------------


def _scenariusze_bledow():
    sekret_w_tresci = {"debug": {"authorization": TOKEN}}
    return {
        "401": (Serwer(_odp(401, json_=sekret_w_tresci)), "token_odrzucony"),
        "AUTHENTICATION_ERROR": (
            Serwer(_odp(200, json_={**_blad_graphql(f"token {TOKEN}", "AUTHENTICATION_ERROR")})),
            "token_odrzucony",
        ),
        "429_ciagle": (Serwer(_odp(429, json_=sekret_w_tresci)), "limit"),
        "500": (Serwer(_odp(500, json_=sekret_w_tresci)), "blad_dostawcy"),
        "blad_graphql": (
            Serwer(_odp(200, json_=_blad_graphql(f"zly token {TOKEN}"))),
            "blad_dostawcy",
        ),
        "polaczenie": (
            Serwer(httpx.ConnectError(f"nie polaczono z naglowkiem {TOKEN}")),
            "blad_dostawcy",
        ),
    }


@pytest.mark.parametrize("nazwa", list(_scenariusze_bledow()))
async def test_token_nie_wycieka_do_bledu_ani_na_wyjscie_w_zadnej_sciezce_bledu(nazwa, capsys):
    serwer, kod = _scenariusze_bledow()[nazwa]
    blad = await _kod_bledu(_pobierz(serwer))
    assert blad.kod == kod
    wyjscie = capsys.readouterr()
    for tekst in (str(blad), repr(blad), blad.komunikat, wyjscie.out, wyjscie.err):
        assert TOKEN not in tekst
        assert "lin_api_" not in tekst


async def test_udane_pobranie_niczego_nie_wypisuje(capsys):
    await _pobierz(Serwer(_odp(json_=_odpowiedz_fixture())))
    wyjscie = capsys.readouterr()
    assert (wyjscie.out, wyjscie.err) == ("", "")


@pytest.mark.parametrize("nazwa", ["401", "429_ciagle", "500", "polaczenie"])
async def test_token_nie_wycieka_takze_z_listy(nazwa, capsys):
    serwer, kod = _scenariusze_bledow()[nazwa]
    blad = await _kod_bledu(_listuj(serwer))
    assert blad.kod == kod
    wyjscie = capsys.readouterr()
    for tekst in (str(blad), repr(blad), blad.komunikat, wyjscie.out, wyjscie.err):
        assert TOKEN not in tekst


# --- listuj ----------------------------------------------------------------


async def test_lista_po_typie_stanu_filtruje_po_typie_a_nie_po_nazwie_stanu():
    serwer = Serwer(_odp(json_=_lista([_wezel("HAR-1", "Pierwsze", "Do zrobienia")])))
    await _listuj(serwer, typy=["unstarted"])
    assert len(serwer.zadania) == 1
    tresc = serwer.tresc(0)
    assert tresc["variables"]["typy"] == ["unstarted"]
    assert "state: { type: { in:" in tresc["query"]
    assert "name" not in tresc["query"].split("filter")[1].split("nodes")[0]
    assert "state: { name" not in tresc["query"]


async def test_lista_przekazuje_wszystkie_wybrane_typy_stanu_w_podanej_kolejnosci():
    serwer = Serwer(_odp(json_=_lista([])))
    await _listuj(serwer, typy=["backlog", "started", "unstarted"])
    assert serwer.tresc(0)["variables"]["typy"] == ["backlog", "started", "unstarted"]


async def test_pierwsza_strona_listy_nie_ma_kursora():
    serwer = Serwer(_odp(json_=_lista([])))
    await _listuj(serwer)
    zmienne = serwer.tresc(0)["variables"]
    assert set(zmienne) == {"typy", "po"}
    assert zmienne["po"] is None


async def test_zadanie_listy_idzie_do_linear_z_samym_kluczem_api():
    serwer = Serwer(_odp(json_=_lista([])))
    await _listuj(serwer)
    zadanie = serwer.zadania[0]
    assert (zadanie.method, str(zadanie.url)) == ("POST", "https://api.linear.app/graphql")
    assert zadanie.headers["authorization"] == TOKEN


async def test_lista_zwraca_numer_tytul_i_nazwe_stanu_w_kolejnosci_zrodla():
    serwer = Serwer(
        _odp(
            json_=_lista(
                [
                    _wezel("HAR-9", "Zażółć gęślą", "Do zrobienia"),
                    _wezel("HAR-2", "Drugie", "Gotowe do pracy"),
                ]
            )
        )
    )
    wynik = await _listuj(serwer)
    assert wynik == [
        {"id": "HAR-9", "tytul": "Zażółć gęślą", "stan": "Do zrobienia"},
        {"id": "HAR-2", "tytul": "Drugie", "stan": "Gotowe do pracy"},
    ]


async def test_lista_bez_zgloszen_w_tym_stanie_jest_pusta_ale_po_prawdziwym_zapytaniu():
    serwer = Serwer(_odp(json_=_lista([])))
    assert await _listuj(serwer) == []
    assert len(serwer.zadania) == 1


async def test_pusta_lista_typow_stanu_daje_pusty_wynik_bez_zadania():
    serwer = Serwer(_odp(json_=_lista([_wezel("HAR-1")])))
    assert await _listuj(serwer, typy=[]) == []
    assert serwer.zadania == []


async def test_lista_dociaga_kolejna_strone_z_kursorem_i_skleja_wyniki_w_kolejnosci():
    serwer = Serwer(
        _odp(json_=_lista([_wezel("HAR-1"), _wezel("HAR-2")], ma_nastepna=True, kursor="kursor-1")),
        _odp(json_=_lista([_wezel("HAR-3")], ma_nastepna=False, kursor="kursor-2")),
    )
    wynik = await _listuj(serwer)
    assert [w["id"] for w in wynik] == ["HAR-1", "HAR-2", "HAR-3"]
    assert len(serwer.zadania) == 2
    assert serwer.tresc(0)["variables"]["po"] is None
    assert serwer.tresc(1)["variables"]["po"] == "kursor-1"
    assert serwer.tresc(1)["variables"]["typy"] == ["unstarted"]


async def test_lista_przechodzi_wiele_stron_za_kursorem_z_poprzedniej_strony():
    serwer = Serwer(
        _odp(json_=_lista([_wezel("HAR-1")], ma_nastepna=True, kursor="a")),
        _odp(json_=_lista([_wezel("HAR-2")], ma_nastepna=True, kursor="b")),
        _odp(json_=_lista([_wezel("HAR-3")], ma_nastepna=False, kursor="c")),
    )
    wynik = await _listuj(serwer)
    assert [w["id"] for w in wynik] == ["HAR-1", "HAR-2", "HAR-3"]
    assert [serwer.tresc(i)["variables"]["po"] for i in range(3)] == [None, "a", "b"]


async def test_lista_konczy_na_stronie_bez_nastepnej_nawet_gdy_kursor_jest_ustawiony():
    serwer = Serwer(
        _odp(json_=_lista([_wezel("HAR-1")], ma_nastepna=False, kursor="zostaly-slady")),
        _odp(json_=_lista([_wezel("HAR-2")])),
    )
    wynik = await _listuj(serwer)
    assert [w["id"] for w in wynik] == ["HAR-1"]
    assert len(serwer.zadania) == 1


async def test_lista_z_dokladnie_maksymalna_liczba_stron_jest_kompletna(monkeypatch):
    monkeypatch.setattr(linear, "MAKS_STRON", 2)
    serwer = Serwer(
        _odp(json_=_lista([_wezel("HAR-1")], ma_nastepna=True, kursor="a")),
        _odp(json_=_lista([_wezel("HAR-2")], ma_nastepna=False, kursor="b")),
    )
    wynik = await _listuj(serwer)
    assert [w["id"] for w in wynik] == ["HAR-1", "HAR-2"]
    assert len(serwer.zadania) == 2


async def test_lista_dluzsza_niz_maksymalna_liczba_stron_to_blad_a_nie_ucieta_lista(monkeypatch):
    monkeypatch.setattr(linear, "MAKS_STRON", 2)
    serwer = Serwer(
        _odp(json_=_lista([_wezel("HAR-1")], ma_nastepna=True, kursor="a")),
        _odp(json_=_lista([_wezel("HAR-2")], ma_nastepna=True, kursor="b")),
        _odp(json_=_lista([_wezel("HAR-3")], ma_nastepna=False, kursor="c")),
    )
    blad = await _kod_bledu(_listuj(serwer))
    assert (blad.dostawca, blad.kod) == ("Linear", "blad_dostawcy")
    assert len(serwer.zadania) == 2


async def test_odrzucony_token_przy_liscie_to_blad_a_nie_pusta_lista():
    serwer = Serwer(_odp(401, json_={"error": "Unauthorized"}))
    blad = await _kod_bledu(_listuj(serwer))
    assert (blad.dostawca, blad.kod) == ("Linear", "token_odrzucony")
    assert blad.komunikat == KOMUNIKAT_TOKENU


async def test_odrzucenie_tokenu_w_tresci_odpowiedzi_przy_liscie_to_odrzucony_token():
    serwer = Serwer(_odp(200, json_=_blad_graphql("auth", "AUTHENTICATION_ERROR")))
    blad = await _kod_bledu(_listuj(serwer))
    assert blad.kod == "token_odrzucony"


async def test_lista_po_limicie_zapytan_dwa_razy_a_potem_sukces_zwraca_wynik():
    serwer = Serwer(
        _odp(429, tresc=b"", naglowki={"Retry-After": "4"}),
        _odp(429, tresc=b""),
        _odp(json_=_lista([_wezel("HAR-1")])),
    )
    czekanie = Odczekania()
    wynik = await _listuj(serwer, czekanie)
    assert [w["id"] for w in wynik] == ["HAR-1"]
    assert len(serwer.zadania) == 3
    assert czekanie.wartosci == [4.0, 1.0]


async def test_ciagly_limit_zapytan_przy_liscie_daje_blad_limit_po_trzech_zadaniach():
    serwer = Serwer(_odp(429, tresc=b""))
    czekanie = Odczekania()
    blad = await _kod_bledu(_listuj(serwer, czekanie))
    assert (blad.dostawca, blad.kod) == ("Linear", "limit")
    assert len(serwer.zadania) == 3
    assert len(czekanie.wartosci) == 2


async def test_limit_zapytan_jest_liczony_osobno_dla_kazdej_strony_listy():
    # lacznie trzy odmowy, ale zadna strona nie przekracza trzech prob
    serwer = Serwer(
        _odp(429, tresc=b""),
        _odp(json_=_lista([_wezel("HAR-1")], ma_nastepna=True, kursor="a")),
        _odp(429, tresc=b""),
        _odp(429, tresc=b""),
        _odp(json_=_lista([_wezel("HAR-2")])),
    )
    wynik = await _listuj(serwer)
    assert [w["id"] for w in wynik] == ["HAR-1", "HAR-2"]
    assert len(serwer.zadania) == 5


async def test_limit_przy_drugiej_stronie_listy_nie_zwraca_polowy_wyniku():
    serwer = Serwer(
        _odp(json_=_lista([_wezel("HAR-1")], ma_nastepna=True, kursor="a")),
        _odp(429, tresc=b""),
    )
    blad = await _kod_bledu(_listuj(serwer))
    assert blad.kod == "limit"
    assert len(serwer.zadania) == 4  # strona 1 + trzy proby strony 2


async def test_blad_polaczenia_przy_liscie_to_blad_dostawcy():
    blad = await _kod_bledu(_listuj(Serwer(httpx.ConnectError("x"))))
    assert blad.kod == "blad_dostawcy"
    assert "ConnectError" in blad.komunikat


async def test_inny_status_przy_liscie_to_blad_dostawcy_z_kodem():
    blad = await _kod_bledu(_listuj(Serwer(_odp(500, json_=_lista([_wezel("HAR-1")])))))
    assert blad.kod == "blad_dostawcy"
    assert "500" in blad.komunikat


async def test_inny_blad_zapytania_przy_liscie_to_blad_dostawcy():
    blad = await _kod_bledu(_listuj(Serwer(_odp(200, json_=_blad_graphql("Zle zapytanie")))))
    assert blad.kod == "blad_dostawcy"


async def test_nieczytelna_odpowiedz_listy_to_blad_dostawcy():
    blad = await _kod_bledu(_listuj(Serwer(_odp(200, tresc=b"<html>"))))
    assert blad.kod == "blad_dostawcy"


@pytest.mark.parametrize(
    "odpowiedz",
    [
        {"data": {}},
        {"data": None},
        {"data": {"issues": {"nodes": [_wezel("HAR-1")]}}},
        {"data": {"issues": {"nodes": [{"identifier": "HAR-1", "title": "T"}],
                             "pageInfo": {"hasNextPage": False, "endCursor": None}}}},
        {"data": {"issues": {"nodes": [{"identifier": "HAR-1", "title": "T", "state": "x"}],
                             "pageInfo": {"hasNextPage": False, "endCursor": None}}}},
        {"data": "tekst"},
        {"data": {"issues": {"nodes": None,
                             "pageInfo": {"hasNextPage": False, "endCursor": None}}}},
        {"data": {"issues": {"nodes": [], "pageInfo": None}}},
    ],
    ids=[
        "brak_issues",
        "data_null",
        "brak_page_info",
        "wezel_bez_stanu",
        "stan_jako_napis",
        "dane_jako_napis",
        "lista_wezlow_null",
        "informacje_o_stronie_null",
    ],
)
async def test_lista_o_niespodziewanej_budowie_to_czytelny_blad_dostawcy(
    odpowiedz,
):
    blad = await _kod_bledu(_listuj(Serwer(_odp(200, json_=copy.deepcopy(odpowiedz)))))
    assert (blad.dostawca, blad.kod) == ("Linear", "blad_dostawcy")


# --- dokladne komunikaty bledow i limit czasu (mutanty HAR-53) -------------

KOMUNIKAT_NIE_JSON = "Linear: odpowiedź nie jest poprawnym JSON-em."
KOMUNIKAT_NIE_OBIEKT = "Linear: odpowiedź nie jest obiektem JSON."
KOMUNIKAT_GRAPHQL = "Linear: błąd zapytania GraphQL."
KOMUNIKAT_STRUKTURA = "Linear: odpowiedź ma nieoczekiwaną strukturę."


@pytest.mark.parametrize("wywolanie", [_pobierz, _listuj], ids=["pobierz", "listuj"])
async def test_nieczytelna_odpowiedz_dostawcy_daje_blad_z_czytelnym_komunikatem(wywolanie):
    blad = await _kod_bledu(wywolanie(Serwer(_odp(200, tresc=b"<html>to nie json</html>"))))
    assert (blad.dostawca, blad.kod) == ("Linear", "blad_dostawcy")
    assert blad.komunikat == KOMUNIKAT_NIE_JSON


@pytest.mark.parametrize("tresc", [b"[]", b'"tekst"', b"null", b"42"])
@pytest.mark.parametrize("wywolanie", [_pobierz, _listuj], ids=["pobierz", "listuj"])
async def test_odpowiedz_dostawcy_bez_obiektu_danych_daje_czytelny_komunikat(
    wywolanie, tresc
):
    blad = await _kod_bledu(wywolanie(Serwer(_odp(200, tresc=tresc))))
    assert (blad.dostawca, blad.kod) == ("Linear", "blad_dostawcy")
    assert blad.komunikat == KOMUNIKAT_NIE_OBIEKT


@pytest.mark.parametrize(
    "bledy",
    ["zepsute", {"message": "x"}, 5, True, 3.5],
    ids=[
        "errors_jako_napis",
        "errors_jako_obiekt",
        "errors_jako_liczba_calkowita",
        "errors_jako_prawda",
        "errors_jako_liczba_zmiennoprzecinkowa",
    ],
)
@pytest.mark.parametrize("wywolanie", [_pobierz, _listuj], ids=["pobierz", "listuj"])
async def test_zepsuta_lista_bledow_od_dostawcy_daje_czytelny_blad_bez_ponowien(
    wywolanie, bledy
):
    serwer = Serwer(_odp(200, json_={"errors": bledy}))
    blad = await _kod_bledu(wywolanie(serwer))
    assert isinstance(blad, BladIntegracji)
    assert blad.kod == "blad_dostawcy"
    assert blad.komunikat == KOMUNIKAT_GRAPHQL
    assert len(serwer.zadania) == 1


@pytest.mark.parametrize(
    "odpowiedz",
    [_blad_graphql("Argument Validation Error"), _blad_graphql("Boom", "INNE")],
    ids=["bez_rozszerzen", "z_nieznanym_kodem"],
)
@pytest.mark.parametrize("wywolanie", [_pobierz, _listuj], ids=["pobierz", "listuj"])
async def test_zwykla_lista_bledow_dostawcy_daje_ten_sam_czytelny_komunikat(wywolanie, odpowiedz):
    blad = await _kod_bledu(wywolanie(Serwer(_odp(200, json_=odpowiedz))))
    assert blad.kod == "blad_dostawcy"
    assert blad.komunikat == KOMUNIKAT_GRAPHQL


@pytest.mark.parametrize(
    "bledy",
    [
        ["tekst"],
        [{"extensions": {"code": "INNE"}}],
        [{"message": 5}],
        [{"message": "x", "extensions": "zle"}],
        [{"message": "x", "extensions": None}],
        [{"message": "x", "extensions": {"code": ["RATELIMITED"]}}],
    ],
    ids=[
        "element_nie_jest_obiektem",
        "element_bez_komunikatu",
        "komunikat_nie_jest_napisem",
        "rozszerzenia_nie_sa_obiektem",
        "rozszerzenia_puste",
        "kod_nie_jest_napisem",
    ],
)
@pytest.mark.parametrize("wywolanie", [_pobierz, _listuj], ids=["pobierz", "listuj"])
async def test_zepsuty_element_listy_bledow_dostawcy_daje_czytelny_blad_bez_ponowien(
    wywolanie, bledy
):
    serwer = Serwer(_odp(200, json_={"errors": bledy}))
    blad = await _kod_bledu(wywolanie(serwer))
    assert (blad.dostawca, blad.kod) == ("Linear", "blad_dostawcy")
    assert blad.komunikat == KOMUNIKAT_GRAPHQL
    assert len(serwer.zadania) == 1


async def test_pobrane_zgloszenie_o_zlej_strukturze_ma_czytelny_komunikat():
    for odpowiedz in ({}, _bez(["data", "issue", "title"])(_odpowiedz_fixture())):
        blad = await _kod_bledu(_pobierz(Serwer(_odp(200, json_=odpowiedz))))
        assert blad.kod == "blad_dostawcy"
        assert blad.komunikat == KOMUNIKAT_STRUKTURA


async def test_lista_o_zlej_strukturze_ma_czytelny_komunikat():
    blad = await _kod_bledu(_listuj(Serwer(_odp(200, json_={"data": {}}))))
    assert blad.kod == "blad_dostawcy"
    assert blad.komunikat == KOMUNIKAT_STRUKTURA


async def test_przekroczenie_maksymalnej_liczby_stron_mowi_ile_stron_i_ze_lista_nie_jest_zwracana(
    monkeypatch,
):
    monkeypatch.setattr(linear, "MAKS_STRON", 2)
    serwer = Serwer(_odp(json_=_lista([_wezel("HAR-1")], ma_nastepna=True, kursor="a")))
    blad = await _kod_bledu(_listuj(serwer))
    assert blad.kod == "blad_dostawcy"
    assert blad.komunikat == (
        "Linear: lista dłuższa niż 2 stron; nie zwracam niepełnej listy."
    )


def _limity_czasu(zadanie: httpx.Request) -> dict:
    return zadanie.extensions["timeout"]


async def test_pobranie_zgloszenia_ma_ustawiony_limit_czasu_na_kazdy_etap_polaczenia():
    serwer = Serwer(_odp(json_=_odpowiedz_fixture()))
    await _pobierz(serwer)
    oczekiwany = dict.fromkeys(("connect", "read", "write", "pool"), linear.TIMEOUT_S)
    assert _limity_czasu(serwer.zadania[0]) == oczekiwany


async def test_lista_ma_ustawiony_limit_czasu_na_kazdy_etap_polaczenia():
    serwer = Serwer(_odp(json_=_lista([])))
    await _listuj(serwer)
    oczekiwany = dict.fromkeys(("connect", "read", "write", "pool"), linear.TIMEOUT_S)
    assert _limity_czasu(serwer.zadania[0]) == oczekiwany
