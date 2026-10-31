# Brak sygnału to nie zielone, kryterium musi móc zawieść

Źródło: audyt G8 (BRO-137) — cztery kryteria akceptacji, które nie mogły zawieść
(BRO-108, BRO-110, BRO-91, BRO-122), oraz BRO-99 z odhaczonym kryterium `tenant_id`.

## Reguła

- **Brak sygnału nie jest wynikiem.** Brak ostrzeżenia, pominięta bramka, pominięty test
  i pusty plik dowodu nie są raportowane jako „bramka aktywna” ani jako zielone.
  Szczegóły i przykłady: [falszywie-zielone](falszywie-zielone.md) oraz niezmiennik
  `evidence_required` z CLAUDE.md.
- **Kryterium akceptacji ma wynik, przy którym zawodzi.** Kryterium spełnione przy każdym
  wyniku pomiaru jest niedopuszczalne. Przykłady z audytu: „poniżej 5% albo komentarz”
  (BRO-110), „zmierzone” (BRO-91), „zgodnie z rozstrzygnięciem” (BRO-122). Przy każdym
  kryterium wskaż konkretny wynik, przy którym by zawiodło (ilustracja, nie cytat:
  pomiar powyżej progu i brak komentarza). Kryterium działa jak gałąź kodu: musi istnieć wejście, na
  którym oblewa (zob. [test-odroznia-regule](test-odroznia-regule.md)).
- **Kryterium o wypchnięciu zmian sprawdza mechanizm, który je rejestruje:**
  `git reflog show origin/<branch>`, nie `git reflog` HEAD. Reflog HEAD nie rejestruje
  wypchnięcia, więc kryterium BRO-108, które sprawdzało wypchnięcie przez `git reflog`
  HEAD, nie mogło zawieść.

## Pułapka

Kryterium prawdziwe literalnie, a obietnica niespełniona. BRO-99: kryterium `tenant_id`
(zapytania niosą `tenant_id`) było prawdą (`run_dla_najemcy` filtruje po `tenant_id`),
ale obietnica izolacji zespołów nie, bo `domyslny_tenant` wybiera dowolny zespół
(`select(Tenant).limit(1)` bez `ORDER BY`). Odhaczone kryterium wygląda jak dowód
i nikt go nie ogląda.

## Co sprawdzić w review

- Odhaczone kryterium bez wskazania wyniku, przy którym by zawiodło = **-25**.
- Kryterium o wypchnięciu zmian sprawdzane przez `git reflog` HEAD zamiast
  `git reflog show origin/<branch>` = **-25**.
- Brak ostrzeżenia, pominięta bramka albo pusty plik dowodu raportowane jako bramka
  aktywna lub zielone — odjęcie z [falszywie-zielone](falszywie-zielone.md) (**-25**),
  nie sumuje się z tą regułą: jedna wada = jedno odjęcie (retro M1.5, HAR-54).
