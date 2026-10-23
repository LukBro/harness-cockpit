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

## Szkielet w przeglądarce

Jeden port, bez CORS: kokpit serwuje zbudowany front i `/api/*` na tym samym adresie.

```bash
createdb cockpit --owner cockpit                 # Postgres (patrz wyżej)
export COCKPIT_DATABASE_URL=postgresql+asyncpg://cockpit:cockpit@localhost/cockpit
alembic upgrade head
python tools/seed_dev.py                         # zespół, runner, token w pliku 0600
(cd web && npm install && npm run build)         # buduje do src/cockpit/static
cockpit                                          # terminal 1
cockpit-runner                                   # terminal 2
```

Otwórz http://127.0.0.1:8765/. Token nie pojawia się na ekranie - `seed_dev`
wypisuje tylko ścieżkę pliku. Zmienna `COCKPIT_CONFIG` pozwala trzymać plik
z tokenem poza `~/.config` (np. w katalogu tymczasowym), a `COCKPIT_RUNNER_TOKEN`
w środowisku ma pierwszeństwo przed plikiem. Uruchom `seed_dev` i `cockpit-runner`
z tym samym `COCKPIT_CONFIG`. Ponowne `seed_dev` generuje nowy token.

## Zasady

Przed pierwszą zmianą przeczytaj `CLAUDE.md` — niezmienniki i decyzje
implementacyjne. Zadania i uzasadnienia: projekt Harness Cockpit w Linearze.
