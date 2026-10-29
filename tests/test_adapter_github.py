"""Adapter GitHub Issues: pobranie zgloszenia i lista po etykietach i stanie (HAR-65)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from runner import secrets
from runner.tickety import TicketJson, do_pliku, github, linear
from runner.tickety.bledy import BladIntegracji

TOKEN = "ghp_SEKRET_HAR65"
TERAZ = datetime(2026, 3, 4, 12, 30, 15, tzinfo=timezone.utc)
KATALOG_FIXTURE = Path(__file__).parent / "fixtures" / "tickety"
FIXTURE = KATALOG_FIXTURE / "github_issue.json"
FIXTURE_LINEARA = KATALOG_FIXTURE / "linear_issue.json"

REPO = "LukBro/harness-cockpit"
ID = "LukBro/harness-cockpit#65"
URL_ZGLOSZENIA = "https://api.github.com/repos/LukBro/harness-cockpit/issues/65"
URL_KOMENTARZY = URL_ZGLOSZENIA + "/comments"
URL_LISTY = "https://api.github.com/repos/LukBro/harness-cockpit/issues"

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

KOMUNIKAT_STRUKTURA = "GitHub: odpowiedź ma nieoczekiwaną strukturę."

NAGLOWKI = {
    "authorization": f"Bearer {TOKEN}",
    "accept": "application/vnd.github+json",
    "x-github-api-version": "2022-11-28",
    "user-agent": "harness-cockpit",
}

ETAPY = ["zgloszenie", "komentarze", "lista"]


# --- pomocnicy -------------------------------------------------------------


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _issue() -> dict:
    return _fixture()["issue"]


def _komentarze() -> list:
    return _fixture()["comments"]


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


def _wpis(numer, tytul="Tytuł", stan="open", *, pr=False):
    wpis = {
        "number": numer,
        "title": tytul,
        "state": stan,
        "html_url": f"https://github.com/LukBro/harness-cockpit/issues/{numer}",
    }
    if pr:
        wpis["pull_request"] = {"url": f"https://api.github.com/repos/x/y/pulls/{numer}"}
    return wpis


def _serwer_ok() -> Serwer:
    return Serwer(_odp(json_=_issue()), _odp(json_=_komentarze()))


def _serwer_z(issue, komentarze) -> Serwer:
    return Serwer(
        _odp(tresc=json.dumps(issue).encode()),
        _odp(tresc=json.dumps(komentarze).encode()),
    )


def _serwer_etapu(etap, *wadliwe, ok_po=False) -> Serwer:
    """Serwer, ktory na wskazanym etapie (pobranie, komentarze, lista) daje `wadliwe` kroki."""
    if etap == "zgloszenie":
        po = [_odp(json_=_issue()), _odp(json_=_komentarze())] if ok_po else []
        return Serwer(*wadliwe, *po)
    if etap == "komentarze":
        po = [_odp(json_=_komentarze())] if ok_po else []
        return Serwer(_odp(json_=_issue()), *wadliwe, *po)
    po = [_odp(json_=[_wpis(1)])] if ok_po else []
    return Serwer(*wadliwe, *po)


def _zadan_na_etapie(serwer: Serwer, etap: str) -> int:
    """Liczba zadan wyslanych na wadliwym etapie (bez udanego pobrania zgloszenia)."""
    return len(serwer.zadania) - (1 if etap == "komentarze" else 0)


@pytest.fixture(autouse=True)
def _token_i_pusty_plik_sekretow(monkeypatch, tmp_path):
    monkeypatch.setenv("COCKPIT_GITHUB_TOKEN", TOKEN)
    monkeypatch.setattr(secrets, "CONFIG", tmp_path / "brak.json")


@pytest.fixture
def bez_tokenu(monkeypatch, tmp_path):
    monkeypatch.delenv("COCKPIT_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(secrets, "CONFIG", tmp_path / "brak.json")


async def _pobierz(serwer, odczekania=None, id=ID):
    return await github.pobierz(
        id,
        transport=serwer.transport,
        odczekaj=odczekania if odczekania is not None else Odczekania(),
        teraz=TERAZ,
    )


async def _listuj(serwer, odczekania=None, repo=REPO, etykiety=(), **reszta):
    return await github.listuj(
        repo,
        list(etykiety),
        transport=serwer.transport,
        odczekaj=odczekania if odczekania is not None else Odczekania(),
        **reszta,
    )


async def _wywolaj(etap, serwer, odczekania=None):
    if etap == "lista":
        return await _listuj(serwer, odczekania)
    return await _pobierz(serwer, odczekania)


async def _kod_bledu(korutyna) -> BladIntegracji:
    with pytest.raises(BladIntegracji) as exc:
        await korutyna
    return exc.value


def _bez_zapytania(url: httpx.URL) -> str:
    return str(url.copy_with(query=None))


def _link(nastepna=None, poprzednia=None, ostatnia=None) -> str:
    czesci = []
    if poprzednia:
        czesci.append(f'<{poprzednia}>; rel="prev"')
    if nastepna:
        czesci.append(f'<{nastepna}>; rel="next"')
    if ostatnia:
        czesci.append(f'<{ostatnia}>; rel="last"')
    return ", ".join(czesci)


URL_STRONY_KOMENTARZY = "https://api.github.com/repositories/777/issues/65/comments?per_page=100&page={}"
URL_STRONY_LISTY = "https://api.github.com/repositories/777/issues?state=open&per_page=100&page={}"


def _strony(ile, wzor_url, zawartosc):
    """`ile` stron; kazda poza ostatnia wskazuje nastepna w naglowku Link."""
    kroki = []
    for i in range(1, ile + 1):
        if i < ile:
            naglowek = _link(
                nastepna=wzor_url.format(i + 1),
                ostatnia=wzor_url.format(ile),
                poprzednia=wzor_url.format(i - 1) if i > 1 else None,
            )
        else:
            naglowek = _link(poprzednia=wzor_url.format(i - 1)) if i > 1 else ""
        naglowki = {"Link": naglowek} if naglowek else None
        kroki.append(_odp(json_=zawartosc(i), naglowki=naglowki))
    return kroki


def _komentarz_nr(i):
    return {
        "body": f"Komentarz {i}",
        "user": {"login": f"autor{i}"},
        "created_at": "2026-03-01T09:15:00Z",
    }


# --- pobierz: odwzorowanie na TicketJson ----------------------------------


async def test_zgloszenie_z_odpowiedzi_github_trafia_do_kontraktu_ticketu_ze_wszystkimi_polami():
    fixture = _fixture()
    ticket = await _pobierz(_serwer_ok())

    assert isinstance(ticket, TicketJson)
    assert ticket.source == "github"
    assert ticket.id == ID
    assert ticket.url == "https://github.com/LukBro/harness-cockpit/issues/65"
    assert ticket.title == "Adapter GitHub Issues: pobranie zgłoszenia"
    assert ticket.description == (
        "Zażółć gęślą jaźń.\n\nRunner pobiera zgłoszenie przez REST API GitHuba."
    )
    assert ticket.acceptance_criteria == []
    assert ticket.status == "closed"
    assert ticket.priority is None
    assert ticket.labels == ["backend", "integracje"]
    assert [(k.author, k.body) for k in ticket.comments] == [
        ("anna-nowak", "Sprawdź limit zapytań — żądań jest dużo."),
        ("nieznany", "Komentarz automatu bez autora."),
    ]
    assert ticket.comments[0].at == datetime(2026, 3, 1, 9, 15, tzinfo=timezone.utc)
    assert ticket.comments[1].at == datetime(2026, 3, 2, 18, 45, 30, tzinfo=timezone.utc)
    assert ticket.comments[0].at.utcoffset() == timedelta(0)
    assert ticket.fetched_at == TERAZ
    assert ticket.raw == fixture["issue"]


async def test_status_otwartego_zgloszenia_to_open_a_zamknietego_closed_dokladnie_jak_w_zrodle():
    otwarte = _issue()
    otwarte["state"] = "open"
    ticket_otwarty = await _pobierz(_serwer_z(otwarte, []))
    ticket_zamkniety = await _pobierz(_serwer_ok())
    assert ticket_otwarty.status == "open"
    assert ticket_zamkniety.status == "closed"


async def test_identyfikator_ticketu_to_wejscie_a_nie_numer_z_odpowiedzi_ani_adres_html():
    # wielkosc liter i numer z odpowiedzi (65) roznia sie od wejscia
    ticket = await _pobierz(_serwer_ok(), id="lukbro/Harness-Cockpit#65")
    assert ticket.id == "lukbro/Harness-Cockpit#65"


async def test_priorytet_github_nie_istnieje_wiec_jest_brakiem_a_nie_napisem_ani_zerem():
    issue = _issue()
    issue["labels"].append({"id": 3, "name": "P1", "color": "ff0000"})
    issue["priority"] = "high"
    ticket = await _pobierz(_serwer_z(issue, []))
    assert ticket.priority is None


async def test_github_nie_ma_kryteriow_akceptacji_wiec_lista_jest_pusta_nawet_gdy_opis_ma_checklist():
    issue = _issue()
    issue["body"] = "Opis\n- [ ] kryterium w tresci\n- [x] drugie"
    ticket = await _pobierz(_serwer_z(issue, []))
    assert ticket.acceptance_criteria == []


async def test_brak_opisu_w_github_daje_pusty_opis_a_nie_null():
    issue = _issue()
    issue["body"] = None
    ticket = await _pobierz(_serwer_z(issue, []))
    assert ticket.description == ""
    assert ticket.raw["body"] is None


async def test_surowa_odpowiedz_w_raw_to_samo_zgloszenie_bez_komentarzy_i_bez_zmian():
    fixture = _fixture()
    ticket = await _pobierz(_serwer_z(fixture["issue"], fixture["comments"]))
    assert ticket.raw == fixture["issue"]
    # REST zwraca w `comments` liczbe komentarzy: to nie ma byc ich lista
    assert ticket.raw["comments"] == 2
    assert ticket.raw["state_reason"] == "completed"


async def test_zgloszenie_bez_etykiet_i_komentarzy_ma_puste_listy():
    issue = _issue()
    issue["labels"] = []
    ticket = await _pobierz(_serwer_z(issue, []))
    assert (ticket.labels, ticket.comments) == ([], [])


async def test_kolejnosc_etykiet_i_komentarzy_jest_taka_jak_w_zrodle():
    issue, komentarze = _issue(), _komentarze()
    issue["labels"].reverse()
    komentarze.reverse()
    ticket = await _pobierz(_serwer_z(issue, komentarze))
    assert ticket.labels == ["integracje", "backend"]
    assert [k.author for k in ticket.comments] == ["nieznany", "anna-nowak"]


async def test_etykieta_to_jej_nazwa_a_nie_kolor_ani_numer():
    ticket = await _pobierz(_serwer_ok())
    assert "0e8a16" not in ticket.labels
    assert 6100000001 not in ticket.labels
    assert ticket.labels[0] == "backend"


async def test_komentarz_bez_autora_ma_autora_nieznany_a_z_autorem_jego_login():
    ticket = await _pobierz(_serwer_ok())
    assert ticket.comments[0].author == "anna-nowak"
    assert ticket.comments[1].author == "nieznany"


async def test_bez_wstrzyknietego_czasu_data_pobrania_to_teraz_w_utc():
    przed = datetime.now(timezone.utc)
    ticket = await github.pobierz(ID, transport=_serwer_ok().transport)
    po = datetime.now(timezone.utc)
    assert ticket.fetched_at.utcoffset() == timedelta(0)
    assert przed <= ticket.fetched_at <= po


async def test_pobrane_zgloszenie_zapisuje_sie_do_poprawnego_ticket_json():
    ticket = await _pobierz(_serwer_ok())
    bajty = do_pliku(ticket)
    wczytany = json.loads(bajty)
    assert list(wczytany.keys()) == KOLEJNOSC
    assert wczytany["source"] == "github"
    assert wczytany["status"] == "closed"
    assert wczytany["priority"] is None
    assert wczytany["acceptance_criteria"] == []
    assert "Zażółć gęślą jaźń".encode("utf-8") in bajty
    assert TicketJson.model_validate_json(bajty) == ticket


# --- ten sam ksztalt co z Lineara -----------------------------------------


async def test_ticket_json_z_github_ma_te_same_klucze_w_tej_samej_kolejnosci_co_z_lineara(
    monkeypatch,
):
    monkeypatch.setenv("COCKPIT_LINEAR_TOKEN", "lin_api_SEKRET_HAR65")
    odpowiedz_lineara = json.loads(FIXTURE_LINEARA.read_text(encoding="utf-8"))
    ticket_lineara = await linear.pobierz(
        "HAR-53",
        transport=Serwer(_odp(json_=odpowiedz_lineara)).transport,
        odczekaj=Odczekania(),
        teraz=TERAZ,
    )
    ticket_github = await _pobierz(_serwer_ok())

    plik_lineara = json.loads(do_pliku(ticket_lineara))
    plik_github = json.loads(do_pliku(ticket_github))

    assert list(plik_github.keys()) == list(plik_lineara.keys())
    assert list(plik_github.keys()) == KOLEJNOSC
    assert (plik_lineara["source"], plik_github["source"]) == ("linear", "github")
    assert len(plik_github["comments"]) == len(plik_lineara["comments"]) == 2
    for komentarz_github, komentarz_lineara in zip(
        plik_github["comments"], plik_lineara["comments"], strict=True
    ):
        assert list(komentarz_github.keys()) == list(komentarz_lineara.keys())
    # roznia sie wylacznie wartosci; typy pol, ktore maja wartosc w obu, sa te same
    for klucz in KOLEJNOSC:
        if plik_github[klucz] is not None and plik_lineara[klucz] is not None:
            assert type(plik_github[klucz]) is type(plik_lineara[klucz]), klucz


# --- pobierz: wysylane zadania --------------------------------------------


async def test_zgloszenie_jest_pobierane_zadaniem_get_pod_adresem_numeru_w_repozytorium():
    serwer = _serwer_ok()
    await _pobierz(serwer)
    zadanie = serwer.zadania[0]
    assert zadanie.method == "GET"
    assert str(zadanie.url) == URL_ZGLOSZENIA


async def test_komentarze_sa_pobierane_zadaniem_get_po_sto_na_strone_pod_adresem_zgloszenia():
    serwer = _serwer_ok()
    await _pobierz(serwer)
    assert len(serwer.zadania) == 2
    zadanie = serwer.zadania[1]
    assert zadanie.method == "GET"
    assert _bez_zapytania(zadanie.url) == URL_KOMENTARZY
    assert dict(zadanie.url.params) == {"per_page": "100"}


async def test_adres_zadania_niesie_wlasciciela_repozytorium_i_numer_z_identyfikatora():
    serwer = _serwer_ok()
    await _pobierz(serwer, id="inny-zespol/inne_repo.v2#907")
    assert str(serwer.zadania[0].url) == (
        "https://api.github.com/repos/inny-zespol/inne_repo.v2/issues/907"
    )
    assert _bez_zapytania(serwer.zadania[1].url) == (
        "https://api.github.com/repos/inny-zespol/inne_repo.v2/issues/907/comments"
    )


@pytest.mark.parametrize("naglowek", list(NAGLOWKI), ids=list(NAGLOWKI))
async def test_zadanie_o_zgloszenie_niesie_wymagany_naglowek(naglowek):
    serwer = _serwer_ok()
    await _pobierz(serwer)
    assert serwer.zadania[0].headers[naglowek] == NAGLOWKI[naglowek]


@pytest.mark.parametrize("naglowek", list(NAGLOWKI), ids=list(NAGLOWKI))
async def test_zadanie_o_komentarze_niesie_wymagany_naglowek(naglowek):
    serwer = _serwer_ok()
    await _pobierz(serwer)
    assert serwer.zadania[1].headers[naglowek] == NAGLOWKI[naglowek]


@pytest.mark.parametrize("naglowek", list(NAGLOWKI), ids=list(NAGLOWKI))
async def test_zadanie_o_liste_niesie_wymagany_naglowek(naglowek):
    serwer = Serwer(_odp(json_=[]))
    await _listuj(serwer)
    assert serwer.zadania[0].headers[naglowek] == NAGLOWKI[naglowek]


async def test_autoryzacja_to_bearer_ze_spacja_i_tokenem_a_nie_sam_token():
    serwer = _serwer_ok()
    await _pobierz(serwer)
    autoryzacja = serwer.zadania[0].headers["authorization"]
    assert autoryzacja == f"Bearer {TOKEN}"
    assert autoryzacja.startswith("Bearer ")
    assert autoryzacja != TOKEN


async def test_wersja_api_jest_podana_jawnie_na_kazdym_zadaniu_takze_na_kolejnych_stronach():
    serwer = Serwer(
        _odp(json_=_issue()),
        *_strony(3, URL_STRONY_KOMENTARZY, lambda i: [_komentarz_nr(i)]),
    )
    await _pobierz(serwer)
    assert len(serwer.zadania) == 4
    for zadanie in serwer.zadania:
        for naglowek, wartosc in NAGLOWKI.items():
            assert zadanie.headers[naglowek] == wartosc


async def test_naglowki_kolejnych_stron_listy_sa_te_same_co_pierwszej():
    serwer = Serwer(*_strony(3, URL_STRONY_LISTY, lambda i: [_wpis(i)]))
    await _listuj(serwer)
    assert len(serwer.zadania) == 3
    for zadanie in serwer.zadania:
        for naglowek, wartosc in NAGLOWKI.items():
            assert zadanie.headers[naglowek] == wartosc


async def test_token_z_pliku_sekretow_jest_uzywany_gdy_nie_ma_zmiennej_srodowiskowej(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("COCKPIT_GITHUB_TOKEN")
    plik = tmp_path / "credentials.json"
    plik.write_text(json.dumps({"github": "ghp_Z_PLIKU"}), encoding="utf-8")
    monkeypatch.setattr(secrets, "CONFIG", plik)
    serwer = _serwer_ok()
    await _pobierz(serwer)
    assert serwer.zadania[0].headers["authorization"] == "Bearer ghp_Z_PLIKU"


# --- identyfikator zgloszenia ----------------------------------------------

ZLE_IDENTYFIKATORY = [
    "owner/repo",
    "repo#1",
    "owner/repo#",
    "owner/repo#0",
    "owner/repo#012",
    "owner/repo#-1",
    "owner/repo#abc",
    "owner/repo#12x",
    "owner/repo#1.5",
    "owner/repo##1",
    "owner/repo#1 ",
    "owner/repo#1\n",
    " owner/repo#1",
    "own er/repo#1",
    "owner/re po#1",
    "owner/repo #1",
    "own:er/repo#1",
    "owner/re:po#1",
    "own@er/repo#1",
    "ówner/repo#1",
    "owner/rępo#1",
    "/repo#1",
    "owner/#1",
    "owner/repo/extra#1",
    "a/b/c#1",
    "",
    "#1",
    "/#1",
]


@pytest.mark.parametrize("id", ZLE_IDENTYFIKATORY, ids=[repr(i) for i in ZLE_IDENTYFIKATORY])
async def test_niepoprawny_identyfikator_to_nie_znaleziono_bez_zadnego_zadania(id):
    serwer = _serwer_ok()
    blad = await _kod_bledu(_pobierz(serwer, id=id))
    assert (blad.dostawca, blad.kod) == ("GitHub", "nie_znaleziono")
    assert blad.komunikat.startswith("GitHub: niepoprawny identyfikator zgłoszenia")
    assert blad.komunikat.endswith("oczekiwano owner/repo#numer.")
    assert serwer.zadania == []


@pytest.mark.parametrize("id", ZLE_IDENTYFIKATORY, ids=[repr(i) for i in ZLE_IDENTYFIKATORY])
async def test_niepoprawny_identyfikator_jest_odrzucany_przed_odczytem_tokenu(bez_tokenu, id):
    blad = await _kod_bledu(_pobierz(_serwer_ok(), id=id))
    assert blad.kod == "nie_znaleziono"


@pytest.mark.parametrize(
    "id",
    [
        "o.w/r#1",
        "o_w/r#1",
        "o-w/r#1",
        "o/r.x#1",
        "o/r_x#1",
        "o/r-x#1",
        "my.org/my_repo-1#5",
        "ABC/xyz09#10",
        "a/b#907",
    ],
)
async def test_identyfikator_ze_znakami_dozwolonymi_w_nazwach_github_jest_przyjety(id):
    serwer = _serwer_ok()
    ticket = await _pobierz(serwer, id=id)
    nazwa, numer = id.split("#")
    assert ticket.id == id
    assert serwer.zadania[0].url.path == f"/repos/{nazwa}/issues/{numer}"


# --- pull requesty ---------------------------------------------------------


async def test_pull_request_pobrany_jako_zgloszenie_to_nie_znaleziono_i_nie_pobiera_komentarzy():
    pr = _issue()
    pr["pull_request"] = {"url": "https://api.github.com/repos/LukBro/harness-cockpit/pulls/65"}
    serwer = Serwer(_odp(json_=pr), _odp(json_=_komentarze()))
    blad = await _kod_bledu(_pobierz(serwer))
    assert (blad.dostawca, blad.kod) == ("GitHub", "nie_znaleziono")
    assert "pull request" in blad.komunikat
    assert len(serwer.zadania) == 1


async def test_pull_request_jest_rozpoznany_zanim_sprawdzimy_reszte_odpowiedzi():
    # minimalna odpowiedz: rozpoznanie PR nie moze zalezec od pozostalych pol
    serwer = Serwer(_odp(json_={"pull_request": {"url": "x"}}))
    blad = await _kod_bledu(_pobierz(serwer))
    assert blad.kod == "nie_znaleziono"
    assert len(serwer.zadania) == 1


# --- pobierz: paginacja komentarzy -----------------------------------------


async def test_komentarze_z_kolejnych_stron_sa_sklejane_w_kolejnosci_a_adres_strony_idzie_z_linku():
    serwer = Serwer(
        _odp(json_=_issue()),
        *_strony(3, URL_STRONY_KOMENTARZY, lambda i: [_komentarz_nr(i), _komentarz_nr(i * 10)]),
    )
    ticket = await _pobierz(serwer)
    assert [k.body for k in ticket.comments] == [
        "Komentarz 1",
        "Komentarz 10",
        "Komentarz 2",
        "Komentarz 20",
        "Komentarz 3",
        "Komentarz 30",
    ]
    assert len(serwer.zadania) == 4
    # adres kolejnej strony to dokladnie ten z naglowka Link (inna sciezka niz pierwsza)
    assert str(serwer.zadania[2].url) == URL_STRONY_KOMENTARZY.format(2)
    assert str(serwer.zadania[3].url) == URL_STRONY_KOMENTARZY.format(3)


async def test_komentarze_koncza_sie_na_stronie_bez_rel_next_nawet_gdy_link_wskazuje_inne_strony():
    serwer = Serwer(
        _odp(json_=_issue()),
        _odp(
            json_=[_komentarz_nr(1)],
            naglowki={
                "Link": _link(
                    poprzednia=URL_STRONY_KOMENTARZY.format(0),
                    ostatnia=URL_STRONY_KOMENTARZY.format(9),
                )
            },
        ),
        _odp(json_=[_komentarz_nr(2)]),
    )
    ticket = await _pobierz(serwer)
    assert [k.body for k in ticket.comments] == ["Komentarz 1"]
    assert len(serwer.zadania) == 2


async def test_komentarze_na_dokladnie_dwudziestu_stronach_sa_kompletne():
    serwer = Serwer(
        _odp(json_=_issue()),
        *_strony(20, URL_STRONY_KOMENTARZY, lambda i: [_komentarz_nr(i)]),
    )
    ticket = await _pobierz(serwer)
    assert [k.body for k in ticket.comments] == [f"Komentarz {i}" for i in range(1, 21)]
    assert len(serwer.zadania) == 21


async def test_komentarze_na_dwudziestu_jeden_stronach_to_blad_a_nie_ucieta_lista():
    serwer = Serwer(
        _odp(json_=_issue()),
        *_strony(21, URL_STRONY_KOMENTARZY, lambda i: [_komentarz_nr(i)]),
    )
    blad = await _kod_bledu(_pobierz(serwer))
    assert (blad.dostawca, blad.kod) == ("GitHub", "blad_dostawcy")
    assert "nie zwracam niepełnej listy" in blad.komunikat


async def test_zla_struktura_komentarzy_na_drugiej_stronie_to_blad_dostawcy():
    serwer = Serwer(
        _odp(json_=_issue()),
        *[
            _odp(
                json_=[_komentarz_nr(1)],
                naglowki={"Link": _link(nastepna=URL_STRONY_KOMENTARZY.format(2))},
            ),
            _odp(json_={}),
        ],
    )
    blad = await _kod_bledu(_pobierz(serwer))
    assert (blad.dostawca, blad.kod) == ("GitHub", "blad_dostawcy")
    assert blad.komunikat == KOMUNIKAT_STRUKTURA


# --- pobierz: zla budowa odpowiedzi ---------------------------------------


def _usun(sciezka):
    def zmien(o):
        wezel = o
        for klucz in sciezka[:-1]:
            wezel = wezel[klucz]
        del wezel[sciezka[-1]]
        return o

    return zmien


def _ustaw(sciezka, wartosc):
    def zmien(o):
        wezel = o
        for klucz in sciezka[:-1]:
            wezel = wezel[klucz]
        wezel[sciezka[-1]] = wartosc
        return o

    return zmien


ZLE_ZGLOSZENIA = {
    "zgloszenie_jako_lista": _ustaw(["issue"], []),
    "zgloszenie_jako_napis": _ustaw(["issue"], "tekst"),
    "zgloszenie_null": _ustaw(["issue"], None),
    "zgloszenie_jako_liczba": _ustaw(["issue"], 5),
    # `in` na napisie/liscie nie rzuca wyjatku: tylko warunek typu odroznia je od
    # pull requesta (bez niego byloby `nie_znaleziono` zamiast bledu struktury)
    "zgloszenie_jako_napis_pull_request": _ustaw(["issue"], "pull_request"),
    "zgloszenie_jako_lista_z_pull_request": _ustaw(["issue"], ["pull_request"]),
    "brak_tytulu": _usun(["issue", "title"]),
    "tytul_jako_liczba": _ustaw(["issue", "title"], 123),
    "brak_adresu_html": _usun(["issue", "html_url"]),
    "adres_html_jako_liczba": _ustaw(["issue", "html_url"], 5),
    "brak_stanu": _usun(["issue", "state"]),
    "stan_jako_liczba": _ustaw(["issue", "state"], 5),
    "brak_etykiet": _usun(["issue", "labels"]),
    "etykiety_null": _ustaw(["issue", "labels"], None),
    "etykiety_jako_napis": _ustaw(["issue", "labels"], "backend"),
    "etykieta_bez_nazwy": _ustaw(["issue", "labels"], [{"id": 1, "color": "ffffff"}]),
    "etykieta_jako_napis": _ustaw(["issue", "labels"], ["backend"]),
    "komentarze_jako_pusty_obiekt": _ustaw(["comments"], {}),
    "komentarze_jako_pusty_napis": _ustaw(["comments"], ""),
    "komentarze_jako_obiekt": _ustaw(["comments"], {"message": "x"}),
    "komentarze_null": _ustaw(["comments"], None),
    "komentarz_jako_napis": _ustaw(["comments"], ["tekst"]),
    "komentarz_bez_tresci": _usun(["comments", 0, "body"]),
    "komentarz_bez_daty": _usun(["comments", 0, "created_at"]),
    "komentarz_z_data_spoza_formatu": _ustaw(["comments", 0, "created_at"], "wczoraj"),
    "komentarz_z_data_bez_strefy": _ustaw(["comments", 0, "created_at"], "2026-03-01T09:15:00"),
    "komentarz_bez_klucza_autora": _usun(["comments", 0, "user"]),
    "autor_komentarza_jako_napis": _ustaw(["comments", 0, "user"], "tekst"),
    "autor_komentarza_bez_loginu": _ustaw(["comments", 0, "user"], {"id": 1}),
}


@pytest.mark.parametrize("zmiana", list(ZLE_ZGLOSZENIA.values()), ids=list(ZLE_ZGLOSZENIA))
async def test_zgloszenie_o_niespodziewanej_budowie_to_czytelny_blad_dostawcy(zmiana):
    o = zmiana(_fixture())
    blad = await _kod_bledu(_pobierz(_serwer_z(o["issue"], o["comments"])))
    assert (blad.dostawca, blad.kod) == ("GitHub", "blad_dostawcy")
    assert blad.komunikat == KOMUNIKAT_STRUKTURA


# --- listuj: zadanie i parametry ------------------------------------------


async def test_lista_idzie_zadaniem_get_pod_adresem_zgloszen_repozytorium():
    serwer = Serwer(_odp(json_=[]))
    await _listuj(serwer)
    zadanie = serwer.zadania[0]
    assert zadanie.method == "GET"
    assert _bez_zapytania(zadanie.url) == URL_LISTY


async def test_lista_bez_podanego_stanu_pyta_o_otwarte_zgloszenia():
    serwer = Serwer(_odp(json_=[]))
    await github.listuj(REPO, [], transport=serwer.transport, odczekaj=Odczekania())
    assert dict(serwer.zadania[0].url.params)["state"] == "open"


@pytest.mark.parametrize("stan", ["open", "closed", "all"])
async def test_lista_przekazuje_wybrany_stan_zgloszen_w_zapytaniu(stan):
    serwer = Serwer(_odp(json_=[]))
    await _listuj(serwer, stan=stan)
    assert dict(serwer.zadania[0].url.params) == {"state": stan, "per_page": "100"}


async def test_lista_bez_etykiet_nie_filtruje_po_etykietach_i_prosi_o_sto_na_strone():
    serwer = Serwer(_odp(json_=[]))
    await _listuj(serwer, etykiety=[])
    assert "labels" not in serwer.zadania[0].url.params
    assert dict(serwer.zadania[0].url.params) == {"state": "open", "per_page": "100"}


async def test_lista_z_jedna_etykieta_przekazuje_ja_w_zapytaniu():
    serwer = Serwer(_odp(json_=[]))
    await _listuj(serwer, etykiety=["backend"])
    assert dict(serwer.zadania[0].url.params) == {
        "state": "open",
        "per_page": "100",
        "labels": "backend",
    }


async def test_lista_z_dwiema_etykietami_laczy_je_przecinkiem_bez_spacji_w_kolejnosci_podania():
    serwer = Serwer(_odp(json_=[]))
    await _listuj(serwer, etykiety=["integracje", "backend"])
    assert dict(serwer.zadania[0].url.params)["labels"] == "integracje,backend"
    assert len(serwer.zadania[0].url.params.get_list("labels")) == 1


async def test_lista_nie_psuje_etykiet_ze_spacjami_i_polskimi_znakami():
    serwer = Serwer(_odp(json_=[]))
    await _listuj(serwer, etykiety=["w recenzji", "błąd"], stan="closed")
    assert dict(serwer.zadania[0].url.params) == {
        "state": "closed",
        "per_page": "100",
        "labels": "w recenzji,błąd",
    }


async def test_lista_z_etykietami_i_stanem_niesie_wszystkie_trzy_parametry_naraz():
    serwer = Serwer(_odp(json_=[]))
    await _listuj(serwer, etykiety=["backend", "integracje"], stan="all")
    assert dict(serwer.zadania[0].url.params) == {
        "state": "all",
        "per_page": "100",
        "labels": "backend,integracje",
    }


@pytest.mark.parametrize("stan", ["OPEN", "Closed", "", "merged", "wszystkie", "open,closed", None])
async def test_lista_ze_stanem_spoza_open_closed_all_to_blad_programisty_bez_zadania(stan):
    serwer = Serwer(_odp(json_=[]))
    with pytest.raises(ValueError):
        await github.listuj(REPO, [], stan, transport=serwer.transport, odczekaj=Odczekania())
    assert serwer.zadania == []


ZLE_REPOZYTORIA = [
    "owner",
    "owner/",
    "/repo",
    "owner/repo/extra",
    "owner/repo#1",
    "own er/repo",
    "owner/re po",
    "own:er/repo",
    "owner/re:po",
    "ówner/repo",
    "owner/rępo",
    "owner/repo\n",
    " owner/repo",
    "owner/repo ",
    "",
    "/",
]


@pytest.mark.parametrize("repo", ZLE_REPOZYTORIA, ids=[repr(r) for r in ZLE_REPOZYTORIA])
async def test_niepoprawne_repozytorium_przy_liscie_to_nie_znaleziono_bez_zadnego_zadania(repo):
    serwer = Serwer(_odp(json_=[]))
    blad = await _kod_bledu(_listuj(serwer, repo=repo))
    assert (blad.dostawca, blad.kod) == ("GitHub", "nie_znaleziono")
    assert serwer.zadania == []


@pytest.mark.parametrize("repo", ZLE_REPOZYTORIA, ids=[repr(r) for r in ZLE_REPOZYTORIA])
async def test_niepoprawne_repozytorium_przy_liscie_jest_odrzucane_przed_odczytem_tokenu(
    bez_tokenu, repo
):
    blad = await _kod_bledu(_listuj(Serwer(_odp(json_=[])), repo=repo))
    assert blad.kod == "nie_znaleziono"


@pytest.mark.parametrize(
    "repo",
    ["o.w/r", "o_w/r", "o-w/r", "o/r.x", "o/r_x", "o/r-x", "my.org/my_repo-1", "ABC/xyz09"],
)
async def test_repozytorium_ze_znakami_dozwolonymi_w_nazwach_github_jest_przyjete_przy_liscie(repo):
    serwer = Serwer(_odp(json_=[_wpis(5)]))
    wynik = await _listuj(serwer, repo=repo)
    assert serwer.zadania[0].url.path == f"/repos/{repo}/issues"
    assert wynik == [{"id": f"{repo}#5", "tytul": "Tytuł", "stan": "open"}]


# --- listuj: wynik ---------------------------------------------------------


async def test_lista_zwraca_identyfikator_tytul_i_stan_w_kolejnosci_zrodla():
    serwer = Serwer(
        _odp(
            json_=[
                _wpis(9, "Zażółć gęślą", "open"),
                _wpis(2, "Drugie", "closed"),
            ]
        )
    )
    wynik = await _listuj(serwer, stan="all")
    assert wynik == [
        {"id": f"{REPO}#9", "tytul": "Zażółć gęślą", "stan": "open"},
        {"id": f"{REPO}#2", "tytul": "Drugie", "stan": "closed"},
    ]


async def test_lista_bez_zgloszen_jest_pusta_ale_po_prawdziwym_zapytaniu():
    serwer = Serwer(_odp(json_=[]))
    assert await _listuj(serwer) == []
    assert len(serwer.zadania) == 1


async def test_lista_pomija_pull_requesty_bo_github_zwraca_je_razem_ze_zgloszeniami():
    serwer = Serwer(
        _odp(
            json_=[
                _wpis(1, "Zgłoszenie pierwsze"),
                _wpis(2, "To jest pull request", pr=True),
                _wpis(3, "Zgłoszenie trzecie"),
            ]
        )
    )
    wynik = await _listuj(serwer)
    assert [w["id"] for w in wynik] == [f"{REPO}#1", f"{REPO}#3"]


async def test_lista_zlozona_wylacznie_z_pull_requestow_jest_pusta():
    serwer = Serwer(_odp(json_=[_wpis(1, pr=True), _wpis(2, pr=True)]))
    assert await _listuj(serwer) == []


async def test_pull_request_jest_pomijany_zanim_sprawdzimy_jego_pozostale_pola():
    serwer = Serwer(_odp(json_=[{"pull_request": {"url": "x"}}, _wpis(4)]))
    wynik = await _listuj(serwer)
    assert [w["id"] for w in wynik] == [f"{REPO}#4"]


# --- listuj: paginacja -----------------------------------------------------


async def test_lista_dociaga_kolejne_strony_z_linku_i_skleja_wyniki_w_kolejnosci_bez_pull_requestow():
    serwer = Serwer(
        *_strony(
            3,
            URL_STRONY_LISTY,
            lambda i: [_wpis(i * 10 + 1), _wpis(i * 10 + 2, pr=True), _wpis(i * 10 + 3)],
        )
    )
    wynik = await _listuj(serwer)
    assert [w["id"] for w in wynik] == [
        f"{REPO}#11",
        f"{REPO}#13",
        f"{REPO}#21",
        f"{REPO}#23",
        f"{REPO}#31",
        f"{REPO}#33",
    ]
    assert len(serwer.zadania) == 3
    assert str(serwer.zadania[1].url) == URL_STRONY_LISTY.format(2)
    assert str(serwer.zadania[2].url) == URL_STRONY_LISTY.format(3)


async def test_lista_konczy_na_stronie_bez_rel_next_nawet_gdy_link_wskazuje_inne_strony():
    serwer = Serwer(
        _odp(
            json_=[_wpis(1)],
            naglowki={
                "Link": _link(
                    poprzednia=URL_STRONY_LISTY.format(0), ostatnia=URL_STRONY_LISTY.format(9)
                )
            },
        ),
        _odp(json_=[_wpis(2)]),
    )
    wynik = await _listuj(serwer)
    assert [w["id"] for w in wynik] == [f"{REPO}#1"]
    assert len(serwer.zadania) == 1


async def test_lista_na_dokladnie_dwudziestu_stronach_jest_kompletna():
    serwer = Serwer(*_strony(20, URL_STRONY_LISTY, lambda i: [_wpis(i)]))
    wynik = await _listuj(serwer)
    assert [w["id"] for w in wynik] == [f"{REPO}#{i}" for i in range(1, 21)]
    assert len(serwer.zadania) == 20


async def test_lista_na_dwudziestu_jeden_stronach_to_blad_a_nie_ucieta_lista():
    serwer = Serwer(*_strony(21, URL_STRONY_LISTY, lambda i: [_wpis(i)]))
    blad = await _kod_bledu(_listuj(serwer))
    assert (blad.dostawca, blad.kod) == ("GitHub", "blad_dostawcy")
    assert "nie zwracam niepełnej listy" in blad.komunikat


# --- listuj: zla budowa odpowiedzi ----------------------------------------

ZLE_STRONY_LISTY = {
    "strona_jako_pusty_obiekt": {},
    "strona_jako_pusty_napis": "",
    "strona_jako_obiekt": {"message": "x"},
    "strona_jako_napis": "tekst",
    "strona_null": None,
    "strona_jako_liczba": 5,
    "element_jako_napis": ["tekst"],
    "element_jako_liczba": [5],
    # `in` na napisie/liscie nie rzuca wyjatku: tylko warunek typu odroznia je od
    # pull requesta (bez niego element bylby po cichu pominiety, a wynik pusty)
    "element_jako_napis_pull_request": ["pull_request"],
    "element_jako_lista_z_pull_request": [["pull_request"]],
    "element_bez_numeru": [{"title": "T", "state": "open"}],
    "element_bez_tytulu": [{"number": 1, "state": "open"}],
    "element_bez_stanu": [{"number": 1, "title": "T"}],
}


@pytest.mark.parametrize("strona", list(ZLE_STRONY_LISTY.values()), ids=list(ZLE_STRONY_LISTY))
async def test_lista_o_niespodziewanej_budowie_to_czytelny_blad_dostawcy(strona):
    serwer = Serwer(_odp(tresc=json.dumps(strona).encode()))
    blad = await _kod_bledu(_listuj(serwer))
    assert (blad.dostawca, blad.kod) == ("GitHub", "blad_dostawcy")
    assert blad.komunikat == KOMUNIKAT_STRUKTURA


ADRESY_Z_LINKU_BEZ_NASZYCH_PARAMETROW = [
    "https://api.github.com/repositories/123/issues?page=2",
    "https://api.github.com/repositories/123/issues?after=abc",
    "https://api.github.com/repositories/123/issues/kolejna",
]


@pytest.mark.parametrize("adres", ADRESY_Z_LINKU_BEZ_NASZYCH_PARAMETROW)
async def test_kolejna_strona_listy_idzie_pod_adres_z_linku_bez_dokladania_stanu_etykiet_ani_rozmiaru(
    adres,
):
    serwer = Serwer(
        _odp(json_=[_wpis(1)], naglowki={"Link": _link(nastepna=adres)}),
        _odp(json_=[_wpis(2)]),
    )
    wynik = await _listuj(serwer, etykiety=["backend"], stan="all")
    assert [w["id"] for w in wynik] == [f"{REPO}#1", f"{REPO}#2"]
    # pierwsze zadanie niesie parametry, drugie dokladnie adres z Link
    assert dict(serwer.zadania[0].url.params) == {
        "state": "all",
        "per_page": "100",
        "labels": "backend",
    }
    assert str(serwer.zadania[1].url) == adres


@pytest.mark.parametrize("adres", ADRESY_Z_LINKU_BEZ_NASZYCH_PARAMETROW)
async def test_kolejna_strona_komentarzy_idzie_pod_adres_z_linku_bez_dokladania_rozmiaru_strony(
    adres,
):
    serwer = Serwer(
        _odp(json_=_issue()),
        _odp(json_=[_komentarz_nr(1)], naglowki={"Link": _link(nastepna=adres)}),
        _odp(json_=[_komentarz_nr(2)]),
    )
    ticket = await _pobierz(serwer)
    assert [k.body for k in ticket.comments] == ["Komentarz 1", "Komentarz 2"]
    assert dict(serwer.zadania[1].url.params) == {"per_page": "100"}
    assert str(serwer.zadania[2].url) == adres


async def test_zla_budowa_drugiej_strony_listy_nie_zwraca_polowy_wyniku():
    serwer = Serwer(
        _odp(json_=[_wpis(1)], naglowki={"Link": _link(nastepna=URL_STRONY_LISTY.format(2))}),
        _odp(json_={}),
    )
    blad = await _kod_bledu(_listuj(serwer))
    assert (blad.dostawca, blad.kod) == ("GitHub", "blad_dostawcy")


# --- interpretacja odpowiedzi HTTP -----------------------------------------


@pytest.mark.parametrize("etap", ETAPY)
async def test_odrzucony_token_daje_blad_z_nazwa_dostawcy_i_miejscem_poprawki_bez_ponowien(etap):
    serwer = _serwer_etapu(etap, _odp(401, json_={"message": "Bad credentials"}))
    blad = await _kod_bledu(_wywolaj(etap, serwer))
    assert (blad.dostawca, blad.kod) == ("GitHub", "token_odrzucony")
    assert "GitHub" in blad.komunikat
    assert "credentials.json" in blad.komunikat
    assert 'klucz "github"' in blad.komunikat
    assert "COCKPIT_GITHUB_TOKEN" in blad.komunikat
    assert _zadan_na_etapie(serwer, etap) == 1


@pytest.mark.parametrize("etap", ETAPY)
async def test_odrzucony_token_jest_rozpoznawany_takze_gdy_odpowiedz_jest_nieczytelna(etap):
    serwer = _serwer_etapu(etap, _odp(401, tresc=b"<html>nie json</html>"))
    blad = await _kod_bledu(_wywolaj(etap, serwer))
    assert blad.kod == "token_odrzucony"


async def test_status_401_to_odrzucony_token_nawet_gdy_odpowiedz_niesie_naglowki_limitu():
    serwer = Serwer(
        _odp(401, json_={}, naglowki={"x-ratelimit-remaining": "0", "retry-after": "5"})
    )
    blad = await _kod_bledu(_pobierz(serwer))
    assert blad.kod == "token_odrzucony"
    assert len(serwer.zadania) == 1


@pytest.mark.parametrize("etap", ETAPY)
async def test_zabroniony_dostep_bez_naglowkow_limitu_to_odrzucony_token_bez_ponowien(etap):
    serwer = _serwer_etapu(etap, _odp(403, json_={"message": "Resource not accessible"}))
    blad = await _kod_bledu(_wywolaj(etap, serwer))
    assert (blad.dostawca, blad.kod) == ("GitHub", "token_odrzucony")
    assert "uprawnień" in blad.komunikat
    assert _zadan_na_etapie(serwer, etap) == 1


async def test_zabroniony_dostep_z_pozostalym_limitem_wiekszym_od_zera_to_odrzucony_token():
    serwer = Serwer(
        _odp(403, json_={"message": "Forbidden"}, naglowki={"x-ratelimit-remaining": "5"})
    )
    czekanie = Odczekania()
    blad = await _kod_bledu(_pobierz(serwer, czekanie))
    assert blad.kod == "token_odrzucony"
    assert len(serwer.zadania) == 1
    assert czekanie.wartosci == []


@pytest.mark.parametrize("etap", ETAPY)
async def test_zabroniony_dostep_z_wyczerpanym_limitem_czeka_domyslna_sekunde_i_ponawia(etap):
    serwer = _serwer_etapu(
        etap,
        _odp(403, json_={"message": "API rate limit exceeded"},
             naglowki={"x-ratelimit-remaining": "0"}),
        ok_po=True,
    )
    czekanie = Odczekania()
    wynik = await _wywolaj(etap, serwer, czekanie)
    assert wynik
    assert czekanie.wartosci == [1.0]
    # jedno odrzucone zadanie i jedno udane na etapie
    assert _zadan_na_etapie(serwer, etap) == 2 + (1 if etap == "zgloszenie" else 0)


async def test_limit_wtorny_z_retry_after_bez_licznika_limitu_czeka_tyle_sekund_ile_podano():
    serwer = Serwer(
        _odp(403, json_={"message": "secondary rate limit"}, naglowki={"retry-after": "7"}),
        _odp(json_=_issue()),
        _odp(json_=_komentarze()),
    )
    czekanie = Odczekania()
    ticket = await _pobierz(serwer, czekanie)
    assert ticket.id == ID
    assert czekanie.wartosci == [7.0]
    assert len(serwer.zadania) == 3


async def test_limit_wtorny_z_retry_after_jest_rozpoznany_mimo_pozostalego_limitu_wiekszego_od_zera():
    serwer = Serwer(
        _odp(
            403,
            json_={"message": "secondary rate limit"},
            naglowki={"retry-after": "3", "x-ratelimit-remaining": "5"},
        ),
        _odp(json_=_issue()),
        _odp(json_=_komentarze()),
    )
    czekanie = Odczekania()
    await _pobierz(serwer, czekanie)
    assert czekanie.wartosci == [3.0]


async def test_wyczerpany_limit_z_retry_after_czeka_tyle_ile_zada_dostawca():
    serwer = Serwer(
        _odp(
            403,
            json_={"message": "rate limit"},
            naglowki={"x-ratelimit-remaining": "0", "retry-after": "7"},
        ),
        _odp(json_=_issue()),
        _odp(json_=_komentarze()),
    )
    czekanie = Odczekania()
    await _pobierz(serwer, czekanie)
    assert czekanie.wartosci == [7.0]


async def test_czas_resetu_limitu_z_naglowka_nie_jest_uzywany_jako_czas_odczekania():
    serwer = Serwer(
        _odp(
            403,
            json_={"message": "rate limit"},
            naglowki={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"},
        ),
        _odp(json_=_issue()),
        _odp(json_=_komentarze()),
    )
    czekanie = Odczekania()
    await _pobierz(serwer, czekanie)
    assert czekanie.wartosci == [1.0]


@pytest.mark.parametrize("etap", ETAPY)
async def test_status_429_z_retry_after_czeka_tyle_sekund_i_ponawia(etap):
    serwer = _serwer_etapu(
        etap, _odp(429, tresc=b"", naglowki={"Retry-After": "7"}), ok_po=True
    )
    czekanie = Odczekania()
    wynik = await _wywolaj(etap, serwer, czekanie)
    assert wynik
    assert czekanie.wartosci == [7.0]


@pytest.mark.parametrize("etap", ETAPY)
async def test_status_429_bez_retry_after_czeka_domyslna_sekunde(etap):
    serwer = _serwer_etapu(etap, _odp(429, tresc=b""), ok_po=True)
    czekanie = Odczekania()
    await _wywolaj(etap, serwer, czekanie)
    assert czekanie.wartosci == [1.0]


@pytest.mark.parametrize("retry_after", ["jutro", "-5", "", "inf", "nan", "Infinity"])
async def test_nieliczbowy_nieskonczony_lub_ujemny_czas_odczekania_jest_ignorowany_i_czeka_domyslna_sekunde(
    retry_after,
):
    serwer = Serwer(
        _odp(429, tresc=b"", naglowki={"Retry-After": retry_after}),
        _odp(json_=_issue()),
        _odp(json_=_komentarze()),
    )
    czekanie = Odczekania()
    await _pobierz(serwer, czekanie)
    assert czekanie.wartosci == [1.0]


async def test_nieliczbowy_retry_after_przy_403_tez_daje_domyslna_sekunde_a_nie_awarie():
    serwer = Serwer(
        _odp(403, json_={}, naglowki={"retry-after": "jutro"}),
        _odp(json_=_issue()),
        _odp(json_=_komentarze()),
    )
    czekanie = Odczekania()
    await _pobierz(serwer, czekanie)
    assert czekanie.wartosci == [1.0]


async def test_zerowy_czas_odczekania_od_dostawcy_jest_respektowany_a_nie_zastepowany_domyslnym():
    serwer = Serwer(
        _odp(429, tresc=b"", naglowki={"Retry-After": "0"}),
        _odp(json_=_issue()),
        _odp(json_=_komentarze()),
    )
    czekanie = Odczekania()
    await _pobierz(serwer, czekanie)
    assert czekanie.wartosci == [0.0]


@pytest.mark.parametrize("etap", ETAPY)
async def test_limit_dwa_razy_a_potem_sukces_daje_wynik_po_trzech_zadaniach_na_etapie(etap):
    serwer = _serwer_etapu(
        etap,
        _odp(429, tresc=b"", naglowki={"Retry-After": "2"}),
        _odp(403, json_={}, naglowki={"x-ratelimit-remaining": "0"}),
        ok_po=True,
    )
    czekanie = Odczekania()
    wynik = await _wywolaj(etap, serwer, czekanie)
    assert wynik
    assert czekanie.wartosci == [2.0, 1.0]
    # dwa nieudane zadania i jedno udane na etapie
    assert _zadan_na_etapie(serwer, etap) == 3 + (1 if etap == "zgloszenie" else 0)


@pytest.mark.parametrize("etap", ETAPY)
@pytest.mark.parametrize(
    "limit",
    [
        _odp(429, tresc=b""),
        _odp(403, json_={}, naglowki={"x-ratelimit-remaining": "0"}),
        _odp(403, json_={}, naglowki={"retry-after": "1"}),
    ],
    ids=["429", "403_wyczerpany_limit", "403_retry_after"],
)
async def test_ciagly_limit_daje_blad_limit_po_trzech_zadaniach_i_dwoch_odczekaniach(etap, limit):
    serwer = _serwer_etapu(etap, limit)
    czekanie = Odczekania()
    blad = await _kod_bledu(_wywolaj(etap, serwer, czekanie))
    assert (blad.dostawca, blad.kod) == ("GitHub", "limit")
    assert "GitHub" in blad.komunikat
    assert "3" in blad.komunikat
    assert _zadan_na_etapie(serwer, etap) == 3
    assert len(czekanie.wartosci) == 2


async def test_limit_na_drugiej_stronie_listy_ponawia_tylko_te_strone_i_wynik_jest_kompletny():
    pierwsza, druga = _strony(2, URL_STRONY_LISTY, lambda i: [_wpis(i)])
    serwer = Serwer(pierwsza, _odp(429, tresc=b""), druga)
    wynik = await _listuj(serwer)
    assert [w["id"] for w in wynik] == [f"{REPO}#1", f"{REPO}#2"]
    assert len(serwer.zadania) == 3
    assert str(serwer.zadania[1].url) == str(serwer.zadania[2].url) == URL_STRONY_LISTY.format(2)


@pytest.mark.parametrize("etap", ETAPY)
async def test_nieistniejace_zasoby_to_nie_znaleziono_z_opisem_bez_ponowien(etap):
    serwer = _serwer_etapu(etap, _odp(404, json_={"message": "Not Found"}))
    blad = await _kod_bledu(_wywolaj(etap, serwer))
    assert (blad.dostawca, blad.kod) == ("GitHub", "nie_znaleziono")
    assert blad.komunikat.startswith("GitHub:")
    assert (ID if etap != "lista" else REPO) in blad.komunikat
    assert _zadan_na_etapie(serwer, etap) == 1


async def test_brak_zgloszenia_wymienia_numer_a_brak_repozytorium_przy_liscie_nie():
    zgloszenie = await _kod_bledu(_pobierz(Serwer(_odp(404, json_={}))))
    lista = await _kod_bledu(_listuj(Serwer(_odp(404, json_={}))))
    assert "#65" in zgloszenie.komunikat
    assert "#" not in lista.komunikat


@pytest.mark.parametrize("etap", ETAPY)
async def test_nieistniejacy_zasob_z_nieczytelna_trescia_to_nadal_nie_znaleziono(etap):
    serwer = _serwer_etapu(etap, _odp(404, tresc=b"<html>Not Found</html>"))
    blad = await _kod_bledu(_wywolaj(etap, serwer))
    assert blad.kod == "nie_znaleziono"


@pytest.mark.parametrize("status", [201, 422, 500, 502, 503])
@pytest.mark.parametrize("etap", ETAPY)
async def test_status_inny_niz_sukces_to_blad_dostawcy_z_kodem_nawet_gdy_tresc_wyglada_poprawnie(
    etap, status
):
    poprawna = {"zgloszenie": _issue(), "komentarze": _komentarze(), "lista": [_wpis(1)]}[etap]
    serwer = _serwer_etapu(etap, _odp(status, json_=poprawna))
    blad = await _kod_bledu(_wywolaj(etap, serwer))
    assert (blad.dostawca, blad.kod) == ("GitHub", "blad_dostawcy")
    assert f"błąd HTTP {status}" in blad.komunikat
    assert _zadan_na_etapie(serwer, etap) == 1


@pytest.mark.parametrize("etap", ETAPY)
@pytest.mark.parametrize("tresc", [b"<html>to nie json</html>", b""], ids=["html", "pusta"])
async def test_odpowiedz_200_bez_poprawnego_json_to_blad_dostawcy(etap, tresc):
    serwer = _serwer_etapu(etap, _odp(200, tresc=tresc))
    blad = await _kod_bledu(_wywolaj(etap, serwer))
    assert (blad.dostawca, blad.kod) == ("GitHub", "blad_dostawcy")
    assert blad.komunikat == "GitHub: odpowiedź nie jest poprawnym JSON-em."
    assert str(blad) == "GitHub: odpowiedź nie jest poprawnym JSON-em."


@pytest.mark.parametrize("etap", ETAPY)
@pytest.mark.parametrize(
    "wyjatek", [httpx.ConnectError("nie polaczono"), httpx.ReadTimeout("wolno")]
)
async def test_blad_polaczenia_to_blad_dostawcy_z_nazwa_klasy_wyjatku_bez_ponowien(etap, wyjatek):
    serwer = _serwer_etapu(etap, wyjatek)
    czekanie = Odczekania()
    blad = await _kod_bledu(_wywolaj(etap, serwer, czekanie))
    assert (blad.dostawca, blad.kod) == ("GitHub", "blad_dostawcy")
    assert blad.komunikat == f"GitHub: błąd połączenia ({type(wyjatek).__name__})"
    assert _zadan_na_etapie(serwer, etap) == 1
    assert czekanie.wartosci == []


# --- brak tokenu -----------------------------------------------------------


async def test_brak_tokenu_przerywa_pobranie_bez_zadnego_zadania_i_wskazuje_gdzie_go_ustawic(
    bez_tokenu,
):
    serwer = _serwer_ok()
    blad = await _kod_bledu(_pobierz(serwer))
    assert (blad.dostawca, blad.kod) == ("GitHub", "brak_tokenu")
    assert "credentials.json" in blad.komunikat
    assert 'klucz "github"' in blad.komunikat
    assert "COCKPIT_GITHUB_TOKEN" in blad.komunikat
    assert "brak tokenu" in blad.komunikat
    assert serwer.zadania == []


async def test_brak_tokenu_przerywa_liste_bez_zadnego_zadania(bez_tokenu):
    serwer = Serwer(_odp(json_=[]))
    blad = await _kod_bledu(_listuj(serwer))
    assert (blad.dostawca, blad.kod) == ("GitHub", "brak_tokenu")
    assert serwer.zadania == []


async def test_token_lineara_w_srodowisku_nie_zastepuje_tokenu_github(bez_tokenu, monkeypatch):
    monkeypatch.setenv("COCKPIT_LINEAR_TOKEN", "lin_api_INNY")
    serwer = _serwer_ok()
    blad = await _kod_bledu(_pobierz(serwer))
    assert blad.kod == "brak_tokenu"
    assert serwer.zadania == []


async def test_plik_sekretow_bez_klucza_github_to_brak_tokenu_bez_zadania(monkeypatch, tmp_path):
    monkeypatch.delenv("COCKPIT_GITHUB_TOKEN")
    plik = tmp_path / "credentials.json"
    plik.write_text(json.dumps({"linear": "lin_api_X"}), encoding="utf-8")
    monkeypatch.setattr(secrets, "CONFIG", plik)
    serwer = _serwer_ok()
    blad = await _kod_bledu(_pobierz(serwer))
    assert blad.kod == "brak_tokenu"
    assert serwer.zadania == []


async def test_pusty_token_w_pliku_sekretow_to_brak_tokenu_bez_zadania(monkeypatch, tmp_path):
    monkeypatch.delenv("COCKPIT_GITHUB_TOKEN")
    plik = tmp_path / "credentials.json"
    plik.write_text(json.dumps({"github": ""}), encoding="utf-8")
    monkeypatch.setattr(secrets, "CONFIG", plik)
    serwer = _serwer_ok()
    blad = await _kod_bledu(_pobierz(serwer))
    assert blad.kod == "brak_tokenu"
    assert serwer.zadania == []


@pytest.mark.parametrize("wartosc", [123, ["x"], {"a": "b"}], ids=["liczba", "lista", "obiekt"])
@pytest.mark.parametrize("wywolanie", [_pobierz, _listuj], ids=["pobierz", "listuj"])
async def test_token_w_pliku_sekretow_ktory_nie_jest_napisem_to_brak_tokenu_bez_zadania(
    monkeypatch, tmp_path, wywolanie, wartosc
):
    monkeypatch.delenv("COCKPIT_GITHUB_TOKEN")
    plik = tmp_path / "credentials.json"
    plik.write_text(json.dumps({"github": wartosc}), encoding="utf-8")
    monkeypatch.setattr(secrets, "CONFIG", plik)
    serwer = _serwer_ok()
    blad = await _kod_bledu(wywolanie(serwer))
    assert (blad.dostawca, blad.kod) == ("GitHub", "brak_tokenu")
    assert "brak tokenu" in blad.komunikat
    assert serwer.zadania == []


@pytest.mark.parametrize("wywolanie", [_pobierz, _listuj], ids=["pobierz", "listuj"])
async def test_plik_sekretow_z_uszkodzona_trescia_to_brak_tokenu_bez_ujawniania_tresci(
    monkeypatch, tmp_path, wywolanie
):
    # json.JSONDecodeError (ValueError) z pliku
    monkeypatch.delenv("COCKPIT_GITHUB_TOKEN")
    plik = tmp_path / "credentials.json"
    plik.write_text('{"github": "ghp_SEKRET_HAR65" <<<', encoding="utf-8")
    monkeypatch.setattr(secrets, "CONFIG", plik)
    serwer = _serwer_ok()
    blad = await _kod_bledu(wywolanie(serwer))
    assert (blad.dostawca, blad.kod) == ("GitHub", "brak_tokenu")
    assert "nieczytelny" in blad.komunikat
    assert TOKEN not in blad.komunikat
    assert serwer.zadania == []


@pytest.mark.parametrize("wywolanie", [_pobierz, _listuj], ids=["pobierz", "listuj"])
async def test_plik_sekretow_w_zlym_kodowaniu_to_brak_tokenu_a_nie_awaria(
    monkeypatch, tmp_path, wywolanie
):
    # UnicodeDecodeError (ValueError) z odczytu tekstu
    monkeypatch.delenv("COCKPIT_GITHUB_TOKEN")
    plik = tmp_path / "credentials.json"
    plik.write_bytes(b"\xff\xfe\x00\x80")
    monkeypatch.setattr(secrets, "CONFIG", plik)
    serwer = _serwer_ok()
    blad = await _kod_bledu(wywolanie(serwer))
    assert blad.kod == "brak_tokenu"
    assert "nieczytelny" in blad.komunikat
    assert serwer.zadania == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root czyta kazdy plik")
@pytest.mark.parametrize("wywolanie", [_pobierz, _listuj], ids=["pobierz", "listuj"])
async def test_plik_sekretow_bez_praw_odczytu_to_brak_tokenu_a_nie_awaria(
    monkeypatch, tmp_path, wywolanie
):
    # OSError (PermissionError) z odczytu pliku
    monkeypatch.delenv("COCKPIT_GITHUB_TOKEN")
    plik = tmp_path / "credentials.json"
    plik.write_text(json.dumps({"github": TOKEN}), encoding="utf-8")
    plik.chmod(0o000)
    monkeypatch.setattr(secrets, "CONFIG", plik)
    try:
        serwer = _serwer_ok()
        blad = await _kod_bledu(wywolanie(serwer))
    finally:
        plik.chmod(0o600)
    assert blad.kod == "brak_tokenu"
    assert "nieczytelny" in blad.komunikat
    assert serwer.zadania == []


@pytest.mark.parametrize("tresc", ["[]", "null", '"tekst"', "5"], ids=["lista", "null", "napis", "liczba"])
@pytest.mark.parametrize("wywolanie", [_pobierz, _listuj], ids=["pobierz", "listuj"])
async def test_plik_sekretow_bedacy_poprawnym_json_ale_nie_zbiorem_kluczy_to_brak_tokenu_bez_zadania(
    monkeypatch, tmp_path, wywolanie, tresc
):
    # AttributeError: `.get` na liscie, napisie albo liczbie
    monkeypatch.delenv("COCKPIT_GITHUB_TOKEN")
    plik = tmp_path / "credentials.json"
    plik.write_text(tresc, encoding="utf-8")
    monkeypatch.setattr(secrets, "CONFIG", plik)
    serwer = _serwer_ok()
    blad = await _kod_bledu(wywolanie(serwer))
    assert (blad.dostawca, blad.kod) == ("GitHub", "brak_tokenu")
    assert "nieczytelny" in blad.komunikat
    assert serwer.zadania == []


# --- nic nie wycieka -------------------------------------------------------


def _scenariusze_bledow():
    sekret_w_tresci = {"debug": {"authorization": f"Bearer {TOKEN}"}}
    sekret_w_naglowku = {"x-echo-authorization": f"Bearer {TOKEN}"}
    return {
        "401": (Serwer(_odp(401, json_=sekret_w_tresci, naglowki=sekret_w_naglowku)), "token_odrzucony"),
        "403": (Serwer(_odp(403, json_=sekret_w_tresci, naglowki=sekret_w_naglowku)), "token_odrzucony"),
        "404": (Serwer(_odp(404, json_=sekret_w_tresci)), "nie_znaleziono"),
        "429_ciagle": (Serwer(_odp(429, json_=sekret_w_tresci, naglowki=sekret_w_naglowku)), "limit"),
        "500": (Serwer(_odp(500, json_=sekret_w_tresci)), "blad_dostawcy"),
        "polaczenie": (
            Serwer(httpx.ConnectError(f"nie polaczono z naglowkiem Bearer {TOKEN}")),
            "blad_dostawcy",
        ),
    }


@pytest.mark.parametrize("wywolanie", [_pobierz, _listuj], ids=["pobierz", "listuj"])
@pytest.mark.parametrize("nazwa", list(_scenariusze_bledow()))
async def test_token_nie_wycieka_do_bledu_ani_na_wyjscie_w_zadnej_sciezce_bledu(
    nazwa, wywolanie, capsys
):
    serwer, kod = _scenariusze_bledow()[nazwa]
    blad = await _kod_bledu(wywolanie(serwer))
    assert blad.kod == kod
    wyjscie = capsys.readouterr()
    for tekst in (str(blad), repr(blad), blad.komunikat, wyjscie.out, wyjscie.err):
        assert TOKEN not in tekst
        assert "ghp_" not in tekst
        assert "Bearer" not in tekst


async def test_udane_pobranie_i_lista_niczego_nie_wypisuja(capsys):
    await _pobierz(_serwer_ok())
    await _listuj(Serwer(_odp(json_=[_wpis(1)])))
    wyjscie = capsys.readouterr()
    assert (wyjscie.out, wyjscie.err) == ("", "")


# --- limit czasu -----------------------------------------------------------


def _limity_czasu(zadanie: httpx.Request) -> dict:
    return zadanie.extensions["timeout"]


async def test_pobranie_zgloszenia_i_komentarzy_ma_ustawiony_limit_czasu_na_kazdy_etap_polaczenia():
    serwer = _serwer_ok()
    await _pobierz(serwer)
    oczekiwany = dict.fromkeys(("connect", "read", "write", "pool"), github.TIMEOUT_S)
    assert _limity_czasu(serwer.zadania[0]) == oczekiwany
    assert _limity_czasu(serwer.zadania[1]) == oczekiwany


async def test_lista_ma_ustawiony_limit_czasu_na_kazdy_etap_polaczenia():
    serwer = Serwer(_odp(json_=[]))
    await _listuj(serwer)
    oczekiwany = dict.fromkeys(("connect", "read", "write", "pool"), github.TIMEOUT_S)
    assert _limity_czasu(serwer.zadania[0]) == oczekiwany
