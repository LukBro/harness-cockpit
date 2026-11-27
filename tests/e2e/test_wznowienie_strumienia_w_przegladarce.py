"""HAR-202: po wznowieniu strumienia zdarzen front nie pobiera ponownie calej historii.

Strumien runu jest wstrzymany w przegladarce, a test sam go otwiera i zrywa: odpowiedz
`text/event-stream` konczy sie od razu, wiec przegladarka zglasza zerwanie i laczy sie ponownie
po czasie z `retry`. Podstawiony strumien nie niesie nowych zdarzen (poza testem, ktory tego
wymaga), wiec moga one dojsc do ekranu tylko przez pobranie szczegolow runu z prawdziwego kokpitu.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from playwright.sync_api import Error as BladPlaywrighta
from playwright.sync_api import expect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [pytest.mark.e2e, pytest.mark.baza]

LIMIT_WYNIKU_MS = 5000
KOMUNIKAT_ZERWANIA = re.compile("Brak połączenia ze strumieniem zdarzeń")
# numery z przerwami: najwyzszy posiadany numer to nie liczba zdarzen
STARE = [3, 7, 12]


def _wstaw_run(numery: list[int], przedrostek: str) -> str:
    """Run w toku zespolu zasianego w bazie, ze zdarzeniami o podanych numerach."""
    run_id = str(uuid.uuid4())

    async def _wstaw():
        engine = create_async_engine(os.environ["COCKPIT_TEST_DATABASE_URL"])
        try:
            async with engine.begin() as conn:
                await conn.execute(text(
                    "INSERT INTO run (id, tenant_id, runner_id, runner_kind, workflow, "
                    "status, created_at) "
                    "SELECT :id, tenant_id, id, 'local', 'szkielet', 'running', now() "
                    "FROM runner LIMIT 1"), {"id": run_id})
                for seq in numery:
                    await conn.execute(text(
                        "INSERT INTO event (id, tenant_id, run_id, seq, step_id, agent_id, "
                        "agent_type, kind, text, cost_source, at, event_id) "
                        "SELECT :zdarzenie, tenant_id, id, :seq, 's0', '', '', 'output', :tekst, "
                        "'unavailable', now(), :zdarzenie FROM run WHERE id = :run"),
                        {"zdarzenie": str(uuid.uuid4()), "seq": seq,
                         "tekst": f"{przedrostek}-{seq}", "run": run_id})
        finally:
            await engine.dispose()

    # sync API playwrighta trzyma petle w watku testu: asyncio.run osobno
    with ThreadPoolExecutor(max_workers=1) as pula:
        pula.submit(lambda: asyncio.run(_wstaw())).result(timeout=60)
    return run_id


def _wyslij(adres: str, token: str, run_id: str, tekst: str) -> int:
    """Zdarzenie od runnera jak w produkcji; zwraca nadany numer."""
    odpowiedz = httpx.post(
        f"{adres}/api/events", headers={"Authorization": f"Bearer {token}"},
        json={"run_id": run_id, "event_id": str(uuid.uuid4()), "step_id": "s0",
              "kind": "output", "text": tekst},
        timeout=5)
    assert odpowiedz.status_code == 200, odpowiedz.text
    return odpowiedz.json()["seq"]


def _czekaj_na(page, warunek, nazwa: str, limit_ms: int = LIMIT_WYNIKU_MS) -> None:
    odliczanie = limit_ms
    while not warunek():
        assert odliczanie > 0, f"przekroczony limit {limit_ms} ms: {nazwa}"
        page.wait_for_timeout(50)
        odliczanie -= 50


@pytest.fixture
def strumienie(page):
    """Zadania o strumien runu, wstrzymane do decyzji testu; pozostale porzucane po tescie."""
    wstrzymane: list = []
    page.route("**/api/runs/*/events", lambda trasa: wstrzymane.append(trasa))
    yield wstrzymane
    for trasa in wstrzymane:
        try:
            trasa.abort()
        except BladPlaywrighta:
            pass


def _otworz_i_zerwij(page, strumienie: list, *, ramki: str = "") -> None:
    """Otwiera wstrzymany strumien i od razu go zrywa; wraca, gdy przegladarka laczy sie ponownie."""
    _czekaj_na(page, lambda: len(strumienie) == 1, "zadanie o strumien runu")
    strumienie.pop().fulfill(
        status=200, headers={"content-type": "text/event-stream"},
        body=f"retry: 200\n\n{ramki}")
    _czekaj_na(page, lambda: len(strumienie) == 1, "ponowne laczenie po zerwaniu strumienia")


def _wznow(page, strumienie: list) -> None:
    """Otwiera strumien po zerwaniu; nastepne ponowienie dopiero za minute, poza testem."""
    strumienie.pop().fulfill(
        status=200, headers={"content-type": "text/event-stream"}, body="retry: 60000\n\n")


def _sledz_pobrania(page, run_id: str) -> list:
    """Odpowiedzi na pobrania szczegolow tego runu (z parametrami i bez), w kolejnosci."""
    pobrania: list = []

    def zanotuj(odpowiedz) -> None:
        if (odpowiedz.request.method == "GET"
                and urlparse(odpowiedz.url).path == f"/api/runs/{run_id}"):
            pobrania.append(odpowiedz)

    page.on("response", zanotuj)
    return pobrania


def _parametry(odpowiedz) -> dict:
    return parse_qs(urlparse(odpowiedz.url).query)


def _numery(odpowiedz) -> list[int]:
    return [z["seq"] for z in odpowiedz.json()["events"]]


def _wiersze(page):
    return page.locator("li", has_text=re.compile(r"(stare|nowe|drugi)-\d+"))


def test_po_wznowieniu_strumienia_front_pobiera_tylko_nowsze_zdarzenia_i_pokazuje_calosc_raz(
    strumienie, zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _wstaw_run(STARE, "stare")
    pobrania = _sledz_pobrania(page, run_id)
    page.goto(f"{adres_kokpitu}/?run={run_id}")
    expect(_wiersze(page)).to_have_count(3, timeout=LIMIT_WYNIKU_MS)
    # pierwsze wejscie: cala historia, bez parametru
    assert [(_parametry(p), _numery(p)) for p in pobrania] == [({}, STARE)]

    _otworz_i_zerwij(page, strumienie)
    expect(page.get_by_text(KOMUNIKAT_ZERWANIA)).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    _czekaj_na(page, lambda: len(pobrania) == 2, "pobranie po otwarciu strumienia")
    nowe = [_wyslij(adres_kokpitu, token_runnera, run_id, f"nowe-{n}") for n in (1, 2)]
    assert nowe == [13, 14]
    _wznow(page, strumienie)
    _czekaj_na(page, lambda: len(pobrania) == 3, "pobranie po wznowieniu strumienia")

    assert (_parametry(pobrania[2]), _numery(pobrania[2])) == ({"po_seq": ["12"]}, nowe)
    assert (_parametry(pobrania[1]), _numery(pobrania[1])) == ({"po_seq": ["12"]}, [])
    # stare zdarzenia zostaja na ekranie, choc odpowiedz ich nie niosla
    expect(_wiersze(page)).to_have_text(
        ["output stare-3", "output stare-7", "output stare-12",
         "output nowe-1", "output nowe-2"], timeout=LIMIT_WYNIKU_MS)


def test_zdarzenie_otrzymane_strumieniem_liczy_sie_do_posiadanych_przy_nastepnym_pobraniu(
    strumienie, zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _wstaw_run(STARE, "stare")
    pobrania = _sledz_pobrania(page, run_id)
    page.goto(f"{adres_kokpitu}/?run={run_id}")
    expect(_wiersze(page)).to_have_count(3, timeout=LIMIT_WYNIKU_MS)
    seq = _wyslij(adres_kokpitu, token_runnera, run_id, "nowe-1")
    ramka = {"seq": seq, "kind": "output", "text": "nowe-1", "step_id": "s0",
             "text_truncated": False, "text_full_len": 6, "at": "2026-01-01T00:00:00+00:00"}
    # pobranie po otwarciu strumienia nie dochodzi: nowe zdarzenie front ma tylko ze strumienia
    odciete: list = []

    def odetnij_pierwsze(trasa) -> None:
        if odciete:
            trasa.continue_()
        else:
            odciete.append(trasa.request.url)
            trasa.abort()

    page.route(re.compile(rf"/api/runs/{re.escape(run_id)}(\?.*)?$"), odetnij_pierwsze)
    _otworz_i_zerwij(page, strumienie,
                     ramki=f"id: {seq}\nevent: run\ndata: {json.dumps(ramka)}\n\n")
    expect(_wiersze(page)).to_have_count(4, timeout=LIMIT_WYNIKU_MS)
    _czekaj_na(page, lambda: len(odciete) == 1, "odciete pobranie po otwarciu strumienia")
    assert len(pobrania) == 1

    _wznow(page, strumienie)
    _czekaj_na(page, lambda: len(pobrania) == 2, "pobranie po wznowieniu strumienia")

    assert (_parametry(pobrania[1]), _numery(pobrania[1])) == ({"po_seq": [str(seq)]}, [])
    expect(_wiersze(page)).to_have_count(4)


def test_starsze_zdarzenie_powtorzone_strumieniem_nie_cofa_numeru_od_ktorego_front_pobiera(
    strumienie, zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    run_id = _wstaw_run(STARE, "stare")
    pobrania = _sledz_pobrania(page, run_id)
    page.goto(f"{adres_kokpitu}/?run={run_id}")
    expect(_wiersze(page)).to_have_count(3, timeout=LIMIT_WYNIKU_MS)
    # zdarzenie, ktore front juz ma, nizsze od najwyzszego posiadanego
    ramka = {"seq": 7, "kind": "output", "text": "stare-7", "step_id": "s0",
             "text_truncated": False, "text_full_len": 7, "at": "2026-01-01T00:00:00+00:00"}

    _otworz_i_zerwij(page, strumienie,
                     ramki=f"id: 7\nevent: run\ndata: {json.dumps(ramka)}\n\n")
    _czekaj_na(page, lambda: len(pobrania) == 2, "pobranie po otwarciu strumienia")
    _wznow(page, strumienie)
    _czekaj_na(page, lambda: len(pobrania) == 3, "pobranie po wznowieniu strumienia")

    assert (_parametry(pobrania[2]), _numery(pobrania[2])) == ({"po_seq": ["12"]}, [])
    expect(_wiersze(page)).to_have_text(
        ["output stare-3", "output stare-7", "output stare-12"], timeout=LIMIT_WYNIKU_MS)


def test_run_bez_zdarzen_jest_po_wznowieniu_pobierany_bez_numeru_poczatkowego(
    strumienie, zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    run_id = _wstaw_run([], "stare")
    pobrania = _sledz_pobrania(page, run_id)
    page.goto(f"{adres_kokpitu}/?run={run_id}")
    _czekaj_na(page, lambda: len(pobrania) == 1, "pierwsze pobranie runu")

    _otworz_i_zerwij(page, strumienie)
    _czekaj_na(page, lambda: len(pobrania) == 2, "pobranie po otwarciu strumienia")
    _wznow(page, strumienie)
    _czekaj_na(page, lambda: len(pobrania) == 3, "pobranie po wznowieniu strumienia")

    assert [_parametry(p) for p in pobrania] == [{}, {}, {}]


def test_pierwsze_pobranie_nastepnego_runu_nie_pomija_zdarzen_przez_numer_poprzedniego(
    strumienie, runner_online, zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    pierwszy = _wstaw_run(STARE, "stare")
    drugi = _wstaw_run([1, 2], "drugi")
    page.goto(f"{adres_kokpitu}/?run={pierwszy}")
    expect(_wiersze(page)).to_have_count(3, timeout=LIMIT_WYNIKU_MS)
    pobrania = _sledz_pobrania(page, drugi)
    # drugi run otwiera przycisk "Uruchom" (odpowiedz podstawiona), bez przeladowania strony
    page.route("**/api/runs", lambda r: r.fulfill(json={"run_id": drugi})
               if r.request.method == "POST" else r.continue_())
    przycisk = page.get_by_role("button", name="Uruchom", exact=True)
    expect(przycisk).to_be_enabled(timeout=2 * LIMIT_WYNIKU_MS)

    przycisk.click()

    expect(_wiersze(page)).to_have_text(
        ["output drugi-1", "output drugi-2"], timeout=LIMIT_WYNIKU_MS)
    assert (_parametry(pobrania[0]), _numery(pobrania[0])) == ({}, [1, 2])
