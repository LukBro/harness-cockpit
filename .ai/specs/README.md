# Decyzje z uzasadnieniem

Jedna decyzja na plik (BRO-96). Spec mówi, co postanowiono, dlaczego i od kiedy
obowiązuje — tak, żeby agent i człowiek nie wracali do rozstrzygniętego sporu.

## Co tu trafia

- Decyzje o kierunku projektu: kolejność prac, wybór technologii, granice zakresu.
- Rozstrzygnięcia, do których odwołuje się więcej niż jeden ticket.

## Czego tu nie ma

- Pułapek z runów — te idą do `.ai/lessons.md`, z zakresem.
- Planów runów — te idą do `docs/plans/`.
- Niezmienników i reguł pracy agentów — te żyją w `CLAUDE.md` i `.claude/rules/`.
- Raportów stanu repozytorium: spec zapisuje decyzję, nie to, co dziś jest w kodzie.

## Kto dopisuje i kiedy

Człowiek albo agent, w tickecie, który podejmuje decyzję — w tym samym PR co
zmiana, która z niej wynika. Zmiana decyzji to edycja speca z nowym źródłem
w sekcji „Źródło”, nie nowy plik obok.

## Nazewnictwo

`kebab-case` po polsku, bez diakrytyków, nazwa mówi, czego decyzja dotyczy:
`kolejnosc-technologii.md`, nie `decyzja-1.md`.

## Szkielet pliku

```markdown
# Tytuł decyzji

## Decyzja
Co postanowiono — jednym akapitem albo tabelą.

## Uzasadnienie
Dlaczego tak, a nie inaczej.

## Źródło
Ticket albo dokument, z którego decyzja pochodzi (np. BRO-95).
```

## Specy

- [`kolejnosc-technologii.md`](kolejnosc-technologii.md) — jedna nowa technologia
  na kamień milowy, M1–M5 (BRO-95).
- [`przelaczenie-na-budowanie-soba.md`](przelaczenie-na-budowanie-soba.md) — kiedy
  harness-cockpit zaczyna budować sam siebie (BRO-95).
