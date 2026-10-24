# Pamięć agenta test-writer

## Reguly z review krytyka

- HAR-2: Test nazwany wynikiem reguły dziedzinowej uruchamia produkcyjny kod tej reguły (prawdziwą aplikację, prawdziwy punkt wejścia) i sprawdza jej skutek w danych (np. który runner dostał run), a nie tylko kod odpowiedzi.
- HAR-2: Test reguły wyboru zasobu (runner, run, zdarzenie) zawsze ma w bazie co najmniej dwa zespoły i sprawdza, że rekord innego zespołu nie zostaje wybrany, bo filtr `tenant_id` jest niezmiennikiem, a nie szczegółem zapytania.
- HAR-3: Asercja, która twierdzi, że coś nie powstało w danych, sprawdza dane; stan interfejsu wynikający wprost z kodu odpowiedzi nie dowodzi niczego o bazie.
