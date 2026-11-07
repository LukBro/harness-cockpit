"""HAR-174, czesc 1: koszt, tokeny, tury i session_id kroku ze strumienia stream-json (TDD).

Agent (`claude -p --output-format stream-json`) wypisuje NDJSON; runner czyta go linia po
linii, zapisuje pomiar z koncowego zdarzenia `result` (brak pomiaru to None, nigdy 0),
ocenia wynik (budzet, kod wyjscia, dowod) i przekazuje pomiar do kokpitu, ktory zapisuje go
w wierszu kroku. Prompt idzie plikiem jako stdin agenta, nie w argv.

Prawdziwe procesy: atrapa `claude` to maly skrypt Pythona wypisujacy zadane linie, uruchamiany
przez prawdziwy `wykonaj_krok` (i prawdziwego straznika). Kazde czekanie ma limit czasu.
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import io
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Float, Integer, String, func, select

from cockpit.api.app import EventIn      # na poziomie modulu: adnotacje sa napisami (__future__)
from cockpit.api.bledy import _bezpieczne_dla_json
from cockpit.db import session as db_session
from cockpit.db.models import Event, Run, StepRun
from cockpit.services.invariants import Rola, Tier
from cockpit.services.skills import rozstrzygnij_rejestr
from cockpit.services.walidacja import Naruszenie, Niezmiennik
from cockpit.services.workflow import (
    BLAD_POLA,
    ma_bledy,
    wczytaj_workflow,
    zapisz_workflow,
)
from cockpit.services.workflow_walidacja import NIEZNANY_KLUCZ, waliduj_workflow
from procesy import czekaj, uruchom_proces, zakoncz_proces
from runner import krok as modul_kroku
from runner.agent import ProfilBackendu, SpecKroku, zbuduj_prompt
from runner.krok import KATALOG_KORZENIA, KATALOG_PAKIETU, PomiarKroku, WynikKroku, wykonaj_krok
from test_silnik_runu import (
    K,
    Szpieg,
    jedno,
    rodzaje,
    swiat as swiat_silnika,
    uruchom as uruchom_run,
)
from test_stan_krokow import INT_MAX, NAGLOWEK, _ok, _zdarzenie, swiat  # noqa: F401
from test_workflow_walidacja import napisz_skill

LIMIT_S = 60
KORZEN_REPO = Path(__file__).resolve().parent.parent
LIMIT_LINII = 8 * 1024 * 1024        # niezalezna od kodu kopia LIMIT_LINII_AGENTA
KROK = "krok-23"
BRAK = object()                       # pole nieobecne w JSON-ie
EVIDENCE_PUSTY = "niezmiennik evidence_required: pusty wynik agenta, brak artefaktu plan.md"

# --- atrapa `claude` ---------------------------------------------------------------------------

# Opis w argv[1] (JSON). `wyjscie` to lista krokow wykonywanych po kolei; stdin zapisany do
# pliku `zapis_wejscia` razem z argv pozostalym po opisie (czyli tym, co dostalby `claude`).
KOD_CLAUDE = r"""
import json, os, sys

p = json.loads(sys.argv[1])
if p.get("zapis_wejscia"):
    wejscie = sys.stdin.buffer.read()
    with open(p["zapis_wejscia"], "w", encoding="utf-8") as f:
        json.dump({"argv": sys.argv[2:], "stdin": wejscie.decode("utf-8")}, f)
if p.get("znacznik"):
    open(p["znacznik"], "w").close()
out, err = sys.stdout.buffer, sys.stderr.buffer
for krok in p.get("wyjscie", []):
    rodzaj = krok[0]
    if rodzaj == "linia":
        out.write(krok[1].encode("utf-8") + b"\n")
    elif rodzaj == "bajty":
        out.write(bytes.fromhex(krok[1]) + b"\n")
    elif rodzaj == "x":
        out.write(b"x" * krok[1] + b"\n")
    elif rodzaj == "pad":
        przod, tyl = b'{"type":"system","pad":"', b'"}'
        out.write(przod + b"x" * (krok[1] - len(przod) - len(tyl)) + tyl + b"\n")
    elif rodzaj == "bez_konca":
        out.write(krok[1].encode("utf-8"))
    elif rodzaj == "chmod":
        os.chmod(krok[1], int(krok[2], 8))
    elif rodzaj == "glebokie":
        out.write(b"[" * krok[1] + b"]" * krok[1] + b"\n")
    elif rodzaj == "powtorz":
        out.write((krok[1].encode("utf-8") + b"\n") * krok[2])
    elif rodzaj == "stderr":
        err.write(krok[1].encode("utf-8"))
        err.flush()
    elif rodzaj == "zapisz":
        with open(krok[1], "w", encoding="utf-8") as f:
            f.write(krok[2])
    out.flush()
sys.exit(p.get("kod", 0))
"""


def atrapa(*wyjscie, kod=0, zapis=None, znacznik=None, dodatkowe=()) -> list[str]:
    opis = {"wyjscie": [list(w) for w in wyjscie], "kod": kod,
            "zapis_wejscia": str(zapis) if zapis else None,
            "znacznik": str(znacznik) if znacznik else None}
    return [sys.executable, "-I", "-c", KOD_CLAUDE, json.dumps(opis), *dodatkowe]


def linia(tresc: str) -> tuple:
    return ("linia", tresc)


USAGE = {"input_tokens": 1234, "output_tokens": 567}


def pola_result(**nadpisania) -> dict:
    pola = {"type": "result", "subtype": "success", "is_error": False, "result": "gotowe",
            "total_cost_usd": 0.0123, "num_turns": 7, "session_id": "sesja-abc",
            "usage": dict(USAGE)}
    pola.update(nadpisania)
    return {k: v for k, v in pola.items() if v is not BRAK}


def result(**nadpisania) -> tuple:
    return linia(json.dumps(pola_result(**nadpisania)))


def result_z_usage(**usage) -> tuple:
    zlozone = {**USAGE, **usage}
    return result(usage={k: v for k, v in zlozone.items() if v is not BRAK})


def tekst(t: str) -> dict:
    return {"type": "text", "text": t}


def asystent(*elementy: dict) -> tuple:
    return linia(json.dumps(
        {"type": "assistant", "message": {"role": "assistant", "content": list(elementy)}}))


POMIAR_PELNY = PomiarKroku(
    cost_usd=0.0123, tokens_in=1234, tokens_out=567, num_turns=7, session_id="sesja-abc")


def spec(rola: Rola = Rola.author, wyjscia: list[str] | None = None,
         step_id: str = KROK) -> SpecKroku:
    return SpecKroku(
        run_id="run-17", step_id=step_id, agent_id="agent-31", skill_id="pisanie", rola=rola,
        wejscia=[], wyjscia=list(wyjscia or []), komendy={})


@pytest.fixture
def katalog(tmp_path: Path) -> Path:
    k = tmp_path / "repo"
    k.mkdir()
    return k


@pytest.fixture
def katalog_runu(tmp_path: Path) -> Path:
    k = tmp_path / "run"
    k.mkdir()
    return k


async def uruchom(
    krok: SpecKroku, polecenie: list[str], katalog: Path, katalog_runu: Path | None,
    prompt: str | None = "prompt kroku",
) -> tuple[WynikKroku, list[dict]]:
    zdarzenia: list[dict] = []

    async def emit(zdarzenie: dict) -> None:
        zdarzenia.append(zdarzenie)

    wynik = await asyncio.wait_for(
        wykonaj_krok(krok, polecenie, katalog, emit, katalog_runu=katalog_runu, prompt=prompt),
        LIMIT_S)
    return wynik, zdarzenia


def zdarzenie_output(text: str, krok: SpecKroku | None = None) -> dict:
    k = krok or spec()
    return {"kind": "output", "run_id": k.run_id, "step_id": k.step_id,
            "agent_id": k.agent_id, "text": text}


def powod_niezrozumialej(tresc: str) -> str:
    return f"niezrozumiała linia wyjścia agenta: {tresc}"


# =================================================================================================
# 1. Pomiar z koncowego zdarzenia result
# =================================================================================================


def test_pomiar_kroku_bez_zadnych_wartosci_nie_ma_zmierzonego_niczego_ani_zera():
    assert dataclasses.astuple(PomiarKroku()) == (None, None, None, None, None)
    assert WynikKroku("zakonczony", None, 0).pomiar == PomiarKroku()
    with pytest.raises(dataclasses.FrozenInstanceError):
        PomiarKroku().cost_usd = 1.0  # type: ignore[misc]


async def test_krok_z_wynikiem_agenta_zapisuje_koszt_tokeny_tury_i_sesje(katalog, katalog_runu):
    wynik, _ = await uruchom(spec(), atrapa(result()), katalog, katalog_runu)

    assert wynik == WynikKroku("zakonczony", None, 0, pomiar=POMIAR_PELNY)


@pytest.mark.parametrize("koszt", [
    pytest.param(BRAK, id="brak-pola"),
    pytest.param(None, id="null"),
    pytest.param(True, id="bool-true"),
    pytest.param(False, id="bool-false"),
    pytest.param(-0.5, id="ujemny"),
    pytest.param(-1, id="ujemny-calkowity"),
    pytest.param("0.5", id="napis"),
    pytest.param([1], id="tablica"),
])
async def test_koszt_niezmierzony_albo_niepoprawny_jest_brakiem_pomiaru_a_nie_zerem(
    katalog, katalog_runu, koszt
):
    wynik, _ = await uruchom(
        spec(), atrapa(result(total_cost_usd=koszt)), katalog, katalog_runu)

    assert wynik.status == "zakonczony"
    assert wynik.pomiar == dataclasses.replace(POMIAR_PELNY, cost_usd=None)
    assert wynik.pomiar.cost_usd is None


@pytest.mark.parametrize("literal", [
    pytest.param("NaN", id="nan"),
    pytest.param("1e999", id="nieskonczonosc"),
])
async def test_koszt_nieskonczony_albo_nie_liczba_jest_brakiem_pomiaru(
    katalog, katalog_runu, literal
):
    surowa = json.dumps(pola_result(total_cost_usd=0.0123)).replace("0.0123", literal)
    assert literal in surowa

    wynik, _ = await uruchom(spec(), atrapa(linia(surowa)), katalog, katalog_runu)

    assert wynik.status == "zakonczony"
    assert wynik.pomiar == dataclasses.replace(POMIAR_PELNY, cost_usd=None)


@pytest.mark.parametrize("koszt", [0, 0.0, 2, 3.75])
async def test_zmierzony_koszt_zero_i_calkowity_jest_zapisany_a_nie_uznany_za_brak(
    katalog, katalog_runu, koszt
):
    wynik, _ = await uruchom(
        spec(), atrapa(result(total_cost_usd=koszt)), katalog, katalog_runu)

    assert wynik.pomiar.cost_usd is not None
    assert wynik.pomiar.cost_usd == koszt


POLA_CALKOWITE = [
    pytest.param("tokens_in", lambda w: result_z_usage(input_tokens=w), id="tokens_in"),
    pytest.param("tokens_out", lambda w: result_z_usage(output_tokens=w), id="tokens_out"),
    pytest.param("num_turns", lambda w: result(num_turns=w), id="num_turns"),
]


@pytest.mark.parametrize(("pole", "zbuduj"), POLA_CALKOWITE)
@pytest.mark.parametrize("zla", [
    pytest.param(BRAK, id="brak-pola"),
    pytest.param(None, id="null"),
    pytest.param(True, id="bool"),
    pytest.param(-1, id="ujemna"),
    pytest.param("5", id="napis"),
    pytest.param(5.5, id="ulamek"),
    pytest.param(2_147_483_648, id="ponad-zakres-int4"),
])
async def test_zla_albo_brakujaca_liczba_zdarzenia_result_jest_brakiem_pomiaru_tylko_swojego_pola(
    katalog, katalog_runu, pole, zbuduj, zla
):
    wynik, _ = await uruchom(spec(), atrapa(zbuduj(zla)), katalog, katalog_runu)

    assert wynik.status == "zakonczony"
    assert wynik.pomiar == dataclasses.replace(POMIAR_PELNY, **{pole: None})


@pytest.mark.parametrize(("pole", "zbuduj"), POLA_CALKOWITE)
@pytest.mark.parametrize("dobra", [0, 2_147_483_647])
async def test_liczba_zdarzenia_result_na_granicach_zakresu_jest_zapisana(
    katalog, katalog_runu, pole, zbuduj, dobra
):
    wynik, _ = await uruchom(spec(), atrapa(zbuduj(dobra)), katalog, katalog_runu)

    assert getattr(wynik.pomiar, pole) == dobra
    assert getattr(wynik.pomiar, pole) is not None


@pytest.mark.parametrize("usage", [
    pytest.param(BRAK, id="brak-usage"),
    pytest.param(None, id="usage-null"),
    pytest.param("tokeny", id="usage-napis"),
    pytest.param([1, 2], id="usage-tablica"),
    pytest.param(5, id="usage-liczba"),
])
async def test_usage_niebedace_obiektem_daje_brak_tokenow_a_nie_wyjatek(
    katalog, katalog_runu, usage
):
    wynik, _ = await uruchom(spec(), atrapa(result(usage=usage)), katalog, katalog_runu)

    assert wynik.status == "zakonczony"
    assert wynik.pomiar == dataclasses.replace(POMIAR_PELNY, tokens_in=None, tokens_out=None)


@pytest.mark.parametrize("sesja", [
    pytest.param(BRAK, id="brak-pola"),
    pytest.param(None, id="null"),
    pytest.param("", id="pusty-napis"),
    pytest.param("s" * 101, id="101-znakow"),
    pytest.param(5, id="liczba"),
    pytest.param(True, id="bool"),
    pytest.param(["sesja"], id="tablica"),
])
async def test_session_id_pusty_za_dlugi_albo_nie_napis_jest_brakiem_pomiaru(
    katalog, katalog_runu, sesja
):
    wynik, _ = await uruchom(spec(), atrapa(result(session_id=sesja)), katalog, katalog_runu)

    assert wynik.status == "zakonczony"
    assert wynik.pomiar == dataclasses.replace(POMIAR_PELNY, session_id=None)


async def test_session_id_dlugi_na_100_znakow_miesci_sie_w_kolumnie_i_jest_zapisany(
    katalog, katalog_runu
):
    wynik, _ = await uruchom(
        spec(), atrapa(result(session_id="s" * 100)), katalog, katalog_runu)

    assert wynik.pomiar.session_id == "s" * 100


async def test_pomiar_pochodzi_ze_zdarzenia_result_a_nie_z_innych_zdarzen_z_session_id(
    katalog, katalog_runu
):
    init = linia(json.dumps({"type": "system", "subtype": "init", "session_id": "z-init"}))

    wynik, _ = await uruchom(
        spec(), atrapa(init, result(session_id="z-result")), katalog, katalog_runu)

    assert wynik.pomiar.session_id == "z-result"


async def test_gdy_agent_wypisze_dwa_zdarzenia_result_pomiar_pochodzi_z_ostatniego(
    katalog, katalog_runu
):
    pierwsze = result(total_cost_usd=1.0, session_id="pierwsza", num_turns=1)
    ostatnie = result(total_cost_usd=2.0, session_id="druga", num_turns=2)

    wynik, _ = await uruchom(spec(), atrapa(pierwsze, ostatnie), katalog, katalog_runu)

    assert wynik.pomiar == dataclasses.replace(
        POMIAR_PELNY, cost_usd=2.0, session_id="druga", num_turns=2)


async def test_ostatni_result_bez_kosztu_nie_dziedziczy_kosztu_z_wczesniejszego(
    katalog, katalog_runu
):
    pierwsze = result(total_cost_usd=1.0)
    ostatnie = result(total_cost_usd=BRAK)

    wynik, _ = await uruchom(spec(), atrapa(pierwsze, ostatnie), katalog, katalog_runu)

    assert wynik.pomiar.cost_usd is None


# =================================================================================================
# 2. Ocena wyniku: budzet, kod wyjscia, dowod, blad agenta
# =================================================================================================


@pytest.mark.parametrize("kod", [0, 1])
@pytest.mark.parametrize("podtyp", ["error_max_budget_usd", "error_max_turns"])
async def test_wyczerpanie_budzetu_albo_tur_konczy_krok_porazka_z_nazwanym_powodem(
    katalog, katalog_runu, podtyp, kod
):
    wynik, _ = await uruchom(
        spec(), atrapa(result(subtype=podtyp, is_error=True), kod=kod), katalog, katalog_runu)

    assert wynik.status == "nieudany"
    assert wynik.powod == f"budżet kroku wyczerpany ({podtyp})"
    assert wynik.pomiar == POMIAR_PELNY


async def test_porazka_budzetu_niesie_koszt_poniesiony_do_tej_pory(katalog, katalog_runu):
    wynik, _ = await uruchom(
        spec(),
        atrapa(result(subtype="error_max_budget_usd", is_error=True, total_cost_usd=0.5)),
        katalog, katalog_runu)

    assert wynik.pomiar.cost_usd == 0.5


async def test_kod_wyjscia_agenta_rozny_od_zera_konczy_krok_porazka_z_kodem_i_pomiarem(
    katalog, katalog_runu
):
    wynik, _ = await uruchom(spec(), atrapa(result(), kod=3), katalog, katalog_runu)

    assert wynik == WynikKroku("nieudany", "kod wyjścia 3", 3, pomiar=POMIAR_PELNY)


async def test_kod_wyjscia_wygrywa_z_podtypem_bledu_agenta(katalog, katalog_runu):
    wynik, _ = await uruchom(
        spec(), atrapa(result(subtype="error_during_execution", is_error=True), kod=1),
        katalog, katalog_runu)

    assert wynik.powod == "kod wyjścia 1"


async def test_kod_wyjscia_bez_zdarzenia_result_jest_porazka_z_kodem_a_nie_z_brakiem_dowodu(
    katalog, katalog_runu
):
    wynik, _ = await uruchom(spec(), atrapa(asystent(tekst("a")), kod=2), katalog, katalog_runu)

    assert wynik == WynikKroku("nieudany", "kod wyjścia 2", 2)


@pytest.mark.parametrize("wyjscie", [
    pytest.param([], id="pusty-stdout"),
    pytest.param([asystent(tekst("tylko tekst"))], id="tylko-tekst-asystenta"),
    pytest.param([linia(""), linia("   ")], id="same-puste-linie"),
    pytest.param(
        [linia(json.dumps({"type": "system", "subtype": "init"}))], id="tylko-system"),
])
async def test_brak_zdarzenia_result_przy_kodzie_zero_jest_brakiem_dowodu_a_nie_sukcesem(
    katalog, katalog_runu, wyjscie
):
    wynik, _ = await uruchom(spec(), atrapa(*wyjscie), katalog, katalog_runu)

    assert wynik == WynikKroku(
        "nieudany",
        str(Naruszenie(Niezmiennik.evidence_required, "brak zdarzenia result w wyjściu agenta")),
        0)
    assert wynik.pomiar == PomiarKroku()


@pytest.mark.parametrize(("nadpisania", "podtyp"), [
    pytest.param({"is_error": True}, "success", id="is_error-przy-podtypie-success"),
    pytest.param({"subtype": "error_during_execution"}, "error_during_execution",
                 id="podtyp-bledu-wykonania-bez-is_error"),
    pytest.param({"subtype": "podtyp_z_przyszlosci"}, "podtyp_z_przyszlosci",
                 id="nieznany-podtyp-bez-is_error"),
])
async def test_agent_zglaszajacy_blad_w_wyniku_konczy_krok_porazka_z_podtypem(
    katalog, katalog_runu, nadpisania, podtyp
):
    wynik, _ = await uruchom(spec(), atrapa(result(**nadpisania)), katalog, katalog_runu)

    assert wynik.status == "nieudany"
    assert wynik.powod == f"agent zakończył się błędem: {podtyp}"
    assert wynik.pomiar == POMIAR_PELNY


async def test_wynik_bez_pola_is_error_z_podtypem_success_jest_sukcesem(katalog, katalog_runu):
    wynik, _ = await uruchom(spec(), atrapa(result(is_error=BRAK)), katalog, katalog_runu)

    assert wynik == WynikKroku("zakonczony", None, 0, pomiar=POMIAR_PELNY)


# =================================================================================================
# 3. Parser NDJSON: niezrozumiala linia, limit linii, zdarzenia pomijane
# =================================================================================================


@pytest.mark.parametrize("surowa", [
    pytest.param("to nie jest json", id="tekst"),
    pytest.param('{"type": "result"', id="json-ucieta"),
    pytest.param('{"foo": 1}', id="obiekt-bez-type"),
    pytest.param('{"type": 5}', id="type-liczba"),
    pytest.param('{"type": null}', id="type-null"),
    pytest.param("[1, 2]", id="tablica"),
    pytest.param("42", id="liczba"),
    pytest.param("null", id="null"),
    pytest.param('"result"', id="napis-json"),
    pytest.param('{"type":"assistant","message":"tekst"}', id="assistant-message-napis"),
    pytest.param('{"type":"assistant","message":{"content":"tekst"}}',
                 id="assistant-content-napis"),
    pytest.param('{"type":"assistant","message":{"content":{"type":"text","text":"x"}}}',
                 id="assistant-content-obiekt"),
])
async def test_niezrozumiala_linia_wyjscia_agenta_konczy_krok_bledem_z_trescia_linii(
    katalog, katalog_runu, surowa
):
    wynik, _ = await uruchom(spec(), atrapa(linia(surowa), result()), katalog, katalog_runu)

    assert wynik.status == "nieudany"
    assert wynik.powod == powod_niezrozumialej(surowa)


async def test_niezrozumiala_linia_w_powodzie_jest_obcieta_do_200_znakow(katalog, katalog_runu):
    surowa = "a" * 200 + "b" * 50

    wynik, _ = await uruchom(spec(), atrapa(linia(surowa)), katalog, katalog_runu)

    assert wynik.powod == powod_niezrozumialej("a" * 200)


async def test_niezrozumiala_linia_wygrywa_z_kodem_wyjscia_agenta(katalog, katalog_runu):
    wynik, _ = await uruchom(spec(), atrapa(linia("smiec"), kod=1), katalog, katalog_runu)

    assert wynik.powod == powod_niezrozumialej("smiec")


async def test_niezrozumiala_linia_wygrywa_z_wyczerpaniem_budzetu(katalog, katalog_runu):
    wynik, _ = await uruchom(
        spec(), atrapa(linia("smiec"), result(subtype="error_max_budget_usd", is_error=True)),
        katalog, katalog_runu)

    assert wynik.powod == powod_niezrozumialej("smiec")


async def test_linia_z_bajtami_niebedacymi_utf8_jest_niezrozumiala_a_nie_wyjatkiem_dekodowania(
    katalog, katalog_runu
):
    wynik, _ = await uruchom(spec(), atrapa(("bajty", "fffe"), result()), katalog, katalog_runu)

    assert wynik.status == "nieudany"
    assert wynik.powod is not None
    assert wynik.powod.startswith("niezrozumiała linia wyjścia agenta: ")


async def test_po_niezrozumialej_linii_runner_czyta_wyjscie_do_konca_i_agent_nie_wisi_na_potoku(
    katalog, katalog_runu
):
    # 3 MB po smieciu: pelny potok (64 KB) zawiesilby agenta, gdyby runner przestal czytac
    system = json.dumps({"type": "system", "wypelniacz": "y" * 1000})

    wynik, _ = await uruchom(
        spec(), atrapa(linia("smiec"), ("powtorz", system, 3000), result()),
        katalog, katalog_runu)

    assert wynik.powod == powod_niezrozumialej("smiec")


async def test_puste_linie_miedzy_zdarzeniami_sa_pomijane_a_nie_niezrozumiale(
    katalog, katalog_runu
):
    wynik, _ = await uruchom(
        spec(), atrapa(linia(""), result(), linia("   "), linia("\t")), katalog, katalog_runu)

    assert wynik == WynikKroku("zakonczony", None, 0, pomiar=POMIAR_PELNY)


@pytest.mark.parametrize("zdarzenie", [
    pytest.param({"type": "system", "subtype": "init", "session_id": "x"}, id="system"),
    pytest.param(
        {"type": "user", "message": {"content": [tekst("NIE EMITUJ")]}}, id="user"),
    pytest.param({"type": "stream_event", "event": {"delta": "NIE EMITUJ"}}, id="stream_event"),
    pytest.param({"type": "rate_limit_event", "info": {}}, id="rate_limit_event"),
    pytest.param({"type": "typ_z_przyszlosci", "message": "zly-ksztalt"}, id="nieznany-typ"),
])
async def test_zdarzenia_innych_rodzajow_sa_pomijane_krok_udany_i_bez_wyjscia(
    katalog, katalog_runu, zdarzenie
):
    wynik, zdarzenia = await uruchom(
        spec(), atrapa(linia(json.dumps(zdarzenie)), result()), katalog, katalog_runu)

    assert wynik == WynikKroku("zakonczony", None, 0, pomiar=POMIAR_PELNY)
    assert zdarzenia == []


async def test_linia_dluzsza_niz_limit_konczy_krok_bledem_z_limitem_w_bajtach(
    katalog, katalog_runu
):
    wynik, _ = await uruchom(
        spec(), atrapa(("x", LIMIT_LINII + 1), result()), katalog, katalog_runu)

    assert wynik.status == "nieudany"
    assert wynik.powod == "linia wyjścia agenta przekracza 8388608 bajtów"


async def test_linia_dokladnie_o_dlugosci_limitu_jest_przyjeta(katalog, katalog_runu):
    wynik, _ = await uruchom(
        spec(), atrapa(("pad", LIMIT_LINII), result()), katalog, katalog_runu)

    assert wynik == WynikKroku("zakonczony", None, 0, pomiar=POMIAR_PELNY)


# =================================================================================================
# 4. Tekst asystenta i stderr jako zdarzenia output
# =================================================================================================


async def test_tekst_asystenta_trafia_do_zdarzen_output_linia_po_linii_a_inne_elementy_sa_pomijane(
    katalog, katalog_runu
):
    narzedzie = {"type": "tool_use", "name": "Bash", "input": {"command": "NIE EMITUJ"}}
    myslenie = {"type": "thinking", "thinking": "NIE EMITUJ"}

    _, zdarzenia = await uruchom(
        spec(),
        atrapa(
            asystent(tekst("pierwsza\ndruga"), narzedzie, tekst("trzecia")),
            asystent(myslenie, tekst("czwarta")),
            result()),
        katalog, katalog_runu)

    assert zdarzenia == [
        zdarzenie_output(t) for t in ["pierwsza", "druga", "trzecia", "czwarta"]]


async def test_stderr_agenta_idzie_jako_output_i_nie_psuje_parsera_strumienia(
    katalog, katalog_runu
):
    wynik, zdarzenia = await uruchom(
        spec(), atrapa(("stderr", "ostrzezenie narzedzia\n"), result()), katalog, katalog_runu)

    assert wynik == WynikKroku("zakonczony", None, 0, pomiar=POMIAR_PELNY)
    assert zdarzenia == [zdarzenie_output("ostrzezenie narzedzia")]


async def test_stderr_wygladajacy_jak_smiec_nie_jest_niezrozumialym_wyjsciem_agenta(
    katalog, katalog_runu
):
    wynik, _ = await uruchom(
        spec(), atrapa(("stderr", "to nie json\n"), result()), katalog, katalog_runu)

    assert wynik.status == "zakonczony"


# =================================================================================================
# 5. Artefakt roli bez zapisu to tekst result.result
# =================================================================================================


async def test_artefakt_recenzenta_to_tekst_wyniku_agenta_a_nie_surowe_ndjson(
    katalog, katalog_runu
):
    plan = "# Plan zażółć\n\n1. zmień A\n"

    wynik, _ = await uruchom(
        spec(Rola.reviewer, ["plan.md"]),
        atrapa(asystent(tekst("rozmowa asystenta, nie artefakt")), result(result=plan)),
        katalog, katalog_runu)

    assert wynik == WynikKroku("zakonczony", None, 0, pomiar=POMIAR_PELNY)
    assert (katalog_runu / KROK / "plan.md").read_bytes() == plan.encode("utf-8")


@pytest.mark.parametrize("zly_wynik", [
    pytest.param(BRAK, id="brak-pola"),
    pytest.param(None, id="null"),
    pytest.param(5, id="liczba"),
    pytest.param(["plan"], id="tablica"),
    pytest.param("", id="pusty-napis"),
    pytest.param("  \n\t", id="same-biale-znaki"),
])
async def test_recenzent_bez_tekstu_wyniku_to_brak_dowodu_a_nie_pusty_artefakt(
    katalog, katalog_runu, zly_wynik
):
    wynik, _ = await uruchom(
        spec(Rola.reviewer, ["plan.md"]), atrapa(result(result=zly_wynik)),
        katalog, katalog_runu)

    assert wynik == WynikKroku("nieudany", EVIDENCE_PUSTY, 0, pomiar=POMIAR_PELNY)
    assert not (katalog_runu / KROK / "plan.md").exists()


async def test_limit_artefaktu_liczy_bajty_utf8_tekstu_wyniku_a_nie_znaki(
    katalog, katalog_runu, monkeypatch
):
    monkeypatch.setattr(modul_kroku, "LIMIT_ARTEFAKTU", 10)

    wynik, _ = await uruchom(
        spec(Rola.reviewer, ["plan.md"]), atrapa(result(result="ą" * 6)),   # 6 znakow, 12 bajtow
        katalog, katalog_runu)

    assert wynik == WynikKroku(
        "nieudany", "wynik agenta przekracza 10 bajtów", 0, pomiar=POMIAR_PELNY)
    assert not (katalog_runu / KROK / "plan.md").exists()


async def test_tekst_wyniku_dokladnie_o_dlugosci_limitu_artefaktu_jest_przyjety(
    katalog, katalog_runu, monkeypatch
):
    monkeypatch.setattr(modul_kroku, "LIMIT_ARTEFAKTU", 10)

    wynik, _ = await uruchom(
        spec(Rola.reviewer, ["plan.md"]), atrapa(result(result="ą" * 5)),   # 10 bajtow
        katalog, katalog_runu)

    assert wynik.status == "zakonczony"
    assert (katalog_runu / KROK / "plan.md").read_bytes() == ("ą" * 5).encode("utf-8")


async def test_autor_w_trybie_agenta_sam_zapisuje_artefakt_a_tekst_wyniku_go_nie_nadpisuje(
    katalog, katalog_runu
):
    wynik, _ = await uruchom(
        spec(Rola.author, ["plan.md"]),
        atrapa(("zapisz", "plan.md", "tresc autora"), result(result="tekst wyniku")),
        katalog, katalog_runu)

    assert wynik.status == "zakonczony"
    assert (katalog / "plan.md").read_text(encoding="utf-8") == "tresc autora"
    assert not (katalog_runu / KROK / "plan.md").exists()


async def test_autor_w_trybie_agenta_bez_zapisanego_artefaktu_nie_dostaje_go_z_tekstu_wyniku(
    katalog, katalog_runu
):
    wynik, _ = await uruchom(
        spec(Rola.author, ["plan.md"]), atrapa(result(result="tekst wyniku")),
        katalog, katalog_runu)

    assert wynik == WynikKroku("nieudany", "brak artefaktu plan.md", 0, pomiar=POMIAR_PELNY)


ZMIENIONE_LINIE_NOTATKI = [{"plik": "src/a.py", "linie": [[10, 12]]}]
NOTATKA_ZMIANY = (
    "## Co zmieniono\n\nRun ma nowy stan.\n\n"
    "## Jakie ryzyko\n\n{ryzyko}\n\n"
    "## Jak to sprawdzić\n\nOtwórz ekran odbioru i sprawdź stan.\n"
)


async def uruchom_notatke(ryzyko: str, katalog: Path, katalog_runu: Path, tmp_path: Path):
    plik_linii = tmp_path / "zmienione-linie.json"
    plik_linii.write_text(json.dumps(ZMIENIONE_LINIE_NOTATKI), encoding="utf-8")

    async def emit(zdarzenie: dict) -> None:
        pass

    return await asyncio.wait_for(wykonaj_krok(
        spec(Rola.reviewer, ["notatka-zmiany.md"]),
        atrapa(result(result=NOTATKA_ZMIANY.format(ryzyko=ryzyko))),
        katalog, emit, katalog_runu=katalog_runu, prompt="prompt kroku",
        zmienione_linie=plik_linii), LIMIT_S)


async def test_notatka_zmiany_z_wyniku_agenta_konczy_krok_sukcesem_z_kotwicami_i_pomiarem(
    tmp_path, katalog, katalog_runu
):
    wynik = await uruchom_notatke(
        "- [src/a.py:11] scenariusz: Run przerwany w zapisie zostawi plik bez werdyktu.",
        katalog, katalog_runu, tmp_path)

    assert wynik == WynikKroku(
        "zakonczony", None, 0, pomiar=POMIAR_PELNY, kotwice=("src/a.py:11",))


async def test_porazka_kotwic_notatki_zmiany_niesie_pomiar_kroku_agenta(
    tmp_path, katalog, katalog_runu
):
    wynik = await uruchom_notatke(
        "- [src/a.py:90] scenariusz: Cos sie zepsuje.", katalog, katalog_runu, tmp_path)

    assert wynik == WynikKroku(
        "nieudany",
        "niezmiennik evidence_required: kotwica src/a.py:90: linia poza zmienionymi liniami "
        "(tolerancja 3)",
        0, pomiar=POMIAR_PELNY)


# =================================================================================================
# 6. Prompt: plik w katalogu runu jako stdin agenta, nigdy argv
# =================================================================================================


async def test_prompt_trafia_do_stdin_agenta_a_nie_do_jego_argv(tmp_path, katalog, katalog_runu):
    prompt = "przeczytaj coś\nzażółć gęślą\n"
    zapis = tmp_path / "wejscie-agenta.json"

    wynik, _ = await uruchom(
        spec(), atrapa(result(), zapis=zapis, dodatkowe=["--flaga-polecenia"]),
        katalog, katalog_runu, prompt=prompt)

    assert wynik.status == "zakonczony"
    wejscie = json.loads(zapis.read_text(encoding="utf-8"))
    assert wejscie["stdin"] == prompt
    assert wejscie["argv"] == ["--flaga-polecenia"]


async def test_prompt_lezy_w_pliku_prompty_krok_txt_w_katalogu_runu_bajt_w_bajt(
    katalog, katalog_runu
):
    prompt = "bez konca linii, zażółć"

    await uruchom(spec(), atrapa(result()), katalog, katalog_runu, prompt=prompt)

    plik = katalog_runu / "prompty" / f"{KROK}.txt"
    assert plik.read_bytes() == prompt.encode("utf-8")


async def test_plik_promptu_kolejnego_kroku_o_tym_samym_id_zastepuje_poprzedni(
    tmp_path, katalog, katalog_runu
):
    await uruchom(spec(), atrapa(result()), katalog, katalog_runu, prompt="pierwszy")
    zapis = tmp_path / "wejscie.json"

    await uruchom(spec(), atrapa(result(), zapis=zapis), katalog, katalog_runu, prompt="drugi")

    assert (katalog_runu / "prompty" / f"{KROK}.txt").read_text(encoding="utf-8") == "drugi"
    assert json.loads(zapis.read_text(encoding="utf-8"))["stdin"] == "drugi"


async def test_pusty_prompt_nadal_oznacza_tryb_agenta_z_parserem_strumienia(
    katalog, katalog_runu
):
    wynik, _ = await uruchom(
        spec(), atrapa(linia("to nie json")), katalog, katalog_runu, prompt="")

    assert wynik.powod == powod_niezrozumialej("to nie json")
    assert (katalog_runu / "prompty" / f"{KROK}.txt").read_bytes() == b""


async def test_tryb_agenta_bez_katalogu_runu_to_porazka_i_agent_nie_rusza(tmp_path, katalog):
    znacznik = tmp_path / "start.znacznik"

    wynik, _ = await uruchom(spec(), atrapa(result(), znacznik=znacznik), katalog, None)

    assert wynik == WynikKroku("nieudany", "brak katalogu runu dla pliku promptu", None)
    assert not znacznik.exists()


@pytest.mark.parametrize("zly_krok", ["..", ".", "", "a/b", "a\\b", "../poza"])
async def test_niedozwolony_identyfikator_kroku_blokuje_zapis_promptu_i_start_agenta(
    tmp_path, katalog, katalog_runu, zly_krok
):
    znacznik = tmp_path / "start.znacznik"

    wynik, _ = await uruchom(
        spec(step_id=zly_krok), atrapa(result(), znacznik=znacznik), katalog, katalog_runu)

    assert wynik == WynikKroku(
        "nieudany", f"niedozwolony identyfikator kroku: {zly_krok!r}", None)
    assert not znacznik.exists()
    assert [p for p in tmp_path.rglob("*.txt")] == []


async def test_katalog_runu_w_repozytorium_blokuje_zapis_promptu_i_start_agenta(
    tmp_path, katalog
):
    w_repo = katalog / ".cockpit" / "run"
    w_repo.mkdir(parents=True)
    znacznik = tmp_path / "start.znacznik"

    wynik, _ = await uruchom(spec(), atrapa(result(), znacznik=znacznik), katalog, w_repo)

    assert wynik.status == "nieudany"
    assert wynik.kod_wyjscia is None
    assert wynik.powod is not None
    assert wynik.powod.startswith("niezmiennik run_dir_outside_repo: ")
    assert not znacznik.exists()
    assert [p for p in katalog.rglob("*") if p.is_file()] == []


async def test_nieudany_zapis_pliku_promptu_to_porazka_z_przyczyna_systemu_i_bez_startu_agenta(
    tmp_path, katalog, katalog_runu
):
    (katalog_runu / "prompty").write_text("zwykly plik zamiast katalogu", encoding="utf-8")
    znacznik = tmp_path / "start.znacznik"

    wynik, _ = await uruchom(spec(), atrapa(result(), znacznik=znacznik), katalog, katalog_runu)

    assert wynik.status == "nieudany"
    assert wynik.kod_wyjscia is None
    assert wynik.powod is not None
    assert wynik.powod.startswith("nie można zapisać pliku promptu: ")
    assert not znacznik.exists()


async def test_straznik_dostaje_plik_promptu_w_stdin_plik_przed_separatorem_i_osobne_potoki(
    katalog, katalog_runu, monkeypatch
):
    zapis: dict = {}

    async def szpieg(*argv, **kwargs):
        zapis["argv"], zapis["kwargs"] = list(argv), kwargs
        zapis["plik_istnial"] = (katalog_runu / "prompty" / f"{KROK}.txt").is_file()
        raise OSError("koniec-har174")

    monkeypatch.setattr(modul_kroku.asyncio, "create_subprocess_exec", szpieg)

    await uruchom(spec(), ["claude", "-p"], katalog, katalog_runu)

    argv, kwargs = zapis["argv"], zapis["kwargs"]
    plik = str(katalog_runu / "prompty" / f"{KROK}.txt")
    assert argv[argv.index("--stdin-plik") + 1] == plik
    assert argv.index("--stdin-plik") < argv.index("--")
    assert argv[argv.index("--") + 1:] == ["claude", "-p"]
    assert zapis["plik_istnial"] is True
    assert kwargs["stdout"] == asyncio.subprocess.PIPE
    assert kwargs["stderr"] == asyncio.subprocess.PIPE


async def test_brak_pliku_promptu_przy_starcie_straznika_to_porazka_startu_polecenia(
    tmp_path, katalog, katalog_runu, monkeypatch
):
    monkeypatch.setattr(modul_kroku, "_zapisz_atomowo", lambda cel, tresc: None)
    znacznik = tmp_path / "start.znacznik"

    wynik, _ = await uruchom(spec(), atrapa(result(), znacznik=znacznik), katalog, katalog_runu)

    assert wynik.status == "nieudany"
    assert wynik.kod_wyjscia is None
    assert wynik.powod is not None
    assert wynik.powod.startswith("nie można uruchomić polecenia: ")
    assert str(katalog_runu / "prompty" / f"{KROK}.txt") in wynik.powod
    assert not znacznik.exists()


async def test_krok_bez_promptu_zachowuje_surowe_wyjscie_bez_parsera_i_bez_pliku_promptu(
    tmp_path, katalog, katalog_runu
):
    zapis = tmp_path / "wejscie.json"

    wynik, zdarzenia = await uruchom(
        spec(), atrapa(linia("to nie json"), linia('{"type": "result"}'), zapis=zapis),
        katalog, katalog_runu, prompt=None)

    assert wynik == WynikKroku("zakonczony", None, 0)
    assert wynik.pomiar == PomiarKroku()
    assert [z["text"] for z in zdarzenia] == ["to nie json", '{"type": "result"}']
    assert not (katalog_runu / "prompty").exists()
    assert json.loads(zapis.read_text(encoding="utf-8"))["stdin"] == ""


# =================================================================================================
# 7. Straznik: --stdin-plik
# =================================================================================================

KOD_ECHO_STDIN = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"


def _srodowisko_straznika() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(KATALOG_PAKIETU)}


async def _straznik(katalog: Path, opcje: list[str], kod_agenta: str, **kw):
    return await uruchom_proces(
        sys.executable, "-m", "runner.straznik", *opcje, "--cwd", str(katalog), "--",
        sys.executable, "-I", "-c", kod_agenta,
        cwd=KATALOG_KORZENIA, env=_srodowisko_straznika(), stdin=asyncio.subprocess.PIPE, **kw)


async def _wyjscie_agenta_pod_straznikiem(katalog: Path, opcje: list[str]) -> bytes:
    proc = await _straznik(katalog, opcje, KOD_ECHO_STDIN, stdout=asyncio.subprocess.PIPE)
    try:
        wyjscie = await czekaj(proc.stdout.read(), 30, "stdout agenta pod straznikiem")
        assert await czekaj(proc.wait(), 30, "koniec straznika") == 0
    finally:
        proc.stdin.close()
        await zakoncz_proces(proc, nazwa="straznik")
    return wyjscie


async def test_agent_pod_straznikiem_dostaje_na_stdin_zawartosc_pliku_bajt_w_bajt(
    tmp_path, katalog
):
    plik = tmp_path / "prompt.txt"
    tresc = b"\xff\x00linia\r\n\xc4\x85 bez konca"
    plik.write_bytes(tresc)

    wyjscie = await _wyjscie_agenta_pod_straznikiem(katalog, ["--stdin-plik", str(plik)])

    assert wyjscie == tresc


async def test_agent_pod_straznikiem_bez_pliku_stdin_dostaje_pusty_stdin(katalog):
    wyjscie = await _wyjscie_agenta_pod_straznikiem(katalog, [])

    assert wyjscie == b""


async def _blad_startu_z_kanalu_statusu(katalog: Path, plik: Path) -> tuple[int, str]:
    odczyt, zapis = os.pipe()
    znacznik = katalog.parent / "agent-wystartowal.znacznik"
    kod_agenta = f"open({str(znacznik)!r}, 'w').close()"
    try:
        proc = await _straznik(
            katalog, ["--status-fd", str(zapis), "--stdin-plik", str(plik)], kod_agenta,
            stdout=asyncio.subprocess.DEVNULL, pass_fds=(zapis,))
    finally:
        os.close(zapis)
    try:
        kod = await czekaj(proc.wait(), 30, "koniec straznika po bledzie startu")
        dane = b""
        while kawalek := os.read(odczyt, 4096):
            dane += kawalek
    finally:
        os.close(odczyt)
        proc.stdin.close()
        await zakoncz_proces(proc, nazwa="straznik")
    assert not znacznik.exists()
    return kod, os.fsdecode(dane)


async def test_nieistniejacy_plik_stdin_jest_bledem_startu_zgloszonym_kanalem_statusu(
    tmp_path, katalog
):
    brak = tmp_path / "nie-ma-promptu.txt"

    kod, status = await _blad_startu_z_kanalu_statusu(katalog, brak)

    assert kod == 127
    assert str(brak) in status
    assert "No such file or directory" in status


async def test_katalog_zamiast_pliku_stdin_jest_bledem_startu_zgloszonym_kanalem_statusu(
    tmp_path, katalog
):
    katalog_zamiast_pliku = tmp_path / "katalog-promptu"
    katalog_zamiast_pliku.mkdir()

    kod, status = await _blad_startu_z_kanalu_statusu(katalog, katalog_zamiast_pliku)

    assert kod == 127
    assert status == f"[Errno 21] Is a directory: {str(katalog_zamiast_pliku)!r}"


async def test_nieistniejacy_plik_stdin_bez_kanalu_statusu_idzie_na_stderr_z_kodem_127(
    tmp_path, katalog
):
    brak = tmp_path / "nie-ma-promptu.txt"
    proc = await _straznik(
        katalog, ["--stdin-plik", str(brak)], "pass",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        blad = await czekaj(proc.stderr.read(), 30, "stderr straznika")
        kod = await czekaj(proc.wait(), 30, "koniec straznika")
    finally:
        proc.stdin.close()
        await zakoncz_proces(proc, nazwa="straznik")

    assert kod == 127
    assert blad.decode().startswith("straznik: nie można uruchomić polecenia: ")
    assert str(brak) in blad.decode()


# =================================================================================================
# 8. Silnik: prompt, budzet i limit tur z workflow, pomiar w zdarzeniach, wyjatki polecenia
# =================================================================================================

POLA_POMIARU_ZDARZENIA = ("cost_usd", "cost_source", "tokens_in", "tokens_out", "num_turns",
                          "session_id")


def pomiar_zdarzenia(zdarzenie: dict) -> dict:
    return {pole: zdarzenie[pole] for pole in POLA_POMIARU_ZDARZENIA}


BRAK_POMIARU_W_ZDARZENIU = {"cost_usd": None, "cost_source": "unavailable", "tokens_in": None,
                            "tokens_out": None, "num_turns": None, "session_id": None}


async def test_koniec_kroku_niesie_zmierzony_koszt_tokeny_tury_i_sesje_ze_zrodlem_measured(
    tmp_path,
):
    s = swiat_silnika(tmp_path, [K("a")])
    agent = Szpieg(wyniki={"a": WynikKroku("zakonczony", None, 0, pomiar=POMIAR_PELNY)})

    zdarzenia = await uruchom_run(s, agent)

    assert pomiar_zdarzenia(jedno(zdarzenia, "step_finished", "a")) == {
        "cost_usd": 0.0123, "cost_source": "measured", "tokens_in": 1234, "tokens_out": 567,
        "num_turns": 7, "session_id": "sesja-abc"}


async def test_koniec_kroku_bez_pomiaru_ma_koszt_null_ze_zrodlem_unavailable_a_nie_zero(tmp_path):
    s = swiat_silnika(tmp_path, [K("a")])

    zdarzenia = await uruchom_run(s, Szpieg())

    assert pomiar_zdarzenia(jedno(zdarzenia, "step_finished", "a")) == BRAK_POMIARU_W_ZDARZENIU


async def test_zmierzony_koszt_zero_jest_zrodlem_measured_a_nie_brakiem_pomiaru(tmp_path):
    s = swiat_silnika(tmp_path, [K("a")])
    pomiar = PomiarKroku(cost_usd=0.0)
    agent = Szpieg(wyniki={"a": WynikKroku("zakonczony", None, 0, pomiar=pomiar)})

    zdarzenia = await uruchom_run(s, agent)

    zdarzenie = jedno(zdarzenia, "step_finished", "a")
    assert zdarzenie["cost_usd"] == 0.0
    assert zdarzenie["cost_usd"] is not None
    assert zdarzenie["cost_source"] == "measured"


async def test_pomiar_jednego_kroku_nie_przecieka_do_zdarzenia_innego_kroku(tmp_path):
    s = swiat_silnika(tmp_path, [K("a"), K("b")])
    agent = Szpieg(wyniki={"a": WynikKroku("zakonczony", None, 0, pomiar=POMIAR_PELNY)})

    zdarzenia = await uruchom_run(s, agent)

    assert pomiar_zdarzenia(jedno(zdarzenia, "step_finished", "a"))["cost_usd"] == 0.0123
    assert pomiar_zdarzenia(jedno(zdarzenia, "step_finished", "b")) == BRAK_POMIARU_W_ZDARZENIU


async def test_porazka_kroku_niesie_pomiar_poniesiony_do_chwili_porazki(tmp_path):
    s = swiat_silnika(tmp_path, [K("a")])
    pomiar = dataclasses.replace(POMIAR_PELNY, cost_usd=0.5)
    agent = Szpieg(wyniki={"a": WynikKroku(
        "nieudany", "budżet kroku wyczerpany (error_max_budget_usd)", 1, pomiar=pomiar)})

    zdarzenia = await uruchom_run(s, agent)

    zdarzenie = jedno(zdarzenia, "step_failed", "a")
    assert zdarzenie["text"] == "budżet kroku wyczerpany (error_max_budget_usd)"
    assert pomiar_zdarzenia(zdarzenie) == {
        "cost_usd": 0.5, "cost_source": "measured", "tokens_in": 1234, "tokens_out": 567,
        "num_turns": 7, "session_id": "sesja-abc"}


async def test_porazka_kroku_bez_pomiaru_ma_koszt_null_ze_zrodlem_unavailable(tmp_path):
    s = swiat_silnika(tmp_path, [K("a")])
    agent = Szpieg(wyniki={"a": WynikKroku("nieudany", "kod wyjścia 1", 1)})

    zdarzenia = await uruchom_run(s, agent)

    assert pomiar_zdarzenia(jedno(zdarzenia, "step_failed", "a")) == BRAK_POMIARU_W_ZDARZENIU


async def test_krok_ktory_nie_ruszyl_z_powodu_profilu_ma_pusty_pomiar_w_zdarzeniu_porazki(tmp_path):
    profil = ProfilBackendu(modele={Tier.execution: "m-execution"})        # brak judgment
    s = swiat_silnika(tmp_path, [K("a")], profil=profil)
    agent = Szpieg()

    zdarzenia = await uruchom_run(s, agent)

    assert pomiar_zdarzenia(jedno(zdarzenia, "step_failed", "a")) == BRAK_POMIARU_W_ZDARZENIU
    assert agent.wywolania == []


async def test_budzet_i_limit_tur_kroku_z_workflow_trafiaja_do_specyfikacji_i_polecenia_agenta(
    tmp_path,
):
    s = swiat_silnika(tmp_path, [
        K("a", extra=", budget_usd: 0.5, max_turns: 3"),
        K("b"),
        K("c", extra=", budget_usd: 5"),
    ])
    agent = Szpieg()

    await uruchom_run(s, agent)

    a, b, c = (agent.spec(i) for i in "abc")
    assert (a.budget_usd, a.max_turns) == (0.5, 3)
    assert (b.budget_usd, b.max_turns) == (None, None)
    assert (c.budget_usd, c.max_turns) == (5.0, None)
    assert isinstance(c.budget_usd, float)
    polecenia = {w.spec.step_id: w.polecenie for w in agent.wywolania}
    assert polecenia["a"][polecenia["a"].index("--max-budget-usd") + 1] == "0.5"
    assert polecenia["a"][polecenia["a"].index("--max-turns") + 1] == "3"
    assert "--max-budget-usd" not in polecenia["b"] and "--max-turns" not in polecenia["b"]
    assert polecenia["c"][polecenia["c"].index("--max-budget-usd") + 1] == "5.0"
    assert "--max-turns" not in polecenia["c"]


async def test_silnik_przekazuje_agentowi_prompt_kroku_osobno_od_polecenia(tmp_path):
    s = swiat_silnika(tmp_path, [K("a")])
    agent = Szpieg()

    await uruchom_run(s, agent)

    (wywolanie,) = agent.wywolania
    assert wywolanie.prompt == zbuduj_prompt(wywolanie.spec)
    assert ".cockpit/_run/skills/s_a/SKILL.md" in wywolanie.prompt
    assert wywolanie.prompt not in wywolanie.polecenie
    assert not any("SKILL.md" in czesc for czesc in wywolanie.polecenie)


async def test_niepoprawne_polecenie_agenta_konczy_krok_porazka_z_komunikatem_a_run_idzie_dalej(
    tmp_path, monkeypatch
):
    from runner import agent as modul_agenta

    def polecenie_ktore_odrzuca_krok_a(spec_kroku, profil, ustawienia=None):
        if spec_kroku.step_id == "a":
            raise ValueError("budżet z testu jest niepoprawny")
        return modul_agenta.polecenie_agenta(spec_kroku, profil, ustawienia)

    monkeypatch.setattr("runner.silnik.polecenie_agenta", polecenie_ktore_odrzuca_krok_a)
    s = swiat_silnika(tmp_path, [K("a"), K("b")])
    agent = Szpieg()

    zdarzenia = await uruchom_run(s, agent)

    assert jedno(zdarzenia, "step_failed", "a")["text"] == "budżet z testu jest niepoprawny"
    jedno(zdarzenia, "step_finished", "b")
    assert agent.kroki == ["b"]
    assert zdarzenia[-1]["kind"] == "run_finished"


# --- cala sciezka runner: prawdziwy wykonaj_krok, prawdziwe polecenie_agenta, atrapa claude ---


@dataclass
class AgentZAtrapaClaude:
    """Silnik -> prawdziwe `wykonaj_krok` w trybie agenta -> atrapa `claude` z argv z `polecenie_agenta`."""

    zapisy: Path
    wyjscia: dict[str, list[tuple]] = field(default_factory=dict)

    async def __call__(self, spec_kroku, polecenie, katalog, emit, *, katalog_runu=None,
                       prompt=None):
        wyjscie = self.wyjscia.get(spec_kroku.step_id, [result()])
        opis = {"wyjscie": [list(w) for w in wyjscie], "kod": 0,
                "zapis_wejscia": str(self.zapisy / f"{spec_kroku.step_id}.json")}
        argv = [sys.executable, "-I", "-c", KOD_CLAUDE, json.dumps(opis), *polecenie[1:]]
        return await wykonaj_krok(
            spec_kroku, argv, katalog, emit, katalog_runu=katalog_runu, prompt=prompt)

    def wejscie(self, krok_id: str) -> dict:
        return json.loads((self.zapisy / f"{krok_id}.json").read_text(encoding="utf-8"))


def _agent_z_atrapa(tmp_path: Path, **wyjscia) -> AgentZAtrapaClaude:
    zapisy = tmp_path / "zapisy-atrapy"
    zapisy.mkdir()
    return AgentZAtrapaClaude(zapisy, wyjscia)


async def test_prompt_agenta_nie_wystepuje_w_argv_procesu_a_jest_w_jego_stdin_i_pliku_runu(tmp_path):
    s = swiat_silnika(tmp_path, [K("a", extra=", budget_usd: 0.5, max_turns: 3")])
    agent = _agent_z_atrapa(tmp_path)

    zdarzenia = await uruchom_run(s, agent)

    jedno(zdarzenia, "step_finished", "a")
    wejscie = agent.wejscie("a")
    assert wejscie["argv"] == [
        "-p", "--model", "m-judgment", "--output-format", "stream-json", "--verbose",
        "--max-budget-usd", "0.5", "--max-turns", "3"]
    assert wejscie["stdin"].startswith("przeczytaj .cockpit/_run/skills/s_a/SKILL.md")
    assert (s.katalog_runu / "prompty" / "a.txt").read_text(encoding="utf-8") == wejscie["stdin"]


async def test_krok_z_atrapa_wypisujaca_result_ma_pomiar_w_zdarzeniu_konca_kroku(tmp_path):
    s = swiat_silnika(tmp_path, [K("a")])

    zdarzenia = await uruchom_run(s, _agent_z_atrapa(tmp_path))

    assert pomiar_zdarzenia(jedno(zdarzenia, "step_finished", "a")) == {
        "cost_usd": 0.0123, "cost_source": "measured", "tokens_in": 1234, "tokens_out": 567,
        "num_turns": 7, "session_id": "sesja-abc"}


async def test_krok_z_atrapa_wyczerpujaca_budzet_konczy_sie_porazka_z_nazwanym_powodem(tmp_path):
    s = swiat_silnika(tmp_path, [K("a", extra=", budget_usd: 0.5"), K("b")])
    agent = _agent_z_atrapa(
        tmp_path, a=[result(subtype="error_max_budget_usd", is_error=True, total_cost_usd=0.51)])

    zdarzenia = await uruchom_run(s, agent)

    zdarzenie = jedno(zdarzenia, "step_failed", "a")
    assert zdarzenie["text"] == "budżet kroku wyczerpany (error_max_budget_usd)"
    assert zdarzenie["cost_usd"] == 0.51
    assert zdarzenie["cost_source"] == "measured"
    jedno(zdarzenia, "step_finished", "b")


async def test_krok_z_atrapa_wypisujaca_smiec_konczy_sie_porazka_a_nie_sukcesem(tmp_path):
    s = swiat_silnika(tmp_path, [K("a")])
    agent = _agent_z_atrapa(tmp_path, a=[linia("zupelnie nie json"), result()])

    zdarzenia = await uruchom_run(s, agent)

    assert jedno(zdarzenia, "step_failed", "a")["text"] == powod_niezrozumialej("zupelnie nie json")
    assert "step_finished" not in rodzaje(zdarzenia)


# =================================================================================================
# 9. Kokpit: zdarzenie -> wiersz StepRun w bazie (asercje czytaja baze)
# =================================================================================================


@pytest.fixture
async def kokpit(swiat):  # noqa: F811
    yield swiat


POMIAR_W_ZDARZENIU = {"cost_usd": 0.5, "cost_source": "measured", "tokens_in": 100,
                      "tokens_out": 50, "num_turns": 4, "session_id": "sesja-kokpitu"}
POMIAR_W_BAZIE = (0.5, 100, 50, 4, "sesja-kokpitu")
BRAK_POMIARU_W_BAZIE = (None, None, None, None, None)


async def _pomiar_w_bazie(run_id: str, step_id: str = "s0") -> tuple:
    async with db_session.SessionLocal() as s:
        w = (await s.execute(
            select(StepRun).where(StepRun.run_id == run_id, StepRun.step_id == step_id)
        )).scalar_one()
    return (w.cost_usd, w.tokens_in, w.tokens_out, w.num_turns, w.session_id)


@pytest.mark.parametrize("kind", ["step_finished", "step_failed"])
async def test_zdarzenie_konca_kroku_z_pomiarem_zapisuje_go_w_wierszu_kroku(kokpit, kind):
    await _ok(kokpit, kind, text="boom", **POMIAR_W_ZDARZENIU)

    assert await _pomiar_w_bazie(kokpit.run_id) == POMIAR_W_BAZIE


@pytest.mark.parametrize("kind", ["step_finished", "step_failed"])
async def test_zdarzenie_konca_kroku_po_starcie_kroku_aktualizuje_pomiar_istniejacego_wiersza(
    kokpit, kind
):
    await _ok(kokpit, "step_started")

    await _ok(kokpit, kind, text="boom", **POMIAR_W_ZDARZENIU)

    assert await _pomiar_w_bazie(kokpit.run_id) == POMIAR_W_BAZIE


@pytest.mark.parametrize("kind", ["step_finished", "step_failed"])
async def test_zdarzenie_konca_kroku_bez_pomiaru_zostawia_w_bazie_null_a_nie_zero(kokpit, kind):
    await _ok(kokpit, kind, text="boom")

    assert await _pomiar_w_bazie(kokpit.run_id) == BRAK_POMIARU_W_BAZIE


async def test_zmierzony_koszt_zero_jest_w_bazie_zerem_a_brak_kosztu_nullem(kokpit):
    await _ok(kokpit, "step_finished", step_id="zmierzone", cost_usd=0.0, cost_source="measured")
    await _ok(kokpit, "step_finished", step_id="niezmierzone")

    assert (await _pomiar_w_bazie(kokpit.run_id, "zmierzone"))[0] == 0.0
    assert (await _pomiar_w_bazie(kokpit.run_id, "zmierzone"))[0] is not None
    assert (await _pomiar_w_bazie(kokpit.run_id, "niezmierzone"))[0] is None


async def test_pomiar_jednego_kroku_nie_trafia_do_wiersza_innego_kroku_tego_samego_runu(kokpit):
    await _ok(kokpit, "step_finished", step_id="zmierzony", **POMIAR_W_ZDARZENIU)
    await _ok(kokpit, "step_finished", step_id="inny")

    assert await _pomiar_w_bazie(kokpit.run_id, "zmierzony") == POMIAR_W_BAZIE
    assert await _pomiar_w_bazie(kokpit.run_id, "inny") == BRAK_POMIARU_W_BAZIE


@pytest.mark.parametrize("kind", ["step_started", "step_skipped", "step_waiting", "step_aborted"])
async def test_zdarzenie_innego_rodzaju_niz_koniec_kroku_nie_zapisuje_pomiaru_ze_zdarzenia(
    kokpit, kind
):
    await _ok(kokpit, kind, text="powod", **POMIAR_W_ZDARZENIU)

    assert await _pomiar_w_bazie(kokpit.run_id) == BRAK_POMIARU_W_BAZIE


@pytest.mark.parametrize("kind", ["step_started", "step_skipped", "step_waiting", "step_aborted"])
async def test_zdarzenie_innego_rodzaju_po_koncu_kroku_nie_zeruje_zapisanego_pomiaru(
    kokpit, kind
):
    await _ok(kokpit, "step_finished", **POMIAR_W_ZDARZENIU)

    await _ok(kokpit, kind, text="powod")

    assert await _pomiar_w_bazie(kokpit.run_id) == POMIAR_W_BAZIE


async def test_kolejne_zdarzenie_konca_kroku_bez_pomiaru_zastepuje_stary_pomiar_brakiem(kokpit):
    await _ok(kokpit, "step_failed", text="boom", **POMIAR_W_ZDARZENIU)

    await _ok(kokpit, "step_finished")

    assert await _pomiar_w_bazie(kokpit.run_id) == BRAK_POMIARU_W_BAZIE


@pytest.mark.parametrize(
    "istniejacy_wiersz", [False, True], ids=["nowy-wiersz", "wiersz-po-starcie"])
async def test_zastosowanie_zdarzenia_konca_kroku_bez_podanego_pomiaru_zapisuje_brak_pomiaru(
    kokpit, istniejacy_wiersz
):
    from cockpit.services.kroki import zastosuj_zdarzenie_kroku

    if istniejacy_wiersz:
        await _ok(kokpit, "step_started")
    async with db_session.SessionLocal() as s:
        run = await s.get(Run, kokpit.run_id)
        await zastosuj_zdarzenie_kroku(s, run, "step_finished", "s0", "", 1, "")
        await s.commit()

    assert await _pomiar_w_bazie(kokpit.run_id) == BRAK_POMIARU_W_BAZIE


async def test_zapis_zdarzenia_konca_kroku_przez_serwis_bez_podanego_pomiaru_zostawia_null(
    kokpit,
):
    from cockpit.services import runs

    async with db_session.SessionLocal() as s:
        run = await s.get(Run, kokpit.run_id)
        await runs.zapisz_zdarzenie(
            s, run, event_id=str(uuid.uuid4()), kind="step_finished", step_id="s0",
            agent_id="", text="")

    assert await _pomiar_w_bazie(kokpit.run_id) == BRAK_POMIARU_W_BAZIE


@pytest.mark.parametrize("literal", [
    pytest.param("-0.01", id="ujemny-ulamek"),
    pytest.param("-1", id="ujemny-calkowity"),
    pytest.param("Infinity", id="nieskonczonosc"),
    pytest.param("-Infinity", id="minus-nieskonczonosc"),
    pytest.param("NaN", id="nie-liczba"),
])
async def test_koszt_zdarzenia_ujemny_nieskonczony_albo_nie_liczba_jest_odrzucony_i_nic_nie_zapisuje(
    kokpit, literal
):
    cialo = {"run_id": kokpit.run_id, "kind": "step_finished", "step_id": "s0",
             "event_id": str(uuid.uuid4()), "text": "", "cost_usd": "__KOSZT__"}
    surowe = json.dumps(cialo).replace('"__KOSZT__"', literal)   # JSON z literalem spoza standardu

    odpowiedz = await kokpit.klient.post(
        "/api/events", content=surowe, headers={**NAGLOWEK, "Content-Type": "application/json"})

    assert odpowiedz.status_code == 422
    async with db_session.SessionLocal() as s:
        wiersze = (await s.execute(select(func.count()).select_from(StepRun))).scalar_one()
        zdarzenia = (await s.execute(select(func.count()).select_from(Event))).scalar_one()
    assert (wiersze, zdarzenia) == (0, 0)


@pytest.mark.parametrize(("literal", "napis"), [
    pytest.param("Infinity", "inf", id="nieskonczonosc"),
    pytest.param("-Infinity", "-inf", id="minus-nieskonczonosc"),
    pytest.param("NaN", "nan", id="nie-liczba"),
])
async def test_odrzucony_koszt_nieskonczony_albo_nie_liczba_niesie_w_odpowiedzi_input_jako_napis(
    kokpit, literal, napis
):
    cialo = {"run_id": kokpit.run_id, "kind": "step_finished", "step_id": "s0",
             "event_id": str(uuid.uuid4()), "text": "", "cost_usd": "__KOSZT__"}
    surowe = json.dumps(cialo).replace('"__KOSZT__"', literal)

    odpowiedz = await kokpit.klient.post(
        "/api/events", content=surowe, headers={**NAGLOWEK, "Content-Type": "application/json"})

    assert odpowiedz.status_code == 422
    (blad,) = odpowiedz.json()["detail"]
    assert blad["loc"] == ["body", "cost_usd"]
    assert blad["input"] == napis


async def test_rejestracja_obslugi_bledow_w_nowej_aplikacji_zamienia_nieskonczonosc_w_422_z_napisem():
    import httpx
    from fastapi import FastAPI

    from cockpit.api.bledy import zarejestruj_obsluge_bledow

    nowa = FastAPI()
    zarejestruj_obsluge_bledow(nowa)

    @nowa.post("/zdarzenie")
    async def zdarzenie(body: EventIn) -> dict:
        return {}

    surowe = json.dumps({"run_id": "r", "kind": "step_finished", "step_id": "s0",
                         "event_id": "e", "text": "", "cost_usd": "__KOSZT__"}
                        ).replace('"__KOSZT__"', "Infinity")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=nowa), base_url="http://nowa"
    ) as klient:
        odpowiedz = await klient.post(
            "/zdarzenie", content=surowe, headers={"Content-Type": "application/json"})

    assert odpowiedz.status_code == 422
    (blad,) = odpowiedz.json()["detail"]
    assert blad["loc"] == ["body", "cost_usd"]
    assert blad["input"] == "inf"


async def test_blad_walidacji_zdarzenia_ma_ten_sam_ksztalt_co_domyslna_odpowiedz_fastapi(kokpit):
    import httpx
    from fastapi import FastAPI

    referencyjna = FastAPI()

    @referencyjna.post("/zdarzenie")
    async def zdarzenie(body: EventIn) -> dict:
        return {}

    cialo = {"run_id": kokpit.run_id, "kind": "step_finished", "step_id": "s0",
             "event_id": str(uuid.uuid4()), "text": "", "tokens_in": -1}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=referencyjna), base_url="http://referencja"
    ) as klient:
        domyslna = await klient.post("/zdarzenie", json=cialo)

    nasza = await kokpit.klient.post("/api/events", json=cialo, headers=NAGLOWEK)

    assert domyslna.status_code == 422
    assert nasza.status_code == 422
    assert list(nasza.json()) == ["detail"]
    assert nasza.json() == domyslna.json()
    assert nasza.json()["detail"][0]["loc"] == ["body", "tokens_in"]


@pytest.mark.parametrize(("wartosc", "oczekiwana"), [
    pytest.param(float("inf"), "inf", id="plus-nieskonczonosc"),
    pytest.param(float("-inf"), "-inf", id="minus-nieskonczonosc"),
    pytest.param(float("nan"), "nan", id="nie-liczba"),
])
def test_nieskonczona_liczba_albo_nan_staje_sie_napisem_bezpiecznym_dla_json(wartosc, oczekiwana):
    wynik = _bezpieczne_dla_json(wartosc)

    assert wynik == oczekiwana
    assert type(wynik) is str


@pytest.mark.parametrize("wartosc", [1.5, 0.0, -2.25])
def test_skonczona_liczba_zmiennoprzecinkowa_zostaje_liczba_bez_zmian(wartosc):
    wynik = _bezpieczne_dla_json(wartosc)

    assert wynik == wartosc
    assert type(wynik) is float


@pytest.mark.parametrize("wartosc", [7, "napis", None, True, False, b"bajty"])
def test_wartosc_niebedaca_liczba_zmiennoprzecinkowa_zostaje_bez_zmian(wartosc):
    wynik = _bezpieczne_dla_json(wartosc)

    assert wynik == wartosc
    assert type(wynik) is type(wartosc)


def test_nieskonczonosc_w_slowniku_jest_zamieniona_a_klucze_i_inne_wartosci_zostaja():
    wynik = _bezpieczne_dla_json({"input": float("inf"), "ctx": 1.5, "msg": "x", "n": None})

    assert wynik == {"input": "inf", "ctx": 1.5, "msg": "x", "n": None}
    assert type(wynik["ctx"]) is float


def test_nieskonczonosc_w_liscie_jest_zamieniona_a_reszta_listy_zostaje():
    wynik = _bezpieczne_dla_json([1.5, float("-inf"), "x", float("nan")])

    assert wynik == [1.5, "-inf", "x", "nan"]
    assert type(wynik) is list


def test_nieskonczonosc_w_krotce_jest_zamieniona_a_wynik_jest_lista():
    wynik = _bezpieczne_dla_json((float("inf"), 2.5, "x"))

    assert wynik == ["inf", 2.5, "x"]
    assert type(wynik) is list


def test_nieskonczonosc_zagniezdzona_na_kazdym_poziomie_jest_zamieniona():
    wynik = _bezpieczne_dla_json(
        [{"loc": ("body", "cost_usd"), "input": float("inf"),
          "ctx": {"lista": [float("-inf"), (float("nan"),)]}}])

    assert wynik == [{"loc": ["body", "cost_usd"], "input": "inf",
                      "ctx": {"lista": ["-inf", ["nan"]]}}]


@pytest.mark.parametrize("pojemnik", [list, tuple], ids=["lista", "krotka"])
def test_kazdy_element_pojemnika_jest_przetworzony_osobno(pojemnik):
    wynik = _bezpieczne_dla_json(pojemnik([float("inf"), float("-inf")]))

    assert wynik == ["inf", "-inf"]


@pytest.mark.parametrize("koszt", [0, 0.0])
async def test_koszt_zdarzenia_rowny_zero_jest_przyjety_i_zapisany_jako_zero_a_nie_null(
    kokpit, koszt
):
    odpowiedz = await _zdarzenie(
        kokpit, "step_finished", cost_usd=koszt, cost_source="measured")

    assert odpowiedz.status_code == 200
    zapisany = (await _pomiar_w_bazie(kokpit.run_id))[0]
    assert zapisany is not None
    assert zapisany == 0.0


async def test_koszt_zdarzenia_jawnie_pusty_jest_zapisany_jako_null_a_nie_zero(kokpit):
    odpowiedz = await _zdarzenie(kokpit, "step_finished", cost_usd=None)

    assert odpowiedz.status_code == 200
    assert (await _pomiar_w_bazie(kokpit.run_id))[0] is None


async def test_szczegoly_runu_zwracaja_pomiar_kroku_z_bazy(kokpit):
    await _ok(kokpit, "step_finished", **POMIAR_W_ZDARZENIU)

    odpowiedz = await kokpit.klient.get(f"/api/runs/{kokpit.run_id}")

    assert odpowiedz.status_code == 200
    (krok,) = odpowiedz.json()["steps"]
    assert (krok["cost_usd"], krok["tokens_in"], krok["tokens_out"], krok["num_turns"],
            krok["session_id"]) == POMIAR_W_BAZIE


async def test_szczegoly_runu_zwracaja_null_dla_kroku_bez_pomiaru(kokpit):
    await _ok(kokpit, "step_finished")

    odpowiedz = await kokpit.klient.get(f"/api/runs/{kokpit.run_id}")

    (krok,) = odpowiedz.json()["steps"]
    assert (krok["cost_usd"], krok["tokens_in"], krok["tokens_out"], krok["num_turns"],
            krok["session_id"]) == BRAK_POMIARU_W_BAZIE


@pytest.mark.parametrize("pole", ["tokens_in", "tokens_out", "num_turns"])
@pytest.mark.parametrize("zla", [-1, INT_MAX + 1, "dużo"], ids=["ujemna", "ponad-int4", "napis"])
async def test_liczba_pomiaru_poza_zakresem_jest_odrzucona_i_nic_nie_zapisuje(kokpit, pole, zla):
    odpowiedz = await _zdarzenie(kokpit, "step_finished", **{pole: zla})

    assert odpowiedz.status_code == 422
    async with db_session.SessionLocal() as s:
        wiersze = (await s.execute(select(func.count()).select_from(StepRun))).scalar_one()
        zdarzenia = (await s.execute(select(func.count()).select_from(Event))).scalar_one()
    assert (wiersze, zdarzenia) == (0, 0)


@pytest.mark.parametrize("pole", ["tokens_in", "tokens_out", "num_turns"])
@pytest.mark.parametrize("dobra", [0, INT_MAX])
async def test_liczba_pomiaru_na_granicach_zakresu_jest_przyjeta_i_zapisana(kokpit, pole, dobra):
    odpowiedz = await _zdarzenie(kokpit, "step_finished", **{pole: dobra})

    assert odpowiedz.status_code == 200
    kolejnosc = ["cost_usd", "tokens_in", "tokens_out", "num_turns", "session_id"]
    zapisane = await _pomiar_w_bazie(kokpit.run_id)
    assert zapisane[kolejnosc.index(pole)] == dobra


@pytest.mark.parametrize("zly", ["", "s" * 101, 5], ids=["pusty", "101-znakow", "liczba"])
async def test_session_id_pusty_za_dlugi_albo_nie_napis_jest_odrzucony_i_nic_nie_zapisuje(
    kokpit, zly
):
    odpowiedz = await _zdarzenie(kokpit, "step_finished", session_id=zly)

    assert odpowiedz.status_code == 422
    async with db_session.SessionLocal() as s:
        wiersze = (await s.execute(select(func.count()).select_from(StepRun))).scalar_one()
    assert wiersze == 0


async def test_session_id_o_stu_znakach_jest_przyjety_i_zapisany(kokpit):
    odpowiedz = await _zdarzenie(kokpit, "step_finished", session_id="s" * 100)

    assert odpowiedz.status_code == 200
    assert (await _pomiar_w_bazie(kokpit.run_id))[4] == "s" * 100


async def test_session_id_o_jednym_znaku_jest_przyjety_i_zapisany(kokpit):
    odpowiedz = await _zdarzenie(kokpit, "step_finished", session_id="s")

    assert odpowiedz.status_code == 200
    assert (await _pomiar_w_bazie(kokpit.run_id))[4] == "s"


# --- cala sciezka: wynik wykonaj_krok -> zdarzenie silnika -> zapisz_zdarzenie -> StepRun ---


async def _wyslij_do_kokpitu(kokpit, zdarzenia: list[dict]) -> None:
    for z in zdarzenia:
        cialo = {**z, "run_id": kokpit.run_id, "event_id": str(uuid.uuid4())}
        odpowiedz = await kokpit.klient.post("/api/events", json=cialo, headers=NAGLOWEK)
        assert odpowiedz.status_code == 200, (z, odpowiedz.text)


async def test_koszt_tokeny_tury_i_sesja_z_wyjscia_agenta_trafiaja_do_wiersza_kroku_w_bazie(
    kokpit, tmp_path
):
    s = swiat_silnika(tmp_path, [K("a")])
    zdarzenia = await uruchom_run(s, _agent_z_atrapa(tmp_path))

    await _wyslij_do_kokpitu(kokpit, zdarzenia)

    assert await _pomiar_w_bazie(kokpit.run_id, "a") == (0.0123, 1234, 567, 7, "sesja-abc")
    odpowiedz = await kokpit.klient.get(f"/api/runs/{kokpit.run_id}")
    (krok,) = odpowiedz.json()["steps"]
    assert (krok["cost_usd"], krok["tokens_in"], krok["tokens_out"], krok["num_turns"],
            krok["session_id"]) == (0.0123, 1234, 567, 7, "sesja-abc")


async def test_krok_bez_pola_kosztu_w_wyniku_agenta_ma_w_bazie_koszt_null_a_nie_zero(
    kokpit, tmp_path
):
    s = swiat_silnika(tmp_path, [K("a")])
    agent = _agent_z_atrapa(tmp_path, a=[result(total_cost_usd=BRAK)])
    zdarzenia = await uruchom_run(s, agent)

    await _wyslij_do_kokpitu(kokpit, zdarzenia)

    zapisane = await _pomiar_w_bazie(kokpit.run_id, "a")
    assert zapisane[0] is None
    assert zapisane[1:] == (1234, 567, 7, "sesja-abc")


async def test_krok_z_wyczerpanym_budzetem_ma_w_bazie_porazke_z_poniesionym_kosztem(
    kokpit, tmp_path
):
    s = swiat_silnika(tmp_path, [K("a", extra=", budget_usd: 0.5")])
    agent = _agent_z_atrapa(
        tmp_path, a=[result(subtype="error_max_budget_usd", is_error=True, total_cost_usd=0.51)])
    zdarzenia = await uruchom_run(s, agent)

    await _wyslij_do_kokpitu(kokpit, zdarzenia)

    assert (await _pomiar_w_bazie(kokpit.run_id, "a"))[0] == 0.51
    async with db_session.SessionLocal() as sesja:
        wiersz = (await sesja.execute(
            select(StepRun).where(StepRun.run_id == kokpit.run_id, StepRun.step_id == "a")
        )).scalar_one()
    assert wiersz.reason == "budżet kroku wyczerpany (error_max_budget_usd)"


# =================================================================================================
# 10. Workflow: budget_usd i max_turns kroku
# =================================================================================================


def _plik_workflow(tmp_path: Path, pola_kroku: str) -> Path:
    plik = tmp_path / "w.yaml"
    plik.write_text(
        "name: w\nsteps:\n  zly:\n    skill: a\n"
        + "".join(f"    {linia_}\n" for linia_ in pola_kroku.splitlines())
        + "  dobry:\n    skill: b\n", encoding="utf-8")
    return plik


@pytest.mark.parametrize("pole", [
    pytest.param("budget_usd: 0", id="budzet-zero"),
    pytest.param("budget_usd: 0.0", id="budzet-zero-zmiennoprzecinkowe"),
    pytest.param("budget_usd: -1", id="budzet-ujemny"),
    pytest.param("budget_usd: -0.5", id="budzet-ujemny-ulamek"),
    pytest.param("budget_usd: true", id="budzet-bool"),
    pytest.param("budget_usd: '5'", id="budzet-napis"),
    pytest.param("budget_usd: .inf", id="budzet-nieskonczonosc"),
    pytest.param("budget_usd: .nan", id="budzet-nan"),
    pytest.param("max_turns: 0", id="tury-zero"),
    pytest.param("max_turns: -1", id="tury-ujemne"),
    pytest.param("max_turns: true", id="tury-bool"),
    pytest.param("max_turns: '3'", id="tury-napis"),
    pytest.param("max_turns: 2.5", id="tury-ulamek"),
])
def test_zly_budzet_albo_limit_tur_robi_krok_niepoprawnym_z_bledem_pola(tmp_path, pole):
    workflow, problemy = wczytaj_workflow(_plik_workflow(tmp_path, pole))

    assert workflow is not None
    assert [(p.krok, p.kod) for p in problemy] == [("zly", BLAD_POLA)]
    assert workflow.niepoprawne == ["zly"]
    assert [k.id for k in workflow.kroki] == ["dobry"]


def test_budzet_calkowity_w_yaml_jest_przyjety_jako_liczba_zmiennoprzecinkowa(tmp_path):
    workflow, problemy = wczytaj_workflow(_plik_workflow(tmp_path, "budget_usd: 5"))

    assert problemy == []
    krok = next(k for k in workflow.kroki if k.id == "zly")
    assert krok.budget_usd == 5.0
    assert isinstance(krok.budget_usd, float)


def test_budzet_ulamkowy_i_limit_tur_sa_przyjete_bez_zmian(tmp_path):
    workflow, problemy = wczytaj_workflow(
        _plik_workflow(tmp_path, "budget_usd: 0.25\nmax_turns: 3"))

    assert problemy == []
    krok = next(k for k in workflow.kroki if k.id == "zly")
    assert (krok.budget_usd, krok.max_turns) == (0.25, 3)


def test_krok_bez_budzetu_i_limitu_tur_ma_oba_pola_puste(tmp_path):
    workflow, problemy = wczytaj_workflow(_plik_workflow(tmp_path, "fanout: 2"))

    assert problemy == []
    krok = next(k for k in workflow.kroki if k.id == "zly")
    assert (krok.budget_usd, krok.max_turns) == (None, None)


def test_walidator_workflow_zna_budzet_i_limit_tur_jako_klucze_kroku(tmp_path):
    repo, dom = tmp_path / "repo", tmp_path / "dom"
    repo.mkdir()
    dom.mkdir()
    napisz_skill(repo / ".cockpit" / "skills", "a")
    napisz_skill(repo / ".cockpit" / "skills", "b")
    workflow, problemy = wczytaj_workflow(
        _plik_workflow(tmp_path, "budget_usd: 5\nmax_turns: 3"))
    assert problemy == []

    problemy = waliduj_workflow(workflow, rozstrzygnij_rejestr(repo, dom), dom)

    assert [p for p in problemy if p.kod == NIEZNANY_KLUCZ] == []
    assert not ma_bledy(problemy)


def test_zapis_z_kanwy_nie_dopisuje_budzetu_ani_limitu_tur_do_kroku_ktory_ich_nie_ma(tmp_path):
    plik = _plik_workflow(tmp_path, "fanout: 2")
    workflow, _ = wczytaj_workflow(plik)
    workflow.kroki[0].fanout = 3

    zapisz_workflow(workflow, plik)

    zapisany = plik.read_text(encoding="utf-8")
    assert "fanout: 3" in zapisany
    assert "budget_usd" not in zapisany
    assert "max_turns" not in zapisany


def test_zapis_z_kanwy_zostawia_budzet_calkowity_tak_jak_go_wpisal_uzytkownik(tmp_path):
    plik = _plik_workflow(tmp_path, "budget_usd: 5\nfanout: 2")
    workflow, _ = wczytaj_workflow(plik)
    workflow.kroki[0].fanout = 3

    zapisz_workflow(workflow, plik)

    zapisany = plik.read_text(encoding="utf-8")
    assert "    budget_usd: 5\n" in zapisany
    assert "fanout: 3" in zapisany


def test_zapis_z_kanwy_dopisuje_ustawiony_budzet_i_limit_tur_i_wczytanie_je_zwraca(tmp_path):
    plik = _plik_workflow(tmp_path, "fanout: 2")
    workflow, _ = wczytaj_workflow(plik)
    workflow.kroki[0].budget_usd = 2.5
    workflow.kroki[0].max_turns = 4

    zapisz_workflow(workflow, plik)

    ponowny, problemy = wczytaj_workflow(plik)
    assert problemy == []
    krok = next(k for k in ponowny.kroki if k.id == "zly")
    assert (krok.budget_usd, krok.max_turns) == (2.5, 4)


def test_zapis_z_kanwy_usuwa_budzet_gdy_uzytkownik_go_wyczyscil(tmp_path):
    plik = _plik_workflow(tmp_path, "budget_usd: 5\nfanout: 2")
    workflow, _ = wczytaj_workflow(plik)
    workflow.kroki[0].budget_usd = None

    zapisz_workflow(workflow, plik)

    zapisany = plik.read_text(encoding="utf-8")
    assert "budget_usd" not in zapisany
    assert "fanout: 2" in zapisany


# =================================================================================================
# 11. Galezie bledu na prawdziwym kodzie, kolumny i migracja pomiaru (HAR-174, runda 2)
# =================================================================================================

# Glebokosc, przy ktorej parser JSON wyczerpuje stos (wiecej niz limit rekurencji i niz stos C);
# linia ma 2 MB, czyli miesci sie w limicie linii agenta (8 MiB) i dociera do parsera.
GLEBOKOSC_ZAGNIEZDZENIA = 1_000_000


async def test_koszt_ponad_zakres_liczby_zmiennoprzecinkowej_jest_brakiem_pomiaru_a_nie_wyjatkiem(
    katalog, katalog_runu
):
    surowa = json.dumps(pola_result(total_cost_usd=10**400))
    assert "e+" not in surowa and len(surowa) > 400      # liczba calkowita, nie ulamek

    wynik, _ = await uruchom(spec(), atrapa(linia(surowa)), katalog, katalog_runu)

    assert wynik == WynikKroku(
        "zakonczony", None, 0, pomiar=dataclasses.replace(POMIAR_PELNY, cost_usd=None))


async def test_wynik_recenzenta_z_samotnym_surogatem_zapisuje_artefakt_ze_znakiem_zastepczym(
    katalog, katalog_runu
):
    surowa = json.dumps(pola_result(result="a\ud800b"))
    assert "\\ud800" in surowa                           # w JSON-ie sekwencja ucieczki

    wynik, _ = await uruchom(
        spec(Rola.reviewer, ["plan.md"]), atrapa(linia(surowa)), katalog, katalog_runu)

    assert wynik == WynikKroku("zakonczony", None, 0, pomiar=POMIAR_PELNY)
    assert (katalog_runu / KROK / "plan.md").read_bytes() == b"a?b"


async def test_gleboko_zagniezdzona_linia_wyjscia_agenta_jest_niezrozumiala_a_nie_wyjatkiem(
    katalog, katalog_runu
):
    wynik, _ = await uruchom(
        spec(), atrapa(("glebokie", GLEBOKOSC_ZAGNIEZDZENIA), result()), katalog, katalog_runu)

    assert wynik.status == "nieudany"
    assert wynik.powod == powod_niezrozumialej("[" * 200)


async def test_katalog_runu_bedacy_dowiazaniem_do_repozytorium_blokuje_zapis_promptu_i_start_agenta(
    tmp_path, katalog
):
    dowiazanie = tmp_path / "run-dowiazanie"
    dowiazanie.symlink_to(katalog, target_is_directory=True)
    znacznik = tmp_path / "start.znacznik"

    wynik, _ = await uruchom(
        spec(), atrapa(result(), znacznik=znacznik), katalog, dowiazanie)

    assert wynik == WynikKroku(
        "nieudany",
        f"niezmiennik run_dir_outside_repo: katalog runu {dowiazanie / 'prompty'} "
        f"lezy w repozytorium {katalog}",
        None)
    assert not znacznik.exists()
    assert list(katalog.rglob("*")) == []                # nic nie wpadlo do repozytorium


# --- kolumny pomiaru w modelu kroku ---

KOLUMNY_POMIARU_KROKU = {
    "cost_usd": Float, "tokens_in": Integer, "tokens_out": Integer, "num_turns": Integer,
    "session_id": String,
}


@pytest.mark.parametrize(("pole", "typ"), list(KOLUMNY_POMIARU_KROKU.items()))
def test_kolumna_pomiaru_kroku_ma_swoj_typ_dopuszcza_brak_pomiaru_i_nie_ma_wartosci_domyslnej(
    pole, typ
):
    kolumna = StepRun.__table__.c[pole]

    assert type(kolumna.type) is typ
    assert kolumna.nullable is True
    assert kolumna.default is None
    assert kolumna.server_default is None


def test_identyfikator_sesji_kroku_miesci_sie_w_kolumnie_o_stu_znakach():
    assert StepRun.__table__.c["session_id"].type.length == 100


def _sql_migracji(zakres: str, w_dol: bool) -> list[str]:
    """Polecenia ALTER i UPDATE migracji generowane przez Alembica bez bazy (tryb offline)."""
    bufor = io.StringIO()
    konfiguracja = Config(str(KORZEN_REPO / "alembic.ini"))
    konfiguracja.set_main_option("script_location", str(KORZEN_REPO / "alembic"))
    konfiguracja.set_main_option("sqlalchemy.url", "postgresql+asyncpg://u:p@localhost/offline")
    # env.py nie przekazuje output_buffer, wiec tryb offline pisze SQL na sys.stdout
    with contextlib.redirect_stdout(bufor):
        (command.downgrade if w_dol else command.upgrade)(konfiguracja, zakres, sql=True)
    return [w for w in bufor.getvalue().splitlines() if w.startswith(("ALTER", "UPDATE"))]


def test_migracja_pomiaru_kroku_dodaje_piec_kolumn_bez_not_null_i_przesuwa_wersje_na_0013():
    assert _sql_migracji("0012:0013", w_dol=False) == [
        "ALTER TABLE step_run ADD COLUMN cost_usd FLOAT;",
        "ALTER TABLE step_run ADD COLUMN tokens_in INTEGER;",
        "ALTER TABLE step_run ADD COLUMN tokens_out INTEGER;",
        "ALTER TABLE step_run ADD COLUMN num_turns INTEGER;",
        "ALTER TABLE step_run ADD COLUMN session_id VARCHAR(100);",
        "UPDATE alembic_version SET version_num='0013' "
        "WHERE alembic_version.version_num = '0012';",
    ]


def test_cofniecie_migracji_pomiaru_kroku_usuwa_piec_kolumn_i_wraca_do_wersji_0012():
    assert _sql_migracji("0013:0012", w_dol=True) == [
        "ALTER TABLE step_run DROP COLUMN session_id;",
        "ALTER TABLE step_run DROP COLUMN num_turns;",
        "ALTER TABLE step_run DROP COLUMN tokens_out;",
        "ALTER TABLE step_run DROP COLUMN tokens_in;",
        "ALTER TABLE step_run DROP COLUMN cost_usd;",
        "UPDATE alembic_version SET version_num='0012' "
        "WHERE alembic_version.version_num = '0013';",
    ]


def test_budzet_i_limit_tur_jawnie_wyczyszczone_w_yaml_to_brak_limitow_a_nie_blad(tmp_path):
    workflow, problemy = wczytaj_workflow(
        _plik_workflow(tmp_path, "budget_usd: null\nmax_turns: null"))

    assert problemy == []
    krok = next(k for k in workflow.kroki if k.id == "zly")
    assert (krok.budget_usd, krok.max_turns) == (None, None)


# =================================================================================================
# 12. Porazki trybu agenta niosa kod wyjscia i pomiar (runda 4: mutanty zmienionych funkcji)
# =================================================================================================

NIE_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root czyta plik bez wzgledu na prawa")


class KawalkiStrumienia:
    """Strumien oddajacy zadane kawalki bajtow po kolei (deterministyczny podzial na odczyty)."""

    def __init__(self, *kawalki: bytes):
        self._kawalki = list(kawalki)

    async def read(self, _n: int) -> bytes:
        return self._kawalki.pop(0) if self._kawalki else b""


async def test_session_id_o_jednym_znaku_w_wyniku_agenta_jest_zapisany_w_pomiarze(
    katalog, katalog_runu
):
    wynik, _ = await uruchom(spec(), atrapa(result(session_id="s")), katalog, katalog_runu)

    assert wynik.pomiar.session_id == "s"


async def test_ostatnia_linia_wyjscia_agenta_bez_znaku_nowej_linii_jest_przetworzona(
    katalog, katalog_runu
):
    wynik, _ = await uruchom(
        spec(), atrapa(("bez_konca", json.dumps(pola_result()))), katalog, katalog_runu)

    assert wynik == WynikKroku("zakonczony", None, 0, pomiar=POMIAR_PELNY)


@pytest.mark.parametrize("kod", [0, 1])
async def test_niezrozumiala_linia_po_wyniku_agenta_niesie_kod_wyjscia_i_zebrany_pomiar(
    katalog, katalog_runu, kod
):
    wynik, _ = await uruchom(
        spec(), atrapa(result(), linia("smiec"), kod=kod), katalog, katalog_runu)

    assert wynik == WynikKroku(
        "nieudany", powod_niezrozumialej("smiec"), kod, pomiar=POMIAR_PELNY)


@pytest.mark.parametrize("kod", [0, 1])
@pytest.mark.parametrize("podtyp", ["error_max_budget_usd", "error_max_turns"])
async def test_wyczerpany_budzet_niesie_kod_wyjscia_i_pomiar_poniesiony_do_tej_pory(
    katalog, katalog_runu, podtyp, kod
):
    wynik, _ = await uruchom(
        spec(), atrapa(result(subtype=podtyp, is_error=True), kod=kod), katalog, katalog_runu)

    assert wynik == WynikKroku(
        "nieudany", f"budżet kroku wyczerpany ({podtyp})", kod, pomiar=POMIAR_PELNY)


async def test_blad_zgloszony_przez_agenta_niesie_kod_zero_i_pomiar(katalog, katalog_runu):
    wynik, _ = await uruchom(
        spec(), atrapa(result(subtype="error_during_execution", is_error=True)),
        katalog, katalog_runu)

    assert wynik == WynikKroku(
        "nieudany", "agent zakończył się błędem: error_during_execution", 0,
        pomiar=POMIAR_PELNY)


async def test_zapis_artefaktu_recenzenta_do_zajetego_katalogu_kroku_niesie_kod_i_pomiar(
    katalog, katalog_runu
):
    blokada = katalog_runu / KROK
    blokada.write_text("zwykly plik zamiast katalogu kroku", encoding="utf-8")
    with pytest.raises(FileExistsError) as przyczyna:
        blokada.mkdir(parents=True, exist_ok=True)

    wynik, _ = await uruchom(
        spec(Rola.reviewer, ["plan.md"]), atrapa(result()), katalog, katalog_runu)

    assert wynik == WynikKroku(
        "nieudany", f"nie można zapisać artefaktu plan.md: {przyczyna.value}", 0,
        pomiar=POMIAR_PELNY)


@NIE_ROOT
async def test_nieczytelny_artefakt_autora_niesie_kod_wyjscia_i_pomiar(katalog, katalog_runu):
    plik = katalog / "plan.md"
    try:
        wynik, _ = await uruchom(
            spec(Rola.author, ["plan.md"]),
            atrapa(("zapisz", "plan.md", "tresc"), ("chmod", "plan.md", "000"), result()),
            katalog, katalog_runu)
        with pytest.raises(PermissionError) as przyczyna:
            plik.read_text(encoding="utf-8")
    finally:
        plik.chmod(0o600)

    assert wynik == WynikKroku(
        "nieudany", f"nie można odczytać artefaktu plan.md: {przyczyna.value}", 0,
        pomiar=POMIAR_PELNY)


async def test_pusty_artefakt_autora_niesie_kod_wyjscia_i_pomiar(katalog, katalog_runu):
    wynik, _ = await uruchom(
        spec(Rola.author, ["plan.md"]),
        atrapa(("zapisz", "plan.md", ""), result()), katalog, katalog_runu)

    assert wynik == WynikKroku(
        "nieudany",
        f"niezmiennik evidence_required: plik dowodu jest pusty: {katalog / 'plan.md'}", 0,
        pomiar=POMIAR_PELNY)


async def test_recenzent_ktory_zapisal_plik_w_repozytorium_niesie_naruszenie_kod_i_pomiar(
    katalog, katalog_runu
):
    wynik, _ = await uruchom(
        spec(Rola.reviewer, ["plan.md"]),
        atrapa(("zapisz", "nowy.txt", "x"), result()), katalog, katalog_runu)

    assert wynik == WynikKroku(
        "nieudany", "niezmiennik reviewer_readonly: nowe: nowy.txt", 0, pomiar=POMIAR_PELNY)


@NIE_ROOT
async def test_nieczytelny_plik_recenzenta_przy_sprawdzeniu_readonly_niesie_kod_i_pomiar(
    katalog, katalog_runu
):
    plik = katalog / "tajne.txt"
    try:
        wynik, _ = await uruchom(
            spec(Rola.reviewer, ["plan.md"]),
            atrapa(("zapisz", "tajne.txt", "x"), ("chmod", "tajne.txt", "000"), result()),
            katalog, katalog_runu)
        with pytest.raises(PermissionError) as przyczyna:
            plik.read_bytes()
    finally:
        plik.chmod(0o600)

    assert wynik == WynikKroku(
        "nieudany",
        f"nie można sprawdzić niezmiennika reviewer_readonly: {przyczyna.value}", 0,
        pomiar=POMIAR_PELNY)


async def test_tresc_asystenta_pomija_elementy_inne_niz_tekst_takze_gdy_niosa_pole_text(
    katalog, katalog_runu
):
    wiadomosc = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "thinking", "text": "ukryta mysl"}, "golyNapis", 7,
        {"type": "text", "text": "widoczne"}]}}

    wynik, zdarzenia = await uruchom(
        spec(), atrapa(linia(json.dumps(wiadomosc)), result()), katalog, katalog_runu)

    assert wynik == WynikKroku("zakonczony", None, 0, pomiar=POMIAR_PELNY)
    assert [z for z in zdarzenia if z["kind"] == "output"] == [zdarzenie_output("widoczne")]


async def test_linia_przekraczajaca_limit_jeszcze_przed_znakiem_nowej_linii_jest_bledem_kroku(
    monkeypatch
):
    monkeypatch.setattr(modul_kroku, "LIMIT_LINII_AGENTA", 10)
    stan = modul_kroku._StanStrumienia()

    async def emit(zdarzenie: dict) -> None:
        pass

    strumien = KawalkiStrumienia(
        b"x" * 20, b"y" * 5 + b"\n" + json.dumps(pola_result()).encode("utf-8") + b"\n")
    await asyncio.wait_for(
        modul_kroku._czytaj_zdarzenia_agenta(strumien, spec(), emit, stan), LIMIT_S)

    assert stan.blad_linii == "linia wyjścia agenta przekracza 10 bajtów"
    assert stan.wynik is None
