"""Adapter Jira: Cloud i Data Center z konfiguracji, JQL, pole kryteriow (HAR-63)."""

from __future__ import annotations

import base64
import copy
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pydantic
import pytest

from cockpit.services.errors import BladDziedziny
from runner import secrets
from runner.tickety import TicketJson, do_pliku, jira
from runner.tickety.bledy import BladIntegracji

TOKEN = "jira_SEKRET_HAR63"
EMAIL = "ktos@firma.example"
BASIC_BASE64 = "a3Rvc0BmaXJtYS5leGFtcGxlOmppcmFfU0VLUkVUX0hBUjYz"
BASIC = f"Basic {BASIC_BASE64}"
BEARER = f"Bearer {TOKEN}"

TERAZ = datetime(2026, 3, 4, 12, 30, 15, tzinfo=timezone.utc)
KATALOG = Path(__file__).parent / "fixtures" / "tickety"

ADRES_CLOUD = "https://firma.atlassian.net"
ADRES_DC = "https://jira.firma.local"
ADRESY = {"cloud": ADRES_CLOUD, "data_center": ADRES_DC}
BAZA_API = {"cloud": "/rest/api/3", "data_center": "/rest/api/2"}
ENDPOINT_LISTY = {"cloud": "/rest/api/3/search/jql", "data_center": "/rest/api/2/search"}
POLE = "customfield_10050"
TRYBY = ["cloud", "data_center"]

JQL = 'project = "AB C" AND labels in (x, "y z") ORDER BY created DESC'
JQL_ZNAKI = 'summary ~ "a&b+c%20#d=e" AND text ~ "zażółć gęślą" OR key = AB-1'

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

KRYTERIA_ADF = ["Lista zwraca klucz, tytuł i stan", "Błędny token daje czytelny komunikat"]
KRYTERIA_DC = [*KRYTERIA_ADF, "- zaczyna się od myślnika"]


# --- pomocnicy -------------------------------------------------------------


def _issue(tryb: str) -> dict:
    nazwa = "jira_cloud_issue.json" if tryb == "cloud" else "jira_dc_issue.json"
    return json.loads((KATALOG / nazwa).read_text(encoding="utf-8"))


def _konf(tryb: str, **reszta) -> jira.KonfiguracjaJira:
    adres = reszta.pop("adres", ADRESY[tryb])
    return jira.konfiguracja({"tryb": tryb, "adres": adres, **reszta})


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


class Odczekania:
    """Wstrzykiwane `odczekaj`: zapisuje argumenty, nigdy nie spi."""

    def __init__(self):
        self.wartosci: list[float] = []

    async def __call__(self, sekundy: float) -> None:
        self.wartosci.append(sekundy)


async def _pobierz(serwer, konf, odczekania=None, klucz="AB-7"):
    return await jira.pobierz(
        klucz,
        konf,
        transport=serwer.transport,
        odczekaj=odczekania if odczekania is not None else Odczekania(),
        teraz=TERAZ,
    )


async def _ticket(tryb, issue=None, **konf):
    odpowiedz = issue if issue is not None else _issue(tryb)
    ticket, _ostrzezenia = await _pobierz(Serwer(_odp(json_=odpowiedz)), _konf(tryb, **konf))
    return ticket


async def _listuj(serwer, konf, odczekania=None):
    return await jira.listuj(
        konf,
        transport=serwer.transport,
        odczekaj=odczekania if odczekania is not None else Odczekania(),
    )


async def _kod_bledu(korutyna) -> BladIntegracji:
    with pytest.raises(BladIntegracji) as exc:
        await korutyna
    assert type(exc.value) is BladIntegracji
    assert exc.value.dostawca == "Jira"
    return exc.value


def _wpis(klucz, tytul="Tytul", stan="Do zrobienia"):
    return {"id": "1", "key": klucz, "fields": {"summary": tytul, "status": {"name": stan}}}


def _strona_cloud(wpisy, nastepny=None):
    strona = {"issues": wpisy}
    if nastepny is not None:
        strona["nextPageToken"] = nastepny
    return strona


def _strona_dc(wpisy, start, total):
    return {"startAt": start, "maxResults": jira.NA_STRONE, "total": total, "issues": wpisy}


def _ustaw(sciezka, wartosc):
    def zmien(odpowiedz):
        wezel = odpowiedz
        for klucz in sciezka[:-1]:
            wezel = wezel[klucz]
        wezel[sciezka[-1]] = wartosc
        return odpowiedz

    return zmien


def _bez(sciezka):
    def zmien(odpowiedz):
        wezel = odpowiedz
        for klucz in sciezka[:-1]:
            wezel = wezel[klucz]
        del wezel[sciezka[-1]]
        return odpowiedz

    return zmien


def _adf(*bloki):
    return {"version": 1, "type": "doc", "content": list(bloki)}


def _akapit(tekst):
    return {"type": "paragraph", "content": [{"type": "text", "text": tekst}]}


def _pozycje(*teksty):
    return [{"type": "listItem", "content": [_akapit(t)]} for t in teksty]


def _punktory(*teksty):
    return {"type": "bulletList", "content": _pozycje(*teksty)}


def _numerowana(*teksty):
    return {"type": "orderedList", "attrs": {"order": 1}, "content": _pozycje(*teksty)}


def _sekrety_w(tekst: str) -> list[str]:
    return [s for s in (TOKEN, EMAIL, BASIC_BASE64, "SEKRET") if s in tekst]


@pytest.fixture(autouse=True)
def _sekrety_i_pusty_plik(monkeypatch, tmp_path):
    monkeypatch.setenv("COCKPIT_JIRA_TOKEN", TOKEN)
    monkeypatch.setenv("COCKPIT_JIRA_EMAIL_TOKEN", EMAIL)
    monkeypatch.setattr(secrets, "CONFIG", tmp_path / "brak.json")


@pytest.fixture
def bez_tokenu(monkeypatch):
    monkeypatch.delenv("COCKPIT_JIRA_TOKEN")


@pytest.fixture
def bez_emaila(monkeypatch):
    monkeypatch.delenv("COCKPIT_JIRA_EMAIL_TOKEN")


def _plik_sekretow(monkeypatch, tmp_path, tresc):
    plik = tmp_path / "credentials.json"
    if isinstance(tresc, bytes):
        plik.write_bytes(tresc)
    else:
        plik.write_text(tresc, encoding="utf-8")
    monkeypatch.setattr(secrets, "CONFIG", plik)
    return plik


# --- stale -----------------------------------------------------------------


def test_stale_adaptera_maja_wartosci_z_umowy():
    assert jira.DOSTAWCA == "Jira"
    assert jira.TIMEOUT_S == 30.0
    assert jira.MAKS_STRON == 20
    assert jira.NA_STRONE == 50
    assert jira.OSTRZEZENIE_BRAK_POLA == "pole kryteriów nieustawione w project.yaml"


def test_blad_konfiguracji_jest_bledem_dziedziny():
    assert issubclass(jira.BlednaKonfiguracja, BladDziedziny)


def test_oczekiwany_naglowek_basic_to_base64_z_emaila_i_tokenu():
    assert BASIC_BASE64 == base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()


# --- konfiguracja ----------------------------------------------------------


def test_konfiguracja_z_samym_trybem_i_adresem_nie_ma_pola_kryteriow_ani_jql():
    konf = jira.konfiguracja({"tryb": "cloud", "adres": ADRES_CLOUD})
    assert isinstance(konf, jira.KonfiguracjaJira)
    assert (konf.tryb, konf.adres) == ("cloud", ADRES_CLOUD)
    assert (konf.pole_kryteriow, konf.jql) == (None, None)


def test_konfiguracja_niesie_pole_kryteriow_i_jql_bez_zmian():
    konf = jira.konfiguracja(
        {"tryb": "data_center", "adres": ADRES_DC, "pole_kryteriow": POLE, "jql": JQL}
    )
    assert (konf.tryb, konf.pole_kryteriow, konf.jql) == ("data_center", POLE, JQL)


@pytest.mark.parametrize("tryb", TRYBY)
def test_koncowy_ukosnik_adresu_jest_obcinany(tryb):
    assert _konf(tryb, adres="https://firma.example/jira/").adres == "https://firma.example/jira"


def test_adres_bez_koncowego_ukosnika_zostaje_bez_zmian():
    assert _konf("cloud", adres="https://firma.example/jira").adres == "https://firma.example/jira"


@pytest.mark.parametrize("adres", [ADRES_CLOUD, "https://firma.example"])
def test_brak_trybu_w_konfiguracji_to_blad_takze_gdy_adres_wyglada_na_cloud(adres):
    with pytest.raises(jira.BlednaKonfiguracja) as exc:
        jira.konfiguracja({"adres": adres})
    assert type(exc.value) is jira.BlednaKonfiguracja
    assert "tryb" in str(exc.value)
    assert adres not in str(exc.value)


@pytest.mark.parametrize("tryb", ["onprem_SEKRET", "Cloud", "dc", "", None, 1])
def test_nieznany_tryb_to_blad_konfiguracji_bez_wartosci_wejscia(tryb):
    with pytest.raises(jira.BlednaKonfiguracja) as exc:
        jira.konfiguracja({"tryb": tryb, "adres": ADRES_DC})
    assert type(exc.value) is jira.BlednaKonfiguracja
    assert "tryb" in str(exc.value)
    assert "SEKRET" not in str(exc.value)


def test_brak_adresu_w_konfiguracji_to_blad_z_nazwa_pola():
    with pytest.raises(jira.BlednaKonfiguracja) as exc:
        jira.konfiguracja({"tryb": "cloud"})
    assert type(exc.value) is jira.BlednaKonfiguracja
    assert "adres" in str(exc.value)


def test_nieznane_pole_konfiguracji_jest_odrzucane_z_nazwa_a_bez_wartosci():
    with pytest.raises(jira.BlednaKonfiguracja) as exc:
        jira.konfiguracja(
            {"tryb": "cloud", "adres": ADRES_CLOUD, "haslo_dostepu": "SEKRET_WARTOSC"}
        )
    assert type(exc.value) is jira.BlednaKonfiguracja
    assert "haslo_dostepu" in str(exc.value)
    assert "SEKRET_WARTOSC" not in str(exc.value)


@pytest.mark.parametrize(
    "pole, wartosc",
    [("adres", 123), ("pole_kryteriow", 10050), ("jql", ["project = AB"])],
    ids=["adres_liczba", "pole_liczba", "jql_lista"],
)
def test_pole_konfiguracji_zlego_typu_to_blad_z_nazwa_pola(pole, wartosc):
    dane = {"tryb": "cloud", "adres": ADRES_CLOUD, pole: wartosc}
    with pytest.raises(jira.BlednaKonfiguracja) as exc:
        jira.konfiguracja(dane)
    assert type(exc.value) is jira.BlednaKonfiguracja
    assert pole in str(exc.value)


def test_komunikat_o_wielu_bledach_konfiguracji_wymienia_wszystkie_pola():
    with pytest.raises(jira.BlednaKonfiguracja) as exc:
        jira.konfiguracja({"tryb": "SEKRET_TRYB", "adres": 5})
    tekst = str(exc.value)
    assert "tryb" in tekst
    assert "adres" in tekst
    assert "SEKRET_TRYB" not in tekst


@pytest.mark.parametrize("dane", [None, "cloud", ["cloud"], 5])
def test_konfiguracja_niebedaca_obiektem_to_blad_konfiguracji(dane):
    with pytest.raises(jira.BlednaKonfiguracja) as exc:
        jira.konfiguracja(dane)
    assert type(exc.value) is jira.BlednaKonfiguracja


def test_konfiguracja_jest_niezmienna():
    konf = _konf("cloud")
    with pytest.raises(pydantic.ValidationError):
        konf.tryb = "data_center"


# --- tryb z konfiguracji: adresy i naglowki --------------------------------


@pytest.mark.parametrize("tryb", TRYBY)
async def test_pobranie_idzie_pod_adres_api_wlasciwy_dla_trybu(tryb):
    serwer = Serwer(_odp(json_=_issue(tryb)))
    await _pobierz(serwer, _konf(tryb))
    assert len(serwer.zadania) == 1
    zadanie = serwer.zadania[0]
    assert zadanie.method == "GET"
    assert f"{zadanie.url.scheme}://{zadanie.url.host}" == ADRESY[tryb]
    assert zadanie.url.path == f"{BAZA_API[tryb]}/issue/AB-7"


async def test_tryb_cloud_uzywa_basic_z_emaila_i_tokenu_oraz_api_v3():
    serwer = Serwer(_odp(json_=_issue("cloud")))
    await _pobierz(serwer, _konf("cloud"))
    zadanie = serwer.zadania[0]
    assert zadanie.headers["authorization"] == BASIC
    assert zadanie.url.path == "/rest/api/3/issue/AB-7"


async def test_tryb_data_center_uzywa_bearer_i_api_v2():
    serwer = Serwer(_odp(json_=_issue("data_center")))
    await _pobierz(serwer, _konf("data_center"))
    zadanie = serwer.zadania[0]
    assert zadanie.headers["authorization"] == BEARER
    assert zadanie.url.path == "/rest/api/2/issue/AB-7"


async def test_tryb_data_center_z_adresem_atlassian_uzywa_bearer_i_api_v2():
    serwer = Serwer(_odp(json_=_issue("data_center")))
    await _pobierz(serwer, _konf("data_center", adres="https://firma.atlassian.net"))
    zadanie = serwer.zadania[0]
    assert zadanie.url.host == "firma.atlassian.net"
    assert zadanie.headers["authorization"] == BEARER
    assert zadanie.url.path == "/rest/api/2/issue/AB-7"


async def test_tryb_cloud_z_adresem_on_prem_uzywa_basic_i_api_v3():
    serwer = Serwer(_odp(json_=_issue("cloud")))
    await _pobierz(serwer, _konf("cloud", adres="https://jira.firma.local"))
    zadanie = serwer.zadania[0]
    assert zadanie.url.host == "jira.firma.local"
    assert zadanie.headers["authorization"] == BASIC
    assert zadanie.url.path == "/rest/api/3/issue/AB-7"


@pytest.mark.parametrize("tryb", TRYBY)
async def test_zadanie_pobrania_prosi_o_json(tryb):
    serwer = Serwer(_odp(json_=_issue(tryb)))
    await _pobierz(serwer, _konf(tryb))
    assert serwer.zadania[0].headers["accept"] == "application/json"


@pytest.mark.parametrize("tryb", TRYBY)
async def test_zadanie_ma_limit_czasu_oczekiwania_z_umowy(tryb):
    serwer = Serwer(_odp(json_=_issue(tryb)))
    await _pobierz(serwer, _konf(tryb))
    limit = serwer.zadania[0].extensions["timeout"]
    assert limit == {"connect": 30.0, "read": 30.0, "write": 30.0, "pool": 30.0}


@pytest.mark.parametrize("tryb", TRYBY)
async def test_adres_z_sciezka_instalacji_i_koncowym_ukosnikiem_nie_daje_podwojnego_ukosnika(
    tryb,
):
    serwer = Serwer(_odp(json_=_issue(tryb)))
    await _pobierz(serwer, _konf(tryb, adres="https://firma.example/jira/"))
    assert serwer.zadania[0].url.path == f"/jira{BAZA_API[tryb]}/issue/AB-7"


async def test_klucz_zgloszenia_trafia_do_adresu_zadania():
    serwer = Serwer(_odp(json_=_issue("cloud")))
    await _pobierz(serwer, _konf("cloud"), klucz="XYZ-123")
    assert serwer.zadania[0].url.path == "/rest/api/3/issue/XYZ-123"


@pytest.mark.parametrize("tryb", TRYBY)
async def test_token_jest_czytany_przy_kazdym_wywolaniu_a_nie_raz_przy_imporcie(
    monkeypatch, tryb
):
    serwer = Serwer(_odp(json_=_issue(tryb)))
    konf = _konf(tryb)
    await _pobierz(serwer, konf)
    monkeypatch.setenv("COCKPIT_JIRA_TOKEN", "nowy_token")
    await _pobierz(serwer, konf)
    pierwszy, drugi = (z.headers["authorization"] for z in serwer.zadania)
    assert pierwszy != drugi
    if tryb == "data_center":
        assert drugi == "Bearer nowy_token"


async def test_token_i_email_z_pliku_sekretow_sa_uzywane_gdy_nie_ma_zmiennych(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("COCKPIT_JIRA_TOKEN")
    monkeypatch.delenv("COCKPIT_JIRA_EMAIL_TOKEN")
    _plik_sekretow(monkeypatch, tmp_path, json.dumps({"jira": TOKEN, "jira_email": EMAIL}))
    serwer = Serwer(_odp(json_=_issue("cloud")))
    await _pobierz(serwer, _konf("cloud"))
    assert serwer.zadania[0].headers["authorization"] == BASIC


async def test_data_center_pyta_tylko_o_token_a_cloud_o_token_i_email(monkeypatch):
    pytane: list[str] = []
    prawdziwa = secrets.get_token

    def podglad(nazwa):
        pytane.append(nazwa)
        return prawdziwa(nazwa)

    monkeypatch.setattr(secrets, "get_token", podglad)
    await _pobierz(Serwer(_odp(json_=_issue("data_center"))), _konf("data_center"))
    assert pytane == ["jira"]
    pytane.clear()
    await _pobierz(Serwer(_odp(json_=_issue("cloud"))), _konf("cloud"))
    assert sorted(pytane) == ["jira", "jira_email"]


# --- ticket.json: ten sam ksztalt w obu trybach ----------------------------


@pytest.mark.parametrize("tryb", TRYBY)
async def test_pobrane_zgloszenie_zapisuje_sie_do_ticket_json_w_kolejnosci_kontraktu(tryb):
    ticket = await _ticket(tryb)
    bajty = do_pliku(ticket)
    wczytany = json.loads(bajty)
    assert list(wczytany.keys()) == KOLEJNOSC
    assert wczytany["source"] == "jira"
    assert "Zażółć gęślą jaźń".encode("utf-8") in bajty
    assert TicketJson.model_validate_json(bajty) == ticket


async def test_ticket_json_z_cloud_i_data_center_ma_ten_sam_ksztalt_kluczy_i_typow():
    cloud = json.loads(do_pliku(await _ticket("cloud", pole_kryteriow=POLE)))
    dc = json.loads(do_pliku(await _ticket("data_center", pole_kryteriow=POLE)))
    assert list(cloud) == list(dc) == KOLEJNOSC
    assert {k: type(v) for k, v in cloud.items()} == {k: type(v) for k, v in dc.items()}
    assert list(cloud["comments"][0]) == list(dc["comments"][0]) == ["author", "body", "at"]
    for pole in ("source", "id", "title", "status", "priority", "labels", "fetched_at"):
        assert cloud[pole] == dc[pole]
    assert cloud["url"] != dc["url"]


# --- mapowanie -------------------------------------------------------------


async def test_zgloszenie_cloud_trafia_do_kontraktu_ticketu_ze_wszystkimi_polami():
    odpowiedz = _issue("cloud")
    ticket = await _ticket("cloud", issue=odpowiedz, pole_kryteriow=POLE)

    assert isinstance(ticket, TicketJson)
    assert ticket.source == "jira"
    assert ticket.id == "AB-7"
    assert ticket.url == "https://firma.atlassian.net/browse/AB-7"
    assert ticket.title == "Adapter Jira: pobranie zgłoszenia"
    assert ticket.acceptance_criteria == KRYTERIA_ADF
    assert ticket.status == "W recenzji"
    assert ticket.priority == "High"
    assert ticket.labels == ["backend", "integracje"]
    assert [(k.author, k.body) for k in ticket.comments] == [
        ("Łukasz Żółć", "Sprawdź limit zapytań — żądań jest dużo."),
        ("nieznany", "Komentarz automatu bez autora."),
    ]
    assert ticket.fetched_at == TERAZ
    assert ticket.raw == odpowiedz


async def test_zgloszenie_data_center_trafia_do_kontraktu_ticketu_ze_wszystkimi_polami():
    odpowiedz = _issue("data_center")
    ticket = await _ticket("data_center", issue=odpowiedz, pole_kryteriow=POLE)

    assert isinstance(ticket, TicketJson)
    assert ticket.source == "jira"
    assert ticket.id == "AB-7"
    assert ticket.url == "https://jira.firma.local/browse/AB-7"
    assert ticket.title == "Adapter Jira: pobranie zgłoszenia"
    assert ticket.acceptance_criteria == KRYTERIA_DC
    assert ticket.status == "W recenzji"
    assert ticket.priority == "High"
    assert ticket.labels == ["backend", "integracje"]
    assert [(k.author, k.body) for k in ticket.comments] == [
        (
            "Łukasz Żółć",
            "Sprawdź *limit* zapytań — żądań jest dużo.\n{code}GET /rest/api/2/issue{code}",
        ),
        ("nieznany", "Komentarz automatu bez autora."),
    ]
    assert ticket.fetched_at == TERAZ
    assert ticket.raw == odpowiedz


@pytest.mark.parametrize("tryb", TRYBY)
async def test_zrodlo_to_jira_takze_dla_data_center(tryb):
    assert (await _ticket(tryb)).source == "jira"


@pytest.mark.parametrize("tryb", TRYBY)
async def test_adres_zgloszenia_to_adres_instancji_z_browse_i_kluczem_bez_koncowego_ukosnika(
    tryb,
):
    ticket = await _ticket(tryb, adres="https://firma.example/jira/")
    assert ticket.url == "https://firma.example/jira/browse/AB-7"


@pytest.mark.parametrize("tryb", TRYBY)
async def test_identyfikator_to_klucz_z_odpowiedzi_a_nie_z_zapytania(tryb):
    serwer = Serwer(_odp(json_=_issue(tryb)))
    ticket, _ = await _pobierz(serwer, _konf(tryb), klucz="ab-7")
    assert ticket.id == "AB-7"
    assert ticket.url.endswith("/browse/AB-7")


@pytest.mark.parametrize("tryb", TRYBY)
async def test_status_i_priorytet_to_nazwy_ze_zrodla_a_nie_identyfikatory(tryb):
    odpowiedz = _issue(tryb)
    odpowiedz["fields"]["status"] = {"id": "3", "name": "W trakcie", "statusCategory": {}}
    odpowiedz["fields"]["priority"] = {"id": "1", "name": "Pilny"}
    ticket = await _ticket(tryb, issue=odpowiedz)
    assert (ticket.status, ticket.priority) == ("W trakcie", "Pilny")


@pytest.mark.parametrize("tryb", TRYBY)
@pytest.mark.parametrize("zmiana", [_ustaw(["fields", "priority"], None), _bez(["fields", "priority"])])
async def test_zgloszenie_bez_priorytetu_ma_priorytet_pusty_a_nie_napis(tryb, zmiana):
    ticket = await _ticket(tryb, issue=zmiana(_issue(tryb)))
    assert ticket.priority is None


@pytest.mark.parametrize("tryb", TRYBY)
@pytest.mark.parametrize("zmiana", [_ustaw(["fields", "labels"], None), _bez(["fields", "labels"])])
async def test_zgloszenie_bez_etykiet_ma_pusta_liste(tryb, zmiana):
    ticket = await _ticket(tryb, issue=zmiana(_issue(tryb)))
    assert ticket.labels == []


@pytest.mark.parametrize("tryb", TRYBY)
async def test_kolejnosc_etykiet_i_komentarzy_jest_taka_jak_w_zrodle(tryb):
    odpowiedz = _issue(tryb)
    odpowiedz["fields"]["labels"].reverse()
    odpowiedz["fields"]["comment"]["comments"].reverse()
    ticket = await _ticket(tryb, issue=odpowiedz)
    assert ticket.labels == ["integracje", "backend"]
    assert [k.author for k in ticket.comments] == ["nieznany", "Łukasz Żółć"]


@pytest.mark.parametrize("tryb", TRYBY)
async def test_zgloszenie_bez_komentarzy_ma_pusta_liste_komentarzy(tryb):
    ticket = await _ticket(tryb, issue=_bez(["fields", "comment"])(_issue(tryb)))
    assert ticket.comments == []


@pytest.mark.parametrize("tryb", TRYBY)
async def test_komentarz_ma_date_ze_strefa_z_formatu_jiry(tryb):
    ticket = await _ticket(tryb)
    pierwszy, drugi = ticket.comments
    assert pierwszy.at == datetime(2026, 3, 4, 12, 30, 15, tzinfo=timezone(timedelta(hours=1)))
    assert pierwszy.at.utcoffset() == timedelta(hours=1)
    assert drugi.at == datetime(2026, 3, 5, 8, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert drugi.at.utcoffset() == timedelta(hours=-5)


@pytest.mark.parametrize("tryb", TRYBY)
@pytest.mark.parametrize("zmiana", [_ustaw(["fields", "comment", "comments", 0, "author"], None), _bez(["fields", "comment", "comments", 0, "author"])])
async def test_komentarz_bez_autora_ma_autora_nieznany(tryb, zmiana):
    ticket = await _ticket(tryb, issue=zmiana(_issue(tryb)))
    assert ticket.comments[0].author == "nieznany"


@pytest.mark.parametrize("tryb", TRYBY)
async def test_komentarz_bez_daty_utworzenia_ma_date_pusta(tryb):
    odpowiedz = _bez(["fields", "comment", "comments", 0, "created"])(_issue(tryb))
    ticket = await _ticket(tryb, issue=odpowiedz)
    assert ticket.comments[0].at is None
    assert ticket.comments[1].at is not None


@pytest.mark.parametrize("tryb", TRYBY)
async def test_bez_wstrzyknietego_czasu_data_pobrania_to_teraz_w_utc(tryb):
    przed = datetime.now(timezone.utc)
    ticket, _ = await jira.pobierz(
        "AB-7", _konf(tryb), transport=Serwer(_odp(json_=_issue(tryb))).transport
    )
    po = datetime.now(timezone.utc)
    assert ticket.fetched_at.utcoffset() == timedelta(0)
    assert przed <= ticket.fetched_at <= po


async def test_opis_z_cloud_w_ticket_json_jest_tekstem_a_nie_drzewem_adf():
    ticket = await _ticket("cloud")
    assert isinstance(ticket.description, str)
    assert "Zażółć gęślą jaźń — runner pobiera zgłoszenie przez REST." in ticket.description
    assert "Z OPISU pierwsze" in ticket.description
    assert "type" not in ticket.description
    assert "doc" not in ticket.description
    zapis = json.loads(do_pliku(ticket))
    assert isinstance(zapis["description"], str)
    assert isinstance(zapis["comments"][0]["body"], str)


async def test_tresc_komentarza_z_cloud_to_tekst_a_nie_drzewo_adf():
    ticket = await _ticket("cloud")
    assert ticket.comments[0].body == "Sprawdź limit zapytań — żądań jest dużo."
    assert "type" not in ticket.comments[0].body


async def test_opis_z_data_center_to_napis_bez_zmian():
    odpowiedz = _issue("data_center")
    ticket = await _ticket("data_center", issue=odpowiedz)
    assert ticket.description == odpowiedz["fields"]["description"]
    assert "h2. Acceptance criteria" in ticket.description


@pytest.mark.parametrize("tryb", TRYBY)
async def test_zgloszenie_bez_opisu_ma_pusty_opis_a_nie_null(tryb):
    odpowiedz = _ustaw(["fields", "description"], None)(_issue(tryb))
    ticket = await _ticket(tryb, issue=odpowiedz)
    assert ticket.description == ""
    assert ticket.raw["fields"]["description"] is None


async def test_data_center_nie_splaszcza_opisu_ktory_dostal_jako_drzewo():
    odpowiedz = _ustaw(["fields", "description"], _adf(_akapit("tekst")))(_issue("data_center"))
    blad = await _kod_bledu(
        _pobierz(Serwer(_odp(json_=odpowiedz)), _konf("data_center"))
    )
    assert blad.kod == "blad_dostawcy"


async def test_data_center_nie_przyjmuje_tresci_komentarza_jako_drzewa():
    odpowiedz = _ustaw(
        ["fields", "comment", "comments", 0, "body"], _adf(_akapit("tekst"))
    )(_issue("data_center"))
    blad = await _kod_bledu(
        _pobierz(Serwer(_odp(json_=odpowiedz)), _konf("data_center"))
    )
    assert blad.kod == "blad_dostawcy"


@pytest.mark.parametrize("tryb", TRYBY)
@pytest.mark.parametrize("kolekcja", [{}, ""], ids=["pusty_obiekt", "pusty_napis"])
async def test_komentarze_zgloszenia_niebedace_lista_to_blad_dostawcy_a_nie_brak_komentarzy(
    tryb, kolekcja
):
    odpowiedz = _ustaw(["fields", "comment", "comments"], kolekcja)(_issue(tryb))
    blad = await _kod_bledu(_pobierz(Serwer(_odp(json_=odpowiedz)), _konf(tryb)))
    assert blad.kod == "blad_dostawcy"


# --- klucz zgloszenia w adresie ---------------------------------------------


@pytest.mark.parametrize("tryb", TRYBY)
async def test_klucz_ze_znakami_zapytania_trafia_do_sciezki_zakodowany_a_nie_do_zapytania(tryb):
    serwer = Serwer(_odp(json_=_issue(tryb)))
    await _pobierz(serwer, _konf(tryb), klucz="AB-7?fields=summary")
    zadanie = serwer.zadania[0]
    assert zadanie.url.raw_path == f"{BAZA_API[tryb]}/issue/AB-7%3Ffields%3Dsummary".encode()
    assert len(zadanie.url.params) == 0


@pytest.mark.parametrize("tryb", TRYBY)
async def test_ukosnik_w_kluczu_nie_dodaje_segmentu_sciezki(tryb):
    serwer = Serwer(_odp(json_=_issue(tryb)))
    await _pobierz(serwer, _konf(tryb), klucz="AB/7")
    assert serwer.zadania[0].url.raw_path == f"{BAZA_API[tryb]}/issue/AB%2F7".encode()


@pytest.mark.parametrize("tryb", TRYBY)
async def test_kratka_w_kluczu_nie_ucina_adresu_do_fragmentu(tryb):
    serwer = Serwer(_odp(json_=_issue(tryb)))
    await _pobierz(serwer, _konf(tryb), klucz="AB#7")
    assert serwer.zadania[0].url.raw_path == f"{BAZA_API[tryb]}/issue/AB%237".encode()


@pytest.mark.parametrize("tryb", TRYBY)
async def test_zwykly_klucz_zgloszenia_zostaje_w_sciezce_bez_zmian(tryb):
    serwer = Serwer(_odp(json_=_issue(tryb)))
    await _pobierz(serwer, _konf(tryb), klucz="AB-7")
    assert serwer.zadania[0].url.raw_path == f"{BAZA_API[tryb]}/issue/AB-7".encode()


# --- pole kryteriow --------------------------------------------------------


async def _kryteria(tryb, wartosc, pole=POLE, **konf):
    odpowiedz = _issue(tryb)
    odpowiedz["fields"][pole] = wartosc
    serwer = Serwer(_odp(json_=odpowiedz))
    ticket, ostrzezenia = await _pobierz(serwer, _konf(tryb, pole_kryteriow=pole, **konf))
    return ticket.acceptance_criteria, ostrzezenia


async def test_pole_kryteriow_cloud_z_lista_adf_wypelnia_kryteria_akceptacji():
    ticket, ostrzezenia = await _pobierz(
        Serwer(_odp(json_=_issue("cloud"))), _konf("cloud", pole_kryteriow=POLE)
    )
    assert ticket.acceptance_criteria == KRYTERIA_ADF
    assert ostrzezenia == []


async def test_pole_kryteriow_data_center_z_napisem_wielolinijkowym_wypelnia_kryteria():
    ticket, ostrzezenia = await _pobierz(
        Serwer(_odp(json_=_issue("data_center"))), _konf("data_center", pole_kryteriow=POLE)
    )
    assert ticket.acceptance_criteria == KRYTERIA_DC
    assert ostrzezenia == []


@pytest.mark.parametrize("tryb", TRYBY)
async def test_kryteria_z_napisu_to_niepuste_linie_bez_obramowania_spacjami(tryb):
    wartosc = "  pierwsze  \n\n   \n\tdrugie\t\n- z myslnikiem\n"
    kryteria, ostrzezenia = await _kryteria(tryb, wartosc)
    assert kryteria == ["pierwsze", "drugie", "- z myslnikiem"]
    assert ostrzezenia == []


@pytest.mark.parametrize("tryb", TRYBY)
async def test_kryteria_z_listy_to_niepuste_elementy_bez_obramowania_spacjami(tryb):
    kryteria, ostrzezenia = await _kryteria(tryb, ["  a ", "", "   ", "- b", "c"])
    assert kryteria == ["a", "- b", "c"]
    assert ostrzezenia == []


@pytest.mark.parametrize("tryb", TRYBY)
@pytest.mark.parametrize("element", [1, None, {"a": "b"}, ["x"], True], ids=str)
async def test_element_listy_kryteriow_niebedacy_napisem_to_blad_dostawcy(tryb, element):
    odpowiedz = _ustaw(["fields", POLE], ["dobre", element])(_issue(tryb))
    serwer = Serwer(_odp(json_=odpowiedz))
    blad = await _kod_bledu(_pobierz(serwer, _konf(tryb, pole_kryteriow=POLE)))
    assert blad.kod == "blad_dostawcy"


@pytest.mark.parametrize("tryb", TRYBY)
@pytest.mark.parametrize("wartosc", [5, 1.5, True], ids=str)
async def test_pole_kryteriow_zlego_typu_to_blad_dostawcy(tryb, wartosc):
    odpowiedz = _ustaw(["fields", POLE], wartosc)(_issue(tryb))
    serwer = Serwer(_odp(json_=odpowiedz))
    blad = await _kod_bledu(_pobierz(serwer, _konf(tryb, pole_kryteriow=POLE)))
    assert blad.kod == "blad_dostawcy"


@pytest.mark.parametrize("tryb", TRYBY)
async def test_pole_kryteriow_ustawione_a_puste_w_zgloszeniu_daje_pusta_liste_bez_ostrzezenia(
    tryb,
):
    odpowiedz = _ustaw(["fields", POLE], None)(_issue(tryb))
    ticket, ostrzezenia = await _pobierz(
        Serwer(_odp(json_=odpowiedz)), _konf(tryb, pole_kryteriow=POLE)
    )
    assert ticket.acceptance_criteria == []
    assert ostrzezenia == []


@pytest.mark.parametrize("tryb", TRYBY)
async def test_pole_kryteriow_ustawione_a_nieobecne_w_zgloszeniu_daje_pusta_liste_bez_ostrzezenia(
    tryb,
):
    odpowiedz = _bez(["fields", POLE])(_issue(tryb))
    ticket, ostrzezenia = await _pobierz(
        Serwer(_odp(json_=odpowiedz)), _konf(tryb, pole_kryteriow=POLE)
    )
    assert ticket.acceptance_criteria == []
    assert ostrzezenia == []


@pytest.mark.parametrize("tryb", TRYBY)
async def test_nieustawione_pole_kryteriow_daje_pusta_liste_i_dokladnie_jedno_ostrzezenie(tryb):
    ticket, ostrzezenia = await _pobierz(Serwer(_odp(json_=_issue(tryb))), _konf(tryb))
    assert ticket.acceptance_criteria == []
    assert ostrzezenia == ["pole kryteriów nieustawione w project.yaml"]
    assert ostrzezenia == [jira.OSTRZEZENIE_BRAK_POLA]


@pytest.mark.parametrize("tryb", TRYBY)
async def test_kryteria_nie_sa_zgadywane_z_naglowka_w_opisie(tryb):
    ticket, ostrzezenia = await _pobierz(Serwer(_odp(json_=_issue(tryb))), _konf(tryb))
    assert "Acceptance criteria" in ticket.description
    assert "Z OPISU pierwsze" in ticket.description
    assert ticket.acceptance_criteria == []
    assert ostrzezenia == [jira.OSTRZEZENIE_BRAK_POLA]


@pytest.mark.parametrize("tryb", TRYBY)
async def test_nieustawione_pole_kryteriow_ostrzega_nawet_gdy_zgloszenie_ma_wypelnione_pole(tryb):
    odpowiedz = _issue(tryb)
    assert odpowiedz["fields"][POLE]
    ticket, ostrzezenia = await _pobierz(Serwer(_odp(json_=odpowiedz)), _konf(tryb))
    assert ticket.acceptance_criteria == []
    assert ostrzezenia == [jira.OSTRZEZENIE_BRAK_POLA]


@pytest.mark.parametrize("tryb", TRYBY)
async def test_kryteria_biora_sie_z_pola_wskazanego_w_konfiguracji_a_nie_z_innego(tryb):
    odpowiedz = _issue(tryb)
    odpowiedz["fields"]["customfield_20000"] = ["z innego pola"]
    ticket, ostrzezenia = await _pobierz(
        Serwer(_odp(json_=odpowiedz)), _konf(tryb, pole_kryteriow="customfield_20000")
    )
    assert ticket.acceptance_criteria == ["z innego pola"]
    assert ostrzezenia == []


@pytest.mark.parametrize("tryb", TRYBY)
async def test_pusta_nazwa_pola_kryteriow_to_ustawione_pole_bez_wartosci_a_nie_brak_konfiguracji(
    tryb,
):
    ticket, ostrzezenia = await _pobierz(
        Serwer(_odp(json_=_issue(tryb))), _konf(tryb, pole_kryteriow="")
    )
    assert ticket.acceptance_criteria == []
    assert ostrzezenia == []


@pytest.mark.parametrize("tryb", TRYBY)
async def test_kryteria_z_adf_to_linie_bez_znacznika_punktu_pominiete_puste(tryb):
    wartosc = _adf(
        _akapit("  z wcięciem  "),
        _akapit("   "),
        _punktory("a", "- b"),
        _numerowana("c"),
    )
    kryteria, ostrzezenia = await _kryteria(tryb, wartosc)
    assert kryteria == ["z wcięciem", "a", "- b", "1. c"]
    assert ostrzezenia == []


@pytest.mark.parametrize("tryb", TRYBY)
async def test_znacznik_punktu_jest_obcinany_tylko_raz_i_tylko_z_poczatku_linii(tryb):
    wartosc = _adf(_punktory("- podwojny", "srodek - myslnika"))
    kryteria, _ = await _kryteria(tryb, wartosc)
    assert kryteria == ["- podwojny", "srodek - myslnika"]


@pytest.mark.parametrize("tryb", TRYBY)
async def test_numeracja_z_adf_zostaje_w_kryteriach(tryb):
    kryteria, _ = await _kryteria(tryb, _adf(_numerowana("pierwsze", "drugie")))
    assert kryteria == ["1. pierwsze", "2. drugie"]


# --- kryteria: ostrzezenia nie sa drukowane --------------------------------


async def test_ostrzezenie_o_braku_pola_jest_zwracane_a_nie_drukowane(capsys):
    await _pobierz(Serwer(_odp(json_=_issue("cloud"))), _konf("cloud"))
    wyjscie = capsys.readouterr()
    assert (wyjscie.out, wyjscie.err) == ("", "")


# --- listuj: JQL ------------------------------------------------------------


@pytest.mark.parametrize("tryb", TRYBY)
@pytest.mark.parametrize("jql", [JQL, JQL_ZNAKI, "  project = AB  ", "a\nb"], ids=str)
async def test_lista_wysyla_jql_z_konfiguracji_dokladnie_bez_zmian(tryb, jql):
    serwer = Serwer(_odp(json_=_strona_cloud([]) if tryb == "cloud" else _strona_dc([], 0, 0)))
    await _listuj(serwer, _konf(tryb, jql=jql))
    assert len(serwer.zadania) == 1
    assert serwer.zadania[0].url.params["jql"] == jql
    assert len(serwer.zadania[0].url.params.get_list("jql")) == 1


@pytest.mark.parametrize("tryb", TRYBY)
async def test_lista_idzie_pod_adres_wyszukiwania_wlasciwy_dla_trybu(tryb):
    serwer = Serwer(_odp(json_=_strona_cloud([]) if tryb == "cloud" else _strona_dc([], 0, 0)))
    await _listuj(serwer, _konf(tryb, jql=JQL))
    zadanie = serwer.zadania[0]
    assert zadanie.method == "GET"
    assert f"{zadanie.url.scheme}://{zadanie.url.host}" == ADRESY[tryb]
    assert zadanie.url.path == ENDPOINT_LISTY[tryb]


@pytest.mark.parametrize("tryb", TRYBY)
async def test_lista_prosi_o_strone_zadanej_wielkosci_i_tylko_potrzebne_pola(tryb):
    serwer = Serwer(_odp(json_=_strona_cloud([]) if tryb == "cloud" else _strona_dc([], 0, 0)))
    await _listuj(serwer, _konf(tryb, jql=JQL))
    params = serwer.zadania[0].url.params
    assert params["maxResults"] == "50"
    assert params["maxResults"] == str(jira.NA_STRONE)
    assert params["fields"] == "summary,status"


@pytest.mark.parametrize("tryb", TRYBY)
async def test_lista_uwierzytelnia_sie_tak_samo_jak_pobranie_i_prosi_o_json(tryb):
    serwer = Serwer(_odp(json_=_strona_cloud([]) if tryb == "cloud" else _strona_dc([], 0, 0)))
    await _listuj(serwer, _konf(tryb, jql=JQL))
    zadanie = serwer.zadania[0]
    assert zadanie.headers["authorization"] == (BASIC if tryb == "cloud" else BEARER)
    assert zadanie.headers["accept"] == "application/json"


@pytest.mark.parametrize("tryb", TRYBY)
async def test_lista_z_adresem_ze_sciezka_instalacji_nie_ma_podwojnego_ukosnika(tryb):
    serwer = Serwer(_odp(json_=_strona_cloud([]) if tryb == "cloud" else _strona_dc([], 0, 0)))
    await _listuj(serwer, _konf(tryb, jql=JQL, adres="https://firma.example/jira/"))
    assert serwer.zadania[0].url.path == f"/jira{ENDPOINT_LISTY[tryb]}"


@pytest.mark.parametrize("tryb", TRYBY)
@pytest.mark.parametrize("jql", [None, "", "   ", "\n\t"], ids=repr)
async def test_brak_jql_w_konfiguracji_to_blad_konfiguracji_bez_zadnego_zadania(tryb, jql):
    serwer = Serwer(_odp(json_=_strona_cloud([_wpis("AB-1")])))
    with pytest.raises(jira.BlednaKonfiguracja) as exc:
        await _listuj(serwer, _konf(tryb, jql=jql))
    assert type(exc.value) is jira.BlednaKonfiguracja
    assert serwer.zadania == []


@pytest.mark.parametrize("tryb", TRYBY)
async def test_brak_jql_jest_zgloszony_przed_sprawdzeniem_tokenu(tryb, bez_tokenu):
    serwer = Serwer(_odp(json_=_strona_cloud([])))
    with pytest.raises(jira.BlednaKonfiguracja):
        await _listuj(serwer, _konf(tryb))
    assert serwer.zadania == []


# --- listuj: wynik i stronicowanie ----------------------------------------


async def test_lista_cloud_zwraca_klucz_tytul_i_stan_w_kolejnosci_zrodla():
    serwer = Serwer(
        _odp(
            json_=_strona_cloud(
                [_wpis("AB-9", "Zażółć gęślą", "Do zrobienia"), _wpis("AB-2", "Drugie", "Gotowe")]
            )
        )
    )
    wynik = await _listuj(serwer, _konf("cloud", jql=JQL))
    assert wynik == [
        {"id": "AB-9", "tytul": "Zażółć gęślą", "stan": "Do zrobienia"},
        {"id": "AB-2", "tytul": "Drugie", "stan": "Gotowe"},
    ]


async def test_lista_data_center_zwraca_klucz_tytul_i_stan_w_kolejnosci_zrodla():
    serwer = Serwer(
        _odp(
            json_=_strona_dc(
                [_wpis("AB-9", "Zażółć gęślą", "Do zrobienia"), _wpis("AB-2", "Drugie", "Gotowe")],
                0,
                2,
            )
        )
    )
    wynik = await _listuj(serwer, _konf("data_center", jql=JQL))
    assert wynik == [
        {"id": "AB-9", "tytul": "Zażółć gęślą", "stan": "Do zrobienia"},
        {"id": "AB-2", "tytul": "Drugie", "stan": "Gotowe"},
    ]


@pytest.mark.parametrize("tryb", TRYBY)
async def test_lista_bez_wynikow_jest_pusta_ale_po_prawdziwym_zapytaniu(tryb):
    serwer = Serwer(_odp(json_=_strona_cloud([]) if tryb == "cloud" else _strona_dc([], 0, 0)))
    assert await _listuj(serwer, _konf(tryb, jql=JQL)) == []
    assert len(serwer.zadania) == 1


async def test_lista_cloud_dociaga_kolejne_strony_po_tokenie_nastepnej_strony():
    serwer = Serwer(
        _odp(json_=_strona_cloud([_wpis("AB-1"), _wpis("AB-2")], "tok-1")),
        _odp(json_=_strona_cloud([_wpis("AB-3")], "tok-2")),
        _odp(json_=_strona_cloud([_wpis("AB-4")])),
    )
    wynik = await _listuj(serwer, _konf("cloud", jql=JQL))
    assert [w["id"] for w in wynik] == ["AB-1", "AB-2", "AB-3", "AB-4"]
    assert len(serwer.zadania) == 3
    params = [z.url.params for z in serwer.zadania]
    assert "nextPageToken" not in params[0]
    assert params[1]["nextPageToken"] == "tok-1"
    assert params[2]["nextPageToken"] == "tok-2"
    for p in params:
        assert p["jql"] == JQL
        assert p["maxResults"] == "50"
        assert p["fields"] == "summary,status"
        assert "startAt" not in p


@pytest.mark.parametrize("token", [None, ""], ids=["null", "pusty"])
async def test_lista_cloud_konczy_na_stronie_bez_niepustego_tokenu_nastepnej(token):
    strona = {"issues": [_wpis("AB-1")], "nextPageToken": token}
    serwer = Serwer(_odp(json_=strona), _odp(json_=_strona_cloud([_wpis("AB-2")])))
    wynik = await _listuj(serwer, _konf("cloud", jql=JQL))
    assert [w["id"] for w in wynik] == ["AB-1"]
    assert len(serwer.zadania) == 1


async def test_lista_cloud_nie_czyta_calkowitej_liczby_wynikow_ze_strony():
    strona = {**_strona_cloud([_wpis("AB-1")]), "total": 100, "startAt": 0}
    serwer = Serwer(_odp(json_=strona), _odp(json_=_strona_cloud([_wpis("AB-2")])))
    wynik = await _listuj(serwer, _konf("cloud", jql=JQL))
    assert [w["id"] for w in wynik] == ["AB-1"]
    assert len(serwer.zadania) == 1


async def test_lista_data_center_dociaga_kolejne_strony_po_start_at_i_total():
    serwer = Serwer(
        _odp(json_=_strona_dc([_wpis("AB-1"), _wpis("AB-2")], 0, 5)),
        _odp(json_=_strona_dc([_wpis("AB-3"), _wpis("AB-4")], 2, 5)),
        _odp(json_=_strona_dc([_wpis("AB-5")], 4, 5)),
    )
    wynik = await _listuj(serwer, _konf("data_center", jql=JQL))
    assert [w["id"] for w in wynik] == ["AB-1", "AB-2", "AB-3", "AB-4", "AB-5"]
    assert len(serwer.zadania) == 3
    params = [z.url.params for z in serwer.zadania]
    assert [p["startAt"] for p in params] == ["0", "2", "4"]
    for p in params:
        assert p["jql"] == JQL
        assert p["maxResults"] == "50"
        assert p["fields"] == "summary,status"
        assert "nextPageToken" not in p


async def test_lista_data_center_konczy_gdy_pobrano_dokladnie_total_wynikow():
    serwer = Serwer(
        _odp(json_=_strona_dc([_wpis("AB-1"), _wpis("AB-2")], 0, 2)),
        _odp(json_=_strona_dc([_wpis("AB-3")], 2, 3)),
    )
    wynik = await _listuj(serwer, _konf("data_center", jql=JQL))
    assert [w["id"] for w in wynik] == ["AB-1", "AB-2"]
    assert len(serwer.zadania) == 1


async def test_lista_data_center_konczy_na_pustej_stronie_mimo_wiekszego_total():
    serwer = Serwer(_odp(json_=_strona_dc([], 0, 5)), _odp(json_=_strona_dc([_wpis("AB-1")], 0, 5)))
    wynik = await _listuj(serwer, _konf("data_center", jql=JQL))
    assert wynik == []
    assert len(serwer.zadania) == 1


async def test_lista_data_center_nie_czyta_tokenu_nastepnej_strony():
    strona = {**_strona_dc([_wpis("AB-1")], 0, 1), "nextPageToken": "obcy"}
    serwer = Serwer(_odp(json_=strona), _odp(json_=_strona_dc([_wpis("AB-2")], 1, 2)))
    wynik = await _listuj(serwer, _konf("data_center", jql=JQL))
    assert [w["id"] for w in wynik] == ["AB-1"]
    assert len(serwer.zadania) == 1


def _lancuch_cloud(strony: int, ostatnia_ma_nastepna: bool):
    kroki = []
    for i in range(strony):
        ma_nastepna = i < strony - 1 or ostatnia_ma_nastepna
        kroki.append(_odp(json_=_strona_cloud([_wpis(f"AB-{i}")], f"tok-{i}" if ma_nastepna else None)))
    return Serwer(*kroki)


def _lancuch_dc(strony: int, total: int):
    kroki = [_odp(json_=_strona_dc([_wpis(f"AB-{i}")], i, total)) for i in range(strony)]
    return Serwer(*kroki)


async def test_lista_cloud_z_dokladnie_maksymalna_liczba_stron_jest_kompletna():
    serwer = _lancuch_cloud(jira.MAKS_STRON, ostatnia_ma_nastepna=False)
    wynik = await _listuj(serwer, _konf("cloud", jql=JQL))
    assert len(serwer.zadania) == jira.MAKS_STRON == 20
    assert [w["id"] for w in wynik] == [f"AB-{i}" for i in range(jira.MAKS_STRON)]


async def test_lista_cloud_dluzsza_niz_maksymalna_liczba_stron_to_blad_a_nie_ucieta_lista():
    serwer = _lancuch_cloud(jira.MAKS_STRON + 1, ostatnia_ma_nastepna=False)
    blad = await _kod_bledu(_listuj(serwer, _konf("cloud", jql=JQL)))
    assert blad.kod == "blad_dostawcy"
    assert "20" in blad.komunikat
    assert len(serwer.zadania) == jira.MAKS_STRON


async def test_lista_data_center_z_dokladnie_maksymalna_liczba_stron_jest_kompletna():
    serwer = _lancuch_dc(jira.MAKS_STRON, total=jira.MAKS_STRON)
    wynik = await _listuj(serwer, _konf("data_center", jql=JQL))
    assert len(serwer.zadania) == jira.MAKS_STRON
    assert [w["id"] for w in wynik] == [f"AB-{i}" for i in range(jira.MAKS_STRON)]


async def test_lista_data_center_dluzsza_niz_maksymalna_liczba_stron_to_blad_a_nie_ucieta_lista():
    serwer = _lancuch_dc(jira.MAKS_STRON + 1, total=jira.MAKS_STRON + 1)
    blad = await _kod_bledu(_listuj(serwer, _konf("data_center", jql=JQL)))
    assert blad.kod == "blad_dostawcy"
    assert "20" in blad.komunikat
    assert len(serwer.zadania) == jira.MAKS_STRON


async def test_limit_zapytan_na_drugiej_stronie_ponawia_te_sama_strone():
    serwer = Serwer(
        _odp(json_=_strona_cloud([_wpis("AB-1")], "tok-1")),
        _odp(429, tresc=b"", naglowki={"Retry-After": "4"}),
        _odp(json_=_strona_cloud([_wpis("AB-2")])),
    )
    czekanie = Odczekania()
    wynik = await _listuj(serwer, _konf("cloud", jql=JQL), czekanie)
    assert [w["id"] for w in wynik] == ["AB-1", "AB-2"]
    assert czekanie.wartosci == [4.0]
    assert [z.url.params.get("nextPageToken") for z in serwer.zadania] == [None, "tok-1", "tok-1"]


# --- listuj: zla struktura strony ------------------------------------------

STRONY_CLOUD_ZLE = [
    pytest.param({}, id="brak_issues"),
    pytest.param({"issues": {"AB-1": 1}}, id="issues_jako_obiekt_z_kluczem"),
    pytest.param({"issues": None}, id="issues_null"),
    pytest.param({"issues": ["AB-1"]}, id="wpis_jako_napis"),
    pytest.param({"issues": [{"fields": {"summary": "t", "status": {"name": "s"}}}]}, id="wpis_bez_klucza"),
    pytest.param({"issues": [{"key": "AB-1"}]}, id="wpis_bez_fields"),
    pytest.param({"issues": [{"key": "AB-1", "fields": {"status": {"name": "s"}}}]}, id="wpis_bez_tytulu"),
    pytest.param({"issues": [{"key": "AB-1", "fields": {"summary": "t"}}]}, id="wpis_bez_statusu"),
    pytest.param({"issues": [{"key": "AB-1", "fields": {"summary": "t", "status": "s"}}]}, id="status_jako_napis"),
    pytest.param({"issues": [{"key": "AB-1", "fields": {"summary": "t", "status": {"id": "3"}}}]}, id="status_bez_nazwy"),
]


@pytest.mark.parametrize("strona", STRONY_CLOUD_ZLE)
async def test_strona_listy_cloud_o_niespodziewanej_budowie_to_blad_dostawcy(strona):
    blad = await _kod_bledu(_listuj(Serwer(_odp(json_=strona)), _konf("cloud", jql=JQL)))
    assert blad.kod == "blad_dostawcy"


@pytest.mark.parametrize("strona", STRONY_CLOUD_ZLE)
async def test_strona_listy_data_center_o_niespodziewanej_budowie_to_blad_dostawcy(strona):
    strona = {**strona, "startAt": 0, "total": 1}
    blad = await _kod_bledu(_listuj(Serwer(_odp(json_=strona)), _konf("data_center", jql=JQL)))
    assert blad.kod == "blad_dostawcy"


@pytest.mark.parametrize("kolekcja", [{}, ""], ids=["pusty_obiekt", "pusty_napis"])
async def test_strona_listy_cloud_z_pusta_kolekcja_niebedaca_lista_to_blad_a_nie_brak_zgloszen(
    kolekcja,
):
    serwer = Serwer(_odp(json_={"issues": kolekcja}))
    blad = await _kod_bledu(_listuj(serwer, _konf("cloud", jql=JQL)))
    assert blad.kod == "blad_dostawcy"


@pytest.mark.parametrize("kolekcja", [{}, ""], ids=["pusty_obiekt", "pusty_napis"])
async def test_strona_listy_data_center_z_pusta_kolekcja_niebedaca_lista_to_blad_a_nie_brak_zgloszen(
    kolekcja,
):
    strona = {"startAt": 0, "maxResults": jira.NA_STRONE, "total": 0, "issues": kolekcja}
    serwer = Serwer(_odp(json_=strona))
    blad = await _kod_bledu(_listuj(serwer, _konf("data_center", jql=JQL)))
    assert blad.kod == "blad_dostawcy"


@pytest.mark.parametrize("kolekcja", [{}, ""], ids=["pusty_obiekt", "pusty_napis"])
async def test_druga_strona_cloud_z_pusta_kolekcja_niebedaca_lista_to_blad_a_nie_lista_z_pierwszej(
    kolekcja,
):
    serwer = Serwer(
        _odp(json_=_strona_cloud([_wpis("AB-1")], "tok-1")),
        _odp(json_={"issues": kolekcja}),
    )
    blad = await _kod_bledu(_listuj(serwer, _konf("cloud", jql=JQL)))
    assert blad.kod == "blad_dostawcy"
    assert len(serwer.zadania) == 2


@pytest.mark.parametrize("kolekcja", [{}, ""], ids=["pusty_obiekt", "pusty_napis"])
async def test_druga_strona_data_center_z_pusta_kolekcja_niebedaca_lista_to_blad_a_nie_lista_z_pierwszej(
    kolekcja,
):
    serwer = Serwer(
        _odp(json_=_strona_dc([_wpis("AB-1")], 0, 5)),
        _odp(json_={"startAt": 1, "maxResults": jira.NA_STRONE, "total": 5, "issues": kolekcja}),
    )
    blad = await _kod_bledu(_listuj(serwer, _konf("data_center", jql=JQL)))
    assert blad.kod == "blad_dostawcy"
    assert len(serwer.zadania) == 2


@pytest.mark.parametrize("total", [None, "dwa"], ids=["brak", "napis"])
async def test_strona_listy_data_center_bez_liczbowego_total_to_blad_dostawcy(total):
    strona = {"startAt": 0, "issues": [_wpis("AB-1")]}
    if total is not None:
        strona["total"] = total
    blad = await _kod_bledu(_listuj(Serwer(_odp(json_=strona)), _konf("data_center", jql=JQL)))
    assert blad.kod == "blad_dostawcy"


# --- brak tokenu -----------------------------------------------------------


@pytest.mark.parametrize("tryb", TRYBY)
async def test_brak_tokenu_przerywa_pobranie_bez_zadnego_zadania_i_wskazuje_klucz_jira(
    tryb, bez_tokenu
):
    serwer = Serwer(_odp(json_=_issue(tryb)))
    blad = await _kod_bledu(_pobierz(serwer, _konf(tryb)))
    assert blad.kod == "brak_tokenu"
    assert "jira" in blad.komunikat
    assert "jira_email" not in blad.komunikat
    assert serwer.zadania == []


@pytest.mark.parametrize("tryb", TRYBY)
async def test_brak_tokenu_przerywa_liste_bez_zadnego_zadania(tryb, bez_tokenu):
    serwer = Serwer(_odp(json_=_strona_cloud([])))
    blad = await _kod_bledu(_listuj(serwer, _konf(tryb, jql=JQL)))
    assert blad.kod == "brak_tokenu"
    assert "jira_email" not in blad.komunikat
    assert serwer.zadania == []


@pytest.mark.parametrize("wywolanie", ["pobierz", "listuj"])
async def test_brak_emaila_w_cloud_to_brak_tokenu_z_nazwa_klucza_jira_email(
    wywolanie, bez_emaila
):
    serwer = Serwer(_odp(json_=_issue("cloud")))
    konf = _konf("cloud", jql=JQL)
    korutyna = _pobierz(serwer, konf) if wywolanie == "pobierz" else _listuj(serwer, konf)
    blad = await _kod_bledu(korutyna)
    assert blad.kod == "brak_tokenu"
    assert "jira_email" in blad.komunikat
    assert serwer.zadania == []


async def test_data_center_nie_wymaga_emaila_ani_przy_pobraniu_ani_przy_liscie(bez_emaila):
    serwer = Serwer(_odp(json_=_issue("data_center")))
    ticket, _ = await _pobierz(serwer, _konf("data_center"))
    assert ticket.id == "AB-7"
    serwer = Serwer(_odp(json_=_strona_dc([], 0, 0)))
    assert await _listuj(serwer, _konf("data_center", jql=JQL)) == []
    assert serwer.zadania[0].headers["authorization"] == BEARER


async def test_pusty_token_w_zmiennej_i_brak_pliku_to_brak_tokenu(monkeypatch):
    monkeypatch.setenv("COCKPIT_JIRA_TOKEN", "")
    serwer = Serwer(_odp(json_=_issue("data_center")))
    blad = await _kod_bledu(_pobierz(serwer, _konf("data_center")))
    assert blad.kod == "brak_tokenu"
    assert serwer.zadania == []


async def test_pusty_email_w_zmiennej_i_brak_pliku_to_brak_tokenu_z_nazwa_jira_email(
    monkeypatch,
):
    monkeypatch.setenv("COCKPIT_JIRA_EMAIL_TOKEN", "")
    serwer = Serwer(_odp(json_=_issue("cloud")))
    blad = await _kod_bledu(_pobierz(serwer, _konf("cloud")))
    assert blad.kod == "brak_tokenu"
    assert "jira_email" in blad.komunikat
    assert serwer.zadania == []


@pytest.mark.parametrize("tryb", TRYBY)
@pytest.mark.parametrize("wartosc", [123, ["x"], {"a": "b"}, True], ids=str)
async def test_token_w_pliku_sekretow_niebedacy_napisem_to_brak_tokenu_bez_zadania(
    monkeypatch, tmp_path, tryb, wartosc
):
    monkeypatch.delenv("COCKPIT_JIRA_TOKEN")
    _plik_sekretow(monkeypatch, tmp_path, json.dumps({"jira": wartosc}))
    serwer = Serwer(_odp(json_=_issue(tryb)))
    blad = await _kod_bledu(_pobierz(serwer, _konf(tryb)))
    assert blad.kod == "brak_tokenu"
    assert "jira_email" not in blad.komunikat
    assert serwer.zadania == []


@pytest.mark.parametrize("wartosc", [123, ["x"], {"a": "b"}, True], ids=str)
async def test_email_w_pliku_sekretow_niebedacy_napisem_to_brak_tokenu_bez_zadania(
    monkeypatch, tmp_path, wartosc
):
    monkeypatch.delenv("COCKPIT_JIRA_EMAIL_TOKEN")
    _plik_sekretow(monkeypatch, tmp_path, json.dumps({"jira_email": wartosc}))
    serwer = Serwer(_odp(json_=_issue("cloud")))
    blad = await _kod_bledu(_pobierz(serwer, _konf("cloud")))
    assert blad.kod == "brak_tokenu"
    assert "jira_email" in blad.komunikat
    assert serwer.zadania == []


NIECZYTELNE_PLIKI = [
    pytest.param(f'{{"jira": "{TOKEN}" <<<'.encode(), id="uszkodzony_json"),
    pytest.param(b"\xff\xfe\x00\x80", id="zle_kodowanie"),
    pytest.param(b"[]", id="lista_zamiast_obiektu"),
    pytest.param(b"null", id="null_zamiast_obiektu"),
    pytest.param(b'"tekst"', id="napis_zamiast_obiektu"),
]


@pytest.mark.parametrize("tryb", TRYBY)
@pytest.mark.parametrize("zawartosc", NIECZYTELNE_PLIKI)
async def test_nieczytelny_plik_sekretow_to_brak_tokenu_a_nie_surowy_wyjatek(
    monkeypatch, tmp_path, tryb, zawartosc
):
    monkeypatch.delenv("COCKPIT_JIRA_TOKEN")
    _plik_sekretow(monkeypatch, tmp_path, zawartosc)
    serwer = Serwer(_odp(json_=_issue(tryb)))
    blad = await _kod_bledu(_pobierz(serwer, _konf(tryb)))
    assert blad.kod == "brak_tokenu"
    assert _sekrety_w(str(blad)) == []
    assert serwer.zadania == []


@pytest.mark.parametrize("zawartosc", NIECZYTELNE_PLIKI)
async def test_nieczytelny_plik_sekretow_przy_emailu_to_brak_tokenu_a_nie_surowy_wyjatek(
    monkeypatch, tmp_path, zawartosc
):
    monkeypatch.delenv("COCKPIT_JIRA_EMAIL_TOKEN")
    _plik_sekretow(monkeypatch, tmp_path, zawartosc)
    serwer = Serwer(_odp(json_=_issue("cloud")))
    blad = await _kod_bledu(_pobierz(serwer, _konf("cloud")))
    assert blad.kod == "brak_tokenu"
    assert _sekrety_w(str(blad)) == []
    assert serwer.zadania == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root czyta kazdy plik")
@pytest.mark.parametrize("tryb", TRYBY)
async def test_plik_sekretow_bez_praw_odczytu_to_brak_tokenu_a_nie_awaria(
    monkeypatch, tmp_path, tryb
):
    monkeypatch.delenv("COCKPIT_JIRA_TOKEN")
    plik = _plik_sekretow(monkeypatch, tmp_path, json.dumps({"jira": TOKEN}))
    plik.chmod(0o000)
    try:
        serwer = Serwer(_odp(json_=_issue(tryb)))
        blad = await _kod_bledu(_pobierz(serwer, _konf(tryb)))
    finally:
        plik.chmod(0o600)
    assert blad.kod == "brak_tokenu"
    assert _sekrety_w(str(blad)) == []
    assert serwer.zadania == []


@pytest.mark.parametrize(
    "wyjatek",
    [OSError(f"odmowa {TOKEN}"), ValueError(f"zle {TOKEN}"), AttributeError(f"brak {TOKEN}")],
    ids=["oserror", "valueerror", "attributeerror"],
)
@pytest.mark.parametrize("tryb", TRYBY)
async def test_wyjatek_przy_czytaniu_tokenu_to_brak_tokenu_bez_tresci_wyjatku(
    monkeypatch, tryb, wyjatek
):
    def zepsute(_nazwa):
        raise wyjatek

    monkeypatch.setattr(secrets, "get_token", zepsute)
    serwer = Serwer(_odp(json_=_issue(tryb)))
    blad = await _kod_bledu(_pobierz(serwer, _konf(tryb)))
    assert blad.kod == "brak_tokenu"
    assert _sekrety_w(str(blad)) == []
    assert serwer.zadania == []


@pytest.mark.parametrize(
    "wyjatek",
    [OSError(f"odmowa {EMAIL}"), ValueError(f"zle {EMAIL}"), AttributeError(f"brak {EMAIL}")],
    ids=["oserror", "valueerror", "attributeerror"],
)
async def test_wyjatek_przy_czytaniu_emaila_to_brak_tokenu_z_nazwa_klucza_bez_tresci_wyjatku(
    monkeypatch, wyjatek
):
    prawdziwa = secrets.get_token

    def zepsute(nazwa):
        if nazwa == "jira_email":
            raise wyjatek
        return prawdziwa(nazwa)

    monkeypatch.setattr(secrets, "get_token", zepsute)
    serwer = Serwer(_odp(json_=_issue("cloud")))
    blad = await _kod_bledu(_pobierz(serwer, _konf("cloud")))
    assert blad.kod == "brak_tokenu"
    assert "jira_email" in blad.komunikat
    assert _sekrety_w(str(blad)) == []
    assert serwer.zadania == []


async def test_wyjatek_przy_czytaniu_emaila_nie_dotyczy_data_center(monkeypatch):
    prawdziwa = secrets.get_token

    def zepsute(nazwa):
        if nazwa == "jira_email":
            raise OSError("nie powinno byc czytane")
        return prawdziwa(nazwa)

    monkeypatch.setattr(secrets, "get_token", zepsute)
    ticket, _ = await _pobierz(Serwer(_odp(json_=_issue("data_center"))), _konf("data_center"))
    assert ticket.id == "AB-7"


# --- bledy dostawcy --------------------------------------------------------


@pytest.mark.parametrize("tryb", TRYBY)
async def test_odrzucony_token_przy_pobraniu_to_token_odrzucony_bez_ponowien(tryb):
    serwer = Serwer(_odp(401, json_={"errorMessages": ["Unauthorized"]}))
    czekanie = Odczekania()
    blad = await _kod_bledu(_pobierz(serwer, _konf(tryb), czekanie))
    assert blad.kod == "token_odrzucony"
    assert (len(serwer.zadania), czekanie.wartosci) == (1, [])


@pytest.mark.parametrize("tryb", TRYBY)
async def test_odrzucony_token_przy_liscie_to_blad_a_nie_pusta_lista(tryb):
    serwer = Serwer(_odp(401, tresc=b"<html>nie json</html>"))
    blad = await _kod_bledu(_listuj(serwer, _konf(tryb, jql=JQL)))
    assert blad.kod == "token_odrzucony"
    assert len(serwer.zadania) == 1


@pytest.mark.parametrize("tryb", TRYBY)
async def test_nieistniejace_zgloszenie_to_nie_znaleziono(tryb):
    serwer = Serwer(_odp(404, json_={"errorMessages": ["Issue does not exist"]}))
    czekanie = Odczekania()
    blad = await _kod_bledu(_pobierz(serwer, _konf(tryb), czekanie, klucz="AB-404"))
    assert blad.kod == "nie_znaleziono"
    assert (len(serwer.zadania), czekanie.wartosci) == (1, [])


@pytest.mark.parametrize("tryb", TRYBY)
async def test_status_404_przy_liscie_to_blad_dostawcy_a_nie_nie_znaleziono(tryb):
    serwer = Serwer(_odp(404, json_={"errorMessages": ["Not found"]}))
    blad = await _kod_bledu(_listuj(serwer, _konf(tryb, jql=JQL)))
    assert blad.kod == "blad_dostawcy"
    assert len(serwer.zadania) == 1


@pytest.mark.parametrize("tryb", TRYBY)
async def test_limit_zapytan_z_czasem_od_dostawcy_czeka_tyle_sekund_i_ponawia(tryb):
    serwer = Serwer(
        _odp(429, tresc=b"", naglowki={"Retry-After": "7"}),
        _odp(json_=_issue(tryb)),
    )
    czekanie = Odczekania()
    ticket, _ = await _pobierz(serwer, _konf(tryb), czekanie)
    assert ticket.id == "AB-7"
    assert czekanie.wartosci == [7.0]
    assert len(serwer.zadania) == 2


async def test_limit_zapytan_bez_retry_after_czeka_domyslna_sekunde():
    serwer = Serwer(_odp(429, tresc=b""), _odp(json_=_issue("cloud")))
    czekanie = Odczekania()
    await _pobierz(serwer, _konf("cloud"), czekanie)
    assert czekanie.wartosci == [1.0]


@pytest.mark.parametrize(
    "retry_after",
    ["jutro", "-5", "", "inf", "nan", "Infinity", "Wed, 21 Oct 2026 07:28:00 GMT"],
)
async def test_nieliczbowy_nieskonczony_lub_ujemny_czas_odczekania_jest_ignorowany(retry_after):
    serwer = Serwer(
        _odp(429, tresc=b"", naglowki={"Retry-After": retry_after}),
        _odp(json_=_issue("cloud")),
    )
    czekanie = Odczekania()
    await _pobierz(serwer, _konf("cloud"), czekanie)
    assert czekanie.wartosci == [1.0]


async def test_zerowy_czas_odczekania_od_dostawcy_jest_respektowany():
    serwer = Serwer(
        _odp(429, tresc=b"", naglowki={"Retry-After": "0"}),
        _odp(json_=_issue("cloud")),
    )
    czekanie = Odczekania()
    await _pobierz(serwer, _konf("cloud"), czekanie)
    assert czekanie.wartosci == [0.0]


async def test_ulamkowy_czas_odczekania_od_dostawcy_jest_respektowany():
    serwer = Serwer(
        _odp(429, tresc=b"", naglowki={"Retry-After": "2.5"}),
        _odp(json_=_issue("cloud")),
    )
    czekanie = Odczekania()
    await _pobierz(serwer, _konf("cloud"), czekanie)
    assert czekanie.wartosci == [2.5]


async def test_bardzo_dlugi_czas_odczekania_od_dostawcy_jest_ucinany_do_minuty():
    serwer = Serwer(
        _odp(429, tresc=b"", naglowki={"Retry-After": "3600"}),
        _odp(json_=_issue("cloud")),
    )
    czekanie = Odczekania()
    await _pobierz(serwer, _konf("cloud"), czekanie)
    assert czekanie.wartosci == [60.0]


@pytest.mark.parametrize("tryb", TRYBY)
async def test_ciagly_limit_zapytan_przy_pobraniu_daje_blad_limit_po_trzech_zadaniach(tryb):
    serwer = Serwer(_odp(429, tresc=b""))
    czekanie = Odczekania()
    blad = await _kod_bledu(_pobierz(serwer, _konf(tryb), czekanie))
    assert blad.kod == "limit"
    assert "Jira" in blad.komunikat
    assert "3" in blad.komunikat
    assert len(serwer.zadania) == 3
    assert len(czekanie.wartosci) == 2


@pytest.mark.parametrize("tryb", TRYBY)
async def test_ciagly_limit_zapytan_przy_liscie_daje_blad_limit_po_trzech_zadaniach(tryb):
    serwer = Serwer(_odp(429, tresc=b""))
    czekanie = Odczekania()
    blad = await _kod_bledu(_listuj(serwer, _konf(tryb, jql=JQL), czekanie))
    assert blad.kod == "limit"
    assert len(serwer.zadania) == 3
    assert len(czekanie.wartosci) == 2


@pytest.mark.parametrize("tryb", TRYBY)
@pytest.mark.parametrize("status", [400, 403, 500, 502, 503])
async def test_status_inny_niz_obslugiwany_to_blad_dostawcy_nawet_gdy_tresc_wyglada_poprawnie(
    tryb, status
):
    serwer = Serwer(_odp(status, json_=_issue(tryb)))
    czekanie = Odczekania()
    blad = await _kod_bledu(_pobierz(serwer, _konf(tryb), czekanie))
    assert blad.kod == "blad_dostawcy"
    assert (len(serwer.zadania), czekanie.wartosci) == (1, [])


@pytest.mark.parametrize("tryb", TRYBY)
@pytest.mark.parametrize("status", [403, 500])
async def test_status_inny_niz_obslugiwany_przy_liscie_to_blad_dostawcy(tryb, status):
    serwer = Serwer(_odp(status, json_=_strona_cloud([_wpis("AB-1")])))
    blad = await _kod_bledu(_listuj(serwer, _konf(tryb, jql=JQL)))
    assert blad.kod == "blad_dostawcy"
    assert len(serwer.zadania) == 1


@pytest.mark.parametrize("wywolanie", ["pobierz", "listuj"])
@pytest.mark.parametrize("tryb", TRYBY)
@pytest.mark.parametrize(
    "tresc",
    [b"<html>to nie json</html>", b"", b"[]", b'"tekst"', b"null", b"42", b"true"],
    ids=["html", "pusta", "lista", "napis", "null", "liczba", "bool"],
)
async def test_odpowiedz_200_niebedaca_obiektem_json_to_blad_dostawcy(wywolanie, tryb, tresc):
    serwer = Serwer(_odp(200, tresc=tresc))
    konf = _konf(tryb, jql=JQL)
    korutyna = _pobierz(serwer, konf) if wywolanie == "pobierz" else _listuj(serwer, konf)
    blad = await _kod_bledu(korutyna)
    assert blad.kod == "blad_dostawcy"


@pytest.mark.parametrize("wywolanie", ["pobierz", "listuj"])
@pytest.mark.parametrize("tryb", TRYBY)
@pytest.mark.parametrize(
    "wyjatek",
    [
        httpx.ConnectError("nie polaczono"),
        httpx.ReadTimeout("wolno"),
        httpx.RemoteProtocolError("zerwano"),
    ],
    ids=["ConnectError", "ReadTimeout", "RemoteProtocolError"],
)
async def test_blad_transportu_to_blad_dostawcy_z_nazwa_klasy_wyjatku_bez_ponowien(
    wywolanie, tryb, wyjatek
):
    serwer = Serwer(wyjatek)
    czekanie = Odczekania()
    konf = _konf(tryb, jql=JQL)
    korutyna = (
        _pobierz(serwer, konf, czekanie)
        if wywolanie == "pobierz"
        else _listuj(serwer, konf, czekanie)
    )
    blad = await _kod_bledu(korutyna)
    assert blad.kod == "blad_dostawcy"
    assert type(wyjatek).__name__ in blad.komunikat
    assert str(wyjatek) not in blad.komunikat
    assert (len(serwer.zadania), czekanie.wartosci) == (1, [])


# --- zla struktura zgloszenia ----------------------------------------------

STRUKTURA_WSPOLNA = [
    pytest.param(lambda o: {}, id="pusty_obiekt"),
    pytest.param(_bez(["key"]), id="brak_klucza"),
    pytest.param(_ustaw(["key"], 7), id="klucz_jako_liczba"),
    pytest.param(_bez(["fields"]), id="brak_fields"),
    pytest.param(_ustaw(["fields"], "tekst"), id="fields_jako_napis"),
    pytest.param(_bez(["fields", "summary"]), id="brak_tytulu"),
    pytest.param(_ustaw(["fields", "summary"], 123), id="tytul_jako_liczba"),
    pytest.param(_bez(["fields", "status"]), id="brak_statusu"),
    pytest.param(_ustaw(["fields", "status"], None), id="status_null"),
    pytest.param(_ustaw(["fields", "status"], "W recenzji"), id="status_jako_napis"),
    pytest.param(_bez(["fields", "status", "name"]), id="status_bez_nazwy"),
    pytest.param(_ustaw(["fields", "priority"], {"id": "2"}), id="priorytet_bez_nazwy"),
    pytest.param(_ustaw(["fields", "priority"], "High"), id="priorytet_jako_napis"),
    pytest.param(_ustaw(["fields", "labels"], [1, 2]), id="etykiety_jako_liczby"),
    pytest.param(_ustaw(["fields", "comment"], "tekst"), id="comment_jako_napis"),
    pytest.param(_ustaw(["fields", "comment", "comments"], None), id="comments_null"),
    pytest.param(_ustaw(["fields", "comment", "comments"], ["tekst"]), id="komentarz_jako_napis"),
    pytest.param(
        _ustaw(["fields", "comment", "comments", 0, "author"], {"accountId": "x"}),
        id="autor_bez_nazwy",
    ),
    pytest.param(_bez(["fields", "comment", "comments", 0, "body"]), id="komentarz_bez_tresci"),
    pytest.param(_ustaw(["fields", "comment", "comments", 0, "created"], "wczoraj"), id="data_spoza_formatu"),
    pytest.param(_ustaw(["fields", "comment", "comments", 0, "created"], "2026-03-04T12:30:15"), id="data_bez_strefy"),
    pytest.param(_ustaw(["fields", "comment", "comments", 0, "created"], 20260304), id="data_jako_liczba"),
]


@pytest.mark.parametrize("tryb", TRYBY)
@pytest.mark.parametrize("zmiana", STRUKTURA_WSPOLNA)
async def test_zgloszenie_o_niespodziewanej_budowie_to_czytelny_blad_dostawcy(tryb, zmiana):
    odpowiedz = zmiana(copy.deepcopy(_issue(tryb)))
    blad = await _kod_bledu(_pobierz(Serwer(_odp(json_=odpowiedz)), _konf(tryb)))
    assert blad.kod == "blad_dostawcy"


@pytest.mark.parametrize("wartosc", [5, ["a"], True], ids=str)
async def test_opis_data_center_niebedacy_napisem_to_blad_dostawcy(wartosc):
    odpowiedz = _ustaw(["fields", "description"], wartosc)(_issue("data_center"))
    blad = await _kod_bledu(_pobierz(Serwer(_odp(json_=odpowiedz)), _konf("data_center")))
    assert blad.kod == "blad_dostawcy"


@pytest.mark.parametrize("wartosc", [None, 5], ids=str)
async def test_tresc_komentarza_data_center_niebedaca_napisem_to_blad_dostawcy(wartosc):
    odpowiedz = _ustaw(["fields", "comment", "comments", 0, "body"], wartosc)(
        _issue("data_center")
    )
    blad = await _kod_bledu(_pobierz(Serwer(_odp(json_=odpowiedz)), _konf("data_center")))
    assert blad.kod == "blad_dostawcy"


# --- nic nie wycieka -------------------------------------------------------


def _scenariusze_bledow(tryb):
    sekret_w_tresci = {"debug": {"authorization": BASIC if tryb == "cloud" else BEARER}}
    sekret_w_tresci["email"] = EMAIL
    return {
        "401": (Serwer(_odp(401, json_=sekret_w_tresci)), "token_odrzucony"),
        "404": (Serwer(_odp(404, json_=sekret_w_tresci)), "nie_znaleziono"),
        "429_ciagle": (Serwer(_odp(429, json_=sekret_w_tresci)), "limit"),
        "500": (Serwer(_odp(500, json_=sekret_w_tresci)), "blad_dostawcy"),
        "nie_json": (
            Serwer(_odp(200, tresc=f"<html>{TOKEN} {EMAIL}</html>".encode())),
            "blad_dostawcy",
        ),
        "zla_struktura": (
            Serwer(_odp(200, json_={**sekret_w_tresci, "key": TOKEN})),
            "blad_dostawcy",
        ),
        "polaczenie": (
            Serwer(httpx.ConnectError(f"nie polaczono {TOKEN} {EMAIL} {BASIC}")),
            "blad_dostawcy",
        ),
    }


def _nic_nie_wycieka(blad, capsys):
    wyjscie = capsys.readouterr()
    for tekst in (str(blad), repr(blad), blad.komunikat, wyjscie.out, wyjscie.err):
        assert _sekrety_w(tekst) == []


@pytest.mark.parametrize("tryb", TRYBY)
@pytest.mark.parametrize("nazwa", list(_scenariusze_bledow("cloud")))
async def test_token_i_email_nie_wyciekaja_z_bledu_pobrania_w_zadnej_sciezce(
    tryb, nazwa, capsys
):
    serwer, kod = _scenariusze_bledow(tryb)[nazwa]
    blad = await _kod_bledu(_pobierz(serwer, _konf(tryb)))
    assert blad.kod == kod
    _nic_nie_wycieka(blad, capsys)


@pytest.mark.parametrize("tryb", TRYBY)
@pytest.mark.parametrize("nazwa", ["401", "429_ciagle", "500", "nie_json", "polaczenie"])
async def test_token_i_email_nie_wyciekaja_z_bledu_listy(tryb, nazwa, capsys):
    serwer, kod = _scenariusze_bledow(tryb)[nazwa]
    blad = await _kod_bledu(_listuj(serwer, _konf(tryb, jql=JQL)))
    assert blad.kod == kod
    _nic_nie_wycieka(blad, capsys)


async def test_brak_emaila_w_komunikacie_nie_ujawnia_tokenu(bez_emaila, capsys):
    blad = await _kod_bledu(_pobierz(Serwer(_odp(json_=_issue("cloud"))), _konf("cloud")))
    assert blad.kod == "brak_tokenu"
    _nic_nie_wycieka(blad, capsys)


@pytest.mark.parametrize("tryb", TRYBY)
async def test_udane_pobranie_i_lista_niczego_nie_wypisuja(tryb, capsys):
    await _pobierz(Serwer(_odp(json_=_issue(tryb))), _konf(tryb, pole_kryteriow=POLE))
    strona = _strona_cloud([_wpis("AB-1")]) if tryb == "cloud" else _strona_dc([_wpis("AB-1")], 0, 1)
    await _listuj(Serwer(_odp(json_=strona)), _konf(tryb, jql=JQL))
    wyjscie = capsys.readouterr()
    assert (wyjscie.out, wyjscie.err) == ("", "")
