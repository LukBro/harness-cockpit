# Praca w sesji w tle bez czekania na człowieka

Sesja w tle nie ma okna: każda komenda, która w trybie `auto` prosi o zgodę, zatrzymuje ją do
rana, a pętla sprintu nie startuje nowych ticketów (HAR-13, HAR-24, HAR-40, HAR-43).

- Nie usuwasz plików ani katalogów (`rm -r`, `shutil.rmtree`). Kopia, mutant, eksperyment idą do
  **nowego** katalogu `~/.claude/jobs/<id>/tmp/<nazwa>-<n>`; ten katalog sprząta środowisko.
- Jedna prosta komenda na wywołanie, bez `cd … &&` i bez przeszukiwania cudzych worktree.
  Złożoną procedurę zapisz jako skrypt w katalogu tymczasowym i uruchom osobno.
- Pliki repozytorium zmieniasz edycją, nie `sed -i` (zostawia artefakty `*-E`).
- Piszesz tylko w swoim worktree i katalogu tymczasowym. Plików w `.claude/` (reguły, pamięć
  agentów, ustawienia) nie edytujesz — regułę z review zapisujesz w komentarzu ticketu jako
  linię `REGULA: …`, a retro przenosi ją przez osobny PR.

## Decyzje: strażnik zamiast człowieka

Sesja w tle nie używa AskUserQuestion (hook `.claude/hooks/pytania_w_tle.py` odmawia; HAR-219, HAR-180).
Decyzje ocenia strażnik decyzji (`.git/monolynx/straznik`) według CLAUDE.md, `.claude/rules` i ticketu.

- Gdy decyzja nie wynika z ticketu, CLAUDE.md ani reguł: zakończ turę wiadomością, której pierwsza linia
  to `DECYZJA-POTRZEBNA: <czego dotyczy>`, dalej dowody (plik:linia, wynik komendy), opcje
  `- <etykieta>: <skutek>` i rekomendacja. Żadnej opcji nie wykonujesz przed odpowiedzią.
- Odpowiedź „Decyzja strażnika …” wykonujesz jak decyzję właściciela.
- `rework` strażnika (komentarz „Strażnik decyzji: rework”) poprawiasz na tej samej gałęzi, bez
  `--force`/`--amend`, ten sam PR.

## Co sprawdzić w review

- Raport agenta wymienia komendę z `rm -rf` albo `sed -i` na pliku repozytorium = **-10**.
- Artefakty narzędzi (`*-E`, `*.orig`, katalogi `mut*`) w diffie = **-25**.
- AskUserQuestion albo pytanie bez linii `DECYZJA-POTRZEBNA` w sesji w tle = **-10**.
