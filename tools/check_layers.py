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


def sprawdz(katalog: Path = API) -> list[str]:
    problemy: list[str] = []
    for plik in sorted(katalog.rglob("*.py")):
        rel = plik.relative_to(katalog.parent)
        drzewo = ast.parse(plik.read_text(), filename=str(plik))
        for w in ast.walk(drzewo):
            if isinstance(w, ast.Import):
                for a in w.names:
                    if _zakazany(a.name):
                        problemy.append(f"{rel}:{w.lineno} import {a.name}")
            if isinstance(w, ast.ImportFrom) and w.module:
                if _zakazany(w.module):
                    # Wyjatek: typ sesji i enumy wejscia sa kontraktem, nie zapytaniem.
                    nazwy = {a.name for a in w.names}
                    if not nazwy <= {"AsyncSession", "RunStatus", "RunnerKind"}:
                        problemy.append(f"{rel}:{w.lineno} import z {w.module}")
                elif w.module == "cockpit.db" and any(a.name == "models" for a in w.names):
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
