"""Pilnuje granicy warstw: api/ nie dotyka bazy (CLAUDE.md).

Regula bez sprawdzenia to zyczenie. To uruchamia sie w CI i w testach.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

API = Path(__file__).resolve().parent.parent / "src" / "cockpit" / "api"
ZAKAZANE_WYWOLANIA = {"select", "insert", "update", "delete", "execute", "add", "commit"}
ZAKAZANE_IMPORTY = {"sqlalchemy", "cockpit.db.models"}


def _zakazany(modul: str) -> bool:
    """Modul zakazany albo jego podmodul (sqlalchemy.orm tak, sqlalchemy_utils nie)."""
    return any(modul == z or modul.startswith(z + ".") for z in ZAKAZANE_IMPORTY)


def _pelny_modul(w: ast.ImportFrom, pakiet: list[str]) -> str | None:
    """Pelna nazwa modulu z `from ... import`; None, gdy poziom wychodzi poza `cockpit`.

    Zalozenie: skanowany katalog to pakiet `cockpit.api`, a `pakiet` to pakiet pliku
    (czesci nazwy). `level=1` to ten pakiet, kazdy kolejny poziom to segment wyzej.
    """
    if w.level == 0:
        return w.module or ""
    # Poziom wyzej niz caly pakiet: ujemny indeks wycinka dalby falszywy prefiks.
    if w.level - 1 >= len(pakiet):
        return None
    baza = pakiet[: len(pakiet) - (w.level - 1)]
    return ".".join([*baza, *([w.module] if w.module else [])])


def sprawdz(katalog: Path = API) -> list[str]:
    problemy: list[str] = []
    for plik in sorted(katalog.rglob("*.py")):
        rel = plik.relative_to(katalog.parent)
        # Pakiet pliku: katalog pliku (dla __init__.py tak samo jak dla zwyklego modulu).
        pakiet = ["cockpit", "api", *plik.relative_to(katalog).parent.parts]
        drzewo = ast.parse(plik.read_text(), filename=str(plik))
        for w in ast.walk(drzewo):
            if isinstance(w, ast.Import):
                for a in w.names:
                    # Pakiet `cockpit.db` dokladnie: `cockpit.db.session` jest dozwolony.
                    if _zakazany(a.name) or a.name == "cockpit.db":
                        problemy.append(f"{rel}:{w.lineno} import {a.name}")
            if isinstance(w, ast.ImportFrom):
                modul = _pelny_modul(w, pakiet)
                nazwy = {a.name for a in w.names}
                if modul is None:
                    problemy.append(f"{rel}:{w.lineno} import wzgledny poza pakietem cockpit")
                elif modul == "cockpit" and "db" in nazwy:
                    problemy.append(f"{rel}:{w.lineno} import z cockpit: db")
                elif _zakazany(modul):
                    # Wyjatek: typ sesji i enumy wejscia sa kontraktem, nie zapytaniem.
                    # Gwiazdka nie miesci sie w wyjatku: nie wiadomo, co wnosi.
                    if not nazwy <= {"AsyncSession", "RunStatus", "RunnerKind"}:
                        problemy.append(f"{rel}:{w.lineno} import z {modul}")
                elif modul == "cockpit.db" and "*" in nazwy:
                    problemy.append(f"{rel}:{w.lineno} import z cockpit.db: *")
                elif modul == "cockpit.db" and "models" in nazwy:
                    problemy.append(f"{rel}:{w.lineno} import z cockpit.db: models")
            if isinstance(w, ast.Call):
                f = w.func
                nazwa = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                if nazwa in ZAKAZANE_WYWOLANIA and not isinstance(f, ast.Name):
                    problemy.append(f"{rel}:{w.lineno} wywolanie .{nazwa}()")
                elif nazwa in {"select", "insert", "update", "delete"} and isinstance(f, ast.Name):
                    problemy.append(f"{rel}:{w.lineno} wywolanie {nazwa}()")
    return problemy


if __name__ == "__main__":
    p = sprawdz()
    if p:
        print("Warstwa api/ dotyka bazy. Przenies to do cockpit/services/:")
        for x in p:
            print("  ", x)
        sys.exit(1)
    print("api/ nie dotyka bazy - w porzadku")
