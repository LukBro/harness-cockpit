"""Runner: wynik komendy trafia do kokpitu jako zdarzenie końcowe kroku.

Transport HTTP jest przechwycony (`httpx.MockTransport`), a komendy to prawdziwe
procesy — sprawdzamy produkcyjny kod `Runner.run_command`, nie jego kopię.
Asercje dotyczą rodzaju zdarzenia (`kind`), nie pola `status` ani `duration_ms`.
"""
from __future__ import annotations

import json

import httpx
import pytest

from runner.client import Runner

RUN_ID = "run-1"
STEP_ID = "krok-1"


async def _uruchom(command: list[str], status: int = 200) -> list[httpx.Request]:
    """Uruchamia komendę przez Runnera i zwraca żądania wysłane do kokpitu.
    `status` to kod, którym kokpit odpowiada na każde zdarzenie."""
    wyslane: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        wyslane.append(request)
        return httpx.Response(status)

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


async def _wyslij_zdarzenie(status: int) -> bool:
    runner = Runner("http://kokpit.test", "tajny-token")
    transport = httpx.MockTransport(lambda request: httpx.Response(status))
    async with httpx.AsyncClient(transport=transport) as client:
        return await runner.send_event(client, run_id=RUN_ID, kind="output", text="x")


@pytest.mark.parametrize("status", [401, 500])
async def test_odpowiedz_odrzucajaca_zdarzenie_jest_widoczna_w_logu_runnera(status, capsys):
    await _wyslij_zdarzenie(status)

    err = capsys.readouterr().err
    assert "output" in err
    assert RUN_ID in err
    assert str(status) in err


@pytest.mark.parametrize("status", [401, 500])
async def test_log_runnera_o_odrzuconym_zdarzeniu_nie_zawiera_tokenu(status, capsys):
    await _uruchom(["sh", "-c", "echo tajne; exit 1"], status=status)

    wyjscie = capsys.readouterr()
    assert wyjscie.err  # log istnieje, wiec brak tokenu cos znaczy
    assert "tajny-token" not in wyjscie.err
    assert "tajny-token" not in wyjscie.out


async def test_przyjete_zdarzenie_nie_zostawia_sladu_w_logu_runnera(capsys):
    assert await _wyslij_zdarzenie(200) is True
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("status", [401, 500, 302])
async def test_wysylka_zdarzenia_odrzuconego_przez_kokpit_zwraca_porazke(status):
    assert await _wyslij_zdarzenie(status) is False


async def test_wysylka_zdarzenia_z_kodem_2xx_innym_niz_200_zwraca_sukces(capsys):
    assert await _wyslij_zdarzenie(204) is True
    assert capsys.readouterr().err == ""


async def test_log_odrzuconego_zdarzenia_koncowego_podaje_jego_rodzaj(capsys):
    await _uruchom(["sh", "-c", "exit 3"], status=500)

    err = capsys.readouterr().err
    assert "step_failed" in err
    assert "500" in err


async def test_zdarzenie_koncowe_niesie_liczbe_linii_output_ktore_runner_wyslal():
    zdarzenia = _zdarzenia(await _uruchom(["sh", "-c", "echo a; echo b"]))

    assert [z["kind"] for z in zdarzenia] == ["output", "output", "step_finished"]
    assert zdarzenia[-1]["wyslane_output"] == 2


async def test_zdarzenie_koncowe_liczy_tez_nieudane_proby_wyslania_output():
    zdarzenia = _zdarzenia(await _uruchom(["sh", "-c", "echo a; echo b; exit 1"], status=500))

    assert zdarzenia[-1]["kind"] == "step_failed"
    assert zdarzenia[-1]["wyslane_output"] == 2


async def test_komenda_bez_wyjscia_zglasza_zero_wyslanych_output():
    zdarzenia = _zdarzenia(await _uruchom(["sh", "-c", "exit 0"]))

    assert zdarzenia[-1]["wyslane_output"] == 0
