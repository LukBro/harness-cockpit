# Plany runów

Dokumenty opisujące, co konkretny run ma zrobić, zanim wystartuje (BRO-96).
Plan pisze się po to, żeby człowiek mógł go przeczytać i zatwierdzić, a po
runie porównać wynik z zamiarem.

## Czym plan tutaj nie jest

- **Nie planem grafu kroków.** W kodzie „plan” to wynik `src/runner/graf.py`
  — wyliczenie, które kroki workflow wystartować albo pominąć. Tamten plan
  powstaje w trakcie runu i nie jest plikiem w tym katalogu.
- **Nie specem.** Decyzja obowiązująca dłużej niż jeden run idzie do
  `.ai/specs/`, jedna na plik.
- **Nie lekcją.** Pułapka z runu idzie do `.ai/lessons.md`, z zakresem.

## Co tu trafia

- Cel runu i obserwowalny stan „gotowe”.
- Workflow i kroki, które run uruchomi, oraz czego nie rusza.
- Kroki z `approval`, na których run czeka na decyzję człowieka.

## Kto dopisuje i kiedy

Człowiek albo agent, który przygotowuje run — przed jego startem, w tym
samym PR co zmiana, którą run przygotowuje. Po runie sekcji planu się nie
poprawia; rozjazd z wynikiem dopisuje się tylko w sekcji „Po runie”.

## Nazewnictwo

`RRRR-MM-DD-kebab-case-po-polsku.md`, bez diakrytyków, np.
`2026-11-02-walidator-lekcji.md`.

## Szkielet pliku

```markdown
# Tytuł runu

## Cel
Obserwowalny stan „gotowe”.

## Zakres
Workflow, kroki, pliki; czego nie ruszać.

## Zatwierdzenia
Kroki z `approval` i kto decyduje.

## Po runie
Co wyszło inaczej niż w planie (dopisywane po runie).
```
