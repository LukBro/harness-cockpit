# ruff: noqa: F811   (fixture `swiat` kokpitu importowana z test_polecenie_run_workflow)
"""HAR-117: ticket zamrozony w katalogu runu na starcie (`ticket.json`).

Runner zapisuje `<katalog_runu>/ticket.json` raz (O_EXCL, prawa 0444) przed pierwszym krokiem.
Istniejacy plik zostaje bez zmian: zmiana opisu u dostawcy w trakcie runu nie zmienia tego,
co widza kroki. Plik lezy poza repozytorium uzytkownika (`run_dir_outside_repo`).

Asercje czytaja dane: plik w katalogu runu, zdarzenia zapisane przez silnik, kolejke polecen
i baze kokpitu. Silnik i `RunCreate` sa prawdziwe; podmieniony jest tylko dostawca ticketow
(`runner.tickety.operacje.ticket_pobierz` - siec) oraz agent.

Zalozenia, ktore implementacja ma spelnic:
- `runner.tickety.zamrozenie.zamroz(katalog_runu: Path, ticket: TicketJson) -> Path`,
  blad zapisu to `BladDziedziny`;
- `silnik.wykonaj_run` wola dostawce przez `runner.tickety.operacje.ticket_pobierz` (atrybut
  modulu, nie kopia nazwy) z `{"projekt": <sciezka z polecenia>, "id": <ticket.id>}`;
- polecenie `run_workflow` niesie `ticket` dokladnie w ksztalcie z `RunCreate.ticket`;
- zdarzenie ostrzezenia: `kind="ticket_ostrzezenie"`, `step_id="ticket"`, niepusty tekst.
"""
from __future__ import annotations

import errno
import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from cockpit.services.errors import BladDziedziny
from runner.operacje import BladOperacji
from runner.tickety import BladIntegracji, TicketJson, do_pliku
from runner.tickety.zamrozenie import BladTicketu, ticket_ze_zlecenia
from test_polecenie_run_workflow import _runy, _w_kolejce, swiat  # noqa: F401  (swiat to fixture)
from test_silnik_runu import (
    RUN_ID,
    K,
    Szpieg,
    jedno,
    rodzaje,
    sprawdz_blad_przed_startem,
    uruchom,
)
from test_silnik_runu import swiat as swiat_silnika

LIMIT_WATKOW_S = 30


def ticket(opis: str, *, id: str = "HAR-1", source: str = "linear") -> TicketJson:
    return TicketJson(
        source=source, id=id, url=None, title="Tytul", description=opis,
        status=None, priority=None, labels=[], comments=[],
        fetched_at=datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc), raw={},
    )


def zamroz_ticket(katalog: Path, t: TicketJson) -> Path:
    from runner.tickety.zamrozenie import zamroz     # import leniwy: brak modulu = czerwony test

    return zamroz(katalog, t)


# --- zamroz: plik raz, niezmienny ----------------------------------------------------------------


def test_zamrozony_ticket_lezy_w_katalogu_runu_jako_ticket_json_z_trescia_kontraktu(tmp_path):
    t = ticket("opis z dostawcy")

    sciezka = zamroz_ticket(tmp_path, t)

    assert sciezka == tmp_path / "ticket.json"
    assert sciezka.read_bytes() == do_pliku(t)


def test_zamrozony_ticket_ma_prawa_tylko_do_odczytu_takze_przy_restrykcyjnej_masce_praw(tmp_path):
    # maska 077 bez jawnego ustawienia praw dalaby 0400 (open z trybem 0444)
    stara = os.umask(0o077)
    try:
        sciezka = zamroz_ticket(tmp_path, ticket("opis"))
    finally:
        os.umask(stara)

    assert stat.S_IMODE(sciezka.stat().st_mode) == 0o444


def test_ponowne_zamrozenie_z_innym_opisem_zostawia_bajty_pierwszego_zapisu(tmp_path):
    pierwszy = ticket("opis pierwszy")
    zamroz_ticket(tmp_path, pierwszy)

    sciezka = zamroz_ticket(tmp_path, ticket("opis zmieniony u dostawcy"))

    assert sciezka == tmp_path / "ticket.json"
    assert sciezka.read_bytes() == do_pliku(pierwszy)
    assert stat.S_IMODE(sciezka.stat().st_mode) == 0o444


def test_ponowne_zamrozenie_nie_nadpisuje_nawet_gdy_plik_ma_inna_tresc_niz_kontrakt(tmp_path):
    (tmp_path / "ticket.json").write_text("recznie podmieniony", encoding="utf-8")

    zamroz_ticket(tmp_path, ticket("opis"))

    assert (tmp_path / "ticket.json").read_text(encoding="utf-8") == "recznie podmieniony"


def test_zamrozenie_nie_zostawia_w_katalogu_runu_plikow_tymczasowych(tmp_path):
    zamroz_ticket(tmp_path, ticket("opis"))
    zamroz_ticket(tmp_path, ticket("inny opis"))

    assert sorted(p.name for p in tmp_path.iterdir()) == ["ticket.json"]


def test_rownolegle_zamrozenia_z_roznym_opisem_zostawiaja_jeden_calosciowy_plik(tmp_path):
    kandydaci = [ticket(f"opis {n}" + "x" * 5000) for n in range(8)]
    start = threading.Barrier(len(kandydaci), timeout=LIMIT_WATKOW_S)

    def zamroz_po_starcie(t: TicketJson) -> None:
        start.wait(timeout=LIMIT_WATKOW_S)
        zamroz_ticket(tmp_path, t)

    with ThreadPoolExecutor(max_workers=len(kandydaci)) as pula:
        for wynik in [pula.submit(zamroz_po_starcie, t) for t in kandydaci]:
            wynik.result(timeout=LIMIT_WATKOW_S)

    zapisany = (tmp_path / "ticket.json").read_bytes()
    assert zapisany in [do_pliku(t) for t in kandydaci]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ticket.json"]


def test_zamrozenie_w_nieistniejacym_katalogu_runu_to_blad_ticketu_z_nazwa_przyczyny(tmp_path):
    with pytest.raises(BladTicketu) as blad:
        zamroz_ticket(tmp_path / "brak" / "runu", ticket("opis"))

    assert not isinstance(blad.value, OSError)
    assert str(blad.value) == "nie zapisano ticket.json w katalogu runu: FileNotFoundError"


def test_zamrozenie_w_katalogu_bez_prawa_zapisu_to_blad_dziedziny_i_brak_pliku(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root pomija prawa katalogu")
    tmp_path.chmod(0o555)
    try:
        with pytest.raises(BladTicketu) as blad:
            zamroz_ticket(tmp_path, ticket("opis"))
    finally:
        tmp_path.chmod(0o755)

    assert str(blad.value) == "nie zapisano ticket.json w katalogu runu: PermissionError"
    assert not (tmp_path / "ticket.json").exists()


def test_zamrozenie_przy_pelnym_dysku_to_blad_dziedziny(tmp_path, monkeypatch):
    prawdziwe = os.open

    def pelny_dysk(sciezka, flagi, *reszta, **nazwane):
        if Path(os.fsdecode(sciezka)).parent == tmp_path:
            raise OSError(errno.ENOSPC, "brak miejsca")
        return prawdziwe(sciezka, flagi, *reszta, **nazwane)

    monkeypatch.setattr(os, "open", pelny_dysk)

    with pytest.raises(BladTicketu) as blad:
        zamroz_ticket(tmp_path, ticket("opis"))

    assert isinstance(blad.value, BladDziedziny)
    assert str(blad.value) == "nie zapisano ticket.json w katalogu runu: OSError"


def test_zamrozenie_nie_nadpisuje_cudzego_pliku_tymczasowego_o_tej_samej_nazwie(
    tmp_path, monkeypatch,
):
    """Plik tymczasowy powstaje wylacznie przez O_EXCL: istniejacy plik o tej nazwie to blad,
    a jego tresc zostaje (bez O_EXCL zapis nadpisalby go i ticket.json dostalby cudza tresc)."""
    stale_uuid = SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="stala"))
    monkeypatch.setattr("runner.tickety.zamrozenie.uuid", stale_uuid)
    cudzy = tmp_path / f".ticket.json.{os.getpid()}.stala.tmp"
    cudzy.write_bytes(b"cudza tresc, ktorej nie wolno ruszyc" * 200)
    przed = cudzy.read_bytes()

    with pytest.raises(BladTicketu) as blad:
        zamroz_ticket(tmp_path, ticket("opis"))

    assert str(blad.value) == "nie zapisano ticket.json w katalogu runu: FileExistsError"
    assert cudzy.read_bytes() == przed
    assert not (tmp_path / "ticket.json").exists()


def test_zamrozenie_znosi_znikniecie_pliku_tymczasowego_przed_sprzataniem(tmp_path, monkeypatch):
    """Ktos usunal plik tymczasowy po zapisie `ticket.json`: zamrozenie nadal sie udaje."""
    prawdziwe_link = os.link

    def link_i_zniknij(zrodlo, cel):
        prawdziwe_link(zrodlo, cel)
        os.unlink(zrodlo)

    monkeypatch.setattr(os, "link", link_i_zniknij)
    t = ticket("opis")

    sciezka = zamroz_ticket(tmp_path, t)

    assert sciezka == tmp_path / "ticket.json"
    assert sciezka.read_bytes() == do_pliku(t)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ticket.json"]


# --- silnik: zamrozenie na starcie runu -----------------------------------------------------------


class SzpiegSprawdzajacyTicket(Szpieg):
    """Zapisuje, czy ticket.json istnial w chwili startu kazdego kroku."""

    def __init__(self, katalog_runu: Path, **kw):
        super().__init__(**kw)
        self.plik_przy_starcie: list[bool] = []
        self._katalog_runu = katalog_runu

    async def __call__(self, spec, polecenie, katalog, emit, **kw):
        self.plik_przy_starcie.append((self._katalog_runu / "ticket.json").exists())
        return await super().__call__(spec, polecenie, katalog, emit, **kw)


@pytest.fixture
def dostawca(monkeypatch):
    """Podmienia siec: `ticket_pobierz` zwraca ticket z `odpowiedz["ticket"]`."""
    stan = type("Dostawca", (), {})()
    stan.wywolania = []
    stan.ticket = ticket("opis z dostawcy")
    stan.blad = None

    async def ticket_pobierz(dane: dict) -> dict:
        stan.wywolania.append(dict(dane))
        if stan.blad is not None:
            raise stan.blad
        return {"ticket": stan.ticket.model_dump(mode="json"), "ostrzezenia": []}

    monkeypatch.setattr("runner.tickety.operacje.ticket_pobierz", ticket_pobierz)
    return stan


ID_PROJEKTU_W_KOKPICIE = "7d3f0c9e-1b2a-4c5d-8e6f-0a1b2c3d4e5f"     # UUID, nie sciezka


def polecenie_z_dostawca(s, id: str = "HAR-1") -> dict:
    """Jak od kokpitu: `ticket.projekt` to id projektu, sciezka repozytorium tylko w `sciezka`."""
    assert s.polecenie["sciezka"] == str(s.repo) != ID_PROJEKTU_W_KOKPICIE
    return {**s.polecenie, "ticket": {"projekt": ID_PROJEKTU_W_KOKPICIE, "id": id}}


def polecenie_z_wklejeniem(s, tekst: str, url: str | None = None) -> dict:
    wklejony = {"wklejony": tekst}
    if url is not None:
        wklejony["url"] = url
    return {**s.polecenie, "ticket": wklejony}


def ostrzezenia(zdarzenia: list[dict]) -> list[dict]:
    return [z for z in zdarzenia if z["kind"] == "ticket_ostrzezenie"]


async def test_ticket_od_dostawcy_jest_zamrozony_w_katalogu_runu_przed_pierwszym_krokiem(
    tmp_path, dostawca,
):
    s = swiat_silnika(tmp_path, [K("a")])
    agent = SzpiegSprawdzajacyTicket(s.katalog_runu)

    zdarzenia = await uruchom(s, agent, polecenie=polecenie_z_dostawca(s))

    assert agent.plik_przy_starcie == [True]
    zapisany = json.loads((s.katalog_runu / "ticket.json").read_text(encoding="utf-8"))
    assert zapisany == dostawca.ticket.model_dump(mode="json")
    assert "run_finished" in rodzaje(zdarzenia)
    assert "step_failed" not in rodzaje(zdarzenia)


async def test_runner_pobiera_ticket_z_projektu_wskazanego_sciezka_polecenia_i_jego_id(
    tmp_path, dostawca,
):
    s = swiat_silnika(tmp_path, [K("a")])

    await uruchom(s, Szpieg(), polecenie=polecenie_z_dostawca(s, "HAR-77"))

    assert dostawca.wywolania == [{"projekt": str(s.repo), "id": "HAR-77"}]


async def _polecenie_od_kokpitu(kokpit, s, ticket_dla_projektu) -> tuple[dict, str]:
    """Polecenie dokladnie w ksztalcie, jaki kokpit kolejkuje runnerowi po POST /api/runs."""
    projekt = await kokpit.nowy_projekt(kokpit.browarek, kokpit.wiekszy, str(s.repo))
    r = await kokpit.klient.post("/api/runs", json={
        "workflow": "w", "project_id": projekt, "ticket": ticket_dla_projektu(projekt)})
    assert r.status_code == 200
    (polecenie,) = _w_kolejce(kokpit.wiekszy)
    return polecenie, projekt


async def test_polecenie_z_kokpitu_z_id_projektu_w_tickecie_pobiera_ticket_ze_sciezki_projektu(
    swiat, tmp_path, dostawca,
):
    s = swiat_silnika(tmp_path, [K("a")])
    polecenie, projekt = await _polecenie_od_kokpitu(
        swiat, s, lambda p: {"projekt": p, "id": "HAR-117"})
    assert polecenie["ticket"]["projekt"] == projekt != polecenie["sciezka"] == str(s.repo)

    await uruchom(s, Szpieg(), polecenie=polecenie)

    assert dostawca.wywolania == [{"projekt": str(s.repo), "id": "HAR-117"}]
    assert (s.root / polecenie["run_id"] / "ticket.json").is_file()


async def test_polecenie_z_kokpitu_z_wklejonym_ticketem_zamraza_tekst_z_url(
    swiat, tmp_path, dostawca,
):
    s = swiat_silnika(tmp_path, [K("a")])
    polecenie, _ = await _polecenie_od_kokpitu(
        swiat, s, lambda p: {"wklejony": "Tytul zlecenia\nopis", "url": "https://x.example/9"})

    await uruchom(s, Szpieg(), polecenie=polecenie)

    zapisany = json.loads(
        (s.root / polecenie["run_id"] / "ticket.json").read_text(encoding="utf-8"))
    assert (zapisany["source"], zapisany["title"], zapisany["url"]) == (
        "manual", "Tytul zlecenia", "https://x.example/9")
    assert dostawca.wywolania == []


async def test_zamrozony_plik_ticketu_ma_prawa_tylko_do_odczytu(tmp_path, dostawca):
    s = swiat_silnika(tmp_path, [K("a")])

    await uruchom(s, Szpieg(), polecenie=polecenie_z_dostawca(s))

    plik = s.katalog_runu / "ticket.json"
    assert stat.S_IMODE(plik.stat().st_mode) == 0o444


async def test_wklejony_ticket_jest_zamrozony_ze_zrodlem_manual_i_tekstem_jako_opisem(tmp_path):
    s = swiat_silnika(tmp_path, [K("a")])
    tekst = "Naprawic logowanie\n\nPo wpisaniu hasla strona sie zawiesza."

    await uruchom(s, Szpieg(), polecenie=polecenie_z_wklejeniem(s, tekst, "https://x.example/1"))

    zapisany = json.loads((s.katalog_runu / "ticket.json").read_text(encoding="utf-8"))
    assert zapisany["source"] == "manual"
    assert zapisany["title"] == "Naprawic logowanie"
    assert zapisany["description"] == tekst
    assert zapisany["url"] == "https://x.example/1"


async def test_wklejony_ticket_nie_wola_dostawcy_ticketow(tmp_path, dostawca):
    s = swiat_silnika(tmp_path, [K("a")])

    await uruchom(s, Szpieg(), polecenie=polecenie_z_wklejeniem(s, "Tytul\nopis"))

    assert (s.katalog_runu / "ticket.json").is_file()
    assert dostawca.wywolania == []


async def test_wklejony_ticket_bez_url_ma_w_pliku_url_null(tmp_path):
    s = swiat_silnika(tmp_path, [K("a")])

    await uruchom(s, Szpieg(), polecenie=polecenie_z_wklejeniem(s, "Tytul\nopis"))

    zapisany = json.loads((s.katalog_runu / "ticket.json").read_text(encoding="utf-8"))
    assert zapisany["url"] is None


async def test_ticket_jest_poza_repozytorium_uzytkownika_i_poza_worktree(tmp_path, dostawca):
    s = swiat_silnika(tmp_path, [K("a")])

    await uruchom(s, Szpieg(), polecenie=polecenie_z_dostawca(s))

    plik = (s.katalog_runu / "ticket.json").resolve()
    assert plik.is_file()
    assert s.repo.resolve() not in plik.parents
    assert s.worktree.resolve() not in plik.parents
    assert not list(s.repo.rglob("ticket.json"))
    assert not list(s.worktree.rglob("ticket.json"))


async def test_run_bez_ticketu_nie_tworzy_ticket_json_ani_ostrzezenia(tmp_path):
    s = swiat_silnika(tmp_path, [K("a")])

    zdarzenia = await uruchom(s, Szpieg())

    assert not (s.katalog_runu / "ticket.json").exists()
    assert ostrzezenia(zdarzenia) == []


@pytest.mark.parametrize("opis", ["", "   ", " \n\t  \n", "  "])
async def test_ticket_bez_opisu_nie_blokuje_runu_a_w_historii_jest_ostrzezenie(
    tmp_path, dostawca, opis,
):
    dostawca.ticket = ticket(opis)
    s = swiat_silnika(tmp_path, [K("a")])
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent, polecenie=polecenie_z_dostawca(s))

    assert agent.kroki == ["a"]                      # run wystartowal
    assert "step_failed" not in rodzaje(zdarzenia)
    assert rodzaje(zdarzenia).count("run_finished") == 1
    (ostrzezenie,) = ostrzezenia(zdarzenia)
    assert ostrzezenie["run_id"] == RUN_ID
    assert ostrzezenie["step_id"] == "ticket"
    assert ostrzezenie["text"] == "ticket HAR-1 nie ma opisu"
    assert zdarzenia.index(ostrzezenie) < zdarzenia.index(jedno(zdarzenia, "step_started", "a"))
    assert (s.katalog_runu / "ticket.json").is_file()   # ticket mimo to zamrozony


async def test_ticket_z_opisem_nie_dostaje_ostrzezenia(tmp_path, dostawca):
    dostawca.ticket = ticket("x")                    # jeden znak to juz opis
    s = swiat_silnika(tmp_path, [K("a")])

    zdarzenia = await uruchom(s, Szpieg(), polecenie=polecenie_z_dostawca(s))

    assert (s.katalog_runu / "ticket.json").is_file()
    assert ostrzezenia(zdarzenia) == []


async def test_wklejony_ticket_z_tytulem_bez_dalszego_tekstu_ma_opis_wiec_bez_ostrzezenia(
    tmp_path,
):
    s = swiat_silnika(tmp_path, [K("a")])

    zdarzenia = await uruchom(s, Szpieg(), polecenie=polecenie_z_wklejeniem(s, "Sam tytul"))

    assert (s.katalog_runu / "ticket.json").is_file()
    assert ostrzezenia(zdarzenia) == []


@pytest.mark.parametrize("opis", ["opis", ""], ids=["z_opisem", "bez_opisu"])
async def test_blad_zapisu_ticketu_konczy_run_przez_step_failed_i_run_finished_bez_krokow(
    tmp_path, dostawca, monkeypatch, opis,
):
    """Takze dla ticketu bez opisu: ostrzezenie idzie dopiero po utworzeniu pliku."""
    dostawca.ticket = ticket(opis)
    if os.geteuid() == 0:
        pytest.skip("root pomija prawa katalogu")
    from runner import silnik

    prawdziwe = silnik._przygotuj

    def przygotuj_i_zablokuj_katalog_runu(*argumenty, **nazwane):
        przygotowany = prawdziwe(*argumenty, **nazwane)
        s.katalog_runu.chmod(0o555)                  # worktree stoi, zapis obok niego juz nie
        return przygotowany

    monkeypatch.setattr(silnik, "_przygotuj", przygotuj_i_zablokuj_katalog_runu)
    s = swiat_silnika(tmp_path, [K("a")])
    agent = Szpieg()
    try:
        zdarzenia = await uruchom(s, agent, polecenie=polecenie_z_dostawca(s))
    finally:
        s.katalog_runu.chmod(0o755)

    tekst = sprawdz_blad_przed_startem(zdarzenia)
    assert tekst == "nie zapisano ticket.json w katalogu runu: PermissionError"
    assert ostrzezenia(zdarzenia) == []
    assert agent.wywolania == []
    assert not (s.katalog_runu / "ticket.json").exists()


async def test_blad_dostawcy_przy_pobraniu_ticketu_konczy_run_komunikatem_bez_krokow(
    tmp_path, dostawca,
):
    """Atrapa rzuca to, co po `_bledy_dostawcy` zwraca prawdziwe `ticket_pobierz`."""
    dostawca.blad = BladOperacji("nie_znaleziono", "linear: nie znaleziono HAR-1")
    s = swiat_silnika(tmp_path, [K("a")])
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent, polecenie=polecenie_z_dostawca(s))

    assert sprawdz_blad_przed_startem(zdarzenia) == (
        "pobranie ticketu HAR-1: linear: nie znaleziono HAR-1")
    assert agent.wywolania == []
    assert not (s.katalog_runu / "ticket.json").exists()


async def test_blad_dziedziny_z_dostawcy_przechodzi_do_konca_runu_z_wlasnym_komunikatem(
    tmp_path, dostawca,
):
    dostawca.blad = BladIntegracji("linear", "limit", "linear: przekroczony limit zapytan")
    s = swiat_silnika(tmp_path, [K("a")])

    zdarzenia = await uruchom(s, Szpieg(), polecenie=polecenie_z_dostawca(s))

    assert sprawdz_blad_przed_startem(zdarzenia) == "linear: przekroczony limit zapytan"


async def test_nieoczekiwany_wyjatek_dostawcy_konczy_run_nazwa_typu_bez_tresci_wyjatku(
    tmp_path, dostawca,
):
    dostawca.blad = httpx.DecodingError("odpowiedz z tokenem tajny-token-xyz")
    s = swiat_silnika(tmp_path, [K("a")])
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent, polecenie=polecenie_z_dostawca(s, "HAR-5"))

    tekst = sprawdz_blad_przed_startem(zdarzenia)
    assert tekst == "pobranie ticketu HAR-5: nieoczekiwany wyjątek dostawcy (DecodingError)"
    assert "tajny-token-xyz" not in tekst
    assert agent.wywolania == []


async def test_sciezka_projektu_poza_zakresem_runnera_przy_pobraniu_ticketu_konczy_run_bez_wyjatku(
    tmp_path, monkeypatch,
):
    """Prawdziwe `ticket_pobierz` (bez atrapy): zakres runnera z konfiguracji nie obejmuje
    repozytorium swiata testowego, wiec odmawia przed odczytem `project.yaml`; blad dziedziny
    przechodzi przez silnik bez zmian i run konczy sie `step_failed` + `run_finished`.
    Konfiguracja runnera i HOME sa w katalogu testu, nie na maszynie, na ktorej biegnie test."""
    inny_projekt = tmp_path / "inny_projekt"
    dom_runnera = tmp_path / "dom_runnera"
    inny_projekt.mkdir()
    dom_runnera.mkdir()
    konfiguracja = tmp_path / "runner.json"
    konfiguracja.write_text(json.dumps({"projekty": [str(inny_projekt)]}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(dom_runnera))
    monkeypatch.setenv("COCKPIT_RUNNER_CONFIG", str(konfiguracja))
    s = swiat_silnika(tmp_path, [K("a")])
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent, polecenie=polecenie_z_dostawca(s, "HAR-9"))

    assert sprawdz_blad_przed_startem(zdarzenia) == (
        f"sciezka poza zakresem (poza_zakresem): '{s.repo}'")
    assert agent.wywolania == []
    assert not (s.katalog_runu / "ticket.json").exists()


@pytest.mark.parametrize(
    "odpowiedz",
    [{}, {"ostrzezenia": []}, [], "napis", None],
    ids=["pusty_obiekt", "bez_klucza_ticket", "lista", "napis", "brak"],
)
async def test_odpowiedz_dostawcy_bez_ticketu_konczy_run_bledem_a_nie_wyjatkiem_z_silnika(
    tmp_path, monkeypatch, odpowiedz,
):
    async def zla_odpowiedz(dane: dict):
        return odpowiedz

    monkeypatch.setattr("runner.tickety.operacje.ticket_pobierz", zla_odpowiedz)
    s = swiat_silnika(tmp_path, [K("a")])
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent, polecenie=polecenie_z_dostawca(s))

    assert sprawdz_blad_przed_startem(zdarzenia) == (
        "odpowiedz dostawcy dla ticketu HAR-1 nie zawiera ticketu")
    assert agent.wywolania == []


@pytest.mark.parametrize(
    ("surowy", "liczba_bledow"),
    [({"id": "HAR-1"}, 10), (5, 1), ("napis", 1), ([], 1)],
    ids=["brak_pol", "liczba", "napis", "lista"],
)
async def test_odpowiedz_dostawcy_z_ticketem_o_zlym_ksztalcie_konczy_run_bledem(
    tmp_path, monkeypatch, surowy, liczba_bledow,
):
    async def zly_ksztalt(dane: dict) -> dict:
        return {"ticket": surowy, "ostrzezenia": []}

    monkeypatch.setattr("runner.tickety.operacje.ticket_pobierz", zly_ksztalt)
    s = swiat_silnika(tmp_path, [K("a")])
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent, polecenie=polecenie_z_dostawca(s))

    assert sprawdz_blad_przed_startem(zdarzenia) == (
        f"odpowiedz dostawcy dla ticketu HAR-1 ma zly ksztalt ({liczba_bledow} bledow)")
    assert agent.wywolania == []


async def test_wklejenie_samych_bialych_znakow_konczy_run_bledem_bez_krokow(tmp_path):
    s = swiat_silnika(tmp_path, [K("a")])
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent, polecenie=polecenie_z_wklejeniem(s, "  \n\t "))

    assert sprawdz_blad_przed_startem(zdarzenia) == (
        "wklejony tekst nie zawiera zadnej niepustej linii")
    assert agent.wywolania == []
    assert not (s.katalog_runu / "ticket.json").exists()


NIEZNANY_KSZTALT = "polecenie runu: ticket ma nieznany ksztalt (projekt+id albo wklejony[+url])"


@pytest.mark.parametrize(
    ("zlecenie", "tekst_bledu"),
    [
        (5, "polecenie runu: ticket musi byc obiektem"),
        ("HAR-1", "polecenie runu: ticket musi byc obiektem"),
        (["HAR-1"], "polecenie runu: ticket musi byc obiektem"),
        ({"wklejony": 5}, "polecenie runu: ticket.wklejony musi byc napisem"),
        ({"wklejony": "Tytul", "url": 5}, "polecenie runu: ticket.url musi byc napisem"),
        ({"wklejony": "Tytul", "projekt": "p"}, NIEZNANY_KSZTALT),
        ({"projekt": ID_PROJEKTU_W_KOKPICIE, "id": "HAR-1", "url": "https://x.example"},
         NIEZNANY_KSZTALT),
        ({"projekt": ID_PROJEKTU_W_KOKPICIE}, NIEZNANY_KSZTALT),
        ({}, NIEZNANY_KSZTALT),
        ({"projekt": 5, "id": "HAR-1"}, "polecenie runu: ticket.projekt musi byc napisem"),
        ({"projekt": ID_PROJEKTU_W_KOKPICIE, "id": 5}, "polecenie runu: ticket.id musi byc napisem"),
    ],
    ids=["liczba", "napis", "lista", "wklejony_liczba", "url_liczba", "wklejony_z_projektem",
         "projekt_z_url", "projekt_bez_id", "pusty_obiekt", "projekt_liczba", "id_liczba"],
)
async def test_zle_pole_ticketu_w_poleceniu_konczy_run_bez_krokow_z_pelnym_komunikatem(
    tmp_path, dostawca, zlecenie, tekst_bledu,
):
    s = swiat_silnika(tmp_path, [K("a")])
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent, polecenie={**s.polecenie, "ticket": zlecenie})

    assert sprawdz_blad_przed_startem(zdarzenia) == tekst_bledu
    assert agent.wywolania == []
    assert dostawca.wywolania == []
    assert not (s.katalog_runu / "ticket.json").exists()


@pytest.mark.parametrize("sciezka", [None, "", 5], ids=["brak", "pusta", "liczba"])
async def test_ticket_od_dostawcy_bez_sciezki_projektu_to_blad_ticketu_bez_pytania_dostawcy(
    dostawca, sciezka,
):
    with pytest.raises(BladTicketu) as blad:
        await ticket_ze_zlecenia({"projekt": ID_PROJEKTU_W_KOKPICIE, "id": "HAR-1"}, sciezka)

    assert str(blad.value) == "polecenie runu: brak sciezki projektu dla ticketu"
    assert dostawca.wywolania == []


async def test_kolejny_start_runu_o_tym_samym_id_nie_zmienia_zamrozonego_ticketu(
    tmp_path, dostawca,
):
    """Zamrozony plik jest wazniejszy niz to, co dostawca oddaje przy ponownym starcie."""
    s = swiat_silnika(tmp_path, [K("a")])
    pierwszy = ticket("opis przy starcie")
    s.katalog_runu.mkdir(parents=True)
    zamroz_ticket(s.katalog_runu, pierwszy)
    dostawca.ticket = ticket("opis zmieniony u dostawcy w trakcie runu")

    await uruchom(s, Szpieg(), polecenie=polecenie_z_dostawca(s))

    zapisany = (s.katalog_runu / "ticket.json").read_bytes()
    assert zapisany == do_pliku(pierwszy)


# --- RunCreate.ticket: dwa ksztalty z limitami ----------------------------------------------------


async def test_run_z_ticketem_projektu_niesie_ticket_w_poleceniu_dla_runnera(swiat):
    r = await swiat.klient.post("/api/runs", json={
        "workflow": "budowa", "project_id": swiat.projekt,
        "ticket": {"projekt": swiat.projekt, "id": "HAR-117"}})

    assert r.status_code == 200
    (polecenie,) = _w_kolejce(swiat.wiekszy)
    assert polecenie["run_id"] == r.json()["run_id"]
    assert polecenie["ticket"] == {"projekt": swiat.projekt, "id": "HAR-117"}
    assert polecenie["sciezka"] == "/home/b/repo-wiekszy"


async def test_run_z_wklejonym_ticketem_niesie_tekst_i_url_w_poleceniu(swiat):
    r = await swiat.klient.post("/api/runs", json={
        "project_id": swiat.projekt,
        "ticket": {"wklejony": "Tytul\nopis", "url": "https://x.example/1"}})

    assert r.status_code == 200
    (polecenie,) = _w_kolejce(swiat.wiekszy)
    assert polecenie["ticket"] == {"wklejony": "Tytul\nopis", "url": "https://x.example/1"}


async def test_run_z_wklejonym_ticketem_bez_url_nie_wymysla_adresu(swiat):
    r = await swiat.klient.post("/api/runs", json={
        "project_id": swiat.projekt, "ticket": {"wklejony": "Tytul\nopis"}})

    assert r.status_code == 200
    (polecenie,) = _w_kolejce(swiat.wiekszy)
    assert polecenie["ticket"]["wklejony"] == "Tytul\nopis"
    assert polecenie["ticket"].get("url") is None


async def test_run_bez_ticketu_nie_niesie_pola_ticket_w_poleceniu(swiat):
    await swiat.klient.post("/api/runs", json={"project_id": swiat.projekt})

    (polecenie,) = _w_kolejce(swiat.wiekszy)
    assert "ticket" not in polecenie


async def test_ticket_w_runie_nie_ustawia_ticket_id_runu_w_bazie(swiat):
    await swiat.klient.post("/api/runs", json={
        "project_id": swiat.projekt, "ticket": {"projekt": swiat.projekt, "id": "HAR-117"}})

    (run,) = await _runy(swiat.maker)
    assert run.ticket_id is None


async def test_ticket_z_projektem_innym_niz_project_id_runu_daje_422_i_run_nie_powstaje(swiat):
    r = await swiat.klient.post("/api/runs", json={
        "project_id": swiat.projekt, "ticket": {"projekt": swiat.projekt_obcy, "id": "HAR-1"}})

    assert r.status_code == 422
    assert await _runy(swiat.maker) == []
    assert _w_kolejce(swiat.wiekszy) == []


@pytest.mark.parametrize(
    "ticket_dla_projektu",
    [
        lambda p: {"projekt": p, "id": ""},
        lambda p: {"projekt": p, "id": "h" * 201},                 # id ponad kolumne String(200)
        lambda p: {"projekt": p},                                  # brak id
        lambda p: {"id": "HAR-1"},                                 # brak projektu
        lambda p: {"projekt": p, "id": "HAR-1", "wklejony": "Tytul"},       # oba ksztalty naraz
        lambda p: {"projekt": p, "id": "HAR-1", "url": "https://x.example"},   # url tylko przy wklejeniu
        lambda p: {"projekt": p, "id": "HAR-1", "inne": "pole"},   # pole spoza kontraktu
        lambda p: {"projekt": p, "id": 7},
        lambda p: {"projekt": 5, "id": "HAR-1"},
        lambda p: {"wklejony": ""},                                # pusty tekst
        lambda p: {"wklejony": "t" * 100_001},                     # tekst ponad limit
        lambda p: {"wklejony": "Tytul", "url": "u" * 2001},        # url ponad limit
        lambda p: {"wklejony": "Tytul", "url": ""},
        lambda p: {"wklejony": "Tytul", "inne": "pole"},
        lambda p: {"wklejony": 5},
        lambda p: {},
        lambda p: "HAR-1",
        lambda p: ["HAR-1"],
    ],
    ids=[
        "id_puste", "id_za_dlugie", "brak_id", "brak_projektu", "oba_ksztalty",
        "url_przy_projekcie", "pole_obce_przy_projekcie", "id_liczba", "projekt_liczba",
        "wklejony_pusty", "wklejony_za_dlugi", "url_za_dlugi", "url_pusty", "pole_obce_przy_wklejeniu",
        "wklejony_liczba", "pusty_obiekt", "napis", "lista",
    ],
)
async def test_zly_ksztalt_ticketu_w_runie_daje_422_i_run_nie_powstaje(swiat, ticket_dla_projektu):
    """Projekt w tickecie to zawsze projekt runu, wiec odrzucenie wynika z samego ksztaltu."""
    r = await swiat.klient.post("/api/runs", json={
        "project_id": swiat.projekt, "ticket": ticket_dla_projektu(swiat.projekt)})

    assert r.status_code == 422
    assert await _runy(swiat.maker) == []
    assert _w_kolejce(swiat.wiekszy) == [] and _w_kolejce(swiat.mniejszy) == []


@pytest.mark.parametrize(
    "ticket_wejscie",
    [{"projekt": "p" * 36, "id": "HAR-1"}, {"wklejony": "Tytul\nopis"}],
    ids=["z_projektu", "wklejony"],
)
async def test_ticket_bez_project_id_daje_422_i_run_nie_powstaje_a_runner_nic_nie_dostaje(
    swiat, ticket_wejscie,
):
    r = await swiat.klient.post("/api/runs", json={"ticket": ticket_wejscie})

    assert r.status_code == 422
    assert await _runy(swiat.maker) == []
    assert _w_kolejce(swiat.wiekszy) == [] and _w_kolejce(swiat.mniejszy) == []


@pytest.mark.parametrize(
    "ticket_dla_projektu",
    [
        lambda p: {"projekt": p, "id": "h" * 200},                 # kolumna String(200)
        lambda p: {"wklejony": "t" * 100_000},
        lambda p: {"wklejony": "Tytul", "url": "u" * 2000},
    ],
    ids=["id_200", "wklejony_100000", "url_2000"],
)
async def test_ticket_o_najwiekszych_dozwolonych_polach_przechodzi_walidacje_i_trafia_do_polecenia(
    swiat, ticket_dla_projektu,
):
    ticket_wejscie = ticket_dla_projektu(swiat.projekt)

    r = await swiat.klient.post("/api/runs", json={
        "project_id": swiat.projekt, "ticket": ticket_wejscie})

    assert r.status_code == 200
    (polecenie,) = _w_kolejce(swiat.wiekszy)
    assert polecenie["ticket"] == ticket_wejscie
    assert len(await _runy(swiat.maker)) == 1
