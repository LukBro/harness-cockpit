# Merge równoległych ticketów: konflikt bez konfliktu tekstu

Źródło: retro sprintu M1.9 — HAR-176 (`KeyError` po HAR-178), HAR-174 (dwa razy `WynikKroku` wołany pozycyjnie po HAR-176
i HAR-177; kolizja numeru migracji z HAR-178), HAR-177 (6 testów silnika padło po HAR-174, atrapa agenta bez stream-json).

## Reguła

- **Po `git merge origin/main` uruchamiasz pełny `pytest -q -n 4`**, nie tylko testy plików z konfliktem. Git łączy bez
  konfliktu kod, który razem nie działa: HAR-178 dodał `rejestr.skille[krok.skill]`, a krok systemowy HAR-176 ma `skill=None`.
- **Nowe pole dataclass zwracanej z wielu miejsc** (`WynikKroku`) dodajesz z wartością domyślną, a wszystkie konstruktory
  w `src/` i `tests/` przepisujesz na argumenty nazwane w tym samym PR. Dwa tickety dodające „czwarte pole” jednocześnie
  dają po merge wartość w złym polu, bez błędu typu.
- **Kod czytający pole, które od tego sprintu może być puste** (`krok.skill` przy kroku systemowym) ma gałąź dla pustej
  wartości i test z takim krokiem.
- **Atrapa zewnętrznego procesu w teście** (agent, `claude`) wypisuje format, który czyta produkcyjny parser (zdarzenie
  `result` stream-json), nie surowy tekst — inaczej zmiana parsera psuje testy cudzych ticketów.
- **Numer migracji** to głowa z `alembic heads` na `origin/main` w chwili merge, nie numer z opisu ticketu.

## Pułapka

Konflikt tekstowy rozwiązany, testy plików z konfliktem zielone, PR zielony na starym `main` — a po scaleniu drugiego PR
silnik rzuca wyjątek i run nie dostaje `run_finished`. Widać to dopiero na `main` albo przy następnym merge.

## Co sprawdzić w review

- Merge `main` w PR bez pełnego `pytest` po merge (raport wymienia tylko testy plików z konfliktem) = **-10**.
- Nowe pole dataclass z wywołaniami pozycyjnymi w `src/` lub `tests/` = **-25**.
