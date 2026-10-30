"""HAR-62: hooki kroku zapisywane do pliku przypisanego do kroku, bez portu.

Hook Claude Code dopisuje surowe zdarzenie do pliku kroku (`hooks/<run_id>/<step_id>.ndjson`
w katalogu stanu runnera); runner czyta plik przyrostowo, normalizuje kernelem i dokłada
`run_id` oraz `step_id` ze ścieżki pliku, nigdy z treści zdarzenia i nigdy po czasie.
Polecenie hooka jest uruchamiane naprawdę (`sh -c`), a czytnik czyta prawdziwe pliki.
Wpisy w logu runnera czytamy ze stderr, a każde oczekiwanie ma twardy limit czasu.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import sys
from asyncio.subprocess import PIPE
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from procesy import czekaj, czekaj_na_warunek, uruchom_proces

from cockpit.kernel.events import normalize
from cockpit.services.errors import BladDziedziny
from cockpit.services.walidacja import sprawdz_katalog_runu
from runner import bufor, hooks
from runner.bufor import katalog_stanu
from runner.hooks import BladPlikuHookow, CzytnikHookow, plik_hookow, ustawienia_hookow

KORZEN = Path(__file__).resolve().parents[1]
FIXTURY = Path(__file__).resolve().parent / "fixtures" / "hooks"
PRZYKLAD = KORZEN / "deploy" / "hooks.example.json"
RUN = "run-1"
KROK_A = "krok-a"
KROK_B = "krok-b"
TAJNE = "SEKRET-ZXQ-91"
LIMIT = 5.0
LIMIT_HOOKA = 30.0


@contextmanager
def _umask(wartosc: int):
    stara = os.umask(wartosc)
    try:
        yield
    finally:
        os.umask(stara)


def _prawa(sciezka: Path) -> int:
    return stat.S_IMODE(sciezka.stat().st_mode)


def _payload(zdarzenie: str = "SessionStart", sesja: str = "sesja-1", **pola) -> dict:
    return {"session_id": sesja, "hook_event_name": zdarzenie, **pola}


def _linia(payload: dict) -> bytes:
    return json.dumps(payload).encode() + b"\n"


def _dopisz(plik: Path, dane: bytes) -> None:
    with plik.open("ab") as f:
        f.write(dane)


def _krok(tmp_path: Path, krok: str = KROK_A, run: str = RUN) -> tuple[Path, CzytnikHookow]:
    plik = plik_hookow(tmp_path / "stan", run, krok)
    return plik, CzytnikHookow(plik, run, krok, interwal=0.01)


def _surowe(nazwa: str) -> list[dict]:
    return [json.loads(w) for w in (FIXTURY / nazwa).read_text().splitlines() if w.strip()]


def _polecenie(plik: Path, zdarzenie: str = "SessionStart") -> str:
    ustawienia = ustawienia_hookow(plik)
    return ustawienia["hooks"][zdarzenie][0]["hooks"][0]["command"]


@dataclass
class _Wynik:
    returncode: int
    stdout: str
    stderr: str


async def _uruchom_hook_async(polecenie: str, stdin: str, env: dict[str, str]) -> _Wynik:
    proc = await uruchom_proces(
        "sh",
        "-c",
        polecenie,
        stdin=PIPE,
        stdout=PIPE,
        stderr=PIPE,
        env=env,
        cwd=str(KORZEN),
    )
    wyjscie, bledy = await czekaj(
        proc.communicate(stdin.encode()), LIMIT_HOOKA, "polecenie hooka (sh -c)"
    )
    return _Wynik(proc.returncode, wyjscie.decode(), bledy.decode())


def _uruchom_hook(
    polecenie: str,
    stdin: str,
    plik: Path | None,
    *,
    bez_zmiennej: bool = False,
    wartosc: str | None = None,
) -> _Wynik:
    """Prawdziwe `sh -c` przez helper procesow (nowa sesja, twardy limit), cwd = korzen repo."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if wartosc is not None:
        env[hooks.ZMIENNA_PLIKU] = wartosc
    elif plik is not None and not bez_zmiennej:
        env[hooks.ZMIENNA_PLIKU] = str(plik)
    return asyncio.run(_uruchom_hook_async(polecenie, stdin, env))


def _wszystkie_napisy(wartosc) -> list[str]:
    if isinstance(wartosc, dict):
        wyniki: list[str] = []
        for klucz, v in wartosc.items():
            wyniki.append(str(klucz))
            wyniki.extend(_wszystkie_napisy(v))
        return wyniki
    if isinstance(wartosc, list):
        return [n for v in wartosc for n in _wszystkie_napisy(v)]
    return [str(wartosc)]


def _wszystkie_klucze(wartosc) -> set[str]:
    if isinstance(wartosc, dict):
        klucze = {str(k) for k in wartosc}
        for v in wartosc.values():
            klucze |= _wszystkie_klucze(v)
        return klucze
    if isinstance(wartosc, list):
        return {k for v in wartosc for k in _wszystkie_klucze(v)}
    return set()


def _haki(ustawienia: dict) -> list[dict]:
    return [
        h
        for grupy in ustawienia["hooks"].values()
        for grupa in grupy
        for h in grupa["hooks"]
    ]


# --- ścieżka pliku kroku, prawa i walidacja identyfikatorów ----------------------


def test_plik_hookow_lezy_w_katalogu_runu_pod_nazwa_kroku(tmp_path):
    plik = plik_hookow(tmp_path / "stan", "run-7", "krok-3")

    assert plik == tmp_path / "stan" / "hooks" / "run-7" / "krok-3.ndjson"
    assert plik.is_file()


def test_plik_hookow_ma_prawa_tylko_dla_wlasciciela_nawet_przy_umask_zero(tmp_path):
    with _umask(0):
        plik = plik_hookow(tmp_path / "stan", RUN, KROK_A)

    assert _prawa(plik) == 0o600


def test_oba_katalogi_hookow_maja_prawa_tylko_dla_wlasciciela_nawet_przy_umask_zero(tmp_path):
    with _umask(0):
        plik = plik_hookow(tmp_path / "stan", RUN, KROK_A)

    assert _prawa(plik.parent) == 0o700
    assert _prawa(plik.parent.parent) == 0o700
    assert plik.parent.parent.name == "hooks"


def test_istniejace_katalogi_hookow_dostaja_prawa_tylko_dla_wlasciciela(tmp_path):
    hooks_dir = tmp_path / "stan" / "hooks"
    run_dir = hooks_dir / RUN
    run_dir.mkdir(parents=True)
    hooks_dir.chmod(0o755)
    run_dir.chmod(0o755)

    with _umask(0):
        plik_hookow(tmp_path / "stan", RUN, KROK_A)

    assert _prawa(hooks_dir) == 0o700
    assert _prawa(run_dir) == 0o700


def test_istniejacy_plik_hookow_dostaje_prawa_tylko_dla_wlasciciela(tmp_path):
    run_dir = tmp_path / "stan" / "hooks" / RUN
    run_dir.mkdir(parents=True)
    plik = run_dir / f"{KROK_A}.ndjson"
    plik.write_bytes(b"")
    plik.chmod(0o644)

    with _umask(0):
        plik_hookow(tmp_path / "stan", RUN, KROK_A)

    assert _prawa(plik) == 0o600


def test_ponowne_przygotowanie_pliku_hookow_nie_kasuje_zapisanych_zdarzen(tmp_path):
    plik = plik_hookow(tmp_path / "stan", RUN, KROK_A)
    _dopisz(plik, b'{"hook_event_name":"Stop"}\n')

    ponownie = plik_hookow(tmp_path / "stan", RUN, KROK_A)

    assert ponownie == plik
    assert plik.read_bytes() == b'{"hook_event_name":"Stop"}\n'


def test_plik_hookow_nie_laduje_w_repozytorium_uzytkownika(tmp_path):
    plik = plik_hookow(katalog_stanu(), RUN, KROK_A)

    assert plik.resolve().is_relative_to(tmp_path.resolve())
    assert sprawdz_katalog_runu(plik.parent, KORZEN) is None
    assert KORZEN.resolve() not in plik.resolve().parents


@pytest.mark.parametrize(
    "zly",
    [
        "",
        ".",
        "..",
        "a/b",
        "a\\b",
        "a b",
        "run\n",
        "a" * 65,
        "ą",
        "@",
        "[",
        "`",
        "{",
        "/",
        ":",
        "a.b",
        5,
        None,
        b"abc",
    ],
)
@pytest.mark.parametrize("pole", ["run_id", "step_id"])
def test_identyfikator_poza_dozwolonym_zestawem_jest_odrzucany_bez_tworzenia_plikow(
    tmp_path, pole, zly
):
    argumenty = {"run_id": RUN, "step_id": KROK_A}
    argumenty[pole] = zly

    with pytest.raises(BladPlikuHookow) as wyjatek:
        plik_hookow(tmp_path / "stan", argumenty["run_id"], argumenty["step_id"])

    assert type(wyjatek.value) is BladPlikuHookow
    assert not (tmp_path / "stan" / "hooks").exists()


def test_blad_pliku_hookow_jest_bledem_dziedziny():
    assert issubclass(BladPlikuHookow, BladDziedziny)


def test_plik_hookow_bez_katalogu_stanu_uzywa_katalogu_stanu_runnera(tmp_path, monkeypatch):
    plik = plik_hookow(None, RUN, KROK_A)

    assert plik == tmp_path / "hooks" / RUN / f"{KROK_A}.ndjson"
    assert plik.is_file()

    inny = tmp_path / "inny-stan"
    monkeypatch.setenv("COCKPIT_STATE_DIR", str(inny))
    plik2 = plik_hookow(None, RUN, KROK_B)

    assert plik2 == inny / "hooks" / RUN / f"{KROK_B}.ndjson"
    assert plik2.is_file()


def test_identyfikatory_hookow_sa_sprawdzane_tym_samym_wzorcem_co_bufor_runnera():
    assert hooks._RUN_ID is bufor._RUN_ID


@pytest.mark.parametrize(
    "dobry",
    ["A", "Z", "a", "z", "0", "9", "_", "-", "AZaz09_-", "a" * 64],
)
@pytest.mark.parametrize("pole", ["run_id", "step_id"])
def test_identyfikator_z_dozwolonego_zestawu_i_do_64_znakow_jest_przyjmowany(
    tmp_path, pole, dobry
):
    argumenty = {"run_id": RUN, "step_id": KROK_A}
    argumenty[pole] = dobry

    plik = plik_hookow(tmp_path / "stan", argumenty["run_id"], argumenty["step_id"])

    assert plik.is_file()
    assert plik.parent.name == argumenty["run_id"]
    assert plik.name == f"{argumenty['step_id']}.ndjson"


def test_katalog_stanu_bedacy_plikiem_daje_blad_pliku_hookow_z_przyczyna_systemowa(tmp_path):
    stan = tmp_path / "stan"
    stan.write_text("to jest plik")

    with pytest.raises(BladPlikuHookow) as wyjatek:
        plik_hookow(stan, RUN, KROK_A)

    assert isinstance(wyjatek.value.__cause__, OSError)


def test_katalog_runu_zajety_przez_plik_daje_blad_pliku_hookow_z_przyczyna_systemowa(tmp_path):
    hooks_dir = tmp_path / "stan" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / RUN).write_text("to jest plik")

    with pytest.raises(BladPlikuHookow) as wyjatek:
        plik_hookow(tmp_path / "stan", RUN, KROK_A)

    assert isinstance(wyjatek.value.__cause__, OSError)


def test_plik_kroku_zajety_przez_katalog_daje_blad_pliku_hookow_z_przyczyna_systemowa(tmp_path):
    (tmp_path / "stan" / "hooks" / RUN / f"{KROK_A}.ndjson").mkdir(parents=True)

    with pytest.raises(BladPlikuHookow) as wyjatek:
        plik_hookow(tmp_path / "stan", RUN, KROK_A)

    assert isinstance(wyjatek.value.__cause__, OSError)


# --- ustawienia hooków: polecenie, a nie adres sieciowy -------------------------------


def test_kazdy_hook_w_ustawieniach_jest_poleceniem_a_nie_wywolaniem_sieciowym(tmp_path):
    ustawienia = ustawienia_hookow(tmp_path / "x.ndjson")

    haki = _haki(ustawienia)
    assert len(haki) == 8
    assert [h["type"] for h in haki] == ["command"] * 8
    assert all(isinstance(h["command"], str) and h["command"] for h in haki)


def test_ustawienia_hookow_nie_zawieraja_adresu_sieciowego_ani_wywolania_asynchronicznego(
    tmp_path,
):
    ustawienia = ustawienia_hookow(tmp_path / "x.ndjson")

    napisy = _wszystkie_napisy(ustawienia)
    for zakazane in ("http", "https://", "://", "127.0.0.1", "localhost", "8765"):
        assert [n for n in napisy if zakazane in n] == [], zakazane
    klucze = _wszystkie_klucze(ustawienia)
    assert "url" not in klucze
    assert "async" not in klucze


def test_polecenie_hooka_bierze_sciezke_pliku_ze_zmiennej_a_nie_z_tekstu_polecenia(tmp_path):
    plik = tmp_path / "unikalny-katalog-9137" / "krok-zz.ndjson"

    ustawienia = ustawienia_hookow(plik)

    for h in _haki(ustawienia):
        assert hooks.ZMIENNA_PLIKU == "COCKPIT_HOOK_FILE"
        assert "COCKPIT_HOOK_FILE" in h["command"]
        assert str(plik) not in h["command"]
        assert "unikalny-katalog-9137" not in h["command"]
        assert "krok-zz" not in h["command"]


def test_ustawienia_hookow_maja_te_same_zdarzenia_matchery_i_warunki_co_przyklad_wdrozeniowy(
    tmp_path,
):
    przyklad = json.loads(PRZYKLAD.read_text())["hooks"]
    ustawienia = ustawienia_hookow(tmp_path / "x.ndjson")["hooks"]

    assert set(ustawienia) == set(przyklad)
    assert len(ustawienia) == 8
    for zdarzenie, grupy_wzorca in przyklad.items():
        grupy = ustawienia[zdarzenie]
        assert len(grupy) == len(grupy_wzorca), zdarzenie
        for grupa, wzorzec in zip(grupy, grupy_wzorca, strict=True):
            assert grupa.get("matcher") == wzorzec.get("matcher"), zdarzenie
            assert len(grupa["hooks"]) == len(wzorzec["hooks"]), zdarzenie
            for h, hw in zip(grupa["hooks"], wzorzec["hooks"], strict=True):
                assert h.get("if") == hw.get("if"), zdarzenie


def test_zdarzenia_narzedzi_maja_matcher_bash_i_warunek_phase_timer(tmp_path):
    ustawienia = ustawienia_hookow(tmp_path / "x.ndjson")["hooks"]

    for zdarzenie in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        grupa = ustawienia[zdarzenie][0]
        assert grupa["matcher"] == "Bash"
        assert grupa["hooks"][0]["if"] == "Bash(*phase_timer*)"
    for zdarzenie in ("SessionStart", "SubagentStart", "SubagentStop", "Stop", "SessionEnd"):
        assert "matcher" not in ustawienia[zdarzenie][0]
        assert "if" not in ustawienia[zdarzenie][0]["hooks"][0]


def test_sciezka_wzgledna_pliku_hookow_jest_odrzucona_bo_rozwinelaby_sie_w_repozytorium():
    with pytest.raises(BladPlikuHookow) as wyjatek:
        ustawienia_hookow(Path("hooks/run-1/krok-a.ndjson"))

    assert type(wyjatek.value) is BladPlikuHookow


# --- polecenie hooka uruchomione naprawdę ----------------------------------------


def test_polecenie_hooka_dopisuje_zdarzenie_jako_jedna_linie_konczaca_sie_nowa_linia(tmp_path):
    plik = plik_hookow(tmp_path / "stan", RUN, KROK_A)
    payload = _payload("SubagentStart", agent_id="agent-1")

    wynik = _uruchom_hook(_polecenie(plik), json.dumps(payload), plik)

    assert wynik.returncode == 0
    assert wynik.stdout == ""
    assert plik.read_bytes() == json.dumps(payload).encode() + b"\n"


def test_wieloliniowy_json_z_hooka_trafia_do_pliku_jako_jedna_poprawna_linia(tmp_path):
    plik = plik_hookow(tmp_path / "stan", RUN, KROK_A)
    payload = _payload("PreToolUse", tool_name="Bash", tool_input={"command": "ls -la"})

    wynik = _uruchom_hook(_polecenie(plik), json.dumps(payload, indent=2) + "\n", plik)

    zapisane = plik.read_bytes()
    assert wynik.returncode == 0
    assert zapisane.endswith(b"\n")
    assert zapisane.count(b"\n") == 1
    assert json.loads(zapisane) == payload


def test_zakonczenia_linii_crlf_w_payloadzie_nie_lamia_linii_pliku(tmp_path):
    plik = plik_hookow(tmp_path / "stan", RUN, KROK_A)
    payload = _payload("Stop", agent_id="a1")
    tekst = json.dumps(payload, indent=2).replace("\n", "\r\n")

    wynik = _uruchom_hook(_polecenie(plik), tekst, plik)

    zapisane = plik.read_bytes()
    assert wynik.returncode == 0
    assert zapisane.count(b"\n") == 1
    assert b"\r" not in zapisane
    assert json.loads(zapisane) == payload


def test_sam_znak_powrotu_karetki_w_payloadzie_nie_zostaje_w_pliku(tmp_path):
    plik = plik_hookow(tmp_path / "stan", RUN, KROK_A)
    payload = _payload("Stop", agent_id="a1")
    tekst = json.dumps(payload, indent=2).replace("\n", "\r")

    wynik = _uruchom_hook(_polecenie(plik), tekst, plik)

    zapisane = plik.read_bytes()
    assert wynik.returncode == 0
    assert b"\r" not in zapisane
    assert zapisane.count(b"\n") == 1
    assert json.loads(zapisane) == payload


def test_hook_z_pustym_wejsciem_niczego_nie_dopisuje_i_konczy_sie_kodem_zero(tmp_path):
    plik = plik_hookow(tmp_path / "stan", RUN, KROK_A)

    wynik = _uruchom_hook(_polecenie(plik), "", plik)

    assert wynik.returncode == 0
    assert wynik.stdout == ""
    assert plik.read_bytes() == b""


def test_kolejne_hooki_dopisuja_linie_jedna_pod_druga_bez_nadpisywania(tmp_path):
    plik = plik_hookow(tmp_path / "stan", RUN, KROK_A)
    polecenie = _polecenie(plik)

    _uruchom_hook(polecenie, json.dumps(_payload("SessionStart")), plik)
    _uruchom_hook(polecenie, json.dumps(_payload("Stop")), plik)

    linie = plik.read_bytes().splitlines()
    assert [json.loads(w)["hook_event_name"] for w in linie] == ["SessionStart", "Stop"]


def test_hook_z_plikiem_w_nieistniejacym_katalogu_konczy_sie_kodem_zero_bez_wyjscia(tmp_path):
    plik = tmp_path / "nie-ma" / "takiego" / "krok.ndjson"

    wynik = _uruchom_hook(_polecenie(plik), json.dumps(_payload()), plik)

    assert wynik.returncode == 0
    assert wynik.stdout == ""
    assert not plik.exists()


def test_hook_nie_tworzy_brakujacego_pliku_kroku(tmp_path):
    katalog = tmp_path / "stan"
    katalog.mkdir()
    plik = katalog / "krok.ndjson"

    wynik = _uruchom_hook(_polecenie(plik), json.dumps(_payload()), plik)

    assert wynik.returncode == 0
    assert not plik.exists()
    assert list(katalog.iterdir()) == []


def test_hook_ze_sciezka_bedaca_katalogiem_konczy_sie_kodem_zero_bez_wyjscia(tmp_path):
    katalog = tmp_path / "to-jest-katalog"
    katalog.mkdir()

    wynik = _uruchom_hook(_polecenie(katalog), json.dumps(_payload()), katalog)

    assert wynik.returncode == 0
    assert wynik.stdout == ""
    assert list(katalog.iterdir()) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root pomija prawa zapisu pliku")
def test_hook_z_plikiem_bez_prawa_zapisu_konczy_sie_kodem_zero_i_nic_nie_zmienia(tmp_path):
    plik = plik_hookow(tmp_path / "stan", RUN, KROK_A)
    plik.chmod(0o400)

    wynik = _uruchom_hook(_polecenie(plik), json.dumps(_payload()), plik)

    assert wynik.returncode == 0
    assert wynik.stdout == ""
    assert plik.read_bytes() == b""


def test_hook_bez_zmiennej_pliku_w_srodowisku_konczy_sie_kodem_zero_bez_wyjscia(tmp_path):
    plik = plik_hookow(tmp_path / "stan", RUN, KROK_A)

    wynik = _uruchom_hook(_polecenie(plik), json.dumps(_payload()), plik, bez_zmiennej=True)

    assert wynik.returncode == 0
    assert wynik.stdout == ""
    assert plik.read_bytes() == b""


def test_hook_z_pustym_wartoscia_zmiennej_pliku_konczy_sie_kodem_zero_bez_wyjscia(tmp_path):
    plik = plik_hookow(tmp_path / "stan", RUN, KROK_A)

    wynik = _uruchom_hook(_polecenie(plik), json.dumps(_payload()), plik, wartosc="")

    assert wynik.returncode == 0
    assert wynik.stdout == ""
    assert plik.read_bytes() == b""


def test_kazde_polecenie_z_ustawien_zapisuje_zdarzenie_do_pliku_ze_zmiennej(tmp_path):
    plik = plik_hookow(tmp_path / "stan", RUN, KROK_A)
    ustawienia = ustawienia_hookow(plik)["hooks"]

    for zdarzenie, grupy in ustawienia.items():
        polecenie = grupy[0]["hooks"][0]["command"]
        wynik = _uruchom_hook(polecenie, json.dumps(_payload(zdarzenie)), plik)
        assert wynik.returncode == 0, zdarzenie

    zapisane = [json.loads(w)["hook_event_name"] for w in plik.read_bytes().splitlines()]
    assert zapisane == list(ustawienia)


# --- czytnik: tożsamość kroku pochodzi z pliku -------------------------------------


def test_zdarzenie_dostaje_krok_z_pliku_nawet_gdy_payload_podaje_inny(tmp_path):
    plik, czytnik = _krok(tmp_path, KROK_A, RUN)
    _dopisz(
        plik,
        _linia(
            _payload(
                "SubagentStart",
                sesja="sesja-x",
                agent_id="agent-9",
                run_id="cudzy-run",
                step_id="cudzy-krok",
            )
        ),
    )

    zdarzenia = czytnik.odczytaj_nowe()

    assert len(zdarzenia) == 1
    assert zdarzenia[0]["run_id"] == RUN
    assert zdarzenia[0]["step_id"] == KROK_A
    assert zdarzenia[0]["session_id"] == "sesja-x"
    assert zdarzenia[0]["event"] == "SubagentStart"
    assert zdarzenia[0]["agent_id"] == "agent-9"


def test_czytnik_normalizuje_zdarzenie_kernelem_i_przenosi_narzedzie(tmp_path):
    plik, czytnik = _krok(tmp_path)
    _dopisz(plik, _linia(_payload("PreToolUse", tool_name="Write", tool_input={"p": "x"})))

    [zdarzenie] = czytnik.odczytaj_nowe()

    assert zdarzenie["event"] == "PreToolUse"
    assert zdarzenie["tool"] == "Write"


def test_znacznik_fazy_w_poleceniu_bash_daje_zdarzenie_fazy_z_krokiem_pliku(tmp_path):
    plik, czytnik = _krok(tmp_path)
    komenda = "python tools/phase_timer.py HAR-62 mark implementacja start"
    _dopisz(plik, _linia(_payload("PreToolUse", tool_name="Bash", tool_input={"command": komenda})))

    [zdarzenie] = czytnik.odczytaj_nowe()

    assert zdarzenie["event"] == "phase_start"
    assert zdarzenie["phase"] == "implementacja"
    assert zdarzenie["step_id"] == KROK_A


def test_zdarzenia_czytnika_trafiaja_do_kolejki_w_kolejnosci_z_pliku(tmp_path):
    plik, czytnik = _krok(tmp_path)
    _dopisz(plik, _linia(_payload("SessionStart")) + _linia(_payload("Stop")))

    zwrocone = czytnik.odczytaj_nowe()

    assert czytnik.kolejka.qsize() == 2
    z_kolejki = [czytnik.kolejka.get_nowait(), czytnik.kolejka.get_nowait()]
    assert z_kolejki == zwrocone
    assert [z["event"] for z in z_kolejki] == ["SessionStart", "Stop"]


def test_drugi_odczyt_nie_powtarza_juz_przeczytanych_zdarzen(tmp_path):
    plik, czytnik = _krok(tmp_path)
    _dopisz(plik, _linia(_payload("SessionStart")))
    assert [z["event"] for z in czytnik.odczytaj_nowe()] == ["SessionStart"]

    assert czytnik.odczytaj_nowe() == []
    _dopisz(plik, _linia(_payload("Stop")))
    assert [z["event"] for z in czytnik.odczytaj_nowe()] == ["Stop"]
    assert czytnik.kolejka.qsize() == 2


def test_niepelna_linia_bez_konca_nie_jest_zdarzeniem_dopoki_nie_zostanie_dokonczona(
    tmp_path, capsys
):
    plik, czytnik = _krok(tmp_path)
    pelna = _linia(_payload("Stop", agent_id="a1"))
    _dopisz(plik, pelna[:-1][:20])

    assert czytnik.odczytaj_nowe() == []
    assert czytnik.kolejka.qsize() == 0

    _dopisz(plik, pelna[:-1][20:] + b"\n")
    zdarzenia = czytnik.odczytaj_nowe()
    assert [(z["event"], z["agent_id"]) for z in zdarzenia] == [("Stop", "a1")]
    assert czytnik.odczytaj_nowe() == []
    assert capsys.readouterr().err == ""


def test_niepelny_ogon_po_pelnych_liniach_nie_blokuje_wczesniejszych_zdarzen(tmp_path):
    plik, czytnik = _krok(tmp_path)
    _dopisz(plik, _linia(_payload("SessionStart")) + b'{"session_id": "s", "hook_ev')

    assert [z["event"] for z in czytnik.odczytaj_nowe()] == ["SessionStart"]


def test_przesuniecie_czytnika_liczy_bajty_a_nie_znaki(tmp_path):
    plik, czytnik = _krok(tmp_path)
    _dopisz(plik, _linia(_payload("SessionStart", sesja="sesja-ąęółź")))
    assert [z["session_id"] for z in czytnik.odczytaj_nowe()] == ["sesja-ąęółź"]

    _dopisz(plik, _linia(_payload("Stop", sesja="sesja-ąęółź")))

    assert [z["event"] for z in czytnik.odczytaj_nowe()] == ["Stop"]


# --- czytnik: linie, których nie da się przeczytać --------------------------------


@pytest.mark.parametrize(
    "zla_linia",
    [
        pytest.param(b'{"session_id": "' + TAJNE.encode() + b'", "hook_ev\n', id="zly-json"),
        pytest.param(b"[]\n", id="json-lista"),
        pytest.param(b"1\n", id="json-liczba"),
        pytest.param(b'"' + TAJNE.encode() + b'"\n', id="json-napis"),
        pytest.param(b"\xff\xfe " + TAJNE.encode() + b"\n", id="zly-utf8"),
        pytest.param(
            _linia(_payload("PreToolUse", tool_name="Bash", tool_input=TAJNE)),
            id="tool-input-nie-jest-obiektem",
        ),
    ],
)
def test_nieparsowalna_linia_nie_daje_zdarzenia_a_kolejne_linie_sa_czytane(
    tmp_path, capsys, zla_linia
):
    plik, czytnik = _krok(tmp_path)
    _dopisz(
        plik,
        _linia(_payload("SessionStart")) + zla_linia + _linia(_payload("Stop", agent_id="a1")),
    )

    zdarzenia = czytnik.odczytaj_nowe()

    assert [z["event"] for z in zdarzenia] == ["SessionStart", "Stop"]
    assert czytnik.kolejka.qsize() == 2
    blad = capsys.readouterr().err
    assert f"hooki: pominieta nieparsowalna linia kroku {KROK_A}" in blad
    assert blad.count("pominieta nieparsowalna linia") == 1
    assert TAJNE not in blad


def test_wpis_o_nieparsowalnej_linii_podaje_przesuniecie_jej_poczatku_w_bajtach(
    tmp_path, capsys
):
    plik, czytnik = _krok(tmp_path)
    pierwsza = _linia(_payload("SessionStart", sesja="sesja-ąę"))
    _dopisz(plik, pierwsza + b"to nie jest json\n")

    czytnik.odczytaj_nowe()

    assert f"(bajt {len(pierwsza)})" in capsys.readouterr().err


def test_nieparsowalna_linia_na_poczatku_pliku_jest_zgloszona_z_przesunieciem_zero(
    tmp_path, capsys
):
    plik, czytnik = _krok(tmp_path)
    _dopisz(plik, b"to nie jest json\n" + _linia(_payload("Stop")))

    assert [z["event"] for z in czytnik.odczytaj_nowe()] == ["Stop"]
    assert "(bajt 0)" in capsys.readouterr().err


def test_nieparsowalna_linia_jest_zglaszana_raz_a_nie_przy_kazdym_odczycie(tmp_path, capsys):
    plik, czytnik = _krok(tmp_path)
    _dopisz(plik, b"to nie jest json\n")
    czytnik.odczytaj_nowe()
    capsys.readouterr()

    assert czytnik.odczytaj_nowe() == []
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("pusta", [b"\n", b"   \n", b"\t \n"])
def test_pusta_linia_nie_jest_zdarzeniem_i_nie_trafia_do_logu(tmp_path, capsys, pusta):
    plik, czytnik = _krok(tmp_path)
    _dopisz(plik, pusta + _linia(_payload("Stop")) + pusta)

    zdarzenia = czytnik.odczytaj_nowe()

    assert [z["event"] for z in zdarzenia] == ["Stop"]
    assert capsys.readouterr().err == ""


def test_brak_pliku_kroku_daje_pusta_liste_bez_wpisu_w_logu(tmp_path, capsys):
    czytnik = CzytnikHookow(tmp_path / "brak.ndjson", RUN, KROK_A)

    assert czytnik.odczytaj_nowe() == []
    assert capsys.readouterr().err == ""


def test_plik_pojawiajacy_sie_po_pierwszym_odczycie_jest_czytany_od_poczatku(tmp_path):
    plik = tmp_path / "pozniej.ndjson"
    czytnik = CzytnikHookow(plik, RUN, KROK_A)
    assert czytnik.odczytaj_nowe() == []

    plik.write_bytes(_linia(_payload("SessionStart")))

    assert [z["event"] for z in czytnik.odczytaj_nowe()] == ["SessionStart"]


def test_plik_obciety_jest_czytany_od_poczatku_i_zglaszany_w_logu(tmp_path, capsys):
    plik, czytnik = _krok(tmp_path)
    _dopisz(
        plik,
        _linia(_payload("SessionStart", sesja="sesja-dluga-1")) + _linia(_payload("Stop")),
    )
    assert len(czytnik.odczytaj_nowe()) == 2
    capsys.readouterr()
    nowa = _linia(_payload("SessionEnd", sesja="s"))
    assert len(nowa) < plik.stat().st_size
    plik.write_bytes(nowa)

    zdarzenia = czytnik.odczytaj_nowe()

    assert [z["event"] for z in zdarzenia] == ["SessionEnd"]
    assert f"hooki: plik kroku {KROK_A} obciety, czytam od poczatku" in capsys.readouterr().err


def test_plik_nieobciety_nie_jest_zglaszany_jako_obciety(tmp_path, capsys):
    plik, czytnik = _krok(tmp_path)
    _dopisz(plik, _linia(_payload("SessionStart")))
    czytnik.odczytaj_nowe()

    assert czytnik.odczytaj_nowe() == []
    assert "obciety" not in capsys.readouterr().err


def test_inny_blad_odczytu_pliku_daje_pusta_liste_i_wpis_z_nazwa_bledu(tmp_path, capsys):
    katalog = tmp_path / "to-katalog"
    katalog.mkdir()
    czytnik = CzytnikHookow(katalog, RUN, KROK_A)

    assert czytnik.odczytaj_nowe() == []

    assert (
        f"hooki: blad odczytu pliku kroku {KROK_A}: IsADirectoryError" in capsys.readouterr().err
    )


# --- nakładające się kroki -----------------------------------------------------------


def _ident(zdarzenie: dict) -> tuple:
    return (
        zdarzenie["session_id"],
        zdarzenie["event"],
        zdarzenie.get("tool"),
        zdarzenie.get("agent_id"),
    )


def _oczekiwane(payloady: list[dict]) -> list[tuple]:
    return [_ident(normalize(p).to_dict()) for p in payloady]


def test_zdarzenia_dwoch_nakladajacych_sie_krokow_trafiaja_do_swojego_kroku(tmp_path):
    plik_a, czytnik_a = _krok(tmp_path, KROK_A)
    plik_b, czytnik_b = _krok(tmp_path, KROK_B)
    a = [_payload("SessionStart", sesja="sa"), _payload("Stop", sesja="sa")]
    b = [_payload("SubagentStart", sesja="sb", agent_id="ab"), _payload("SessionEnd", sesja="sb")]

    _dopisz(plik_b, _linia(b[0]))
    _dopisz(plik_a, _linia(a[0]))
    _dopisz(plik_b, _linia(b[1]))
    _dopisz(plik_a, _linia(a[1]))
    z_b = czytnik_b.odczytaj_nowe()
    z_a = czytnik_a.odczytaj_nowe()

    assert [(z["step_id"], z["run_id"]) for z in z_a] == [(KROK_A, RUN)] * 2
    assert [(z["step_id"], z["run_id"]) for z in z_b] == [(KROK_B, RUN)] * 2
    assert _oczekiwane(a) == [_ident(z) for z in z_a]
    assert _oczekiwane(b) == [_ident(z) for z in z_b]


def test_przeplecione_zapisy_realnych_hookow_subagentow_nie_mieszaja_krokow(tmp_path):
    plik_a, czytnik_a = _krok(tmp_path, KROK_A)
    plik_b, czytnik_b = _krok(tmp_path, KROK_B)
    payloady = _surowe("subagenty.ndjson")
    dla_a = payloady[0::2]
    dla_b = payloady[1::2]
    assert len(dla_a) > 3 and len(dla_b) > 3

    zdarzenia_a: list[dict] = []
    zdarzenia_b: list[dict] = []
    for p in payloady:
        czy_a = any(p is x for x in dla_a)
        _dopisz(plik_a if czy_a else plik_b, _linia(p))
        # odczyt naprzemienny, w innej kolejności niż zapis
        zdarzenia_b += czytnik_b.odczytaj_nowe()
        zdarzenia_a += czytnik_a.odczytaj_nowe()

    assert {z["step_id"] for z in zdarzenia_a} == {KROK_A}
    assert {z["step_id"] for z in zdarzenia_b} == {KROK_B}
    assert {z["run_id"] for z in zdarzenia_a + zdarzenia_b} == {RUN}
    assert _oczekiwane(dla_a) == [_ident(z) for z in zdarzenia_a]
    assert _oczekiwane(dla_b) == [_ident(z) for z in zdarzenia_b]
    assert len(zdarzenia_a) + len(zdarzenia_b) == len(payloady)
    assert {z["agent_id"] for z in zdarzenia_a + zdarzenia_b if "agent_id" in z} == {
        "agent-1",
        "agent-2",
    }


def test_zdarzenie_z_pliku_jednego_kroku_nie_trafia_do_kolejki_drugiego(tmp_path):
    plik_a, czytnik_a = _krok(tmp_path, KROK_A)
    _plik_b, czytnik_b = _krok(tmp_path, KROK_B)
    _dopisz(plik_a, _linia(_payload("Stop")))

    czytnik_a.odczytaj_nowe()
    czytnik_b.odczytaj_nowe()

    assert czytnik_a.kolejka.qsize() == 1
    assert czytnik_b.kolejka.qsize() == 0


def test_hook_uruchomiony_naprawde_przypisuje_zdarzenie_do_kroku_ze_zmiennej_pliku(tmp_path):
    plik_a, czytnik_a = _krok(tmp_path, KROK_A)
    plik_b, czytnik_b = _krok(tmp_path, KROK_B)
    polecenie = _polecenie(plik_a)

    _uruchom_hook(polecenie, json.dumps(_payload("Stop", sesja="s-a")), plik_a)
    _uruchom_hook(polecenie, json.dumps(_payload("SessionEnd", sesja="s-b")), plik_b)

    [z_a] = czytnik_a.odczytaj_nowe()
    [z_b] = czytnik_b.odczytaj_nowe()
    assert (z_a["step_id"], z_a["event"], z_a["session_id"]) == (KROK_A, "Stop", "s-a")
    assert (z_b["step_id"], z_b["event"], z_b["session_id"]) == (KROK_B, "SessionEnd", "s-b")


# --- pętla w tle i zatrzymanie -------------------------------------------------------


def _zadania_poza_biezacym() -> set[asyncio.Task]:
    return asyncio.all_tasks() - {asyncio.current_task()}


async def test_petla_w_tle_dostarcza_dopisane_zdarzenia_do_kolejki_do_zatrzymania(tmp_path):
    plik, czytnik = _krok(tmp_path)
    przed = _zadania_poza_biezacym()

    czytnik.start()
    nowe = _zadania_poza_biezacym() - przed
    try:
        _dopisz(plik, _linia(_payload("SessionStart")))
        await czekaj_na_warunek(
            lambda: czytnik.kolejka.qsize() >= 1, LIMIT, "pierwsze zdarzenie w kolejce"
        )
        _dopisz(plik, _linia(_payload("Stop")))
        await czekaj_na_warunek(
            lambda: czytnik.kolejka.qsize() >= 2, LIMIT, "drugie zdarzenie w kolejce"
        )
    finally:
        await czekaj(czytnik.zatrzymaj(), LIMIT, "zatrzymanie czytnika")

    zdarzenia = [czytnik.kolejka.get_nowait(), czytnik.kolejka.get_nowait()]
    assert [(z["event"], z["step_id"], z["run_id"]) for z in zdarzenia] == [
        ("SessionStart", KROK_A, RUN),
        ("Stop", KROK_A, RUN),
    ]
    assert len(nowe) == 1
    assert all(zadanie.done() for zadanie in nowe)


async def test_po_zatrzymaniu_nowe_linie_nie_trafiaja_do_kolejki(tmp_path):
    plik, czytnik = _krok(tmp_path)
    czytnik.start()
    _dopisz(plik, _linia(_payload("SessionStart")))
    await czekaj_na_warunek(lambda: czytnik.kolejka.qsize() >= 1, LIMIT, "zdarzenie przed stopem")
    await czekaj(czytnik.zatrzymaj(), LIMIT, "zatrzymanie czytnika")

    _dopisz(plik, _linia(_payload("Stop")))
    await asyncio.sleep(0.15)  # kilkanaście przebiegów pętli, gdyby jeszcze działała

    assert czytnik.kolejka.qsize() == 1


async def test_drugi_start_przy_dzialajacym_zadaniu_nie_tworzy_drugiego_zadania(tmp_path):
    _plik, czytnik = _krok(tmp_path)
    przed = _zadania_poza_biezacym()

    czytnik.start()
    po_pierwszym = _zadania_poza_biezacym() - przed
    czytnik.start()
    po_drugim = _zadania_poza_biezacym() - przed
    await czekaj(czytnik.zatrzymaj(), LIMIT, "zatrzymanie czytnika")

    assert len(po_pierwszym) == 1
    assert po_drugim == po_pierwszym
    assert all(zadanie.done() for zadanie in po_pierwszym)


async def test_zatrzymanie_przerywa_oczekiwanie_pomiedzy_odczytami_od_razu(tmp_path):
    plik, czytnik = _krok(tmp_path)
    czytnik = CzytnikHookow(plik, RUN, KROK_A, interwal=60.0)
    przed = _zadania_poza_biezacym()
    czytnik.start()
    zadania = _zadania_poza_biezacym() - przed
    await asyncio.sleep(0.05)

    await czekaj(czytnik.zatrzymaj(), LIMIT, "zatrzymanie czytnika z dlugim interwalem")

    assert len(zadania) == 1
    assert all(zadanie.done() for zadanie in zadania)


async def test_zatrzymanie_z_nieprzeczytanymi_liniami_zapisuje_ich_liczbe_i_krok_w_logu(
    tmp_path, capsys
):
    plik, _ = _krok(tmp_path)
    czytnik = CzytnikHookow(plik, RUN, KROK_A, interwal=60.0)
    czytnik.start()
    await asyncio.sleep(0.05)  # pierwszy, pusty odczyt już się odbył
    _dopisz(
        plik,
        _linia(_payload("SessionStart")) + _linia(_payload("Stop")) + b'{"hook_event_na',
    )
    capsys.readouterr()

    await czekaj(czytnik.zatrzymaj(), LIMIT, "zatrzymanie czytnika")

    assert capsys.readouterr().err.strip() == f"hooki: nieprzeczytane zdarzenia kroku {KROK_A}: 3"
    assert czytnik.kolejka.qsize() == 0


async def test_zatrzymanie_bez_nieprzeczytanych_linii_nie_zapisuje_nic_w_logu(tmp_path, capsys):
    plik, czytnik = _krok(tmp_path)
    czytnik.start()
    _dopisz(plik, _linia(_payload("SessionStart")))
    await czekaj_na_warunek(lambda: czytnik.kolejka.qsize() >= 1, LIMIT, "zdarzenie przed stopem")
    capsys.readouterr()

    await czekaj(czytnik.zatrzymaj(), LIMIT, "zatrzymanie czytnika")

    assert "nieprzeczytane" not in capsys.readouterr().err


async def test_drugie_zatrzymanie_nie_powtarza_wpisu_i_nie_rzuca_wyjatku(tmp_path, capsys):
    plik, _ = _krok(tmp_path)
    czytnik = CzytnikHookow(plik, RUN, KROK_A, interwal=60.0)
    _dopisz(plik, _linia(_payload("SessionStart")))
    await czekaj(czytnik.zatrzymaj(), LIMIT, "pierwsze zatrzymanie")
    pierwszy = capsys.readouterr().err

    await czekaj(czytnik.zatrzymaj(), LIMIT, "drugie zatrzymanie")

    assert pierwszy.count("nieprzeczytane zdarzenia") == 1
    assert capsys.readouterr().err == ""


async def test_zatrzymanie_bez_startu_dziala_i_zglasza_nieprzeczytane_linie(tmp_path, capsys):
    plik, czytnik = _krok(tmp_path)
    _dopisz(plik, _linia(_payload("SessionStart")) + _linia(_payload("Stop")))

    await czekaj(czytnik.zatrzymaj(), LIMIT, "zatrzymanie bez startu")

    assert capsys.readouterr().err.strip() == f"hooki: nieprzeczytane zdarzenia kroku {KROK_A}: 2"
    assert czytnik.kolejka.qsize() == 0


async def test_zatrzymanie_nie_liczy_pustych_linii_jako_nieprzeczytanych_zdarzen(
    tmp_path, capsys
):
    plik, czytnik = _krok(tmp_path)
    _dopisz(plik, b"\n\n" + _linia(_payload("Stop")) + b"\n")

    await czekaj(czytnik.zatrzymaj(), LIMIT, "zatrzymanie czytnika")

    assert capsys.readouterr().err.strip() == f"hooki: nieprzeczytane zdarzenia kroku {KROK_A}: 1"


async def test_zatrzymanie_liczy_tylko_linie_po_ostatnim_odczycie(tmp_path, capsys):
    plik, czytnik = _krok(tmp_path)
    _dopisz(plik, _linia(_payload("SessionStart")) + _linia(_payload("Stop")))
    czytnik.odczytaj_nowe()
    _dopisz(plik, _linia(_payload("SessionEnd")))
    capsys.readouterr()

    await czekaj(czytnik.zatrzymaj(), LIMIT, "zatrzymanie czytnika")

    assert capsys.readouterr().err.strip() == f"hooki: nieprzeczytane zdarzenia kroku {KROK_A}: 1"


async def test_zatrzymanie_z_brakiem_pliku_kroku_nie_rzuca_wyjatku_i_nie_zapisuje_w_logu(
    tmp_path, capsys
):
    czytnik = CzytnikHookow(tmp_path / "brak.ndjson", RUN, KROK_A)

    await czekaj(czytnik.zatrzymaj(), LIMIT, "zatrzymanie czytnika")

    assert capsys.readouterr().err == ""


# --- bez portu ------------------------------------------------------------------------


async def test_odczyt_hookow_nie_otwiera_zadnego_gniazda(tmp_path, monkeypatch):
    # Pętla zdarzeń już istnieje: self-pipe asyncio powstał przed podmianą.
    def _zakaz(*args, **kwargs):
        raise AssertionError("hooki nie moga otwierac gniazd")

    with monkeypatch.context() as m:
        m.setattr(socket, "socket", _zakaz)
        m.setattr(socket, "create_connection", _zakaz)
        m.setattr(socket, "socketpair", _zakaz)

        plik = plik_hookow(tmp_path / "stan", RUN, KROK_A)
        ustawienia = ustawienia_hookow(plik)
        czytnik = CzytnikHookow(plik, RUN, KROK_A, interwal=0.01)
        czytnik.start()
        try:
            _dopisz(plik, _linia(_payload("SessionStart")))
            await czekaj_na_warunek(
                lambda: czytnik.kolejka.qsize() >= 1, LIMIT, "zdarzenie bez gniazda"
            )
        finally:
            await czekaj(czytnik.zatrzymaj(), LIMIT, "zatrzymanie czytnika")

    assert ustawienia["hooks"]
    assert czytnik.kolejka.get_nowait()["event"] == "SessionStart"


def test_podmiana_gniazda_w_tescie_naprawde_przerywa_proba_polaczenia(monkeypatch):
    def _zakaz(*args, **kwargs):
        raise AssertionError("zakaz gniazd")

    monkeypatch.setattr(socket, "socket", _zakaz)

    with pytest.raises(AssertionError):
        socket.socket()


def test_polecenie_hooka_uzywa_interpretera_runnera_a_nie_polaczenia_sieciowego(tmp_path):
    polecenie = _polecenie(tmp_path / "x.ndjson")

    assert sys.executable in polecenie or "'" + sys.executable + "'" in polecenie


# --- komunikaty błędów i dokładne przesunięcia (mutanty HAR-62, iteracja 2) -----------


def test_zly_run_id_ma_komunikat_o_run_id_a_zly_step_id_o_step_id(tmp_path):
    with pytest.raises(BladPlikuHookow) as zly_run:
        plik_hookow(tmp_path / "stan", "a/b", KROK_A)
    with pytest.raises(BladPlikuHookow) as zly_krok:
        plik_hookow(tmp_path / "stan", RUN, "a/b")

    assert str(zly_run.value) == "niedozwolony run_id hookow"
    assert str(zly_krok.value) == "niedozwolony step_id hookow"


def test_blad_tworzenia_pliku_hookow_niesie_nazwe_klasy_bledu_systemowego(tmp_path):
    stan = tmp_path / "stan"
    stan.write_text("to jest plik")

    with pytest.raises(BladPlikuHookow) as wyjatek:
        plik_hookow(stan, RUN, KROK_A)

    nazwa = type(wyjatek.value.__cause__).__name__
    assert nazwa in ("FileExistsError", "NotADirectoryError")
    assert str(wyjatek.value) == f"plik hookow kroku: {nazwa}"
    assert "NoneType" not in str(wyjatek.value)


def test_blad_otwarcia_pliku_kroku_niesie_nazwe_klasy_bledu_systemowego(tmp_path):
    (tmp_path / "stan" / "hooks" / RUN / f"{KROK_A}.ndjson").mkdir(parents=True)

    with pytest.raises(BladPlikuHookow) as wyjatek:
        plik_hookow(tmp_path / "stan", RUN, KROK_A)

    assert str(wyjatek.value) == "plik hookow kroku: IsADirectoryError"


def test_sciezka_wzgledna_ma_komunikat_o_wymaganej_sciezce_bezwzglednej():
    with pytest.raises(BladPlikuHookow) as wyjatek:
        ustawienia_hookow(Path("wzgledna/krok.ndjson"))

    assert str(wyjatek.value) == "sciezka pliku hookow musi byc bezwzgledna"


@pytest.mark.parametrize(
    "zla_linia",
    [
        pytest.param(b"to nie jest json\n", id="zly-json"),
        pytest.param(b"\xff\xfe\n", id="zly-utf8"),
        pytest.param(b"[]\n", id="json-nie-obiekt"),
        pytest.param(
            _linia(_payload("PreToolUse", tool_name="Bash", tool_input="x")),
            id="tool-input-nie-jest-obiektem",
        ),
    ],
)
def test_wpis_o_nieparsowalnej_linii_po_dwoch_poprawnych_podaje_sumaryczne_przesuniecie(
    tmp_path, capsys, zla_linia
):
    plik, czytnik = _krok(tmp_path)
    pierwsza = _linia(_payload("SessionStart", sesja="s"))
    druga = _linia(_payload("Stop", sesja="sesja-o-dluzszej-nazwie"))
    assert len(pierwsza) != len(druga)
    _dopisz(plik, pierwsza + druga + zla_linia)

    assert [z["event"] for z in czytnik.odczytaj_nowe()] == ["SessionStart", "Stop"]

    oczekiwany = (
        f"hooki: pominieta nieparsowalna linia kroku {KROK_A} (bajt {len(pierwsza) + len(druga)})"
    )
    assert capsys.readouterr().err.strip() == oczekiwany


def test_po_odczycie_obcietego_pliku_kolejny_odczyt_zwraca_tylko_nowe_zdarzenie(
    tmp_path, capsys
):
    plik, czytnik = _krok(tmp_path)
    _dopisz(
        plik,
        _linia(_payload("SessionStart", sesja="sesja-dluga-1"))
        + _linia(_payload("Stop", sesja="sesja-dluga-1")),
    )
    czytnik.odczytaj_nowe()
    capsys.readouterr()
    krotka = _linia(_payload("SessionStart", sesja="s"))
    assert len(krotka) < plik.stat().st_size
    plik.write_bytes(krotka)
    assert [z["event"] for z in czytnik.odczytaj_nowe()] == ["SessionStart"]
    assert "obciety" in capsys.readouterr().err

    _dopisz(plik, _linia(_payload("SessionEnd", sesja="s")))

    zdarzenia = czytnik.odczytaj_nowe()
    assert [(z["event"], z["session_id"]) for z in zdarzenia] == [("SessionEnd", "s")]
    assert capsys.readouterr().err == ""


def test_zla_wartosc_polecenia_narzedzia_jest_pominieta_a_linia_po_niej_czytana(
    tmp_path, capsys
):
    plik, czytnik = _krok(tmp_path)
    pierwsza = _linia(_payload("SessionStart", sesja="s"))
    zla = _linia(_payload("PreToolUse", tool_name="Bash", tool_input={"command": 5}))
    _dopisz(plik, pierwsza + zla + _linia(_payload("Stop", agent_id="po-zlej")))

    zdarzenia = czytnik.odczytaj_nowe()

    assert [(z["event"], z.get("agent_id")) for z in zdarzenia] == [
        ("SessionStart", None),
        ("Stop", "po-zlej"),
    ]
    assert capsys.readouterr().err.strip() == (
        f"hooki: pominieta nieparsowalna linia kroku {KROK_A} (bajt {len(pierwsza)})"
    )


def test_gleboko_zagniezdzony_json_jest_pominiety_a_linia_po_nim_czytana(tmp_path, capsys):
    plik, czytnik = _krok(tmp_path)
    pierwsza = _linia(_payload("SessionStart", sesja="s"))
    _dopisz(
        plik,
        pierwsza + b"[" * 200000 + b"\n" + _linia(_payload("Stop", agent_id="po-glebokiej")),
    )

    zdarzenia = czytnik.odczytaj_nowe()

    assert [(z["event"], z.get("agent_id")) for z in zdarzenia] == [
        ("SessionStart", None),
        ("Stop", "po-glebokiej"),
    ]
    assert capsys.readouterr().err.strip() == (
        f"hooki: pominieta nieparsowalna linia kroku {KROK_A} (bajt {len(pierwsza)})"
    )


async def test_zatrzymanie_nie_liczy_linii_z_samych_bialych_znakow_jako_nieprzeczytanych(
    tmp_path, capsys
):
    plik, czytnik = _krok(tmp_path)
    _dopisz(plik, _linia(_payload("SessionStart")))
    czytnik.odczytaj_nowe()
    _dopisz(plik, b"   \n\t \n" + _linia(_payload("Stop")))
    capsys.readouterr()

    await czekaj(czytnik.zatrzymaj(), LIMIT, "zatrzymanie czytnika")

    assert capsys.readouterr().err.strip() == f"hooki: nieprzeczytane zdarzenia kroku {KROK_A}: 1"


def test_domyslny_interwal_czytnika_to_dwie_dziesiate_sekundy(tmp_path):
    plik = plik_hookow(tmp_path / "stan", RUN, KROK_A)

    assert CzytnikHookow(plik, RUN, KROK_A).interwal == 0.2


def test_jednobajtowa_nieparsowalna_linia_na_poczatku_nie_blokuje_kolejnej(tmp_path, capsys):
    plik, czytnik = _krok(tmp_path)
    _dopisz(plik, b"x\n")

    assert czytnik.odczytaj_nowe() == []
    assert capsys.readouterr().err.strip() == (
        f"hooki: pominieta nieparsowalna linia kroku {KROK_A} (bajt 0)"
    )

    _dopisz(plik, _linia(_payload("Stop")))

    assert [z["event"] for z in czytnik.odczytaj_nowe()] == ["Stop"]
    assert capsys.readouterr().err == ""
