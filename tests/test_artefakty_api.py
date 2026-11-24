"""HAR-182: podglad artefaktu runu - `GET /api/runs/{run_id}/artifacts/{p:path}` i serwis
`cockpit.services.artefakty.artefakt_runu(s, tenant_id, run_id, sciezka, polaczone, wyslij)`.

Kontrakt (przyjety przez test-writera, opisany w raporcie):
 * odpowiedz to model `Artefakt {path, text, truncated, full_len}`; `path` to sciezka tak, jak ja
   podano (wzgledna wobec katalogu runu), nigdy bezwzgledna sciezka z dysku runnera;
 * serwis pyta WYLACZNIE runnera runu (`run.runner_id`) operacja `read_artifact` z danymi
   `{run_id, sciezka}`; offline to 409 bez przekierowania na inny runner zespolu;
 * sciezka z `..`, bezwzgledna albo z NUL to `NiespojneDane` (422) PRZED wyslaniem do runnera;
 * run innego zespolu albo nieistniejacy to 404 bez pytania runnera;
 * zly ksztalt odpowiedzi runnera (nie slownik, brak `text`, `truncated` nie bool, `full_len`
   ujemny / nie liczba / krotszy niz tekst) to StanNiemozliwy (409), nie 500;
 * odmowa runnera `poza_zakresem` to 403; inne kody runnera (`binarny`, `nie_istnieje`) to 409.
Serwis testowany na prawdziwej bazie z wstrzykiwana wysylka; trasa przez prawdziwa aplikacje
z prawdziwa operacja runnera na plikach w katalogu testu (asercje na danych, nie tylko na kodzie).
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api import bus
from cockpit.db.models import Base, Run, RunStatus, Runner, Tenant
from cockpit.services.errors import (
    NiespojneDane,
    NieZnaleziono,
    PlikPozaZakresem,
    RunnerOffline,
    StanNiemozliwy,
)
from runner import operacje
from test_projekty_zmiana import swiat

FIXTURY_Z_INNYCH_TESTOW = (swiat,)   # fixture pytest zzaimportowana z innego pliku testow

TRESC_SEKRETNA = "tresc-cudzego-artefaktu-90210"
KOMUNIKAT_POZA_ZAKRESEM = "sciezka poza zakresem runnera"


def _artefakty():
    """Import w teście: brak modulu to porazka tego testu, nie bledu zbierania calego pliku."""
    return importlib.import_module("cockpit.services.artefakty")


def _slownik(wynik) -> dict:
    return wynik.model_dump() if hasattr(wynik, "model_dump") else dict(wynik)


# =================================================================================
# 1. Serwis: zespol, runner runu, walidacja sciezki, ksztalt odpowiedzi
# =================================================================================

@pytest.fixture
async def zespoly(tmp_path):
    """Zespol A (dwa runnery online: `runner-a` ma run, `runner-a2` nie) i zespol B (z wlasnym runem)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'artefakty.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with maker() as s:
        b, a = Tenant(name="Beta"), Tenant(name="Alfa")
        s.add(b)
        await s.flush()
        s.add(a)
        await s.flush()
        teraz = datetime.now(timezone.utc)
        for rid, tenant in (("runner-a", a), ("runner-a2", a), ("runner-b", b)):
            s.add(Runner(id=rid, tenant_id=tenant.id, name=rid, token_hash=rid, last_seen_at=teraz))
        await s.flush()
        run_a = Run(tenant_id=a.id, runner_id="runner-a", workflow="w", status=RunStatus.running)
        run_b = Run(tenant_id=b.id, runner_id="runner-b", workflow="w", status=RunStatus.running)
        s.add_all([run_a, run_b])
        await s.commit()
        ids = SimpleNamespace(a=a.id, b=b.id, run_a=run_a.id, run_b=run_b.id)
    yield SimpleNamespace(maker=maker, ids=ids)
    await engine.dispose()


POLACZONE = frozenset({"runner-a", "runner-a2", "runner-b"})
ODPOWIEDZ_OK = {"ok": True, "wynik": {"text": "tresc", "truncated": False, "full_len": 5}}


class _Wysylka:
    def __init__(self, odpowiedz) -> None:
        self.odpowiedz = odpowiedz
        self.wywolania: list[tuple] = []

    async def __call__(self, runner_id, typ, dane, timeout_s):
        self.wywolania.append((runner_id, typ, dane))
        if isinstance(self.odpowiedz, Exception):
            raise self.odpowiedz
        return self.odpowiedz


async def _pobierz(zespoly, sciezka, *, run_id=None, tenant_id=None, odpowiedz=ODPOWIEDZ_OK,
                   polaczone=POLACZONE):
    wysylka = _Wysylka(odpowiedz)
    async with zespoly.maker() as s:
        try:
            wynik = await _artefakty().artefakt_runu(
                s, tenant_id or zespoly.ids.a, run_id or zespoly.ids.run_a, sciezka,
                polaczone, wysylka)
        except Exception as e:
            return e, wysylka
    return wynik, wysylka


async def test_artefakt_runu_pyta_runnera_runu_operacja_read_artifact_z_identyfikatorem_i_sciezka(
    zespoly,
):
    wynik, wysylka = await _pobierz(zespoly, "wyniki/plan.md")

    assert _slownik(wynik) == {
        "path": "wyniki/plan.md", "text": "tresc", "truncated": False, "full_len": 5}
    assert wysylka.wywolania == [
        ("runner-a", "read_artifact", {"run_id": zespoly.ids.run_a, "sciezka": "wyniki/plan.md"})]


async def test_artefakt_obciety_przez_runnera_zachowuje_znacznik_i_pelna_dlugosc(zespoly):
    wynik, _ = await _pobierz(zespoly, "duzy.md", odpowiedz={
        "ok": True, "wynik": {"text": "abc", "truncated": True, "full_len": 90000}})

    assert _slownik(wynik) == {
        "path": "duzy.md", "text": "abc", "truncated": True, "full_len": 90000}


async def test_runner_runu_offline_to_runner_offline_bez_przekierowania_na_inny_runner_zespolu(zespoly):
    wynik, wysylka = await _pobierz(zespoly, "plan.md", polaczone=frozenset({"runner-a2", "runner-b"}))

    assert type(wynik) is RunnerOffline
    assert isinstance(wynik, StanNiemozliwy)
    assert wysylka.wywolania == []


async def test_run_innego_zespolu_to_nie_znaleziono_a_runner_nie_jest_pytany(zespoly):
    wynik, wysylka = await _pobierz(zespoly, "plan.md", run_id=zespoly.ids.run_b)

    assert type(wynik) is NieZnaleziono
    assert wysylka.wywolania == []


async def test_nieistniejacy_run_to_nie_znaleziono_a_runner_nie_jest_pytany(zespoly):
    wynik, wysylka = await _pobierz(zespoly, "plan.md", run_id="nie-ma-takiego-runu")

    assert type(wynik) is NieZnaleziono
    assert wysylka.wywolania == []


@pytest.mark.parametrize("sciezka", [
    "../plan.md", "a/../../plan.md", "..", "a/../b.md", "/etc/passwd", "/plan.md", "a\x00b.md", "",
], ids=["dwie_kropki", "dwie_kropki_w_glebi", "same_kropki", "kropki_wracajace_do_srodka",
        "bezwzgledna", "bezwzgledna_do_pliku_runu", "bajt_zerowy", "pusta"])
async def test_niebezpieczna_sciezka_artefaktu_to_niespojne_dane_przed_wyslaniem_do_runnera(
    zespoly, sciezka,
):
    wynik, wysylka = await _pobierz(zespoly, sciezka)

    assert type(wynik) is NiespojneDane
    assert wysylka.wywolania == []


@pytest.mark.parametrize("sciezka", ["plan.md", "a/b/c.md", "..plan.md", "a/..b/c.md", "plan..md",
                                     "wyniki v2/raport (1).md"],
                         ids=["zwykla", "zagniezdzona", "kropki_w_nazwie_pliku",
                              "kropki_w_nazwie_katalogu", "kropki_w_srodku_nazwy", "spacje_i_nawiasy"])
async def test_sciezka_z_kropkami_wewnatrz_nazwy_nie_jest_wyjsciem_z_katalogu_i_idzie_do_runnera(
    zespoly, sciezka,
):
    wynik, wysylka = await _pobierz(zespoly, sciezka)

    assert _slownik(wynik)["path"] == sciezka
    assert [w[2]["sciezka"] for w in wysylka.wywolania] == [sciezka]


@pytest.mark.parametrize("wynik_runnera", [
    "tekst", ["a"], None, {},
    {"truncated": False, "full_len": 1},
    {"text": 5, "truncated": False, "full_len": 1},
    {"text": None, "truncated": False, "full_len": 1},
    {"text": "a", "full_len": 1},
    {"text": "a", "truncated": "nie", "full_len": 1},
    {"text": "a", "truncated": 0, "full_len": 1},
    {"text": "a", "truncated": False},
    {"text": "a", "truncated": False, "full_len": None},
    {"text": "a", "truncated": False, "full_len": "1"},
    {"text": "a", "truncated": False, "full_len": 1.5},
    {"text": "a", "truncated": False, "full_len": True},
    {"text": "a", "truncated": False, "full_len": -1},
    {"text": "abc", "truncated": True, "full_len": 2},
], ids=["napis", "lista", "none", "pusty_slownik", "brak_text", "text_liczba", "text_none",
        "brak_truncated", "truncated_napis", "truncated_zero", "brak_full_len", "full_len_none",
        "full_len_napis", "full_len_ulamek", "full_len_bool", "full_len_ujemny",
        "full_len_krotszy_niz_tekst"])
async def test_zly_ksztalt_odpowiedzi_runnera_o_artefakt_to_stan_niemozliwy_a_nie_500(
    zespoly, wynik_runnera,
):
    wynik, _ = await _pobierz(zespoly, "plan.md", odpowiedz={"ok": True, "wynik": wynik_runnera})

    assert type(wynik) is StanNiemozliwy
    assert str(wynik).startswith("odpowiedz runnera ma zly ksztalt")


@pytest.mark.parametrize(("wynik_runnera", "powod"), [
    ("tekst", "wynik nie jest slownikiem"),
    ({"truncated": False, "full_len": 1}, "pole text nie jest napisem"),
    ({"text": "a", "full_len": 1}, "pole truncated nie jest wartoscia logiczna"),
    ({"text": "a", "truncated": False, "full_len": -1},
     "pole full_len nie jest nieujemna liczba calkowita"),
    ({"text": "\ud800", "truncated": False, "full_len": 100},
     "pole text nie jest poprawnym tekstem UTF-8"),
    ({"text": "zażółć", "truncated": False, "full_len": 3},
     "pole full_len jest krotsze niz przyslany tekst"),
], ids=["nie_slownik", "text", "truncated", "full_len_ujemny", "surogat", "full_len_krotszy"])
async def test_zly_ksztalt_odpowiedzi_runnera_o_artefakt_podaje_dokladna_przyczyne(
    zespoly, wynik_runnera, powod,
):
    wynik, _ = await _pobierz(zespoly, "plan.md", odpowiedz={"ok": True, "wynik": wynik_runnera})

    assert type(wynik) is StanNiemozliwy
    assert str(wynik) == f"odpowiedz runnera ma zly ksztalt ({powod})"


@pytest.mark.parametrize("sciezka", ["", "a\x00b.md"], ids=["pusta", "bajt_zerowy"])
async def test_pusta_sciezka_artefaktu_albo_z_nul_ma_komunikat_o_pustej_lub_nul(zespoly, sciezka):
    wynik, wysylka = await _pobierz(zespoly, sciezka)

    assert type(wynik) is NiespojneDane
    assert str(wynik) == "sciezka artefaktu jest pusta albo zawiera znak NUL"
    assert wysylka.wywolania == []


@pytest.mark.parametrize("sciezka", [
    "../plan.md", "/plan.md", "..\\plan.md", "a\\..\\b.md", "a/..\\b.md"],
    ids=["kropki", "bezwzgledna", "kropki_z_odwrotnym_ukosnikiem", "kropki_w_glebi_odwrotne",
         "kropki_mieszane"])
async def test_sciezka_wychodzaca_z_katalogu_runu_ma_komunikat_o_wzglednej_bez_kropek(
    zespoly, sciezka,
):
    wynik, wysylka = await _pobierz(zespoly, sciezka)

    assert type(wynik) is NiespojneDane
    assert str(wynik) == "sciezka artefaktu musi byc wzgledna i bez `..`"
    assert wysylka.wywolania == []


async def test_odmowa_runnera_poza_zakresem_to_plik_poza_zakresem(zespoly):
    wynik, _ = await _pobierz(zespoly, "plan.md", odpowiedz={
        "ok": False, "blad": KOMUNIKAT_POZA_ZAKRESEM, "kod": "poza_zakresem"})

    assert type(wynik) is PlikPozaZakresem


@pytest.mark.parametrize(("kod", "komunikat"), [
    ("binarny", "plik nie jest tekstem UTF-8"), ("nie_istnieje", "sciezka nie istnieje"),
    ("to_katalog", "sciezka jest katalogiem")])
async def test_inna_odmowa_runnera_o_artefakt_to_stan_niemozliwy_z_komunikatem_runnera(
    zespoly, kod, komunikat,
):
    wynik, _ = await _pobierz(zespoly, "plan.md", odpowiedz={
        "ok": False, "blad": komunikat, "kod": kod})

    assert type(wynik) is StanNiemozliwy
    assert str(wynik) == komunikat


async def test_brak_odpowiedzi_runnera_o_artefakt_w_limicie_czasu_to_stan_niemozliwy(zespoly):
    wynik, _ = await _pobierz(zespoly, "plan.md", odpowiedz=TimeoutError())

    assert type(wynik) is StanNiemozliwy


# =================================================================================
# 2. Trasa HTTP: prawdziwa aplikacja i prawdziwa operacja runnera na plikach runu
# =================================================================================

@pytest.fixture
def katalog_runow(swiat, tmp_path, monkeypatch):
    runy = tmp_path.resolve() / "runy"
    runy.mkdir()
    monkeypatch.setenv("COCKPIT_RUNS_DIR", str(runy))
    return runy


async def _utworz_run(swiat, tenant_id, runner_id) -> str:
    async with swiat.maker() as s:
        run = Run(tenant_id=tenant_id, runner_id=runner_id, workflow="w", status=RunStatus.running)
        s.add(run)
        await s.commit()
        return run.id


def _artefakt_na_dysku(katalog_runow, run_id, sciezka, tresc: bytes):
    plik = katalog_runow / run_id / sciezka
    plik.parent.mkdir(parents=True, exist_ok=True)
    plik.write_bytes(tresc)
    return plik


async def _pobierz_http(klient, run_id, sciezka):
    return await klient.get(f"/api/runs/{run_id}/artifacts/{sciezka}")


async def test_artefakt_runu_zespolu_wraca_z_trescia_obcieciem_i_pelna_dlugoscia(swiat, katalog_runow):
    run_id = await _utworz_run(swiat, swiat.tenant_a, swiat.runner_a)
    _artefakt_na_dysku(katalog_runow, run_id, "wyniki/plan.md", "# Plan zażółć\n".encode("utf-8"))
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _pobierz_http(klient, run_id, "wyniki/plan.md")

    assert odp.status_code == 200, odp.text
    assert odp.json() == {"path": "wyniki/plan.md", "text": "# Plan zażółć\n", "truncated": False,
                          "full_len": len("# Plan zażółć\n".encode("utf-8"))}
    assert swiat.runner.wywolania == [
        (swiat.runner_a, "read_artifact", {"run_id": run_id, "sciezka": "wyniki/plan.md"})]


async def test_artefakt_wiekszy_niz_limit_wraca_obciety_z_prawdziwa_pelna_dlugoscia(
    swiat, katalog_runow, monkeypatch,
):
    monkeypatch.setattr(operacje, "LIMIT_ARTEFAKTU_B", 50)
    run_id = await _utworz_run(swiat, swiat.tenant_a, swiat.runner_a)
    _artefakt_na_dysku(katalog_runow, run_id, "duzy.md", b"0123456789" * 13)   # 130 bajtow
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _pobierz_http(klient, run_id, "duzy.md")

    assert odp.status_code == 200, odp.text
    assert odp.json() == {"path": "duzy.md", "text": "0123456789" * 5, "truncated": True,
                          "full_len": 130}


async def test_odpowiedz_z_artefaktem_nie_zawiera_sciezki_katalogu_runow_runnera(swiat, katalog_runow):
    run_id = await _utworz_run(swiat, swiat.tenant_a, swiat.runner_a)
    _artefakt_na_dysku(katalog_runow, run_id, "plan.md", b"plan")
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _pobierz_http(klient, run_id, "plan.md")

    assert odp.status_code == 200, odp.text
    assert str(katalog_runow) not in odp.text


async def test_czlonek_zespolu_nie_czyta_artefaktu_runu_innego_zespolu_a_runner_nie_jest_pytany(
    swiat, katalog_runow,
):
    cudzy = await _utworz_run(swiat, swiat.tenant_b, swiat.runner_b)
    _artefakt_na_dysku(katalog_runow, cudzy, "plan.md", TRESC_SEKRETNA.encode())
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _pobierz_http(klient, cudzy, "plan.md")

    assert odp.status_code == 404
    assert odp.json() == {"detail": "run nieznany"}   # odmowa trasy, nie brak trasy
    assert TRESC_SEKRETNA not in odp.text
    assert swiat.runner.wywolania == []


async def test_nieistniejacy_run_daje_404_a_runner_nie_jest_pytany(swiat, katalog_runow):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _pobierz_http(klient, "nie-ma-takiego-runu", "plan.md")

    assert odp.status_code == 404
    assert odp.json() == {"detail": "run nieznany"}   # odmowa trasy, nie brak trasy
    assert swiat.runner.wywolania == []


async def test_runner_runu_offline_daje_409_a_inny_runner_zespolu_nie_przejmuje_odczytu(
    swiat, katalog_runow,
):
    run_id = await _utworz_run(swiat, swiat.tenant_a, swiat.runner_a)
    _artefakt_na_dysku(katalog_runow, run_id, "plan.md", b"plan")
    bus.mark_runner_disconnected(swiat.runner_a)   # runner_a2 tego samego zespolu zostaje online
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _pobierz_http(klient, run_id, "plan.md")

    assert odp.status_code == 409
    assert isinstance(odp.json()["detail"], str) and odp.json()["detail"]
    assert swiat.runner.wywolania == []


@pytest.mark.parametrize("sciezka", [
    "%2e%2e/plan.md", "a/%2e%2e/%2e%2e/plan.md", "%2e%2e", "a/%2e%2e/b.md", "..%2fplan.md",
    "/etc/passwd", "%2fetc%2fpasswd", "a%00b.md",
], ids=["dwie_kropki", "dwie_kropki_w_glebi", "same_kropki", "kropki_wracajace_do_srodka",
        "kropki_z_ukosnikiem", "bezwzgledna", "bezwzgledna_zakodowana", "bajt_zerowy"])
async def test_niebezpieczna_sciezka_artefaktu_w_adresie_daje_422_a_runner_nie_jest_pytany(
    swiat, katalog_runow, sciezka,
):
    run_id = await _utworz_run(swiat, swiat.tenant_a, swiat.runner_a)
    (katalog_runow / "plan.md").write_text(TRESC_SEKRETNA, encoding="utf-8")
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _pobierz_http(klient, run_id, sciezka)

    assert odp.status_code == 422, odp.text
    assert TRESC_SEKRETNA not in odp.text
    assert swiat.runner.wywolania == []


async def test_dowiazanie_wychodzace_z_katalogu_runu_daje_403_i_nie_oddaje_tresci_celu(
    swiat, katalog_runow,
):
    run_id = await _utworz_run(swiat, swiat.tenant_a, swiat.runner_a)
    cel = katalog_runow / "obcy.txt"
    cel.write_text(TRESC_SEKRETNA, encoding="utf-8")
    (katalog_runow / run_id).mkdir()
    (katalog_runow / run_id / "wyjscie.md").symlink_to(cel)
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _pobierz_http(klient, run_id, "wyjscie.md")

    assert odp.status_code == 403
    assert odp.json() == {"detail": KOMUNIKAT_POZA_ZAKRESEM}
    assert TRESC_SEKRETNA not in odp.text


async def test_sekret_w_katalogu_runu_daje_403_i_nie_oddaje_tresci(swiat, katalog_runow):
    run_id = await _utworz_run(swiat, swiat.tenant_a, swiat.runner_a)
    _artefakt_na_dysku(katalog_runow, run_id, ".env", TRESC_SEKRETNA.encode())
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _pobierz_http(klient, run_id, ".env")

    assert odp.status_code == 403
    assert TRESC_SEKRETNA not in odp.text


async def test_artefakt_binarny_daje_409_z_komunikatem_runnera_bez_tresci(swiat, katalog_runow):
    run_id = await _utworz_run(swiat, swiat.tenant_a, swiat.runner_a)
    _artefakt_na_dysku(katalog_runow, run_id, "obraz.bin", TRESC_SEKRETNA.encode() + b"\xff\xfe")
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _pobierz_http(klient, run_id, "obraz.bin")

    assert odp.status_code == 409
    assert odp.json() == {"detail": "plik nie jest tekstem UTF-8"}


async def test_brak_artefaktu_daje_409_z_komunikatem_runnera(swiat, katalog_runow):
    run_id = await _utworz_run(swiat, swiat.tenant_a, swiat.runner_a)
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _pobierz_http(klient, run_id, "brak.md")

    assert odp.status_code == 409
    assert odp.json() == {"detail": "sciezka nie istnieje"}


async def test_odczyt_artefaktu_nie_zmienia_bazy_ani_pliku_na_dysku(swiat, katalog_runow):
    run_id = await _utworz_run(swiat, swiat.tenant_a, swiat.runner_a)
    plik = _artefakt_na_dysku(katalog_runow, run_id, "plan.md", b"plan")
    klient = await swiat.zalogowany(swiat.wlasciciel_a)
    async with swiat.maker() as s:
        przed = (await s.execute(select(func.count()).select_from(Run))).scalar_one()

    odp = await _pobierz_http(klient, run_id, "plan.md")

    assert odp.status_code == 200, odp.text
    async with swiat.maker() as s:
        assert (await s.execute(select(func.count()).select_from(Run))).scalar_one() == przed
    assert plik.read_bytes() == b"plan"
    assert json.loads(odp.text)["text"] == "plan"
