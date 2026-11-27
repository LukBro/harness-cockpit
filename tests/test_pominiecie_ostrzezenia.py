"""HAR-246: "uruchom mimo to" - czlowiek pomija ostrzezenie o atrapie bramki (TDD).

`POST /api/runs/{run_id}/pominiecie-ostrzezenia` z `{"step_id": ...}` zapisuje w historii runu
zdarzenie `atrapa_pominieta` z `user_id`. Pominac mozna tylko ostrzezenie atrapy tego kroku
nowsze niz ostatnie pominiecie. Status runu sie nie zmienia, runner nie moze tego zglosic.

Skutki czytamy z bazy. Serwis wolamy przez modul (`uslugi.pomin_ostrzezenie`), wiec przed
implementacja testy oblewaja sie na braku funkcji, a nie na bledzie zbierania.
"""
# fixture'y importowane z test_anulowanie_runu sa uzywane tylko jako nazwy parametrow
# ruff: noqa: F401, F811
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from cockpit.api import bus
from cockpit.db.models import Event, Run, RunStatus
from cockpit.services import runs as uslugi
from cockpit.services.errors import NaruszonyNiezmiennik, NieZnaleziono, StanNiemozliwy
from procesy import czekaj
from test_anulowanie_runu import (
    LIMIT_S,
    SesjaZBramka,
    _czeka_na_blokade_wiersza,
    _stan,
    _swiat_pg,
    _zajety_numer,
    _zdarzenia,
    _zdarzenie_runnera,
    maker_pg,
    swiat,
    tylko_postgres,
    zapytania_o_runy_dla_postgresa,
)

KIND = "atrapa_pominieta"
KROK = "test"
TEKST = ("atrapa bramki — kod wyjścia wymuszony na zero: „pytest || true”. "
         "Co poprawić: usuń || true.")
ODMOWA = "krok nie ma ostrzeżenia o atrapie bramki do pominięcia"


async def _dopisz(swiat, run_id: str, seq: int, *, kind="warning", step_id=KROK, text=TEKST,
                  tenant_id: str | None = None) -> None:
    async with swiat.maker() as s:
        s.add(Event(tenant_id=tenant_id or swiat.tenant_a, run_id=run_id, seq=seq, kind=kind,
                    step_id=step_id, text=text))
        await s.commit()


async def _run_z_ostrzezeniem(swiat, status=RunStatus.running) -> str:
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, status)
    await _dopisz(swiat, run_id, 1)
    return run_id


def _pomin(klient, run_id: str, step_id: str = KROK):
    return klient.post(f"/api/runs/{run_id}/pominiecie-ostrzezenia", json={"step_id": step_id})


async def _pominiecia(swiat, run_id: str) -> list[Event]:
    return [e for e in await _zdarzenia(swiat, run_id) if e.kind == KIND]


# --- udane pominiecie ------------------------------------------------------------------------


@pytest.mark.parametrize("status", [RunStatus.running, RunStatus.to_accept])
async def test_pominiecie_ostrzezenia_zostaje_w_historii_runu_z_osoba_i_nie_zmienia_stanu_runu(
    swiat, status,
):
    run_id = await _run_z_ostrzezeniem(swiat, status)
    klient = await swiat.zalogowany(swiat.czlonek_a)

    odp = await _pomin(klient, run_id)

    assert odp.status_code == 200, odp.text
    assert odp.json() == {"run_id": run_id, "step_id": KROK}
    (ostrzezenie, pominiecie) = await _zdarzenia(swiat, run_id)
    assert (ostrzezenie.kind, ostrzezenie.text) == ("warning", TEKST)
    assert (pominiecie.seq, pominiecie.kind, pominiecie.step_id, pominiecie.tenant_id) == (
        2, KIND, KROK, swiat.tenant_a)
    assert pominiecie.dane == {"user_id": swiat.czlonek_a.id}
    assert pominiecie.text == "ostrzeżenie pominięte przez człowieka"
    assert (pominiecie.text_full_len, pominiecie.text_truncated) == (len(pominiecie.text), False)
    assert await _stan(swiat, run_id) == (status, None)


async def test_ostrzezenie_zgloszone_przez_runnera_czlowiek_moze_pominac(swiat):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a)
    klient = await swiat.zalogowany(swiat.czlonek_a)
    zgloszone = await _zdarzenie_runnera(klient, run_id, "warning", text=TEKST)
    assert zgloszone.status_code == 200, zgloszone.text

    odp = await _pomin(klient, run_id, "s0")

    assert odp.status_code == 200, odp.text
    assert [(e.kind, e.step_id) for e in await _zdarzenia(swiat, run_id)] == [
        ("warning", "s0"), (KIND, "s0")]


async def test_pominiecie_nie_wysyla_nic_runnerowi_runu(swiat):
    run_id = await _run_z_ostrzezeniem(swiat)
    klient = await swiat.zalogowany(swiat.czlonek_a)

    assert (await _pomin(klient, run_id)).status_code == 200

    assert bus.runner_queue(swiat.runner_a).empty()


async def test_pominiecie_jest_od_razu_widoczne_w_strumieniu_runu(swiat):
    run_id = await _run_z_ostrzezeniem(swiat)
    klient = await swiat.zalogowany(swiat.czlonek_a)
    q = bus.subscribe_run(run_id)
    try:
        await _pomin(klient, run_id)
        ev = await asyncio.wait_for(q.get(), 2)
    finally:
        bus.unsubscribe_run(run_id, q)

    assert (ev["seq"], ev["kind"], ev["step_id"]) == (2, KIND, KROK)


# --- kiedy nie ma czego pominac --------------------------------------------------------------


@pytest.mark.parametrize(
    "wpisy",
    [
        pytest.param([], id="brak ostrzezenia"),
        pytest.param([{"step_id": "lint"}], id="ostrzezenie innego kroku"),
        pytest.param([{"text": "autor: pozorny_konsensus: jeden dostawca"}],
                     id="ostrzezenie walidatora"),
        pytest.param([{"text": "uwaga: " + TEKST}], id="prefiks w srodku tekstu"),
        pytest.param([{"kind": "output"}], id="zwykle wyjscie z tekstem atrapy"),
    ],
)
async def test_bez_ostrzezenia_atrapy_na_tym_kroku_pominiecie_daje_409_i_nic_nie_zapisuje(
    swiat, wpisy,
):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a)
    for seq, wpis in enumerate(wpisy, 1):
        await _dopisz(swiat, run_id, seq, **wpis)
    klient = await swiat.zalogowany(swiat.czlonek_a)

    odp = await _pomin(klient, run_id)

    assert (odp.status_code, odp.json()) == (409, {"detail": ODMOWA})
    assert await _pominiecia(swiat, run_id) == []


async def test_ostrzezenie_innego_runu_nie_pozwala_pominac(swiat):
    await _run_z_ostrzezeniem(swiat)
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a)
    klient = await swiat.zalogowany(swiat.czlonek_a)

    odp = await _pomin(klient, run_id)

    assert (odp.status_code, odp.json()) == (409, {"detail": ODMOWA})
    assert await _zdarzenia(swiat, run_id) == []


async def test_ostrzezenie_zapisane_pod_obcym_zespolem_nie_pozwala_pominac(swiat):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a)
    await _dopisz(swiat, run_id, 1, tenant_id=swiat.tenant_b)

    async with swiat.maker() as s:
        with pytest.raises(StanNiemozliwy) as wyjatek:
            await uslugi.pomin_ostrzezenie(s, run_id, swiat.tenant_a, swiat.czlonek_a.id, KROK)

    assert str(wyjatek.value) == ODMOWA
    assert await _pominiecia(swiat, run_id) == []


async def test_pominiecie_zapisane_pod_obcym_zespolem_nie_uniewaznia_ostrzezenia(swiat):
    run_id = await _run_z_ostrzezeniem(swiat)
    await _dopisz(swiat, run_id, 1000, kind=KIND, tenant_id=swiat.tenant_b)

    async with swiat.maker() as s:
        await uslugi.pomin_ostrzezenie(s, run_id, swiat.tenant_a, swiat.czlonek_a.id, KROK)

    assert [e.tenant_id for e in await _pominiecia(swiat, run_id)] == [
        swiat.tenant_a, swiat.tenant_b]


async def test_drugie_pominiecie_tego_samego_ostrzezenia_daje_409_i_jeden_wpis(swiat):
    run_id = await _run_z_ostrzezeniem(swiat)
    klient = await swiat.zalogowany(swiat.czlonek_a)
    assert (await _pomin(klient, run_id)).status_code == 200

    odp = await _pomin(klient, run_id)

    assert (odp.status_code, odp.json()) == (409, {"detail": ODMOWA})
    assert len(await _pominiecia(swiat, run_id)) == 1


async def test_nowe_ostrzezenie_po_pominieciu_mozna_pominac_ponownie(swiat):
    run_id = await _run_z_ostrzezeniem(swiat)
    klient = await swiat.zalogowany(swiat.czlonek_a)
    assert (await _pomin(klient, run_id)).status_code == 200
    await _dopisz(swiat, run_id, 3)

    odp = await _pomin(klient, run_id)

    assert odp.status_code == 200, odp.text
    assert [e.seq for e in await _pominiecia(swiat, run_id)] == [2, 4]


async def test_pominiecie_jednego_kroku_nie_pomija_ostrzezenia_drugiego(swiat):
    run_id = await _run_z_ostrzezeniem(swiat)
    await _dopisz(swiat, run_id, 2, step_id="lint")
    klient = await swiat.zalogowany(swiat.czlonek_a)
    assert (await _pomin(klient, run_id, "lint")).status_code == 200

    odp = await _pomin(klient, run_id)

    assert odp.status_code == 200, odp.text
    assert [e.step_id for e in await _pominiecia(swiat, run_id)] == ["lint", KROK]


# --- zespol i wejscie ------------------------------------------------------------------------


async def test_pominiecie_na_runie_obcego_zespolu_daje_404_i_nic_nie_zapisuje(swiat):
    wlasny = await _run_z_ostrzezeniem(swiat)
    obcy = await swiat.run(swiat.tenant_b, swiat.runner_b)
    await _dopisz(swiat, obcy, 1, tenant_id=swiat.tenant_b)
    klient = await swiat.zalogowany(swiat.czlonek_a)
    assert (await _pomin(klient, wlasny)).status_code == 200    # 404 to nie brak trasy

    odp = await _pomin(klient, obcy)

    assert odp.status_code == 404
    assert await _pominiecia(swiat, obcy) == []


async def test_pominiecie_na_nieistniejacym_runie_daje_404(swiat):
    wlasny = await _run_z_ostrzezeniem(swiat)
    klient = await swiat.zalogowany(swiat.czlonek_a)
    assert (await _pomin(klient, wlasny)).status_code == 200    # 404 to nie brak trasy

    assert (await _pomin(klient, "nie-ma-takiego-runu")).status_code == 404


async def test_serwis_pominiecia_runu_obcego_zespolu_rzuca_nieznaleziono(swiat):
    obcy = await swiat.run(swiat.tenant_b, swiat.runner_b)
    await _dopisz(swiat, obcy, 1, tenant_id=swiat.tenant_b)

    async with swiat.maker() as s:
        with pytest.raises(NieZnaleziono):
            await uslugi.pomin_ostrzezenie(s, obcy, swiat.tenant_a, swiat.czlonek_a.id, KROK)

    assert await _pominiecia(swiat, obcy) == []


@pytest.mark.parametrize(("dlugosc", "kod"), [(0, 422), (1, 409), (100, 409), (101, 422)])
async def test_identyfikator_kroku_dluzszy_niz_kolumna_jest_odrzucany_na_wejsciu(
    swiat, dlugosc, kod,
):
    run_id = await _run_z_ostrzezeniem(swiat)
    klient = await swiat.zalogowany(swiat.czlonek_a)

    odp = await _pomin(klient, run_id, "k" * dlugosc)

    assert odp.status_code == kod
    assert await _pominiecia(swiat, run_id) == []


async def test_pominiecie_bez_wskazania_kroku_daje_422(swiat):
    run_id = await _run_z_ostrzezeniem(swiat)
    klient = await swiat.zalogowany(swiat.czlonek_a)

    odp = await klient.post(f"/api/runs/{run_id}/pominiecie-ostrzezenia", json={})

    assert odp.status_code == 422
    assert await _pominiecia(swiat, run_id) == []


# --- runner nie decyduje za czlowieka --------------------------------------------------------


async def test_runner_nie_moze_zglosic_pominiecia_ostrzezenia(swiat):
    run_id = await _run_z_ostrzezeniem(swiat)
    klient = await swiat.zalogowany(swiat.czlonek_a)

    odp = await _zdarzenie_runnera(klient, run_id, KIND)

    assert odp.status_code == 422
    # odmowa regula czlowieka, a nie nieznanym rodzajem na wejsciu
    assert "zarezerwowany dla decyzji czlowieka" in odp.text
    assert await _pominiecia(swiat, run_id) == []


async def test_serwis_zdarzen_runnera_odrzuca_pominiecie_niezmiennikiem(swiat):
    run_id = await _run_z_ostrzezeniem(swiat)

    async with swiat.maker() as s:
        run = await s.get(Run, run_id)
        with pytest.raises(NaruszonyNiezmiennik) as wyjatek:
            await uslugi.zapisz_zdarzenie(s, run, event_id=str(uuid.uuid4()), kind=KIND)

    assert wyjatek.value.nazwa == "approval_required"
    assert await _pominiecia(swiat, run_id) == []


# --- warunek i zapis razem -------------------------------------------------------------------


async def test_pominiecie_czyta_run_pod_blokada_wiersza(swiat, zapytania_o_runy_dla_postgresa):
    run_id = await _run_z_ostrzezeniem(swiat)

    async with swiat.maker() as s:
        await uslugi.pomin_ostrzezenie(s, run_id, swiat.tenant_a, swiat.czlonek_a.id, KROK)

    assert [q for q in zapytania_o_runy_dla_postgresa if "FOR UPDATE" in q]


async def test_odmowa_pominiecia_nie_zostawia_otwartej_transakcji_z_blokada(swiat):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a)

    async with swiat.maker() as s:
        with pytest.raises(StanNiemozliwy) as wyjatek:
            await uslugi.pomin_ostrzezenie(s, run_id, swiat.tenant_a, swiat.czlonek_a.id, KROK)

        assert str(wyjatek.value) == ODMOWA
        assert not s.in_transaction()


async def test_kolizja_numeru_zdarzenia_przy_pominieciu_jest_ponawiana(swiat, monkeypatch):
    run_id = await _run_z_ostrzezeniem(swiat)
    _zajety_numer(monkeypatch, zajety=1)

    async with swiat.maker() as s:
        await uslugi.pomin_ostrzezenie(s, run_id, swiat.tenant_a, swiat.czlonek_a.id, KROK)

    assert [(e.seq, e.kind) for e in await _zdarzenia(swiat, run_id)] == [
        (1, "warning"), (2, KIND)]


@pytest.mark.parametrize("moment", ["przed numerem", "po numerze"])
async def test_pominiecie_wyprzedzone_przez_drugie_miedzy_sprawdzeniem_a_zapisem_jest_odrzucane(
    swiat, monkeypatch, moment,
):
    # druga sesja zapisuje pominiecie w trakcie liczenia numeru pierwszej; "przed numerem" lapie
    # warunek sprawdzony przed wyliczeniem numeru (numer bylby swiezy i oba zapisy by przeszly)
    run_id = await _run_z_ostrzezeniem(swiat)
    prawdziwa = uslugi._nastepny_seq
    przeplot: list[bool] = []

    async def z_przeplotem(s, run, tenant_id):
        numer = await prawdziwa(s, run, tenant_id) if moment == "po numerze" else None
        if not przeplot:
            przeplot.append(True)
            async with swiat.maker() as druga:
                await uslugi.pomin_ostrzezenie(
                    druga, run_id, swiat.tenant_a, swiat.wlasciciel_a.id, KROK)
        return numer if numer is not None else await prawdziwa(s, run, tenant_id)

    monkeypatch.setattr(uslugi, "_nastepny_seq", z_przeplotem)

    async with swiat.maker() as s:
        with pytest.raises(StanNiemozliwy) as wyjatek:
            await czekaj(
                uslugi.pomin_ostrzezenie(s, run_id, swiat.tenant_a, swiat.czlonek_a.id, KROK),
                LIMIT_S, "pierwsze pominiecie")

    assert str(wyjatek.value) == ODMOWA
    (jedyne,) = await _pominiecia(swiat, run_id)
    assert jedyne.dane == {"user_id": swiat.wlasciciel_a.id}


@pytest.mark.baza
@tylko_postgres
async def test_dwa_rownoczesne_pominiecia_na_postgresie_daja_jeden_wpis_i_jedna_odmowe(maker_pg):
    swiat_pg = await _swiat_pg(maker_pg)
    async with maker_pg() as s:
        s.add(Event(tenant_id=swiat_pg.tenant_id, run_id=swiat_pg.run_id, seq=1, kind="warning",
                    step_id=KROK, text=TEKST))
        await s.commit()
    bramka = SesjaZBramka(bind=maker_pg.kw["bind"], expire_on_commit=False)
    bramka.po_zapisach, bramka.zwolnij = asyncio.Event(), asyncio.Event()

    async def pomin(sesja):
        async with sesja:
            return await uslugi.pomin_ostrzezenie(
                sesja, swiat_pg.run_id, swiat_pg.tenant_id, swiat_pg.user_id, KROK)

    pierwsze = asyncio.create_task(pomin(bramka))
    drugie = None
    try:
        stoi = asyncio.ensure_future(
            czekaj(bramka.po_zapisach.wait(), LIMIT_S, "pierwsza sesja przed commitem"))
        await asyncio.wait({stoi, pierwsze}, timeout=LIMIT_S, return_when=asyncio.FIRST_COMPLETED)
        if not bramka.po_zapisach.is_set():
            stoi.cancel()
            powod = pierwsze.exception() if pierwsze.done() else "brak commitu w limicie"
            raise AssertionError(f"pierwsza sesja nie doszla do commitu: {powod!r}")
        drugie = asyncio.create_task(pomin(maker_pg()))
        koniec = asyncio.get_running_loop().time() + LIMIT_S
        while not drugie.done() and not await _czeka_na_blokade_wiersza(maker_pg):
            assert asyncio.get_running_loop().time() < koniec, (
                "druga sesja ani nie skonczyla, ani nie czeka na blokade")
            await asyncio.sleep(0.05)
    finally:
        bramka.zwolnij.set()
    wynik_1, wynik_2 = await asyncio.wait_for(
        asyncio.gather(pierwsze, drugie, return_exceptions=True), LIMIT_S)

    assert not isinstance(wynik_1, BaseException), wynik_1
    assert type(wynik_2) is StanNiemozliwy, wynik_2
    assert str(wynik_2) == ODMOWA
    async with maker_pg() as s:
        rodzaje = (await s.execute(
            select(Event.kind).where(Event.run_id == swiat_pg.run_id).order_by(Event.seq)
        )).scalars().all()
    assert list(rodzaje) == ["warning", KIND]
