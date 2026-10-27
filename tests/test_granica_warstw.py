"""Granica warstw: api/ nie importuje modelu ani SQLAlchemy żadną drogą (HAR-27).

Każdy wzorzec leży w osobnym pliku w osobnym katalogu `api/` (osobny `tmp_path`
na przypadek), więc o wyniku decyduje wyłącznie ta jedna reguła.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from check_layers import sprawdz  # noqa: E402


def _sprawdz_plik_api(tmp_path: Path, tresc: str) -> list[str]:
    """Zapisuje jeden plik warstwy api/ i sprawdza granicę warstw tylko na nim."""
    api = tmp_path / "api"
    api.mkdir()
    (api / "m.py").write_text(tresc, encoding="utf-8")
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


def test_naruszenie_wskazuje_numer_linii_importu_a_nie_poczatek_pliku(tmp_path):
    tresc = '"""Trasa."""\n\nimport cockpit.db.models as m2\n'
    assert _sprawdz_plik_api(tmp_path, tresc) == ["api/m.py:3 import cockpit.db.models"]


def test_dzisiejsza_warstwa_api_nie_ma_naruszen():
    assert sprawdz() == []
