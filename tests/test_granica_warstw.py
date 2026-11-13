"""Granica warstw: api/ nie importuje modelu ani SQLAlchemy żadną drogą (HAR-27).

Każdy wzorzec leży w osobnym pliku w osobnym katalogu `api/` (osobny `tmp_path`
na przypadek), więc o wyniku decyduje wyłącznie ta jedna reguła.
Plik pilnuje też wywołań metod sesji na zmiennej typu `AsyncSession` (HAR-28).
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

KORZEN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KORZEN / "tools"))
sys.path.insert(0, str(KORZEN / "tests"))
from check_layers import sprawdz, sprawdz_role_czlonkostwa  # noqa: E402
from procesy import uruchom_proces_sync, zakoncz_proces_sync  # noqa: E402


def _sprawdz_plik_api(tmp_path: Path, tresc: str, sciezka: str = "m.py") -> list[str]:
    """Zapisuje jeden plik warstwy api/ (ścieżka względem api/) i sprawdza go samego."""
    api = tmp_path / "api"
    plik = api / sciezka
    plik.parent.mkdir(parents=True)
    plik.write_text(tresc, encoding="utf-8")
    return sprawdz(api)


@pytest.mark.parametrize(
    ("tresc", "oczekiwane"),
    [
        pytest.param(
            "from cockpit.db import models\n",
            "api/m.py:1 import z cockpit.db: models",
            id="modul_models_z_pakietu_bazy",
        ),
        pytest.param(
            "from cockpit.db import models as m\n",
            "api/m.py:1 import z cockpit.db: models",
            id="modul_models_z_pakietu_bazy_pod_aliasem",
        ),
        pytest.param(
            "from cockpit.db import session, models\n",
            "api/m.py:1 import z cockpit.db: models",
            id="modul_models_wsrod_innych_nazw",
        ),
        pytest.param(
            "import cockpit.db.models as m2\n",
            "api/m.py:1 import cockpit.db.models",
            id="import_modulu_models_pod_aliasem",
        ),
        pytest.param(
            "import cockpit.db.models\n",
            "api/m.py:1 import cockpit.db.models",
            id="import_modulu_models_bez_aliasu",
        ),
        pytest.param(
            "from cockpit.db.models import Run\n",
            "api/m.py:1 import z cockpit.db.models",
            id="encja_run_z_modulu_models",
        ),
        pytest.param(
            "from sqlalchemy import select\n",
            "api/m.py:1 import z sqlalchemy",
            id="zapytanie_select_z_sqlalchemy",
        ),
        pytest.param(
            "from cockpit.db.models import RunStatus, Run\n",
            "api/m.py:1 import z cockpit.db.models",
            id="dozwolona_nazwa_obok_encji_nie_ratuje_importu",
        ),
        pytest.param(
            "from sqlalchemy.orm import Session\n",
            "api/m.py:1 import z sqlalchemy.orm",
            id="podmodul_sqlalchemy_z_prefiksem_kropkowym",
        ),
        pytest.param(
            "import sqlalchemy\n",
            "api/m.py:1 import sqlalchemy",
            id="import_sqlalchemy",
        ),
        pytest.param(
            "import sqlalchemy.orm\n",
            "api/m.py:1 import sqlalchemy.orm",
            id="import_podmodulu_sqlalchemy",
        ),
    ],
)
def test_import_modelu_albo_sqlalchemy_w_warstwie_api_lamie_granice_warstw(
    tmp_path, tresc, oczekiwane
):
    assert _sprawdz_plik_api(tmp_path, tresc) == [oczekiwane]


@pytest.mark.parametrize(
    "tresc",
    [
        pytest.param("from cockpit.db import session\n", id="sesja_z_pakietu_bazy"),
        pytest.param(
            "from cockpit.db.session import get_session\n", id="funkcja_get_session"
        ),
        pytest.param("import cockpit.db.session\n", id="import_modulu_sesji"),
        pytest.param(
            "import sqlalchemy_utils\n", id="pakiet_o_nazwie_zaczynajacej_sie_jak_sqlalchemy"
        ),
        pytest.param(
            "from sqlalchemy.ext.asyncio import AsyncSession\n", id="typ_async_session"
        ),
        pytest.param(
            "from cockpit.db.models import RunStatus, RunnerKind\n",
            id="enumy_wejscia_run_status_i_runner_kind",
        ),
        pytest.param(
            "from cockpit.db.models import AsyncSession, RunStatus, RunnerKind\n",
            id="wszystkie_nazwy_z_wyjatku_naraz",
        ),
        pytest.param(
            "from cockpit.db_pomocnicze import models\n",
            id="pakiet_o_nazwie_zaczynajacej_sie_jak_pakiet_bazy",
        ),
    ],
)
def test_dozwolony_import_nie_lamie_granicy_warstw(tmp_path, tresc):
    assert _sprawdz_plik_api(tmp_path, tresc) == []


@pytest.mark.parametrize(
    ("tresc", "oczekiwane"),
    [
        pytest.param(
            "import cockpit.db\n",
            "api/m.py:1 import cockpit.db",
            id="import_pakietu_bazy",
        ),
        pytest.param(
            "import cockpit.db as d\n",
            "api/m.py:1 import cockpit.db",
            id="import_pakietu_bazy_pod_aliasem",
        ),
        pytest.param(
            "from cockpit import db\n",
            "api/m.py:1 import z cockpit: db",
            id="pakiet_bazy_z_pakietu_cockpit",
        ),
        pytest.param(
            "from cockpit import db as d\n",
            "api/m.py:1 import z cockpit: db",
            id="pakiet_bazy_z_pakietu_cockpit_pod_aliasem",
        ),
        pytest.param(
            "from cockpit import services, db\n",
            "api/m.py:1 import z cockpit: db",
            id="pakiet_bazy_wsrod_innych_nazw_z_cockpit",
        ),
        pytest.param(
            "from .. import db\n",
            "api/m.py:1 import z cockpit: db",
            id="pakiet_bazy_importem_wzglednym",
        ),
        pytest.param(
            "from cockpit.db import *\n",
            "api/m.py:1 import z cockpit.db: *",
            id="gwiazdka_z_pakietu_bazy",
        ),
        pytest.param(
            "from ..db import *\n",
            "api/m.py:1 import z cockpit.db: *",
            id="gwiazdka_z_pakietu_bazy_importem_wzglednym",
        ),
        pytest.param(
            "from ..db import models\n",
            "api/m.py:1 import z cockpit.db: models",
            id="modul_models_importem_wzglednym",
        ),
        pytest.param(
            "from ..db.models import Run\n",
            "api/m.py:1 import z cockpit.db.models",
            id="encja_run_importem_wzglednym",
        ),
        pytest.param(
            "from cockpit.db.models import *\n",
            "api/m.py:1 import z cockpit.db.models",
            id="gwiazdka_z_modulu_models_nie_jest_ratowana_wyjatkiem_typow",
        ),
        pytest.param(
            "from ... import x\n",
            "api/m.py:1 import wzgledny poza pakietem cockpit",
            id="import_wzgledny_wychodzacy_poza_pakiet_cockpit",
        ),
        pytest.param(
            "from .... import x\n",
            "api/m.py:1 import wzgledny poza pakietem cockpit",
            id="cztery_kropki_wychodza_dwa_poziomy_poza_pakiet_cockpit",
        ),
        pytest.param(
            "from ....db import models\n",
            "api/m.py:1 import wzgledny poza pakietem cockpit",
            id="cztery_kropki_do_db_nie_udaja_pakietu_bazy",
        ),
    ],
)
def test_import_wzgledny_pakietu_bazy_albo_gwiazdki_lamie_granice_warstw(
    tmp_path, tresc, oczekiwane
):
    assert _sprawdz_plik_api(tmp_path, tresc) == [oczekiwane]


@pytest.mark.parametrize(
    "tresc",
    [
        pytest.param("from . import bus\n", id="modul_z_tego_samego_pakietu_api"),
        pytest.param("from .bledy import x\n", id="nazwa_z_modulu_siostrzanego_api"),
        pytest.param("from ..services import runs\n", id="modul_uslug_importem_wzglednym"),
        pytest.param("from ..db import session\n", id="sesja_z_pakietu_bazy_importem_wzglednym"),
        pytest.param(
            "from ..db.session import get_session\n",
            id="funkcja_get_session_importem_wzglednym",
        ),
        pytest.param("from cockpit import services\n", id="pakiet_uslug_z_cockpit"),
        pytest.param("import cockpit\n", id="import_samego_pakietu_cockpit"),
        pytest.param(
            "from cockpit.db.session import *\n", id="gwiazdka_z_modulu_sesji"
        ),
        pytest.param(
            "from ..db.models import RunStatus\n",
            id="dozwolony_typ_wejscia_importem_wzglednym",
        ),
    ],
)
def test_dozwolony_import_wzgledny_i_pakietowy_nie_lamie_granicy_warstw(tmp_path, tresc):
    assert _sprawdz_plik_api(tmp_path, tresc) == []


@pytest.mark.parametrize(
    ("sciezka", "tresc", "oczekiwane"),
    [
        pytest.param(
            "sub/x.py",
            "from ...db import models\n",
            ["api/sub/x.py:1 import z cockpit.db: models"],
            id="trzy_kropki_w_podpakiecie_siegaja_do_pakietu_bazy",
        ),
        pytest.param(
            "sub/x.py",
            "from ..db import models\n",
            [],
            id="dwie_kropki_w_podpakiecie_to_db_wewnatrz_api",
        ),
        pytest.param(
            "sub/__init__.py",
            "from ..db import models\n",
            [],
            id="pakietem_pliku_init_jest_jego_katalog",
        ),
        pytest.param(
            "__init__.py",
            "from .. import db\n",
            ["api/__init__.py:1 import z cockpit: db"],
            id="dwie_kropki_w_init_api_siegaja_do_pakietu_cockpit",
        ),
    ],
)
def test_poziom_importu_wzglednego_liczy_sie_od_pakietu_pliku(
    tmp_path, sciezka, tresc, oczekiwane
):
    assert _sprawdz_plik_api(tmp_path, tresc, sciezka) == oczekiwane


def test_naruszenie_wskazuje_numer_linii_importu_a_nie_poczatek_pliku(tmp_path):
    tresc = '"""Trasa."""\n\nimport cockpit.db.models as m2\n'
    assert _sprawdz_plik_api(tmp_path, tresc) == ["api/m.py:3 import cockpit.db.models"]


def _modul_z_sesja(metoda: str) -> str:
    """Moduł z wywołaniem metody na parametrze `s: AsyncSession` w linii 4."""
    return (
        "from sqlalchemy.ext.asyncio import AsyncSession\n"
        "\n"
        "async def f(s: AsyncSession):\n"
        f"    return await s.{metoda}(1)\n"
    )


@pytest.mark.parametrize(
    "metoda",
    [
        pytest.param("get", id="pobranie_po_kluczu_get"),
        pytest.param("scalar", id="pojedyncza_wartosc_scalar"),
        pytest.param("scalars", id="seria_wartosci_scalars"),
        pytest.param("merge", id="scalenie_encji_merge"),
        pytest.param("flush", id="zrzut_zmian_flush"),
        pytest.param("refresh", id="odswiezenie_encji_refresh"),
        pytest.param("stream", id="strumien_wierszy_stream"),
        pytest.param("stream_scalars", id="strumien_wartosci_stream_scalars"),
    ],
)
def test_metoda_sesji_wolana_na_parametrze_async_session_lamie_granice_warstw(
    tmp_path, metoda
):
    # Wywołanie leży w linii 4 modułu pomocniczego; liczymy numer z samej treści.
    tresc = _modul_z_sesja(metoda)
    linia = tresc.splitlines().index(f"    return await s.{metoda}(1)") + 1
    assert _sprawdz_plik_api(tmp_path, tresc) == [
        f"api/m.py:{linia} wywolanie .{metoda}()"
    ]


@pytest.mark.parametrize(
    ("tresc", "oczekiwane"),
    [
        pytest.param(
            "def f(s: AsyncSession):\n    return s.get(1)\n",
            ["api/m.py:2 wywolanie .get()"],
            id="parametr_w_zwyklej_funkcji_nie_async",
        ),
        pytest.param(
            "def f(s: AsyncSession, /):\n    return s.get(1)\n",
            ["api/m.py:2 wywolanie .get()"],
            id="parametr_tylko_pozycyjny",
        ),
        pytest.param(
            "def f(*, s: AsyncSession):\n    return s.get(1)\n",
            ["api/m.py:2 wywolanie .get()"],
            id="parametr_tylko_nazwany",
        ),
        pytest.param(
            "def f():\n    s: AsyncSession = zrob()\n    return s.get(1)\n",
            ["api/m.py:3 wywolanie .get()"],
            id="zmienna_z_adnotacja_w_ciele_funkcji_bez_parametru_sesji",
        ),
        pytest.param(
            "async def f(sesja: AsyncSession):\n    return await sesja.get(1)\n",
            ["api/m.py:2 wywolanie .get()"],
            id="sesja_pod_inna_nazwa_niz_s",
        ),
        pytest.param(
            "def f(s: AsyncSession):\n    def g(s):\n        return 1\n    return s.get(1)\n",
            ["api/m.py:4 wywolanie .get()"],
            id="wywolanie_po_funkcji_zagniezdzonej_jest_zgloszone_z_wlasna_linia",
        ),
    ],
)
def test_sesja_zwiazana_na_rozne_sposoby_jest_rozpoznana_po_typie(
    tmp_path, tresc, oczekiwane
):
    assert _sprawdz_plik_api(tmp_path, tresc) == oczekiwane


def test_wywolanie_execute_na_sesji_jest_zgloszone_dokladnie_raz(tmp_path):
    tresc = "async def f(s: AsyncSession):\n    return await s.execute('x')\n"
    assert _sprawdz_plik_api(tmp_path, tresc) == ["api/m.py:2 wywolanie .execute()"]


@pytest.mark.parametrize(
    "tresc",
    [
        pytest.param(
            "def f(d: dict):\n    return d.get('k')\n",
            id="get_na_slowniku",
        ),
        pytest.param(
            "import asyncio\n\nasync def f(q: asyncio.Queue):\n    return await q.get()\n",
            id="get_na_kolejce_asyncio",
        ),
        pytest.param(
            "from fastapi import FastAPI\n\n"
            "app = FastAPI()\n\n"
            "@app.get('/x')\n"
            "async def trasa(s: AsyncSession):\n"
            "    return 1\n",
            id="dekorator_trasy_get_na_obiekcie_app_choc_sesja_jest_w_trasie",
        ),
        pytest.param(
            "def f(s: AsyncSession):\n    return 1\n\n"
            "def g(s):\n    return s.get('k')\n",
            id="sesja_w_jednej_funkcji_nie_dotyczy_innej_funkcji_o_tej_samej_nazwie",
        ),
        pytest.param(
            "def f(s: AsyncSession):\n"
            "    def g(s):\n"
            "        return s.get('k')\n"
            "    return g\n",
            id="funkcja_zagniezdzona_z_wlasnym_parametrem_bez_adnotacji",
        ),
        pytest.param(
            "def f(s: dict):\n    return s.get('k')\n",
            id="adnotacja_innego_typu_niz_async_session",
        ),
        pytest.param(
            "async def f(s: AsyncSession):\n    await s.rollback()\n",
            id="metoda_spoza_zbioru_na_sesji",
        ),
        pytest.param(
            "def f(s: AsyncSession, request):\n    return request.cookies.get('x')\n",
            id="odbiorca_inny_niz_sesja_choc_sesja_jest_w_funkcji",
        ),
        pytest.param(
            "def f():\n    x: dict = {}\n    return x.get('k')\n",
            id="zmienna_w_ciele_z_adnotacja_innego_typu_niz_sesja",
        ),
        pytest.param(
            "def f(self):\n    self.s: AsyncSession = zrob()\n    return self.s.get(1)\n",
            id="adnotacja_sesji_na_atrybucie_obiektu_nie_robi_z_niego_nazwy_sesji",
        ),
        pytest.param(
            "def f():\n    def g():\n        x: AsyncSession = zrob()\n    return x.get(1)\n",
            id="zmienna_sesji_z_funkcji_zagniezdzonej_nie_przecieka_do_zewnetrznej",
        ),
        pytest.param(
            "def f():\n    async def g():\n        x: AsyncSession = zrob()\n    return x.get(1)\n",
            id="zmienna_sesji_z_asynchronicznej_funkcji_zagniezdzonej_nie_przecieka",
        ),
        pytest.param(
            "cfg = {}\ncfg.get('k')\n",
            id="get_na_slowniku_poza_jakakolwiek_funkcja_na_poziomie_modulu",
        ),
        pytest.param(
            "class Ustawienia:\n    cfg = {}\n    wartosc = cfg.get('k')\n",
            id="get_na_slowniku_w_ciele_klasy_poza_funkcja",
        ),
    ],
)
def test_wywolanie_ktore_nie_jest_metoda_sesji_na_sesji_nie_lamie_granicy_warstw(
    tmp_path, tresc
):
    assert _sprawdz_plik_api(tmp_path, tresc) == []


def test_dzisiejsza_warstwa_api_nie_ma_naruszen():
    assert sprawdz() == []


# --- rola czlonkostwa w zespole czytana tylko w services/dostep.py i db/models.py (HAR-187) -----

KOMUNIKAT = "{rel}:{linia} rola czlonkostwa poza services/dostep.py: {opis}"


def _sprawdz_role(tmp_path: Path, pliki: dict[str, str]) -> list[str]:
    """Zapisuje pliki pod `tmp_path` (klucz: sciezka wzgledna POSIX) i sprawdza regule roli."""
    for sciezka, tresc in pliki.items():
        plik = tmp_path / sciezka
        plik.parent.mkdir(parents=True, exist_ok=True)
        plik.write_text(tresc, encoding="utf-8")
    return sprawdz_role_czlonkostwa(tmp_path)


def _m(linia: int, opis: str, rel: str = "services/m.py") -> str:
    return KOMUNIKAT.format(rel=rel, linia=linia, opis=opis)


@pytest.mark.parametrize(
    ("tresc", "oczekiwane"),
    [
        pytest.param(
            "from x import select\nz = select(Membership.rola)\n",
            [_m(2, "Membership.rola")],
            id="zapytanie_o_kolumne_rola_modelu_membership",
        ),
        pytest.param(
            "z = models.Membership.rola\n",
            [_m(1, "models.Membership.rola")],
            id="kolumna_rola_przez_modul_models",
        ),
        pytest.param(
            "def f(kontekst: Kontekst):\n    return kontekst.rola\n",
            [_m(2, "kontekst.rola")],
            id="rola_odczytana_z_kontekstu_przekazanego_parametrem",
        ),
        pytest.param(
            "def f(m: Membership):\n    return m.rola\n",
            [_m(2, "m.rola")],
            id="rola_odczytana_z_czlonkostwa_przekazanego_parametrem",
        ),
        pytest.param(
            "async def f(kontekst: Kontekst):\n    return kontekst.rola\n",
            [_m(2, "kontekst.rola")],
            id="rola_z_kontekstu_w_funkcji_asynchronicznej",
        ),
        pytest.param(
            "def f(*, k: Kontekst):\n    return k.rola\n",
            [_m(2, "k.rola")],
            id="kontekst_jako_parametr_tylko_nazwany",
        ),
        pytest.param(
            "def f(k: Kontekst, /):\n    return k.rola\n",
            [_m(2, "k.rola")],
            id="kontekst_jako_parametr_tylko_pozycyjny",
        ),
        pytest.param(
            "def f(*, m: Membership):\n    return m.rola\n",
            [_m(2, "m.rola")],
            id="czlonkostwo_jako_parametr_tylko_nazwany",
        ),
        pytest.param(
            "def f(m: Membership, /):\n    return m.rola\n",
            [_m(2, "m.rola")],
            id="czlonkostwo_jako_parametr_tylko_pozycyjny",
        ),
        pytest.param(
            "def f():\n    k: Kontekst = zrob()\n    return k.rola\n",
            [_m(3, "k.rola")],
            id="kontekst_jako_zmienna_z_adnotacja_w_ciele_funkcji",
        ),
        pytest.param(
            "def f():\n    m: Membership = zrob()\n    return m.rola\n",
            [_m(3, "m.rola")],
            id="czlonkostwo_jako_zmienna_z_adnotacja_w_ciele_funkcji",
        ),
        pytest.param(
            "def f(k: dostep.Kontekst):\n    return k.rola\n",
            [_m(2, "k.rola")],
            id="kontekst_z_adnotacja_przez_modul",
        ),
        pytest.param(
            "def f(m: models.Membership):\n    return m.rola\n",
            [_m(2, "m.rola")],
            id="czlonkostwo_z_adnotacja_przez_modul",
        ),
        pytest.param(
            "def f():\n    k: dostep.Kontekst = zrob()\n    return k.rola\n",
            [_m(3, "k.rola")],
            id="zmienna_z_adnotacja_kontekstu_przez_modul",
        ),
        pytest.param(
            "x = RolaWZespole.owner\n",
            [_m(1, "RolaWZespole")],
            id="uzycie_enuma_roli_w_zespole",
        ),
        pytest.param(
            "x = models.RolaWZespole.owner\n",
            [_m(1, "models.RolaWZespole")],
            id="uzycie_enuma_roli_w_zespole_przez_modul",
        ),
        pytest.param(
            "from cockpit.db.models import RolaWZespole\n",
            [_m(1, "import RolaWZespole")],
            id="import_enuma_roli_w_zespole",
        ),
        pytest.param(
            "from cockpit.db.models import Membership, RolaWZespole\n",
            [_m(1, "import RolaWZespole")],
            id="import_enuma_roli_wsrod_innych_nazw",
        ),
        pytest.param(
            "from cockpit.db.models import RolaWZespole as R\n",
            [_m(1, "import RolaWZespole")],
            id="import_enuma_roli_pod_aliasem",
        ),
        pytest.param(
            "from cockpit.db.models import RolaWZespole\nx = RolaWZespole.owner\n",
            [_m(1, "import RolaWZespole"), _m(2, "RolaWZespole")],
            id="import_i_uzycie_to_dwa_osobne_komunikaty",
        ),
        pytest.param(
            "\n\n\n\ndef f(k: Kontekst):\n    pass\n    return k.rola\n",
            [_m(7, "k.rola")],
            id="numer_linii_to_linia_odczytu_a_nie_deklaracji_ani_poczatek_pliku",
        ),
    ],
)
def test_odczyt_roli_czlonkostwa_poza_dostepem_jest_zgloszony_calym_komunikatem(
    tmp_path, tresc, oczekiwane
):
    assert _sprawdz_role(tmp_path, {"services/m.py": tresc}) == oczekiwane


@pytest.mark.parametrize(
    "tresc",
    [
        pytest.param(
            "def f(lista):\n    return [{'rola': w.rola} for w in lista]\n",
            id="rola_skilla_z_petli_jak_w_katalogu",
        ),
        pytest.param(
            "def f(lista):\n    for w in lista:\n        return {'rola': w.rola}\n",
            id="rola_skilla_w_zwyklej_petli",
        ),
        pytest.param(
            "def f(kontekst):\n    return kontekst.rola\n",
            id="parametr_bez_adnotacji",
        ),
        pytest.param(
            "def f(d: Definicja):\n    return d.rola\n",
            id="parametr_z_adnotacja_innego_typu",
        ),
        pytest.param(
            "def f(d: katalog.Definicja):\n    return d.rola\n",
            id="parametr_z_adnotacja_innego_typu_przez_modul",
        ),
        pytest.param(
            "def f():\n    d: Definicja = zrob()\n    return d.rola\n",
            id="zmienna_z_adnotacja_innego_typu",
        ),
        pytest.param(
            "def f(k: Kontekst, d):\n    return d.rola\n",
            id="inny_odbiorca_niz_parametr_typu_kontekst",
        ),
        pytest.param(
            "def f(k: Kontekst):\n    return k.x.rola\n",
            id="odbiorca_zlozony_nie_jest_nazwa_parametru",
        ),
        pytest.param(
            "def f(k: Kontekst):\n    return k.user_id\n",
            id="inny_atrybut_kontekstu_niz_rola",
        ),
        pytest.param(
            "def f(k: dostep.Inny):\n    return k.rola\n",
            id="adnotacja_przez_modul_o_innej_nazwie_typu",
        ),
        pytest.param(
            "x = Membership.user_id\n",
            id="inna_kolumna_czlonkostwa_user_id",
        ),
        pytest.param(
            "x = Membership.tenant_id\n",
            id="inna_kolumna_czlonkostwa_tenant_id",
        ),
        pytest.param(
            "x = Kontekst.rola\n",
            id="nazwa_typu_kontekst_bez_wiazania_z_parametrem",
        ),
        pytest.param(
            "from cockpit.db.models import Membership\nx = models.Membership\n",
            id="samo_czlonkostwo_bez_odczytu_roli",
        ),
        pytest.param(
            "x = RolaWZespoleInna.owner\n",
            id="nazwa_tylko_zaczynajaca_sie_jak_enum_roli",
        ),
        pytest.param(
            "from cockpit.db.models import RolaInna\n",
            id="import_innej_nazwy_z_modelu",
        ),
        pytest.param(
            "def f(k: Kontekst):\n    return 1\n\ndef g(k):\n    return k.rola\n",
            id="kontekst_w_jednej_funkcji_nie_dotyczy_innej_o_tej_samej_nazwie_parametru",
        ),
        pytest.param(
            "def f():\n    k: Kontekst = zrob()\n    def g(k):\n        return k.rola\n    return g\n",
            id="funkcja_zagniezdzona_bez_adnotacji_nie_dziedziczy_typu_zmiennej",
        ),
        pytest.param(
            "def f(k: Kontekst):\n    def g(k):\n        return k.rola\n    return g\n",
            id="funkcja_zagniezdzona_bez_adnotacji_nie_dziedziczy_typu_parametru",
        ),
        pytest.param(
            "def f():\n    def g():\n        k: Kontekst = zrob()\n    return k.rola\n",
            id="zmienna_z_funkcji_zagniezdzonej_nie_przecieka_do_zewnetrznej",
        ),
        pytest.param(
            "def f():\n    async def g():\n        m: Membership = zrob()\n    return m.rola\n",
            id="zmienna_z_asynchronicznej_funkcji_zagniezdzonej_nie_przecieka",
        ),
    ],
)
def test_odczyt_roli_ktory_nie_dotyczy_czlonkostwa_nie_jest_zgloszony(tmp_path, tresc):
    assert _sprawdz_role(tmp_path, {"services/m.py": tresc}) == []


NARUSZENIE = (
    "from cockpit.db.models import RolaWZespole\n"
    "def f(k: Kontekst):\n"
    "    return k.rola, Membership.rola\n"
)


@pytest.mark.parametrize(
    "sciezka",
    [
        pytest.param("services/dostep.py", id="uslugi_dostepu"),
        pytest.param("db/models.py", id="model_bazy"),
    ],
)
def test_odczyt_roli_czlonkostwa_w_dozwolonym_miejscu_nie_jest_zgloszony(tmp_path, sciezka):
    assert _sprawdz_role(tmp_path, {sciezka: NARUSZENIE}) == []


@pytest.mark.parametrize(
    "sciezka",
    [
        pytest.param("services/dostep_inny.py", id="plik_o_nazwie_zaczynajacej_sie_jak_dostep"),
        pytest.param("inne/services/dostep.py", id="dostep_w_innym_katalogu_o_tym_samym_sufiksie"),
        pytest.param("services/inne/dostep.py", id="dostep_w_podkatalogu_uslug"),
        pytest.param("dostep.py", id="dostep_w_korzeniu_zamiast_w_uslugach"),
        pytest.param("api/dostep.py", id="dostep_w_warstwie_api"),
        pytest.param("services/models.py", id="models_w_uslugach"),
        pytest.param("inne/db/models.py", id="model_w_innym_katalogu_o_tym_samym_sufiksie"),
        pytest.param("db/models_inny.py", id="plik_o_nazwie_zaczynajacej_sie_jak_models"),
        pytest.param("models.py", id="models_w_korzeniu_zamiast_w_db"),
    ],
)
def test_odczyt_roli_czlonkostwa_w_pliku_o_podobnej_sciezce_do_dozwolonej_jest_zgloszony(
    tmp_path, sciezka
):
    assert _sprawdz_role(tmp_path, {sciezka: NARUSZENIE}) == [
        _m(1, "import RolaWZespole", sciezka),
        _m(3, "k.rola", sciezka),
        _m(3, "Membership.rola", sciezka),
    ]


def test_naruszenia_z_kilku_plikow_ida_w_kolejnosci_posortowanych_sciezek(tmp_path):
    tresc = "x = RolaWZespole.owner\n"
    wynik = _sprawdz_role(
        tmp_path,
        {"services/b.py": tresc, "api/a.py": tresc, "services/dostep.py": tresc},
    )
    assert wynik == [
        _m(1, "RolaWZespole", "api/a.py"),
        _m(1, "RolaWZespole", "services/b.py"),
    ]


def test_naruszenia_w_plytkim_pliku_ida_po_naruszeniach_w_podkatalogu_gdy_sciezki_sa_posortowane(
    tmp_path,
):
    # rglob oddaje najpierw pliki plytkie, potem podkatalogi (services/z.py, services/b.py,
    # services/a/x.py); posortowane sciezki zaczynaja sie od services/a/x.py.
    tresc = "x = RolaWZespole.owner\n"
    wynik = _sprawdz_role(
        tmp_path,
        {"services/z.py": tresc, "services/b.py": tresc, "services/a/x.py": tresc},
    )
    assert wynik == [
        _m(1, "RolaWZespole", "services/a/x.py"),
        _m(1, "RolaWZespole", "services/b.py"),
        _m(1, "RolaWZespole", "services/z.py"),
    ]


def test_naruszenia_w_jednym_pliku_ida_w_kolejnosci_linii_nie_glebokosci_drzewa(tmp_path):
    # ast.walk odwiedza plytki odczyt z linii 3 przed glebszym z linii 2.
    tresc = "def f():\n    return Membership.rola\nx = RolaWZespole\n"
    assert _sprawdz_role(tmp_path, {"services/m.py": tresc}) == [
        _m(2, "Membership.rola"),
        _m(3, "RolaWZespole"),
    ]


def test_dwa_naruszenia_w_jednej_linii_ida_w_kolejnosci_kolumn_nie_opisow(tmp_path):
    # ast.walk odwiedza Membership.rola (plytszy, kolumna 28) przed RolaWZespole (glebszy,
    # kolumna 7); opisy alfabetycznie tez daja Membership pierwsze, wiec decyduje kolumna.
    tresc = "x = [f(RolaWZespole.owner), Membership.rola]\n"
    assert _sprawdz_role(tmp_path, {"services/m.py": tresc}) == [
        _m(1, "RolaWZespole"),
        _m(1, "Membership.rola"),
    ]


def test_dzisiejszy_kod_nie_czyta_roli_czlonkostwa_poza_dostepem():
    assert sprawdz_role_czlonkostwa() == []


def _uruchom_skrypt(skrypt: Path) -> tuple[int, list[str]]:
    proc = uruchom_proces_sync(
        [sys.executable, str(skrypt)],
        cwd=KORZEN,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        wyjscie, _ = proc.communicate(timeout=60)
    finally:
        zakoncz_proces_sync(proc, nazwa="check_layers")
    return proc.returncode, wyjscie.splitlines()


def _kopia_skryptu(tmp_path: Path, pliki: dict[str, str]) -> Path:
    """Kopia skryptu w `tmp/tools/` i drzewo `tmp/src/cockpit/...` czytane przez jego SRC i API."""
    skrypt = tmp_path / "tools" / "check_layers.py"
    skrypt.parent.mkdir(parents=True)
    shutil.copy(KORZEN / "tools" / "check_layers.py", skrypt)
    (tmp_path / "src" / "cockpit" / "api").mkdir(parents=True)
    (tmp_path / "src" / "cockpit" / "services").mkdir(parents=True)
    for sciezka, tresc in pliki.items():
        plik = tmp_path / "src" / "cockpit" / sciezka
        plik.parent.mkdir(parents=True, exist_ok=True)
        plik.write_text(tresc, encoding="utf-8")
    return skrypt


NAGLOWEK_ROLI = "Rola czlonkostwa czytana poza services/dostep.py i db/models.py:"
NAGLOWEK_API = "Warstwa api/ dotyka bazy. Przenies to do cockpit/services/:"
LINIA_API_OK = "api/ nie dotyka bazy - w porzadku"
LINIA_ROLA_OK = "rola czlonkostwa tylko w services/dostep.py - w porzadku"


def test_skrypt_na_czystym_kodzie_konczy_sie_kodem_zero_i_dwiema_liniami_w_porzadku():
    kod, linie = _uruchom_skrypt(KORZEN / "tools" / "check_layers.py")
    assert kod == 0
    assert linie == [LINIA_API_OK, LINIA_ROLA_OK]


def test_skrypt_z_odczytem_roli_czlonkostwa_konczy_sie_kodem_jeden_z_naglowkiem_i_naruszeniem(
    tmp_path,
):
    skrypt = _kopia_skryptu(tmp_path, {"services/konta.py": "x = RolaWZespole.owner\n"})
    kod, linie = _uruchom_skrypt(skrypt)
    assert kod == 1
    assert linie[0] == NAGLOWEK_ROLI
    assert [x.strip() for x in linie[1:]] == [_m(1, "RolaWZespole", "services/konta.py")]
    assert LINIA_ROLA_OK not in linie


def test_skrypt_z_naruszeniem_tylko_w_api_konczy_sie_kodem_jeden_i_nie_chwali_warstwy_api(
    tmp_path,
):
    skrypt = _kopia_skryptu(tmp_path, {"api/m.py": "import sqlalchemy\n"})
    kod, linie = _uruchom_skrypt(skrypt)
    assert kod == 1
    assert linie[0] == NAGLOWEK_API
    assert [x.strip() for x in linie[1:]] == ["api/m.py:1 import sqlalchemy"]
    assert LINIA_API_OK not in linie
    assert NAGLOWEK_ROLI not in linie


def test_skrypt_z_obiema_grupami_naruszen_wypisuje_obie_i_konczy_sie_kodem_jeden(tmp_path):
    skrypt = _kopia_skryptu(
        tmp_path,
        {"api/m.py": "import sqlalchemy\n", "services/konta.py": "x = RolaWZespole.owner\n"},
    )
    kod, linie = _uruchom_skrypt(skrypt)
    assert kod == 1
    assert NAGLOWEK_API in linie
    assert NAGLOWEK_ROLI in linie
    assert any(x.strip() == "api/m.py:1 import sqlalchemy" for x in linie)
    assert any(x.strip() == _m(1, "RolaWZespole", "services/konta.py") for x in linie)
