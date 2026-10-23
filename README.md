# Harness Cockpit

Kokpit do składania, uruchamiania i obserwowania workflow agentowych.

## Szkielet na wylot

Dowód, że architektura się trzyma: klik w przeglądarce → kokpit → runner →
proces na maszynie użytkownika → zdarzenie → ekran.

```bash
pip install -e ".[dev]"
python3 tools/demo_szkielet.py     # kokpit + runner + run, wszystko w jednym procesie
pytest                             # testy backendu
```

## PostgreSQL i migracje

Schemat buduje wyłącznie Alembic (`create_all` zostaje w testach i demo).

```bash
createuser cockpit --pwprompt                 # hasło np. cockpit
createdb cockpit --owner cockpit
createdb cockpit_test --owner cockpit         # baza dla testu migracji
export COCKPIT_DATABASE_URL=postgresql+asyncpg://cockpit:cockpit@localhost/cockpit
alembic upgrade head                          # przed pierwszym `cockpit`
```

Test migracji (bez zmiennej jest pomijany):

```bash
COCKPIT_TEST_DATABASE_URL=postgresql+asyncpg://cockpit:cockpit@localhost/cockpit_test \
  pytest tests/test_migracje.py
```

Osobno:

```bash
cockpit
COCKPIT_RUNNER_TOKEN=... COCKPIT_URL=http://127.0.0.1:8765 cockpit-runner
cd web && npm install && npm run dev
```

## Zasady

Przed pierwszą zmianą przeczytaj `CLAUDE.md` — niezmienniki i decyzje
implementacyjne. Zadania i uzasadnienia: projekt Harness Cockpit w Linearze.
