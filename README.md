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

Osobno:

```bash
COCKPIT_DATABASE_URL=postgresql+asyncpg://cockpit:cockpit@localhost/cockpit cockpit
COCKPIT_RUNNER_TOKEN=... COCKPIT_URL=http://127.0.0.1:8765 cockpit-runner
cd web && npm install && npm run dev
```

## Zasady

Przed pierwszą zmianą przeczytaj `CLAUDE.md` — niezmienniki i decyzje
implementacyjne. Zadania i uzasadnienia: projekt Harness Cockpit w Linearze.
