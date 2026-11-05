"""HAR-173 (czesc 2): polecenie `resume` - kokpit wysyla decyzje, runner wznawia run (TDD).

Runner (`runner.client.Runner`) czyta polecenie ze strumienia SSE kokpitu-atrapy i wznawia run
w OSOBNYM zadaniu (strumien jest czytany dalej, jak przy `run_workflow`). Zle polecenie nie
dochodzi do silnika, a runner zostawia slad na stderr. Podstawiony jest tylko silnik
(`runner.silnik.wznow_run`) - klient i transport sa produkcyjne.
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import asyncio

import pytest

from procesy import zadanie_tla
from runner.bufor import BrakMiejsca, Bufor
from test_polecenie_run_workflow import LIMIT_S, Kokpit, _poczekaj, _runner

RUN_ID = "run-173"
POLECENIE = {"type": "resume", "run_id": RUN_ID, "step_id": "b", "decyzja": "approve",
             "powod": "sprawdzone"}
PREFIKS_ODMOWY = "resume odrzucone:"


class Wznowienie:
    """Podstawiony `silnik.wznow_run`: zapisuje wywolania i czeka na zwolnienie."""

    def __init__(self) -> None:
        self.wywolania: list[dict] = []
        self.wystartowalo = asyncio.Event()
        self.zwolnij = asyncio.Event()
        self.zakonczone = asyncio.Event()
        self.zdarzenia_do_wyslania: list[dict] = []

    async def __call__(self, run_id, step_id, decyzja, emit, *args, powod=None, **kwargs):
        self.wywolania.append({"run_id": run_id, "step_id": step_id, "decyzja": decyzja,
                               "powod": powod, "args": args, "kwargs": kwargs})
        self.wystartowalo.set()
        try:
            for zdarzenie in self.zdarzenia_do_wyslania:
                await emit(dict(zdarzenie))
            await asyncio.wait_for(self.zwolnij.wait(), LIMIT_S)
        finally:
            self.zakonczone.set()


@pytest.fixture
def wznowienie(monkeypatch) -> Wznowienie:
    atrapa = Wznowienie()
    monkeypatch.setattr("runner.silnik.wznow_run", atrapa)
    return atrapa


async def _po_przetworzeniu(kokpit: Kokpit, req_id: str) -> None:
    """Ping za poleceniem: odpowiedz na niego dowodzi, ze polecenie przed nim juz przetworzono."""
    kokpit.wyslij({"type": "ping", "req_id": req_id})
    await _poczekaj(lambda: any(o["req_id"] == req_id for o in kokpit.odpowiedzi),
                    f"odpowiedz na {req_id}")


# =====================================================================================
# 1. Polecenie dochodzi do silnika w osobnym zadaniu
# =====================================================================================


async def test_polecenie_resume_trafia_do_silnika_z_run_krokiem_decyzja_i_powodem(tmp_path, wznowienie):
    kokpit = Kokpit()
    runner = _runner(kokpit, tmp_path)
    async with zadanie_tla(runner.listen(), "nasluch runnera"):
        kokpit.wyslij(POLECENIE)
        await asyncio.wait_for(wznowienie.wystartowalo.wait(), LIMIT_S)

        assert wznowienie.wywolania == [{
            "run_id": RUN_ID, "step_id": "b", "decyzja": "approve", "powod": "sprawdzone",
            "args": (), "kwargs": {}}]
        wznowienie.zwolnij.set()
        await asyncio.wait_for(wznowienie.zakonczone.wait(), LIMIT_S)


async def test_trwajace_wznowienie_nie_blokuje_czytania_strumienia_polecen(tmp_path, wznowienie):
    kokpit = Kokpit()
    runner = _runner(kokpit, tmp_path)
    async with zadanie_tla(runner.listen(), "nasluch runnera"):
        kokpit.wyslij(POLECENIE)
        await asyncio.wait_for(wznowienie.wystartowalo.wait(), LIMIT_S)

        await _po_przetworzeniu(kokpit, "zapytanie-w-trakcie-wznowienia")

        assert not wznowienie.zwolnij.is_set()          # wznowienie nadal trwa
        assert not wznowienie.zakonczone.is_set()
        wznowienie.zwolnij.set()
        await asyncio.wait_for(wznowienie.zakonczone.wait(), LIMIT_S)


async def test_zdarzenia_wznowienia_trafiaja_do_kokpitu_z_tokenem_runnera_i_event_id(tmp_path, wznowienie):
    kokpit = Kokpit()
    wznowienie.zdarzenia_do_wyslania = [
        {"kind": "step_started", "run_id": RUN_ID, "step_id": "b", "agent_id": "b", "text": ""}]
    runner = _runner(kokpit, tmp_path)
    async with zadanie_tla(runner.listen(), "nasluch runnera"):
        kokpit.wyslij(POLECENIE)
        await _poczekaj(lambda: len(kokpit.zdarzenia) == 1, "zdarzenie wznowienia u kokpitu")
        wznowienie.zwolnij.set()

    zdarzenie = kokpit.zdarzenia[0]
    assert zdarzenie["kind"] == "step_started"
    assert zdarzenie["run_id"] == RUN_ID
    assert isinstance(zdarzenie["event_id"], str) and zdarzenie["event_id"]
    assert kokpit.naglowki_zdarzen[0]["Authorization"].startswith("Bearer ")


async def test_zatwierdzenie_bez_powodu_dochodzi_do_silnika_z_powodem_none(tmp_path, wznowienie):
    kokpit = Kokpit()
    runner = _runner(kokpit, tmp_path)
    polecenie = {**POLECENIE, "powod": None}
    async with zadanie_tla(runner.listen(), "nasluch runnera"):
        kokpit.wyslij(polecenie)
        await asyncio.wait_for(wznowienie.wystartowalo.wait(), LIMIT_S)
        wznowienie.zwolnij.set()

    assert wznowienie.wywolania[0]["powod"] is None
    assert wznowienie.wywolania[0]["decyzja"] == "approve"


async def test_polecenie_bez_klucza_powod_dochodzi_do_silnika_z_powodem_none(tmp_path, wznowienie):
    kokpit = Kokpit()
    runner = _runner(kokpit, tmp_path)
    polecenie = {k: v for k, v in POLECENIE.items() if k != "powod"}
    async with zadanie_tla(runner.listen(), "nasluch runnera"):
        kokpit.wyslij(polecenie)
        await asyncio.wait_for(wznowienie.wystartowalo.wait(), LIMIT_S)
        wznowienie.zwolnij.set()

    assert len(wznowienie.wywolania) == 1
    assert wznowienie.wywolania[0]["powod"] is None


async def test_odrzucenie_z_powodem_dochodzi_do_silnika_z_decyzja_reject(tmp_path, wznowienie):
    kokpit = Kokpit()
    runner = _runner(kokpit, tmp_path)
    polecenie = {**POLECENIE, "decyzja": "reject", "powod": "za ryzykowne"}
    async with zadanie_tla(runner.listen(), "nasluch runnera"):
        kokpit.wyslij(polecenie)
        await asyncio.wait_for(wznowienie.wystartowalo.wait(), LIMIT_S)
        wznowienie.zwolnij.set()

    assert wznowienie.wywolania[0]["decyzja"] == "reject"
    assert wznowienie.wywolania[0]["powod"] == "za ryzykowne"


async def test_zakonczone_wznowienie_znika_z_zadan_runnera(tmp_path, wznowienie):
    kokpit = Kokpit()
    runner = _runner(kokpit, tmp_path)
    async with zadanie_tla(runner.listen(), "nasluch runnera"):
        kokpit.wyslij(POLECENIE)
        await asyncio.wait_for(wznowienie.wystartowalo.wait(), LIMIT_S)
        assert len(runner._runy) == 1                    # trwajace wznowienie jest sledzone

        wznowienie.zwolnij.set()
        await asyncio.wait_for(wznowienie.zakonczone.wait(), LIMIT_S)

        await _poczekaj(lambda: not runner._runy, "usuniecie zakonczonego wznowienia z zadan")


async def test_stop_runnera_anuluje_trwajace_wznowienie(tmp_path, wznowienie):
    kokpit = Kokpit()
    runner = _runner(kokpit, tmp_path)
    async with zadanie_tla(runner.listen(), "nasluch runnera"):
        kokpit.wyslij(POLECENIE)
        await asyncio.wait_for(wznowienie.wystartowalo.wait(), LIMIT_S)

        await asyncio.wait_for(runner.stop(), LIMIT_S)

        assert wznowienie.zakonczone.is_set()           # zadanie wznowienia anulowane przez stop()
        assert not wznowienie.zwolnij.is_set()


async def test_brak_miejsca_na_zdarzenia_wznowienia_zglasza_kokpitowi_przerwanie_kroku_bramki(
    tmp_path, wznowienie,
):
    class PelnyBufor(Bufor):
        def dopisz(self, zdarzenie: dict) -> None:
            if zdarzenie.get("kind") != "step_aborted":
                raise BrakMiejsca("zapis bufora: brak miejsca")
            super().dopisz(zdarzenie)

    wznowienie.zdarzenia_do_wyslania = [
        {"kind": "step_started", "run_id": RUN_ID, "step_id": "b", "agent_id": "b", "text": ""}]
    kokpit = Kokpit()
    runner = _runner(kokpit, tmp_path, bufor=PelnyBufor(tmp_path / "bufor"))
    async with zadanie_tla(runner.listen(), "nasluch runnera"):
        kokpit.wyslij(POLECENIE)
        await _poczekaj(lambda: kokpit.zdarzenia, "przerwanie wznowienia u kokpitu")
        await _poczekaj(wznowienie.zakonczone.is_set, "koniec zadania wznowienia")

    (przerwanie,) = kokpit.zdarzenia
    assert przerwanie["kind"] == "step_aborted"
    assert przerwanie["run_id"] == RUN_ID
    assert przerwanie["step_id"] == "b"
    assert przerwanie["text"] == "brak miejsca: zapis zdarzen runnera nie powiodl sie (ENOSPC)"


# =====================================================================================
# 2. Zle polecenia: odmowa z sladem na stderr, silnik nietkniety
# =====================================================================================

# kazdy przypadek zmienia dokladnie jeden warunek walidacji
ZLE_POLECENIA = {
    "brak_run_id": lambda p: {k: v for k, v in p.items() if k != "run_id"},
    "pusty_run_id": lambda p: {**p, "run_id": ""},
    "run_id_nie_jest_napisem": lambda p: {**p, "run_id": 173},
    "brak_step_id": lambda p: {k: v for k, v in p.items() if k != "step_id"},
    "pusty_step_id": lambda p: {**p, "step_id": ""},
    "step_id_nie_jest_napisem": lambda p: {**p, "step_id": 5},
    "decyzja_spoza_listy": lambda p: {**p, "decyzja": "maybe"},
    "decyzja_wielkimi_literami": lambda p: {**p, "decyzja": "APPROVE"},
    "brak_decyzji": lambda p: {k: v for k, v in p.items() if k != "decyzja"},
    "powod_nie_jest_napisem": lambda p: {**p, "powod": 7},
    "powod_jest_lista": lambda p: {**p, "powod": ["a"]},
}


@pytest.mark.parametrize("zepsuj", list(ZLE_POLECENIA.values()), ids=list(ZLE_POLECENIA))
async def test_zle_polecenie_resume_nie_dochodzi_do_silnika_i_zostawia_slad_na_stderr(
    tmp_path, wznowienie, capsys, zepsuj,
):
    kokpit = Kokpit()
    runner = _runner(kokpit, tmp_path)
    zle = zepsuj(POLECENIE)
    capsys.readouterr()
    async with zadanie_tla(runner.listen(), "nasluch runnera"):
        kokpit.wyslij(zle)
        await _po_przetworzeniu(kokpit, "po-zlym-poleceniu")
        # silnik mogl ruszyc w osobnym zadaniu: dajemy mu kilka obrotow petli
        for _ in range(5):
            await asyncio.sleep(0)

    assert wznowienie.wywolania == []
    assert kokpit.zdarzenia == []                           # nic nie poszlo do kokpitu
    linie = [w for w in capsys.readouterr().err.splitlines() if w.startswith(PREFIKS_ODMOWY)]
    assert linie == [f"{PREFIKS_ODMOWY} niepoprawne polecenie {zle!r}"]     # cala linia


async def test_po_odrzuconym_poleceniu_resume_runner_przyjmuje_kolejne_poprawne(tmp_path, wznowienie):
    kokpit = Kokpit()
    runner = _runner(kokpit, tmp_path)
    async with zadanie_tla(runner.listen(), "nasluch runnera"):
        kokpit.wyslij({**POLECENIE, "decyzja": "maybe"})
        kokpit.wyslij(POLECENIE)
        await asyncio.wait_for(wznowienie.wystartowalo.wait(), LIMIT_S)
        wznowienie.zwolnij.set()

    assert [w["decyzja"] for w in wznowienie.wywolania] == ["approve"]


async def test_poprawne_polecenie_resume_nie_zostawia_sladu_odmowy_na_stderr(tmp_path, wznowienie, capsys):
    kokpit = Kokpit()
    runner = _runner(kokpit, tmp_path)
    capsys.readouterr()
    async with zadanie_tla(runner.listen(), "nasluch runnera"):
        kokpit.wyslij(POLECENIE)
        await asyncio.wait_for(wznowienie.wystartowalo.wait(), LIMIT_S)
        wznowienie.zwolnij.set()
        await asyncio.wait_for(wznowienie.zakonczone.wait(), LIMIT_S)

    assert PREFIKS_ODMOWY not in capsys.readouterr().err
