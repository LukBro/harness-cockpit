# Kolejność wprowadzania technologii

## Decyzja

Jedna nowa rzecz na kamień milowy, nie pięć naraz:

| Etap | Nowe | Znane |
| -- | -- | -- |
| M1 | PostgreSQL, SQLAlchemy | Python, FastAPI |
| M2 | React, TypeScript, Tailwind, shadcn/ui — na jednym ekranie powłoki | backend z M1 |
| M3 | React Flow — w już oswojonym otoczeniu | reszta |
| M4–M5 | nic nowego | rozwijanie |

Tabela to decyzja o kolejności, nie raport stanu. Repozytorium może ją
wyprzedzać (np. `web/` z Vite, React i TypeScript już istnieje) — kolejność mówi,
na której technologii skupia się nauka i ocena w danym etapie.

## Uzasadnienie

- Problem do rozwiązania: jak oceniać kod w technologii, której się nie zna,
  pisany przez autonomicznego agenta.
- Szkielet na wylot „uczy każdej nieznanej technologii na małej powierzchni,
  gdzie błąd kosztuje godzinę zamiast tygodnia”.
- Każdy etap stawia nowe obok tego, co już znane (kolumna „Znane”), więc nowa
  technologia uczy się w oswojonym otoczeniu.
- „W nieznanej technologii kompilator jest tańszym recenzentem niż człowiek.”
  Z tym zgodne są typy frontu generowane z OpenAPI (`CLAUDE.md`, „Decyzje
  implementacyjne”).

## Źródło

BRO-95, sekcja „Kolejność wprowadzania nieznanych technologii”.
