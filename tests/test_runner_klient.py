"""Runner: wynik komendy trafia do kokpitu jako zdarzenie końcowe kroku.

Transport HTTP jest przechwycony (`httpx.MockTransport`), a komendy to prawdziwe
procesy — sprawdzamy produkcyjny kod `Runner.run_command`, nie jego kopię.
Asercje dotyczą rodzaju zdarzenia (`kind`), nie pola `status` ani `duration_ms`.
"""
from __future__ import annotations

import json

import httpx

from runner.client import Runner

RUN_ID = "run-1"
STEP_ID = "krok-1"


async def _uruchom(command: list[str]) -> list[httpx.Request]:
    """Uruchamia komendę przez Runnera i zwraca żądania wysłane do kokpitu."""
    wyslane: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        wyslane.append(request)
        return httpx.Response(200)

    runner = Runner("http://kokpit.test", "tajny-token")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await runner.run_command(client, RUN_ID, STEP_ID, command)
    return wyslane


def _zdarzenia(zadania: list[httpx.Request]) -> list[dict]:
    return [json.loads(z.content) for z in zadania]


async def test_komenda_z_niezerowym_kodem_wyjscia_konczy_krok_jako_nieudany():
    zadania = await _uruchom(["sh", "-c", "exit 3"])
    zdarzenia = _zdarzenia(zadania)

    koncowe = zdarzenia[-1]
    assert koncowe["kind"] == "step_failed"
    assert koncowe["text"] == "kod wyjscia 3"
    assert koncowe["run_id"] == RUN_ID
    assert koncowe["step_id"] == STEP_ID
    assert all(z["kind"] != "step_finished" for z in zdarzenia)


async def test_komenda_z_kodem_zero_konczy_krok_jako_zakonczony():
    zadania = await _uruchom(["sh", "-c", "exit 0"])
    zdarzenia = _zdarzenia(zadania)

    koncowe = zdarzenia[-1]
    assert koncowe["kind"] == "step_finished"
    assert koncowe["text"] == "kod wyjscia 0"
    assert koncowe["run_id"] == RUN_ID
    assert koncowe["step_id"] == STEP_ID
    assert all(z["kind"] != "step_failed" for z in zdarzenia)


async def test_nieudana_komenda_najpierw_zglasza_swoj_wynik_a_zdarzenie_koncowe_idzie_ostatnie():
    zadania = await _uruchom(["sh", "-c", "echo blad; exit 2"])
    zdarzenia = _zdarzenia(zadania)

    assert [z["kind"] for z in zdarzenia] == ["output", "step_failed"]
    assert zdarzenia[0]["text"] == "blad"
    assert zdarzenia[-1]["text"] == "kod wyjscia 2"


async def test_zdarzenia_runnera_trafiaja_do_kokpitu_z_tokenem_runnera():
    zadania = await _uruchom(["sh", "-c", "exit 3"])

    assert zadania, "runner nie wysłał żadnego zdarzenia"
    for zadanie in zadania:
        assert zadanie.method == "POST"
        assert zadanie.url.path == "/api/events"
        assert zadanie.headers["Authorization"] == "Bearer tajny-token"
