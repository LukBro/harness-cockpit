"""Dowod na wylot: klik -> runner -> proces -> zdarzenie -> widok runu (BRO-99)."""
import asyncio
import hashlib
import os
import sys
from pathlib import Path

KORZEN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KORZEN / "src"))

# Bezposrednio z env, przed importem cockpit.*: settings czyta tez .env z cwd,
# a skrypt kasuje dane wskazanej bazy.
URL = os.environ.get("COCKPIT_DATABASE_URL", "").strip()
if not URL:
    sys.exit("Brak COCKPIT_DATABASE_URL. Przelot idzie na PostgreSQL ze schematem "
             "z alembic, np. COCKPIT_DATABASE_URL="
             "postgresql+asyncpg://cockpit:cockpit@localhost:5432/cockpit_test\n"
             "UWAGA: skrypt robi `alembic downgrade base` przed i po przelocie "
             "i kasuje dane wskazanej bazy - uzyj bazy testowej.")
os.environ["COCKPIT_DATABASE_URL"] = URL

import httpx
import uvicorn
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, InvalidRequestError, NoSuchModuleError, SQLAlchemyError


def zle_url(powod: str):
    # powod to stala fraza lub nazwa modulu, nigdy tekst wyjatku: ten moze niesc URL z haslem
    sys.exit(f"COCKPIT_DATABASE_URL jest niepoprawny ({powod}). Oczekiwany "
             "format: postgresql+asyncpg://uzytkownik:haslo@host:5432/baza")


try:
    ADRES = make_url(URL).render_as_string(hide_password=True)
except ValueError:  # make_url rzuca ValueError (nie ArgumentError) przy porcie/hoscie
    zle_url("niepoprawny port lub host")  # zawezone do make_url: importy ponizej to nie port
except ArgumentError:
    zle_url("nie da sie go sparsowac")
try:
    # engine powstaje juz przy imporcie cockpit.db.session, wiec zly URL
    # wybucha tam, nie w migracjach
    from cockpit.db.models import Runner as RunnerRow
    from cockpit.db.models import Tenant
    from cockpit.db.session import SessionLocal, engine
except NoSuchModuleError:
    zle_url("nieznany sterownik bazy")
except InvalidRequestError:  # sterownik synchroniczny (np. sqlite://) w silniku async
    zle_url("sterownik nie jest asynchroniczny")
except ModuleNotFoundError as e:
    # brak pakietu sterownika (np. postgresql:// bez +asyncpg szuka psycopg); e.name
    # to nazwa modulu, nie sekret
    zle_url(f"sterownik bazy '{e.name}' nie jest zainstalowany; "
            "uzyj postgresql+asyncpg://")
from runner.client import Runner

BASE = "http://127.0.0.1:8799"
TOKEN = "demo-token"


async def seed():
    async with SessionLocal() as s:
        t = Tenant(name="Browarek")
        s.add(t)
        await s.flush()
        s.add(RunnerRow(tenant_id=t.id, name="macbook",
                        token_hash=hashlib.sha256(TOKEN.encode()).hexdigest()))
        await s.commit()


async def main():
    await seed()

    config = uvicorn.Config("cockpit.api.app:app", host="127.0.0.1", port=8799,
                            log_level="error")
    server = uvicorn.Server(config)
    serve = asyncio.create_task(server.serve())
    await asyncio.sleep(1.2)

    listen = asyncio.create_task(Runner(BASE, TOKEN, "macbook").listen())
    await asyncio.sleep(1.0)

    async with httpx.AsyncClient() as c:
        r = await c.post(f"{BASE}/api/runs",
                         json={"workflow": "szkielet",
                               "command": ["python3", "-c",
                                           "print('krok wykonany na maszynie runnera')"]})
        run_id = r.json()["run_id"]
        print("run utworzony:", run_id)
        await asyncio.sleep(2.0)
        szczegoly = (await c.get(f"{BASE}/api/runs/{run_id}")).json()

    print("status:", szczegoly["status"])
    for e in szczegoly["events"]:
        print(f"  [{e['seq']}] {e['kind']}: {e['text']}")

    listen.cancel()
    server.should_exit = True
    await serve
    ok = szczegoly["status"] == "to_accept" and any(
        "krok wykonany" in e["text"] for e in szczegoly["events"])
    print("\nWYNIK:", "szkielet dziala na wylot" if ok else "BLAD")
    return 0 if ok else 1


async def przelot():
    try:
        return await main()
    finally:
        # polaczenia z puli nie moga trzymac blokad przy downgrade
        await engine.dispose()


def migracje() -> Config:
    # alembic/env.py robi asyncio.run, wiec migracje ida poza petla zdarzen
    cfg = Config(str(KORZEN / "alembic.ini"))
    cfg.set_main_option("script_location", str(KORZEN / "alembic"))
    cfg.set_main_option("sqlalchemy.url", URL.replace("%", "%%"))
    return cfg


cfg = migracje()
przyczyna = None
try:
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    kod = asyncio.run(przelot())
except (OSError, SQLAlchemyError) as e:
    przyczyna = e
finally:
    # sprzatanie nie moze zasloniac pierwotnej przyczyny porazki
    try:
        command.downgrade(cfg, "base")
    except (OSError, SQLAlchemyError) as e:
        przyczyna = przyczyna or e
if przyczyna is not None:
    sys.exit(f"Blad polaczenia z baza pod COCKPIT_DATABASE_URL={ADRES} "
             f"({type(przyczyna).__name__}). Sprawdz, czy PostgreSQL dziala "
             "i adres jest poprawny.")
raise SystemExit(kod)
