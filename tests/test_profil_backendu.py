"""HAR-174, czesc 2: profil backendu z pliku `backendy.json` (TDD).

Model dla tieru wynika z trzech warstw, od najslabszej: profil domyslny < plik
`backendy.json` < zmienna `COCKPIT_MODEL_<TIER>`. Plik nadpisuje pojedyncze tiery.
Rola dochodzi do tieru wylacznie przez `profil_roli`. Uszkodzony albo bledny plik to
`BladProfiluBackendu` (blad dziedziny z komunikatem), nigdy wyjatek biblioteki;
brak pliku to profil domyslny, nie blad.
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from cockpit.services.errors import BladDziedziny
from cockpit.services.invariants import Rola, Tier, profil_roli
from procesy import czekaj, uruchom_proces, zakoncz_proces
from runner.agent import (
    DOMYSLNE_MODELE,
    PROFIL_DOMYSLNY,
    BladProfiluBackendu,
    ProfilBackendu,
    SpecKroku,
    polecenie_agenta,
    sciezka_profilu,
)
from runner.krok import KATALOG_KORZENIA, KATALOG_PAKIETU
from test_silnik_runu import K, Szpieg, sprawdz_blad_przed_startem, swiat, uruchom

BRAK_ROOTA = pytest.mark.skipif(os.geteuid() == 0, reason="root czyta plik bez wzgledu na prawa")

# Stala lista modeli z pliku: kazdy tier ma inny, zeby pomylony tier byl widoczny.
MODELE_Z_PLIKU = {"judgment": "plik-sedzia", "execution": "plik-wykonawca",
                  "retrieval": "plik-zbieracz"}


def zapisz_profil(katalog: Path, tresc: object, nazwa: str = "backendy.json") -> Path:
    plik = katalog / nazwa
    plik.write_text(tresc if isinstance(tresc, str) else json.dumps(tresc), encoding="utf-8")
    return plik


def modele(profil: ProfilBackendu) -> dict[Tier, str]:
    return {t: profil.model(t) for t in Tier}


def blad_profilu(**kw) -> BladProfiluBackendu:
    with pytest.raises(BladProfiluBackendu) as wyjatek:
        ProfilBackendu.z_konfiguracji(**kw)
    assert type(wyjatek.value) is BladProfiluBackendu
    assert isinstance(wyjatek.value, BladDziedziny)
    assert str(wyjatek.value).strip() != ""
    return wyjatek.value


def przyczyna_odczytu(plik: Path) -> str:
    """Komunikat, jaki system albo dekoder zwraca dla odczytu pliku (niezalezny od kodu profilu)."""
    with pytest.raises((OSError, UnicodeDecodeError)) as przyczyna:
        plik.read_text(encoding="utf-8")
    return str(przyczyna.value)


def spec(rola: Rola) -> SpecKroku:
    return SpecKroku(run_id="r", step_id="s", agent_id="a", skill_id="x", rola=rola,
                     wejscia=[], wyjscia=[], komendy={})


# --- sciezka pliku profilu ------------------------------------------------------------------


def test_zmienna_cockpit_backendy_wskazuje_plik_profilu_przed_wszystkimi_innymi_zrodlami(tmp_path):
    env = {"COCKPIT_BACKENDY": "/inny/katalog/moje.json", "COCKPIT_CONFIG": "/konfig/dane.json",
           "HOME": str(tmp_path)}

    assert sciezka_profilu(env) == Path("/inny/katalog/moje.json")


def test_pusta_zmienna_cockpit_backendy_nie_wskazuje_pliku_i_decyduje_cockpit_config():
    env = {"COCKPIT_BACKENDY": "", "COCKPIT_CONFIG": "/konfig/dane.json"}

    assert sciezka_profilu(env) == Path("/konfig/backendy.json")


def test_plik_profilu_lezy_obok_pliku_z_cockpit_config_gdy_nie_podano_cockpit_backendy():
    env = {"COCKPIT_CONFIG": "/konfig/podkatalog/dane.json"}

    assert sciezka_profilu(env) == Path("/konfig/podkatalog/backendy.json")


def test_bez_zmiennych_plik_profilu_lezy_w_katalogu_konfiguracji_uzytkownika(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "dom"))

    assert sciezka_profilu({}) == tmp_path / "dom" / ".config" / "cockpit" / "backendy.json"


def test_sciezka_profilu_bez_podanego_srodowiska_czyta_zmienne_procesu(monkeypatch):
    monkeypatch.setenv("COCKPIT_BACKENDY", "/z/procesu.json")
    monkeypatch.setenv("COCKPIT_CONFIG", "/z/konfiguracji.json")

    assert sciezka_profilu() == Path("/z/procesu.json")


def test_sciezka_profilu_bez_podanego_srodowiska_i_bez_backendy_bierze_cockpit_config_procesu(
    monkeypatch,
):
    monkeypatch.delenv("COCKPIT_BACKENDY", raising=False)
    monkeypatch.setenv("COCKPIT_CONFIG", "/z/konfiguracji.json")

    assert sciezka_profilu() == Path("/z/backendy.json")


# --- profil domyslny ------------------------------------------------------------------------


def test_profil_domyslny_claude_to_domyslne_modele_tierow():
    assert dict(PROFIL_DOMYSLNY["claude"]) == {
        Tier.judgment: "opus", Tier.execution: "sonnet", Tier.retrieval: "haiku"}
    assert dict(PROFIL_DOMYSLNY["claude"]) == dict(DOMYSLNE_MODELE)


def test_profil_domyslny_jest_tylko_do_odczytu():
    with pytest.raises(TypeError):
        PROFIL_DOMYSLNY["claude"][Tier.judgment] = "podmieniony"  # type: ignore[index]
    with pytest.raises(TypeError):
        PROFIL_DOMYSLNY["nowy"] = {}  # type: ignore[index]


def test_brak_pliku_profilu_daje_profil_domyslny_a_nie_blad(tmp_path):
    profil = ProfilBackendu.z_konfiguracji(env={}, plik=tmp_path / "brak" / "backendy.json")

    assert modele(profil) == dict(DOMYSLNE_MODELE)


def test_bez_podanego_pliku_brak_pliku_w_katalogu_konfiguracji_daje_profil_domyslny(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "pusty-dom"))

    assert modele(ProfilBackendu.z_konfiguracji(env={})) == dict(DOMYSLNE_MODELE)


@pytest.mark.parametrize("tresc", [{}, {"claude": {}}], ids=["pusty-obiekt", "backend-bez-tierow"])
def test_plik_niezmieniajacy_zadnego_tieru_zostawia_profil_domyslny(tmp_path, tresc):
    plik = zapisz_profil(tmp_path, tresc)

    assert modele(ProfilBackendu.z_konfiguracji(env={}, plik=plik)) == dict(DOMYSLNE_MODELE)


# --- plik nadpisuje pojedyncze tiery ---------------------------------------------------------


def test_plik_nadpisuje_tylko_ten_tier_ktory_wymienia_a_reszta_zostaje_z_profilu_domyslnego(
    tmp_path,
):
    plik = zapisz_profil(tmp_path, {"claude": {"judgment": "plik-sedzia"}})

    profil = ProfilBackendu.z_konfiguracji(env={}, plik=plik)

    assert modele(profil) == {
        Tier.judgment: "plik-sedzia", Tier.execution: "sonnet", Tier.retrieval: "haiku"}


def test_plik_z_dwoma_tierami_zostawia_trzeci_domyslny(tmp_path):
    plik = zapisz_profil(tmp_path, {"claude": {"execution": "e", "retrieval": "r"}})

    profil = ProfilBackendu.z_konfiguracji(env={}, plik=plik)

    assert modele(profil) == {Tier.judgment: "opus", Tier.execution: "e", Tier.retrieval: "r"}


def test_plik_ze_wszystkimi_tierami_wymienia_wszystkie_modele(tmp_path):
    plik = zapisz_profil(tmp_path, {"claude": MODELE_Z_PLIKU})

    profil = ProfilBackendu.z_konfiguracji(env={}, plik=plik)

    assert modele(profil) == {
        Tier.judgment: "plik-sedzia", Tier.execution: "plik-wykonawca",
        Tier.retrieval: "plik-zbieracz"}


def test_plik_wskazany_zmienna_cockpit_backendy_jest_czytany_gdy_nie_podano_pliku(tmp_path):
    plik = zapisz_profil(tmp_path, {"claude": {"retrieval": "z-env-sciezki"}}, "inna-nazwa.json")

    profil = ProfilBackendu.z_konfiguracji(env={"COCKPIT_BACKENDY": str(plik)})

    assert profil.model(Tier.retrieval) == "z-env-sciezki"


def test_plik_obok_cockpit_config_jest_czytany_gdy_nie_podano_pliku_ani_cockpit_backendy(tmp_path):
    zapisz_profil(tmp_path, {"claude": {"execution": "obok-configu"}})

    profil = ProfilBackendu.z_konfiguracji(env={"COCKPIT_CONFIG": str(tmp_path / "dane.json")})

    assert profil.model(Tier.execution) == "obok-configu"


def test_kazde_wywolanie_czyta_plik_od_nowa(tmp_path):
    plik = zapisz_profil(tmp_path, {"claude": {"judgment": "pierwszy"}})
    assert ProfilBackendu.z_konfiguracji(env={}, plik=plik).model(Tier.judgment) == "pierwszy"

    zapisz_profil(tmp_path, {"claude": {"judgment": "drugi"}})

    assert ProfilBackendu.z_konfiguracji(env={}, plik=plik).model(Tier.judgment) == "drugi"


def test_nadpisanie_z_pliku_nie_zmienia_profilu_domyslnego_wspolnego_dla_kolejnych_wywolan(
    tmp_path,
):
    plik = zapisz_profil(tmp_path, {"claude": MODELE_Z_PLIKU})

    ProfilBackendu.z_konfiguracji(env={"COCKPIT_MODEL_JUDGMENT": "z-env"}, plik=plik)

    assert PROFIL_DOMYSLNY["claude"][Tier.judgment] == "opus"
    assert modele(ProfilBackendu.z_konfiguracji(env={}, plik=tmp_path / "brak.json")) == dict(
        DOMYSLNE_MODELE)


def test_profil_ze_srodowiska_nie_czyta_pliku_profilu(tmp_path):
    plik = zapisz_profil(tmp_path, {"claude": MODELE_Z_PLIKU})

    profil = ProfilBackendu.z_srodowiska({"COCKPIT_BACKENDY": str(plik)})

    assert modele(profil) == dict(DOMYSLNE_MODELE)


# --- kolejnosc: zmienna srodowiska > plik > domyslny -----------------------------------------


def test_zmienna_srodowiska_wygrywa_z_plikiem_dla_tego_samego_tieru(tmp_path):
    plik = zapisz_profil(tmp_path, {"claude": MODELE_Z_PLIKU})

    profil = ProfilBackendu.z_konfiguracji(
        env={"COCKPIT_MODEL_JUDGMENT": "z-srodowiska"}, plik=plik)

    assert profil.model(Tier.judgment) == "z-srodowiska"


def test_zmienna_srodowiska_zmienia_tylko_swoj_tier_a_pozostale_zostaja_z_pliku(tmp_path):
    plik = zapisz_profil(tmp_path, {"claude": MODELE_Z_PLIKU})

    profil = ProfilBackendu.z_konfiguracji(
        env={"COCKPIT_MODEL_EXECUTION": "z-srodowiska"}, plik=plik)

    assert modele(profil) == {
        Tier.judgment: "plik-sedzia", Tier.execution: "z-srodowiska",
        Tier.retrieval: "plik-zbieracz"}


def test_zmienna_srodowiska_dziala_takze_na_tier_ktorego_plik_nie_wymienia(tmp_path):
    plik = zapisz_profil(tmp_path, {"claude": {"judgment": "plik-sedzia"}})

    profil = ProfilBackendu.z_konfiguracji(
        env={"COCKPIT_MODEL_RETRIEVAL": "z-srodowiska"}, plik=plik)

    assert modele(profil) == {
        Tier.judgment: "plik-sedzia", Tier.execution: "sonnet", Tier.retrieval: "z-srodowiska"}


def test_pusta_zmienna_srodowiska_nie_przykrywa_modelu_z_pliku(tmp_path):
    plik = zapisz_profil(tmp_path, {"claude": MODELE_Z_PLIKU})

    profil = ProfilBackendu.z_konfiguracji(env={"COCKPIT_MODEL_JUDGMENT": ""}, plik=plik)

    assert profil.model(Tier.judgment) == "plik-sedzia"


def test_zmienna_srodowiska_z_samych_spacji_nie_przykrywa_modelu_z_pliku(tmp_path):
    plik = zapisz_profil(tmp_path, {"claude": MODELE_Z_PLIKU})

    profil = ProfilBackendu.z_konfiguracji(env={"COCKPIT_MODEL_RETRIEVAL": "   "}, plik=plik)

    assert profil.model(Tier.retrieval) == "plik-zbieracz"


def test_zmienna_srodowiska_wygrywa_z_profilem_domyslnym_gdy_pliku_nie_ma(tmp_path):
    profil = ProfilBackendu.z_konfiguracji(
        env={"COCKPIT_MODEL_JUDGMENT": "z-srodowiska"}, plik=tmp_path / "brak.json")

    assert modele(profil) == {
        Tier.judgment: "z-srodowiska", Tier.execution: "sonnet", Tier.retrieval: "haiku"}


def test_profil_bez_podanego_srodowiska_czyta_zmienne_procesu_takze_dla_modelu(
    tmp_path, monkeypatch
):
    plik = zapisz_profil(tmp_path, {"claude": MODELE_Z_PLIKU})
    _modele_z_srodowiska_wyczyszczone(monkeypatch)
    monkeypatch.setenv("COCKPIT_BACKENDY", str(plik))
    monkeypatch.setenv("COCKPIT_MODEL_EXECUTION", "z-procesu")

    profil = ProfilBackendu.z_konfiguracji()

    assert modele(profil) == {
        Tier.judgment: "plik-sedzia", Tier.execution: "z-procesu",
        Tier.retrieval: "plik-zbieracz"}


# --- rola -> tier -> model, wylacznie przez profil_roli ---------------------------------------


@pytest.mark.parametrize(("rola", "model"), [
    pytest.param(Rola.author, "plik-sedzia", id="autor-judgment"),
    pytest.param(Rola.reviewer, "plik-sedzia", id="recenzent-judgment"),
    pytest.param(Rola.executor, "plik-wykonawca", id="wykonawca-execution"),
    pytest.param(Rola.gatherer, "plik-zbieracz", id="zbieracz-retrieval"),
])
def test_rola_dostaje_z_profilu_z_pliku_model_swojego_tieru(tmp_path, rola, model):
    plik = zapisz_profil(tmp_path, {"claude": MODELE_Z_PLIKU})
    profil = ProfilBackendu.z_konfiguracji(env={}, plik=plik)

    assert profil.model(profil_roli(rola).tier) == model
    argv = polecenie_agenta(spec(rola), profil)
    assert argv[argv.index("--model") + 1] == model


def test_recenzent_dostaje_model_tieru_judgment_z_profilu_zbudowanego_z_konfiguracji(tmp_path):
    profil = ProfilBackendu.z_konfiguracji(env={}, plik=tmp_path / "brak.json")

    assert profil.model(profil_roli(Rola.reviewer).tier) == profil.model(Tier.judgment) == "opus"


# --- uszkodzony albo bledny plik: blad dziedziny z komunikatem ---------------------------------


def test_nieznany_backend_to_blad_profilu_z_jego_nazwa(tmp_path):
    blad = blad_profilu(backend="gpt", env={}, plik=tmp_path / "brak.json")

    assert str(blad) == "nieznany backend: gpt"


def test_nieznany_backend_jest_bledem_takze_gdy_plik_istnieje_i_jest_poprawny(tmp_path):
    plik = zapisz_profil(tmp_path, {"claude": MODELE_Z_PLIKU})

    blad = blad_profilu(backend="gpt", env={}, plik=plik)

    assert str(blad) == "nieznany backend: gpt"


def test_uszkodzony_json_to_blad_profilu_z_nazwa_pliku_i_przyczyna_parsera(tmp_path):
    plik = zapisz_profil(tmp_path, '{"claude": ')

    blad = blad_profilu(env={}, plik=plik)

    with pytest.raises(json.JSONDecodeError) as przyczyna:
        json.loads('{"claude": ')
    assert str(blad) == f"plik profilu backendu {plik}: {przyczyna.value}"
    assert "Expecting value" in str(blad)


def test_plik_nie_w_utf8_to_blad_profilu_z_nazwa_pliku_i_przyczyna_dekodowania(tmp_path):
    plik = tmp_path / "backendy.json"
    plik.write_bytes(b'{"claude": {"judgment": "\xff\xfe"}}')

    blad = blad_profilu(env={}, plik=plik)

    assert str(blad) == f"plik profilu backendu {plik}: {przyczyna_odczytu(plik)}"
    assert "can't decode byte 0xff" in str(blad)


def test_katalog_zamiast_pliku_profilu_to_blad_profilu_z_przyczyna_systemu(tmp_path):
    plik = tmp_path / "backendy.json"
    plik.mkdir()

    blad = blad_profilu(env={}, plik=plik)

    assert str(blad) == f"plik profilu backendu {plik}: {przyczyna_odczytu(plik)}"
    assert "Is a directory" in str(blad)


def test_plik_profilu_pod_zwyklym_plikiem_zamiast_katalogu_to_blad_a_nie_brak_pliku(tmp_path):
    zwykly = tmp_path / "zwykly-plik"
    zwykly.write_text("x", encoding="utf-8")

    plik = zwykly / "backendy.json"

    blad = blad_profilu(env={}, plik=plik)

    assert str(blad) == f"plik profilu backendu {plik}: {przyczyna_odczytu(plik)}"
    assert "Not a directory" in str(blad)


@BRAK_ROOTA
def test_plik_profilu_bez_praw_odczytu_to_blad_profilu_z_przyczyna_systemu(tmp_path):
    plik = zapisz_profil(tmp_path, {"claude": MODELE_Z_PLIKU})
    plik.chmod(0)
    try:
        blad = blad_profilu(env={}, plik=plik)
        oczekiwana_przyczyna = przyczyna_odczytu(plik)
    finally:
        plik.chmod(0o600)

    assert str(blad) == f"plik profilu backendu {plik}: {oczekiwana_przyczyna}"
    assert "Permission denied" in str(blad)


@pytest.mark.parametrize("tresc", [
    pytest.param("[]", id="tablica-pusta"),
    pytest.param('["claude"]', id="tablica-z-napisem"),
    pytest.param("null", id="null"),
    pytest.param('"claude"', id="napis"),
    pytest.param("5", id="liczba"),
    pytest.param("true", id="bool"),
])
def test_plik_ktorego_najwyzszy_poziom_nie_jest_obiektem_to_blad_profilu_z_nazwa_pliku(
    tmp_path, tresc
):
    plik = zapisz_profil(tmp_path, tresc)

    blad = blad_profilu(env={}, plik=plik)

    assert str(blad) == f"plik profilu backendu {plik}: oczekiwano obiektu z backendami"


@pytest.mark.parametrize("wartosc", [
    pytest.param("opus", id="napis"),
    pytest.param(["opus"], id="tablica"),
    pytest.param(None, id="null"),
    pytest.param(5, id="liczba"),
    pytest.param(True, id="bool"),
])
def test_wartosc_backendu_ktora_nie_jest_obiektem_to_blad_profilu_wskazujacy_backend(
    tmp_path, wartosc
):
    plik = zapisz_profil(tmp_path, {"claude": wartosc})

    blad = blad_profilu(env={}, plik=plik)

    assert str(blad) == (
        f"plik profilu backendu {plik}: backend 'claude' musi byc obiektem tierow")


def test_nieznany_tier_w_pliku_to_blad_profilu_wskazujacy_ten_tier(tmp_path):
    plik = zapisz_profil(tmp_path, {"claude": {"judgment": "ok", "bogactwo-7": "x"}})

    blad = blad_profilu(env={}, plik=plik)

    assert str(blad) == (
        f"plik profilu backendu {plik}: nieznany tier 'bogactwo-7' backendu 'claude'")


@pytest.mark.parametrize("model", [
    pytest.param(5, id="liczba"),
    pytest.param(True, id="bool"),
    pytest.param(None, id="null"),
    pytest.param(["opus"], id="tablica"),
    pytest.param({"nazwa": "opus"}, id="obiekt"),
])
def test_model_ktory_nie_jest_napisem_to_blad_profilu_wskazujacy_tier(tmp_path, model):
    plik = zapisz_profil(tmp_path, {"claude": {"execution": model}})

    blad = blad_profilu(env={}, plik=plik)

    assert str(blad) == (
        f"plik profilu backendu {plik}: model tieru 'execution' backendu 'claude' "
        "musi byc niepustym napisem")


def test_pusty_model_w_pliku_to_blad_profilu_wskazujacy_tier(tmp_path):
    plik = zapisz_profil(tmp_path, {"claude": {"retrieval": ""}})

    blad = blad_profilu(env={}, plik=plik)

    assert str(blad) == (
        f"plik profilu backendu {plik}: model tieru 'retrieval' backendu 'claude' "
        "musi byc niepustym napisem")


def test_blad_w_jednym_tierze_odrzuca_caly_plik_zamiast_zastosowac_poprawne_tiery(tmp_path):
    plik = zapisz_profil(tmp_path, {"claude": {"judgment": "dobry", "execution": ""}})

    blad_profilu(env={}, plik=plik)


def test_zmienna_srodowiska_nie_naprawia_uszkodzonego_pliku(tmp_path):
    plik = zapisz_profil(tmp_path, '{"claude": ')

    blad_profilu(env={"COCKPIT_MODEL_JUDGMENT": "z-srodowiska"}, plik=plik)


# --- silnik: profil z pliku, blad profilu przed startem krokow -------------------------------


def _modele_z_srodowiska_wyczyszczone(monkeypatch) -> None:
    for tier in Tier:
        monkeypatch.delenv(f"COCKPIT_MODEL_{tier.value.upper()}", raising=False)


async def test_run_bez_podanego_profilu_bierze_model_z_pliku_profilu(tmp_path, monkeypatch):
    plik = zapisz_profil(tmp_path, {"claude": MODELE_Z_PLIKU}, "moje-backendy.json")
    _modele_z_srodowiska_wyczyszczone(monkeypatch)
    monkeypatch.setenv("COCKPIT_BACKENDY", str(plik))
    s = swiat(tmp_path, [K("a"), K("b")], opcje_skilli={"b": {"role": "executor"}})
    s.profil = None
    agent = Szpieg()

    await uruchom(s, agent)

    modele_krokow = {w.spec.step_id: w.polecenie[w.polecenie.index("--model") + 1]
                     for w in agent.wywolania}
    assert modele_krokow == {"a": "plik-sedzia", "b": "plik-wykonawca"}


async def test_run_bez_podanego_profilu_ma_zmienna_srodowiska_wazniejsza_od_pliku(
    tmp_path, monkeypatch
):
    plik = zapisz_profil(tmp_path, {"claude": MODELE_Z_PLIKU}, "moje-backendy.json")
    _modele_z_srodowiska_wyczyszczone(monkeypatch)
    monkeypatch.setenv("COCKPIT_BACKENDY", str(plik))
    monkeypatch.setenv("COCKPIT_MODEL_JUDGMENT", "z-srodowiska-runnera")
    s = swiat(tmp_path, [K("a")])
    s.profil = None
    agent = Szpieg()

    await uruchom(s, agent)

    (wywolanie,) = agent.wywolania
    assert wywolanie.polecenie[wywolanie.polecenie.index("--model") + 1] == "z-srodowiska-runnera"


async def test_uszkodzony_plik_profilu_konczy_run_bledem_przed_startem_zadnego_kroku(
    tmp_path, monkeypatch
):
    plik = zapisz_profil(tmp_path, '{"claude": ', "uszkodzone.json")
    _modele_z_srodowiska_wyczyszczone(monkeypatch)
    monkeypatch.setenv("COCKPIT_BACKENDY", str(plik))
    oczekiwany = blad_profilu(env={"COCKPIT_BACKENDY": str(plik)})
    s = swiat(tmp_path, [K("a"), K("b")])
    s.profil = None
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent)

    assert sprawdz_blad_przed_startem(zdarzenia) == str(oczekiwany)
    assert agent.wywolania == []


async def test_nieznany_tier_w_pliku_profilu_konczy_run_bledem_z_komunikatem_profilu(
    tmp_path, monkeypatch
):
    plik = zapisz_profil(tmp_path, {"claude": {"bogactwo-7": "x"}}, "zly-tier.json")
    _modele_z_srodowiska_wyczyszczone(monkeypatch)
    monkeypatch.setenv("COCKPIT_BACKENDY", str(plik))
    s = swiat(tmp_path, [K("a")])
    s.profil = None
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent)

    assert sprawdz_blad_przed_startem(zdarzenia) == (
        f"plik profilu backendu {plik}: nieznany tier 'bogactwo-7' backendu 'claude'")
    assert agent.wywolania == []


KOD_CZYTAJACY_PROFIL = (
    "import sys\n"
    "from pathlib import Path\n"
    "from cockpit.services.invariants import Tier\n"
    "from runner.agent import ProfilBackendu\n"
    "profil = ProfilBackendu.z_konfiguracji(env={}, plik=Path(sys.argv[1]))\n"
    "sys.stdout.buffer.write(profil.model(Tier.judgment).encode('utf-8'))\n"
)


async def test_plik_profilu_z_polskimi_znakami_jest_czytany_jako_utf8_mimo_innego_kodowania_systemu(
    tmp_path
):
    plik = tmp_path / "backendy.json"
    plik.write_text('{"claude": {"judgment": "zażółć"}}', encoding="utf-8")
    # proces z kodowaniem ASCII: bez jawnego UTF-8 odczyt pliku zawiodlby na polskich znakach
    srodowisko = {
        **os.environ, "PYTHONPATH": str(KATALOG_PAKIETU), "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0", "LC_ALL": "C", "LANG": "C",
    }
    proc = await uruchom_proces(
        sys.executable, "-c", KOD_CZYTAJACY_PROFIL, str(plik), cwd=KATALOG_KORZENIA,
        env=srodowisko, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        wyjscie, blad = await czekaj(proc.communicate(), 60, "odczyt profilu w procesie")
    finally:
        await zakoncz_proces(proc, nazwa="odczyt profilu")

    assert (proc.returncode, wyjscie) == (0, "zażółć".encode("utf-8")), blad


def test_liczba_o_pieciu_tysiacach_cyfr_w_pliku_profilu_to_blad_profilu_a_nie_blad_biblioteki(
    tmp_path
):
    tresc = '{"claude": {"judgment": 1' + "0" * 4999 + "}}"       # 5000 cyfr, limit parsera to 4300
    plik = zapisz_profil(tmp_path, tresc)
    with pytest.raises(ValueError) as przyczyna:
        json.loads(tresc)
    assert type(przyczyna.value) is ValueError                    # zwykly ValueError, nie JSONDecodeError

    blad = blad_profilu(env={}, plik=plik)

    assert str(blad) == f"plik profilu backendu {plik}: {przyczyna.value}"
    assert "Exceeds the limit (4300 digits)" in str(blad)
    assert type(blad.__cause__) is ValueError


def test_gleboko_zagniezdzony_plik_profilu_to_blad_profilu_a_nie_przepelnienie_stosu(tmp_path):
    # 2 MB zagniezdzenia: wiecej niz stos parsera JSON, wiec parser rzuca RecursionError
    plik = zapisz_profil(tmp_path, "[" * 1_000_000 + "]" * 1_000_000)

    blad = blad_profilu(env={}, plik=plik)

    assert str(blad).startswith(f"plik profilu backendu {plik}: ")
    assert type(blad.__cause__) is RecursionError


async def test_wznowienie_z_uszkodzonym_plikiem_profilu_konczy_sie_porazka_a_decyzja_nie_przepada(
    tmp_path, monkeypatch
):
    from runner import silnik
    from runner.checkpoint import wczytaj_checkpoint
    from test_silnik_runu import RUN_ID
    from test_wznowienie_runu import run_zatrzymany_na_bramce

    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    plik = zapisz_profil(tmp_path, '{"claude": ', "uszkodzone.json")
    monkeypatch.setenv("COCKPIT_BACKENDY", str(plik))
    oczekiwany = str(blad_profilu(env={"COCKPIT_BACKENDY": str(plik)}))
    agent = Szpieg()
    zdarzenia: list[dict] = []

    async def emit(zdarzenie: dict) -> None:
        zdarzenia.append(zdarzenie)

    await asyncio.wait_for(
        silnik.wznow_run(RUN_ID, "b", "approve", emit, agent, dom=s.dom, root=s.root), 30)

    assert [(z["kind"], z["step_id"], z["text"]) for z in zdarzenia] == [
        ("step_failed", "b", oczekiwany), ("run_finished", "b", oczekiwany)]
    assert agent.wywolania == []
    assert wczytaj_checkpoint(s.katalog_runu).waiting_on is not None   # oczekiwanie zostaje

    plik.unlink()                       # poprawka pliku: brak pliku to profil domyslny
    po_poprawce = Szpieg()
    await asyncio.wait_for(
        silnik.wznow_run(RUN_ID, "b", "approve", emit, po_poprawce, dom=s.dom, root=s.root), 30)

    assert po_poprawce.kroki == ["b", "c"]
