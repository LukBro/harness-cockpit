"""HAR-204: wyjscie komendy kroku plynie do kokpitu na biezaco, nie po koncu procesu.

Komendy to prawdziwe procesy, transport HTTP jest przechwycony (`httpx.MockTransport`);
sprawdzamy produkcyjny `Runner.run_command`. Kazde czekanie ma limit czasu.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

import runner.client as runner_client
from procesy import czekaj, czekaj_na_warunek, czy_zyje, zapamietaj_proces
from runner.bufor import BladBufora, BrakMiejsca
from runner.client import NiewaznePoleZdarzenia, Runner

RUN_ID = "run-204"
STEP_ID = "krok-204"
LIMIT_S = 20


def _runner() -> Runner:
    async def bez_czekania(_: float) -> None:
        return None

    return Runner("http://kokpit.test", "tajne", odczekaj=bez_czekania)


def _zdarzenia(wyslane: list[httpx.Request]) -> list[dict]:
    return [json.loads(z.content) for z in wyslane]


async def _uruchom(
    command: list[str], po_zdarzeniu=None, runner: Runner | None = None
) -> list[dict]:
    wyslane: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        wyslane.append(request)
        if po_zdarzeniu is not None:
            po_zdarzeniu(json.loads(request.content))
        return httpx.Response(200)

    runner = runner or _runner()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await czekaj(runner.run_command(client, RUN_ID, STEP_ID, command), LIMIT_S, "run_command")
    return _zdarzenia(wyslane)


async def test_linia_komendy_trafia_do_kokpitu_zanim_komenda_sie_skonczy(tmp_path: Path):
    zgoda = tmp_path / "zgoda"
    # komenda konczy sie dopiero po tym, jak kokpit dostal jej linie: odczyt po koncu procesu
    # nigdy nie wyslalby tej linii, wiec krok wisialby do limitu czasu
    skrypt = f'echo pierwsza; while [ ! -e "{zgoda}" ]; do sleep 0.05; done; echo druga'

    def po_zdarzeniu(zdarzenie: dict) -> None:
        if zdarzenie["kind"] == "output" and zdarzenie["text"] == "pierwsza":
            zgoda.touch()

    zdarzenia = await _uruchom(["sh", "-c", skrypt], po_zdarzeniu)

    assert [z["text"] for z in zdarzenia if z["kind"] == "output"] == ["pierwsza", "druga"]
    assert [z["kind"] for z in zdarzenia][-2:] == ["step_finished", "run_finished"]


async def test_ostatnia_linia_komendy_bez_znaku_nowej_linii_trafia_do_kokpitu():
    zdarzenia = await _uruchom(["printf", "a\\nb"])

    assert [z["text"] for z in zdarzenia if z["kind"] == "output"] == ["a", "b"]


async def test_nieoczekiwany_blad_w_trakcie_wyjscia_konczy_komende_kroku(tmp_path: Path):
    pid_plik = tmp_path / "pid"
    runner = _runner()
    za_dlugi_run_id = "r" * 37          # pierwsze zdarzenie output nie przejdzie walidacji
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as c:
        with pytest.raises(NiewaznePoleZdarzenia):
            await czekaj(
                runner.run_command(
                    c, za_dlugi_run_id, STEP_ID,
                    ["sh", "-c", f"echo $$ > {pid_plik}; echo a; sleep 60"]),
                LIMIT_S, "run_command z niewaznym zdarzeniem")

    pid = int(pid_plik.read_text())
    zyje = czy_zyje(pid)
    if zyje:
        zapamietaj_proces(pid)          # sprzata conftest, gdy test oblewa
    assert not zyje
    assert runner._procesy == set()


async def test_koniec_kroku_niesie_liczbe_prob_wyslania_wyjscia_i_kod_wyjscia():
    zdarzenia = await _uruchom(["sh", "-c", "echo a; echo; echo b; exit 2"])

    koncowe = next(z for z in zdarzenia if z["kind"] == "step_failed")
    assert koncowe["wyslane_output"] == 3
    assert koncowe["text"] == "kod wyjscia 2"
    assert [z["text"] for z in zdarzenia if z["kind"] == "output"] == ["a", "", "b"]


async def test_linia_dluzsza_niz_limit_zdarzenia_idzie_obcieta_z_pelna_dlugoscia(monkeypatch):
    monkeypatch.setattr(runner_client, "LIMIT_TEKSTU_ZDARZENIA", 100)
    zdarzenia = await _uruchom(["sh", "-c", "head -c 5000 /dev/zero | tr '\\0' a; echo; echo ok"])

    wyjscie = [z for z in zdarzenia if z["kind"] == "output"]
    assert wyjscie[0]["text"] == "a" * 100
    assert wyjscie[0]["text_full_len"] == 5000
    assert wyjscie[1]["text"] == "ok"
    assert "text_full_len" not in wyjscie[1]


async def test_zabity_krok_w_trakcie_wyjscia_konczy_proces_komendy(tmp_path: Path):
    pid_plik = tmp_path / "pid"
    pierwszy = asyncio.Event()

    wyslane: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        wyslane.append(request)
        if json.loads(request.content)["kind"] == "output":
            pierwszy.set()
        return httpx.Response(200)

    runner = _runner()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        zadanie = asyncio.ensure_future(runner.run_command(
            client, RUN_ID, STEP_ID, ["sh", "-c", f"echo $$ > {pid_plik}; echo start; sleep 60"]))
        try:
            await czekaj(pierwszy.wait(), LIMIT_S, "pierwsza linia wyjscia")
            pid = int(pid_plik.read_text())
            zadanie.cancel()
            with pytest.raises(asyncio.CancelledError):
                await czekaj(zadanie, LIMIT_S, "anulowanie run_command")
            await czekaj_na_warunek(lambda: not czy_zyje(pid), LIMIT_S, "koniec procesu komendy")
        finally:
            zadanie.cancel()

    assert runner._procesy == set()
    assert all(z["kind"] != "step_finished" for z in _zdarzenia(wyslane))


@pytest.mark.parametrize("blad", [BrakMiejsca, BladBufora])
async def test_zepsuty_bufor_w_trakcie_wyjscia_przerywa_krok_i_konczy_komende(
    tmp_path: Path, blad
):
    pid_plik = tmp_path / "pid"
    runner = _runner()
    bezposrednie: list[dict] = []

    async def zepsute_wysylanie(client, **kw):
        raise blad()

    async def bezposrednio(client, **kw):
        bezposrednie.append(kw)
        return True

    runner.send_event = zepsute_wysylanie
    runner._wyslij_bezposrednio = bezposrednio
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as c:
        await czekaj(
            runner.run_command(
                c, RUN_ID, STEP_ID, ["sh", "-c", f"echo $$ > {pid_plik}; echo start; sleep 60"]),
            LIMIT_S, "run_command z zepsutym buforem")

    assert [z["kind"] for z in bezposrednie] == ["step_aborted"]
    assert not czy_zyje(int(pid_plik.read_text()))
    assert runner._procesy == set()


async def test_zatrzymanie_runnera_z_wnetrza_wysylki_wyjscia_kroku_nie_zawiesza_sie():
    runner = _runner()
    wyslij = runner.send_event
    zatrzymano = False

    async def wysylka_z_zatrzymaniem(client, **kw):
        nonlocal zatrzymano
        if kw["kind"] == "output" and not zatrzymano:
            zatrzymano = True
            await czekaj(runner.stop(), LIMIT_S, "stop() z wnetrza wysylki")
        return await wyslij(client, **kw)

    runner.send_event = wysylka_z_zatrzymaniem
    await _uruchom(["sh", "-c", "echo a"], runner=runner)

    assert zatrzymano
    assert runner._procesy == set()


async def test_zatrzymanie_runnera_ubija_dzialajaca_komende_kroku(tmp_path: Path):
    pid_plik = tmp_path / "pid"
    runner = _runner()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as c:
        zadanie = asyncio.ensure_future(runner.run_command(
            c, RUN_ID, STEP_ID, ["sh", "-c", f"echo $$ > {pid_plik}; sleep 60"]))
        try:
            await czekaj_na_warunek(
                lambda: pid_plik.exists() and pid_plik.read_text().strip(), LIMIT_S, "pid komendy")
            pid = int(pid_plik.read_text())

            await czekaj(runner.stop(), LIMIT_S, "stop() przy dzialajacej komendzie")
            await czekaj(zadanie, LIMIT_S, "koniec run_command po stop()")

            assert not czy_zyje(pid)
        finally:
            zadanie.cancel()
