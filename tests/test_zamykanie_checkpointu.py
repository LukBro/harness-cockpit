"""HAR-126: anulowanie runu zamyka jego checkpoint, wiec spozniony `resume` niczego nie wznowi.

Run `waiting` nie ma zadania u runnera: o jego anulowaniu wie kokpit, a runner po `cancel_run`
zamyka checkpoint (zapis bez `waiting_on`) pod ta sama blokada runu co `wznow_run`. Bez tego
`resume` wyslany do nowego runnera (pusta pamiec anulowanych) wznowilby anulowany run.

Prawdziwy kod: `runner.silnik.zamknij_checkpoint` i `wznow_run` na prawdziwym repo git z
`tests/test_silnik_runu.py` (zaslepiony jest tylko agent) oraz prawdziwy `Runner.listen` na
atrapie kokpitu. Skutki czytamy z pliku checkpointu na dysku.
"""
from __future__ import annotations

import asyncio
import gc
import os
import threading
import time
from dataclasses import replace

import pytest

from procesy import czekaj, czekaj_na_warunek, zadanie_tla
from runner import client as runner_client
from runner import silnik
from runner.checkpoint import (
    BladCheckpointu,
    Checkpoint,
    Oczekiwanie,
    StanKrokuCheckpointu,
    wczytaj_checkpoint,
    zapisz_checkpoint,
)
from runner.client import Runner
from runner.worktree import BladWorktree
from test_runner_klient import TOKEN, StrumienPolecen, _krok, _runner, kokpit_ze_strumieniem
from test_silnik_runu import RUN_ID, K, Szpieg, rodzaje, swiat
from test_wznowienie_runu import BRAMKA, run_zatrzymany_na_bramce, wznow

LIMIT_S = 15
STARY_CZAS_NS = 1_600_000_000_000_000_000


def _checkpoint_z_bramka(run_id: str) -> Checkpoint:
    return Checkpoint(
        run_id=run_id, repozytorium="/repo", workflow="w", base_commit="abc", worktree="/wt",
        kroki={"b": StanKrokuCheckpointu("czeka", 0)},
        waiting_on=Oczekiwanie("b", "2026-01-01T00:00:00+00:00"))


def _katalog_z_checkpointem(tmp_path, run_id: str, monkeypatch):
    """Katalog runow runnera w katalogu testu, z checkpointem czekajacym na bramce."""
    root = tmp_path / "runy"
    katalog = root / run_id
    katalog.mkdir(parents=True)
    monkeypatch.setenv("COCKPIT_RUNS_DIR", str(root))
    zapisz_checkpoint(katalog, _checkpoint_z_bramka(run_id))
    return katalog


# --- silnik.zamknij_checkpoint na prawdziwym checkpoincie ---------------------------------------

async def test_zamkniecie_checkpointu_czekajacego_na_bramce_zdejmuje_oczekiwanie_i_zostawia_reszte(
    tmp_path,
):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    przed = wczytaj_checkpoint(s.katalog_runu)
    assert przed.waiting_on is not None and przed.waiting_on.step_id == "b"

    zamknieto = await czekaj(silnik.zamknij_checkpoint(RUN_ID, s.root), LIMIT_S, "zamkniecie")

    assert zamknieto is True
    assert wczytaj_checkpoint(s.katalog_runu) == replace(przed, waiting_on=None)


@pytest.mark.parametrize("katalog_runu_istnieje", [False, True],
                         ids=["brak_katalogu_runu", "katalog_runu_bez_checkpointu"])
async def test_zamkniecie_runu_bez_checkpointu_nie_jest_bledem_i_nic_nie_zapisuje(
    tmp_path, katalog_runu_istnieje,
):
    root = tmp_path / "runy"
    if katalog_runu_istnieje:
        (root / RUN_ID).mkdir(parents=True)

    zamknieto = await czekaj(silnik.zamknij_checkpoint(RUN_ID, root), LIMIT_S, "zamkniecie")

    assert zamknieto is False
    assert not (root / RUN_ID / "checkpoint.json").exists()


async def test_run_ktory_juz_nie_czeka_na_bramce_nie_ma_czego_zamykac_a_jego_checkpoint_zostaje_nietkniety(
    tmp_path,
):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    await wznow(s, Szpieg())                                # checkpoint bez `waiting_on`
    plik = s.katalog_runu / "checkpoint.json"
    assert wczytaj_checkpoint(s.katalog_runu).waiting_on is None
    os.utime(plik, ns=(STARY_CZAS_NS, STARY_CZAS_NS))
    zawartosc = plik.read_bytes()
    pliki = sorted(os.listdir(s.katalog_runu))

    zamknieto = await czekaj(silnik.zamknij_checkpoint(RUN_ID, s.root), LIMIT_S, "zamkniecie")

    assert zamknieto is False
    assert plik.read_bytes() == zawartosc
    assert plik.stat().st_mtime_ns == STARY_CZAS_NS         # plik nie zostal przepisany
    assert sorted(os.listdir(s.katalog_runu)) == pliki


@pytest.mark.parametrize("jak", ["uszkodzony_json", "katalog_zamiast_pliku"])
async def test_zamkniecie_uszkodzonego_albo_nieczytelnego_checkpointu_to_blad_checkpointu(
    tmp_path, jak,
):
    katalog = tmp_path / "runy" / RUN_ID
    katalog.mkdir(parents=True)
    plik = katalog / "checkpoint.json"
    if jak == "uszkodzony_json":
        plik.write_text("{nie json", encoding="utf-8")
    else:
        plik.mkdir()                    # IsADirectoryError: nie brak pliku, wiec nie `False`

    with pytest.raises(BladCheckpointu):
        await czekaj(silnik.zamknij_checkpoint(RUN_ID, tmp_path / "runy"), LIMIT_S, "zamkniecie")

    if jak == "uszkodzony_json":
        assert plik.read_text(encoding="utf-8") == "{nie json"


async def test_nieudany_zapis_zamkniecia_to_blad_checkpointu_a_checkpoint_dalej_czeka_na_bramce(
    tmp_path, monkeypatch,
):
    katalog = _katalog_z_checkpointem(tmp_path, RUN_ID, monkeypatch)
    prawdziwy_zapis = silnik.zapisz_checkpoint

    def zapis_do_nieistniejacego_katalogu(katalog_runu, cp):
        return prawdziwy_zapis(katalog_runu / "nie-ma-takiego-katalogu", cp)

    monkeypatch.setattr(silnik, "zapisz_checkpoint", zapis_do_nieistniejacego_katalogu)

    with pytest.raises(BladCheckpointu, match="^zapis checkpointu "):
        await czekaj(silnik.zamknij_checkpoint(RUN_ID, tmp_path / "runy"), LIMIT_S, "zamkniecie")

    assert wczytaj_checkpoint(katalog).waiting_on == Oczekiwanie("b", "2026-01-01T00:00:00+00:00")


async def test_zamkniecie_checkpointu_i_wznowienie_przeplecione_nie_zostawiaja_runu_wznowionego_i_zamknietego(
    tmp_path, monkeypatch,
):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    agent = Szpieg()
    prawdziwy_odczyt = silnik.wczytaj_checkpoint

    def powolny_odczyt(katalog):
        cp = prawdziwy_odczyt(katalog)
        time.sleep(0.2)                 # watek: bez blokady drugie wywolanie czyta ten sam stary stan
        return cp

    monkeypatch.setattr(silnik, "wczytaj_checkpoint", powolny_odczyt)
    zdarzenia: list[dict] = []

    async def emit(zdarzenie: dict) -> None:
        zdarzenia.append(zdarzenie)

    zamknieto, _ = await czekaj(asyncio.gather(
        silnik.zamknij_checkpoint(RUN_ID, s.root),
        silnik.wznow_run(RUN_ID, "b", "approve", emit, agent,
                         profil=s.profil, dom=s.dom, root=s.root),
    ), LIMIT_S, "zamkniecie i wznowienie")

    # niezmiennik: run zamkniety nie rusza krokiem, run wznowiony nie jest zamkniety
    assert zamknieto == (agent.wywolania == [])
    assert zamknieto is True                        # zamkniecie zaczelo pierwsze i trzyma blokade
    assert rodzaje(zdarzenia) == ["resume_ignored"]
    assert wczytaj_checkpoint(s.katalog_runu).waiting_on is None


# --- Runner: cancel_run zamyka checkpoint ----------------------------------------------------

async def test_cancel_run_runu_na_bramce_zamyka_checkpoint_a_resume_nowego_runnera_nie_startuje_kroku(
    tmp_path, monkeypatch,
):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    monkeypatch.setenv("COCKPIT_RUNS_DIR", str(s.root))
    agent = Szpieg()
    prawdziwe_wznowienie = silnik.wznow_run

    async def wznowienie_z_atrapa_agenta(run_id, step_id, decyzja, emit, powod=None):
        await prawdziwe_wznowienie(
            run_id, step_id, decyzja, emit, agent, powod=powod,
            profil=s.profil, dom=s.dom, root=s.root)

    monkeypatch.setattr(runner_client.silnik, "wznow_run", wznowienie_z_atrapa_agenta)
    assert wczytaj_checkpoint(s.katalog_runu).waiting_on.step_id == "b"

    strumien = StrumienPolecen()
    transport, _ = kokpit_ze_strumieniem(strumien)
    runner, _ = _runner(transport=transport)
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            strumien.wyslij({"type": "cancel_run", "run_id": RUN_ID})
            await czekaj_na_warunek(
                lambda: wczytaj_checkpoint(s.katalog_runu).waiting_on is None,
                LIMIT_S, "checkpoint zamkniety po cancel_run")
    finally:
        await czekaj(runner.stop(), LIMIT_S, "stop() runnera")

    # nowy runner nie pamieta anulowania; o wznowieniu decyduje tylko checkpoint na dysku
    strumien2 = StrumienPolecen()
    transport2, zdarzenia = kokpit_ze_strumieniem(strumien2)
    runner2, _ = _runner(transport=transport2)
    try:
        async with zadanie_tla(runner2.listen(), "nasluch nowego runnera"):
            strumien2.wyslij({"type": "resume", "run_id": RUN_ID, "step_id": "b",
                              "decyzja": "approve"})
            await czekaj_na_warunek(
                lambda: any(z["kind"] == "resume_ignored" for z in zdarzenia),
                LIMIT_S, "resume zignorowane")
    finally:
        await czekaj(runner2.stop(), LIMIT_S, "stop() nowego runnera")

    assert agent.wywolania == []
    assert [z["kind"] for z in zdarzenia] == ["resume_ignored"]
    assert wczytaj_checkpoint(s.katalog_runu).waiting_on is None


@pytest.mark.parametrize("uszkodzenie", ["uszkodzony_json", "blad_zapisu"])
async def test_gdy_nie_da_sie_zamknac_checkpointu_anulowanego_runu_log_mowi_ktorego_runu_i_dlaczego_a_anulowanie_nie_pada(
    tmp_path, monkeypatch, capsys, uszkodzenie,
):
    run_id = "c" * 40
    katalog = _katalog_z_checkpointem(tmp_path, run_id, monkeypatch)
    if uszkodzenie == "uszkodzony_json":
        (katalog / "checkpoint.json").write_text("{nie json", encoding="utf-8")
    else:
        prawdziwy_zapis = silnik.zapisz_checkpoint

        def zapis_do_nieistniejacego_katalogu(katalog_runu, cp):
            return prawdziwy_zapis(katalog_runu / "nie-ma-takiego-katalogu", cp)

        monkeypatch.setattr(silnik, "zapisz_checkpoint", zapis_do_nieistniejacego_katalogu)
    runner = Runner("http://kokpit.test", TOKEN)

    await czekaj(runner._zamknij_checkpoint(run_id, None), LIMIT_S, "zamkniecie w runnerze")

    assert (f"cancel_run: checkpoint runu {'c' * 36!r} niezamkniety: BladCheckpointu"
            in capsys.readouterr().err.splitlines())


async def test_nieudane_zamkniecie_checkpointu_po_cancel_run_nie_zatrzymuje_runnera(
    tmp_path, monkeypatch, capsys,
):
    katalog = _katalog_z_checkpointem(tmp_path, RUN_ID, monkeypatch)
    (katalog / "checkpoint.json").write_text("{nie json", encoding="utf-8")
    monkeypatch.setattr(runner_client, "OPERACJE_RUNU", {"hej": ("sh", "-c", "echo hej")})
    strumien = StrumienPolecen()
    transport, zdarzenia = kokpit_ze_strumieniem(strumien)
    runner, _ = _runner(transport=transport)
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            strumien.wyslij({"type": "cancel_run", "run_id": RUN_ID})
            strumien.wyslij(_krok("run-2", "hej"))
            await czekaj_na_warunek(
                lambda: any(z["kind"] == "run_finished" for z in zdarzenia),
                LIMIT_S, "krok po nieudanym zamknieciu checkpointu")
            assert not runner._zatrzymany.is_set()
    finally:
        await czekaj(runner.stop(), LIMIT_S, "stop() runnera")

    err = capsys.readouterr().err
    assert f"cancel_run: checkpoint runu {RUN_ID!r} niezamkniety: BladCheckpointu" in err.splitlines()
    assert "Traceback" not in err


async def test_zamkniecie_checkpointu_czeka_na_koniec_anulowanego_zadania_zeby_jego_zapis_go_nie_wyprzedzil(
    tmp_path, monkeypatch,
):
    katalog = tmp_path / "runy" / RUN_ID
    katalog.mkdir(parents=True)
    monkeypatch.setenv("COCKPIT_RUNS_DIR", str(tmp_path / "runy"))

    async def wykonaj(cmd, emit):
        try:
            await asyncio.sleep(300)
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)            # zadanie konczy sie wolno i dopiero wtedy zapisuje bramke
            zapisz_checkpoint(katalog, _checkpoint_z_bramka(RUN_ID))
            raise

    monkeypatch.setattr(runner_client.silnik, "wykonaj_run", wykonaj)
    strumien = StrumienPolecen()
    transport, _ = kokpit_ze_strumieniem(strumien)
    runner, _ = _runner(transport=transport)
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            strumien.wyslij({"type": "run_workflow", "run_id": RUN_ID, "workflow": "w",
                             "sciezka": "/repo"})
            await czekaj_na_warunek(lambda: RUN_ID in runner._runy, LIMIT_S, "start runu")
            strumien.wyslij({"type": "cancel_run", "run_id": RUN_ID})
            await czekaj_na_warunek(lambda: not runner._runy, LIMIT_S, "koniec zadania runu")
            await czekaj_na_warunek(lambda: not runner._zamykania, LIMIT_S, "koniec zamykania")
    finally:
        await czekaj(runner.stop(), LIMIT_S, "stop() runnera")

    assert wczytaj_checkpoint(katalog).waiting_on is None


async def test_stop_runnera_odczekuje_trwajace_zamykanie_checkpointu_anulowanego_runu(monkeypatch):
    zamkniete: list[str] = []

    async def wolne_zamykanie(run_id, root=None):
        await asyncio.sleep(0.3)
        zamkniete.append(run_id)
        return True

    monkeypatch.setattr(runner_client.silnik, "zamknij_checkpoint", wolne_zamykanie)
    runner = Runner("http://kokpit.test", TOKEN)

    runner._anuluj_run({"type": "cancel_run", "run_id": RUN_ID})
    assert len(runner._zamykania) == 1
    await czekaj(runner.stop(), LIMIT_S, "stop() runnera")

    assert zamkniete == [RUN_ID]
    assert runner._zamykania == set()


# --- anulowanie w trakcie zapisu checkpointu (`silnik._zapis_do_konca`, `_zapisz`) -------------------

def _powolny_zapis(
    monkeypatch, sekundy: float, *, blad: Exception | None = None, tylko_z_bramka: bool = False,
    tylko_zamykajacy: bool = False,
) -> threading.Event:
    """Zapis checkpointu trwa `sekundy` (prawdziwy `os.replace` dopiero po tym czasie); zwrocone
    zdarzenie wskazuje, ze watek zapisu ruszyl. `blad`: zapis konczy sie tym bledem.
    `tylko_z_bramka`: spowolniony tylko zapis z `waiting_on`; `tylko_zamykajacy`: tylko zapis
    zamykajacy (bez `waiting_on`)."""
    prawdziwy_zapis = silnik.zapisz_checkpoint
    ruszyl = threading.Event()

    def zapis(katalog, cp):
        if tylko_z_bramka and cp.waiting_on is None:
            return prawdziwy_zapis(katalog, cp)
        if tylko_zamykajacy and cp.waiting_on is not None:
            return prawdziwy_zapis(katalog, cp)
        ruszyl.set()
        time.sleep(sekundy)
        if blad is not None:
            raise blad
        return prawdziwy_zapis(katalog, cp)

    monkeypatch.setattr(silnik, "zapisz_checkpoint", zapis)
    return ruszyl


async def test_anulowanie_runu_w_trakcie_zapisu_bramki_nie_zostawia_checkpointu_czekajacego_na_decyzje(
    tmp_path, monkeypatch,
):
    # Sonda wyscigu (prawdziwe `wykonaj_run`, spowolniony tylko zapis checkpointu z bramka).
    # Weryfikacja mutantem: w `_zapis_do_konca` zamien cialo na `await asyncio.to_thread(
    # zapisz_checkpoint, katalog, cp)` albo usun `async with _BLOKADY...` w `_zapisz`: zamkniecie
    # czyta wtedy brak pliku (False), a spozniony zapis zostawia checkpoint z `waiting_on`.
    s = swiat(tmp_path, [K("a"), K("b", ("a",), extra=BRAMKA)])
    zapis_ruszyl = _powolny_zapis(monkeypatch, 0.3, tylko_z_bramka=True)
    zdarzenia: list[dict] = []

    async def emit(zdarzenie: dict) -> None:
        zdarzenia.append(zdarzenie)

    zadanie = asyncio.ensure_future(silnik.wykonaj_run(
        s.polecenie, emit, Szpieg(), profil=s.profil, dom=s.dom, root=s.root, zakres=s.zakres))
    try:
        await czekaj_na_warunek(zapis_ruszyl.is_set, LIMIT_S, "zapis checkpointu z bramka ruszyl")
        zadanie.cancel()                    # cancel_run przychodzi w trakcie zapisu
        zamknieto = await czekaj(
            silnik.zamknij_checkpoint(RUN_ID, s.root), LIMIT_S, "zamkniecie checkpointu")
        await czekaj(asyncio.wait([zadanie]), LIMIT_S, "koniec anulowanego zadania")
    finally:
        if not zadanie.done():
            zadanie.cancel()
            await asyncio.wait([zadanie], timeout=LIMIT_S)

    assert zamknieto is True                # zamkniecie zobaczylo zapis bramki i go zdjelo
    assert zadanie.cancelled()
    assert wczytaj_checkpoint(s.katalog_runu).waiting_on is None
    assert "step_waiting" not in rodzaje(zdarzenia)


def _katalog_runu_w_tmp(tmp_path):
    katalog = tmp_path / "runy" / RUN_ID
    katalog.mkdir(parents=True)
    return katalog


async def test_zapis_checkpointu_przerwany_anulowaniem_konczy_sie_przed_wyjsciem_z_anulowania(
    tmp_path, monkeypatch,
):
    katalog = _katalog_runu_w_tmp(tmp_path)
    cp = _checkpoint_z_bramka(RUN_ID)
    zapis_ruszyl = _powolny_zapis(monkeypatch, 0.3)
    zadanie = asyncio.ensure_future(silnik._zapis_do_konca(katalog, cp))
    await czekaj_na_warunek(zapis_ruszyl.is_set, LIMIT_S, "zapis ruszyl")

    zadanie.cancel()
    await czekaj(asyncio.wait([zadanie]), LIMIT_S, "koniec zadania")

    assert zadanie.cancelled()
    assert wczytaj_checkpoint(katalog) == cp         # plik jest na dysku, zanim anulowanie wyszlo


async def test_drugie_anulowanie_w_trakcie_czekania_na_zapis_tez_czeka_na_jego_koniec(
    tmp_path, monkeypatch,
):
    katalog = _katalog_runu_w_tmp(tmp_path)
    cp = _checkpoint_z_bramka(RUN_ID)
    zapis_ruszyl = _powolny_zapis(monkeypatch, 0.4)
    zadanie = asyncio.ensure_future(silnik._zapis_do_konca(katalog, cp))
    await czekaj_na_warunek(zapis_ruszyl.is_set, LIMIT_S, "zapis ruszyl")

    zadanie.cancel()
    await asyncio.sleep(0.05)                        # zadanie czeka juz na watek
    assert not zadanie.done()
    zadanie.cancel()
    await czekaj(asyncio.wait([zadanie]), LIMIT_S, "koniec zadania")

    assert zadanie.cancelled()
    assert wczytaj_checkpoint(katalog) == cp


async def test_awaria_dysku_podczas_anulowania_runu_nie_blokuje_anulowania_i_nie_zostawia_cichego_bledu(
    tmp_path, monkeypatch, caplog,
):
    katalog = _katalog_runu_w_tmp(tmp_path)
    zapis_ruszyl = _powolny_zapis(monkeypatch, 0.2, blad=BladCheckpointu("zapis-nieudany-har126"))
    zadanie = asyncio.ensure_future(silnik._zapis_do_konca(katalog, _checkpoint_z_bramka(RUN_ID)))
    await czekaj_na_warunek(zapis_ruszyl.is_set, LIMIT_S, "zapis ruszyl")

    zadanie.cancel()
    await czekaj(asyncio.wait([zadanie]), LIMIT_S, "koniec zadania")
    cancelled = zadanie.cancelled()
    del zadanie
    gc.collect()
    gc.collect()

    assert cancelled                                 # anulowanie wygrywa z bledem zapisu
    assert not [r for r in caplog.records if "zapis-nieudany-har126" in r.getMessage()]


async def test_przerwanie_zapisu_checkpointu_nie_zawiesza_anulowania_runu_a_plik_i_tak_powstaje(
    tmp_path, monkeypatch,
):
    # wewnetrzne zadanie zapisu (`to_thread`) anulowane z zewnatrz: nie ma na co czekac
    katalog = _katalog_runu_w_tmp(tmp_path)
    zapis_ruszyl = _powolny_zapis(monkeypatch, 0.2)
    zadanie = asyncio.ensure_future(silnik._zapis_do_konca(katalog, _checkpoint_z_bramka(RUN_ID)))
    await czekaj_na_warunek(zapis_ruszyl.is_set, LIMIT_S, "zapis ruszyl")
    (wewnetrzne,) = [t for t in asyncio.all_tasks()
                     if t is not zadanie and t.get_coro().__qualname__ == "to_thread"]

    wewnetrzne.cancel()
    await czekaj(asyncio.wait([zadanie]), LIMIT_S, "koniec zadania")

    assert zadanie.cancelled()
    # watek zapisu i tak dobiega konca: plik powstaje, a katalog zostaje spojny
    await czekaj_na_warunek(
        lambda: (katalog / "checkpoint.json").exists(), LIMIT_S, "plik checkpointu")
    assert wczytaj_checkpoint(katalog).waiting_on is not None


async def test_powod_anulowania_przez_czlowieka_dociera_do_wolajacego_gdy_zapis_checkpointu_zostal_przerwany(
    tmp_path, monkeypatch,
):
    # Zadanie zapisu anulowane z powodem, a potem przerwane jest tez jego wewnetrzne zadanie
    # watku: wolajacy dostaje to samo anulowanie z tym powodem, a plik i tak powstaje.
    # Weryfikacja mutantem: `if not zapis.cancelled():` -> `if True:` podmienia anulowanie na
    # nowe, bez powodu (`args == ()`).
    katalog = _katalog_runu_w_tmp(tmp_path)
    cp = _checkpoint_z_bramka(RUN_ID)
    zapis_ruszyl = _powolny_zapis(monkeypatch, 0.3)
    zadanie = asyncio.ensure_future(silnik._zapis_do_konca(katalog, cp))
    await czekaj_na_warunek(zapis_ruszyl.is_set, LIMIT_S, "zapis ruszyl")

    zadanie.cancel("anulowanie-czlowieka")
    await asyncio.sleep(0.05)                        # zadanie czeka juz na watek zapisu
    (wewnetrzne,) = [t for t in asyncio.all_tasks()
                     if t is not zadanie and t.get_coro().__qualname__ == "to_thread"]
    wewnetrzne.cancel()
    with pytest.raises(asyncio.CancelledError) as anulowanie:
        await czekaj(zadanie, LIMIT_S, "koniec anulowanego zapisu")

    assert anulowanie.value.args == ("anulowanie-czlowieka",)
    await czekaj_na_warunek(
        lambda: (katalog / "checkpoint.json").exists(), LIMIT_S, "plik checkpointu")
    assert wczytaj_checkpoint(katalog) == cp


async def test_anulowane_zamykanie_checkpointu_w_trakcie_zapisu_konczy_sie_dopiero_gdy_checkpoint_jest_zamkniety(
    tmp_path, monkeypatch,
):
    # Weryfikacja mutantem: w `zamknij_checkpoint` zamien `_zapis_do_konca(katalog, ...)` na
    # `asyncio.to_thread(zapisz_checkpoint, katalog, ...)`: anulowanie wraca przed `os.replace`,
    # wiec zaraz po nim checkpoint nadal czeka na decyzje.
    katalog = _katalog_z_checkpointem(tmp_path, RUN_ID, monkeypatch)
    zapis_ruszyl = _powolny_zapis(monkeypatch, 0.3, tylko_zamykajacy=True)
    zamykanie = asyncio.ensure_future(silnik.zamknij_checkpoint(RUN_ID, tmp_path / "runy"))
    await czekaj_na_warunek(zapis_ruszyl.is_set, LIMIT_S, "zapis zamykajacy ruszyl")

    zamykanie.cancel()
    await czekaj(asyncio.wait([zamykanie]), LIMIT_S, "koniec anulowanego zamykania")

    assert zamykanie.cancelled()
    assert wczytaj_checkpoint(katalog).waiting_on is None       # zaraz po anulowaniu, bez czekania


async def test_wznowienie_czekajace_na_zamykanie_anulowanego_runu_jest_ignorowane_i_nie_uruchamia_agenta(
    tmp_path, monkeypatch,
):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    agent = Szpieg()
    zapis_ruszyl = _powolny_zapis(monkeypatch, 0.3, tylko_zamykajacy=True)
    zdarzenia: list[dict] = []

    async def emit(zdarzenie: dict) -> None:
        zdarzenia.append(zdarzenie)

    zamykanie = asyncio.ensure_future(silnik.zamknij_checkpoint(RUN_ID, s.root))
    await czekaj_na_warunek(zapis_ruszyl.is_set, LIMIT_S, "zapis zamykajacy ruszyl")
    wznowienie = asyncio.ensure_future(silnik.wznow_run(
        RUN_ID, "b", "approve", emit, agent, profil=s.profil, dom=s.dom, root=s.root))
    await asyncio.sleep(0.05)               # wznowienie stoi na blokadzie runu, trzymanej przez zamykanie

    zamykanie.cancel()
    await czekaj(asyncio.wait([zamykanie, wznowienie]), LIMIT_S, "koniec zamykania i wznowienia")

    assert zamykanie.cancelled()
    assert rodzaje(zdarzenia) == ["resume_ignored"]
    assert agent.wywolania == []
    assert wczytaj_checkpoint(s.katalog_runu).waiting_on is None


# --- zamkniecie checkpointu: zle identyfikatory runu ------------------------------------------------

async def test_run_z_bajtem_nul_w_id_nie_da_sie_zamknac_a_przyczyna_zostaje_w_bledzie_checkpointu(
    tmp_path,
):
    with pytest.raises(BladCheckpointu) as wyjatek:
        await czekaj(silnik.zamknij_checkpoint("a\u0000b", tmp_path / "runy"), LIMIT_S, "zamkniecie")

    assert isinstance(wyjatek.value.__cause__, ValueError)
    assert str(wyjatek.value) == "odczyt checkpointu: ValueError"


@pytest.mark.parametrize("run_id", ["a/b", ".."])
async def test_zamkniecie_checkpointu_runu_z_niedozwolonym_id_to_blad_worktree(tmp_path, run_id):
    with pytest.raises(BladWorktree):
        await czekaj(silnik.zamknij_checkpoint(run_id, tmp_path / "runy"), LIMIT_S, "zamkniecie")


@pytest.mark.parametrize("run_id, typ_bledu", [
    ("a\u0000b", "BladCheckpointu"), ("a/b", "BladWorktree"), ("..", "BladWorktree"),
], ids=["bajt_nul", "ukosnik", "dwie_kropki"])
async def test_anulowanie_runu_ze_zlym_id_zostawia_w_logu_przyczyne_a_runner_dziala_dalej_bez_awarii(
    tmp_path, monkeypatch, capsys, caplog, run_id, typ_bledu,
):
    monkeypatch.setenv("COCKPIT_RUNS_DIR", str(tmp_path / "runy"))
    monkeypatch.setattr(runner_client, "OPERACJE_RUNU", {"hej": ("sh", "-c", "echo hej")})
    strumien = StrumienPolecen()
    transport, zdarzenia = kokpit_ze_strumieniem(strumien)
    runner, _ = _runner(transport=transport)
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            strumien.wyslij({"type": "cancel_run", "run_id": run_id})
            strumien.wyslij(_krok("run-2", "hej"))
            await czekaj_na_warunek(
                lambda: any(z["kind"] == "run_finished" for z in zdarzenia),
                LIMIT_S, "krok po cancel_run ze zlym id")
            assert not runner._zatrzymany.is_set()
    finally:
        await czekaj(runner.stop(), LIMIT_S, "stop() runnera")
    gc.collect()

    err = capsys.readouterr().err
    assert f"cancel_run: checkpoint runu {run_id!r} niezamkniety: {typ_bledu}" in err.splitlines()
    assert "Traceback" not in err
    assert "never retrieved" not in caplog.text
