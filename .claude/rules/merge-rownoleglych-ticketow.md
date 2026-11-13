# Merge równoległych ticketów

Git łączy bez konfliktu kod, który razem nie działa (HAR-174, HAR-176, HAR-177, HAR-178).

- Po `git merge origin/main` uruchamiasz pełny `pytest -q -n 4`, nie tylko testy plików
  z konfliktem. CI na PR testuje scalony stan, ale dopiero po wypchnięciu.
- Nowe pole dataclass zwracanej z wielu miejsc (`WynikKroku`) ma wartość domyślną, a wszystkie
  konstruktory w `src/` i `tests/` przepisujesz na argumenty nazwane w tym samym PR.
- Kod czytający pole, które od teraz może być puste (`krok.skill` kroku systemowego), ma gałąź
  i test dla pustej wartości.
- Atrapa zewnętrznego procesu (`claude`) wypisuje format produkcyjnego parsera (stream-json), nie tekst.
- Numer migracji to głowa z `alembic heads` na `origin/main` w chwili merge; dwie głowy łapie
  test `len(skrypty.get_heads()) == 1` w `tests/test_bramka_zatwierdzenia.py`.

## Co sprawdzić w review

- Merge `main` w PR bez pełnego `pytest` po merge (raport wymienia tylko testy plików
  z konfliktem) = **-10**.
- Nowe pole dataclass z wywołaniami pozycyjnymi w `src/` lub `tests/` = **-25**.
