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


def sprawdz() -> list[str]:
    problemy: list[str] = []
    for plik in sorted(API.rglob("*.py")):
        drzewo = ast.parse(plik.read_text(), filename=str(plik))
        for w in ast.walk(drzewo):
            if isinstance(w, ast.ImportFrom) and w.module:
                for zakazany in ZAKAZANE_IMPORTY:
                    if w.module == zakazany or w.module.startswith(zakazany + "."):
                        # Wyjatek: typ sesji i enumy wejscia sa kontraktem, nie zapytaniem.
                        nazwy = {a.name for a in w.names}
                        if nazwy <= {"AsyncSession", "RunStatus", "RunnerKind"}:
                            continue
                        problemy.append(
                            f"{plik.relative_to(API.parent)}:{w.lineno} import z {w.module}"
                        )
            if isinstance(w, ast.Call):
                f = w.func
                nazwa = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                if nazwa in ZAKAZANE_WYWOLANIA and not isinstance(f, ast.Name):
                    problemy.append(
                        f"{plik.relative_to(API.parent)}:{w.lineno} wywolanie .{nazwa}()"
                    )
                elif nazwa in {"select", "insert", "update", "delete"} and isinstance(f, ast.Name):
                    problemy.append(
                        f"{plik.relative_to(API.parent)}:{w.lineno} wywolanie {nazwa}()"
                    )
    return problemy


if __name__ == "__main__":
    p = sprawdz()
    if p:
        print("Warstwa api/ dotyka bazy. Przenies to do cockpit/services/:")
        for x in p:
            print("  ", x)
        sys.exit(1)
    print("api/ nie dotyka bazy - w porzadku")
