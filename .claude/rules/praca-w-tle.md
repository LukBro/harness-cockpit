# Praca w sesji w tle bez czekania na człowieka

Źródło: retro sprintu M1.2 — sesje HAR-13, HAR-24, HAR-40 i HAR-43 stały łącznie
kilkadziesiąt godzin na prośbach o zgodę, bo komendy usuwały pliki albo łączyły wiele
operacji w jednym wywołaniu.

## Reguła

- **Nie usuwaj plików ani katalogów** (`rm -rf`, `rm -r`, `shutil.rmtree`) w komendach
  narzędzi. Kopia robocza, mutant albo eksperyment idzie do **nowego** katalogu pod
  katalogiem tymczasowym zadania (`~/.claude/jobs/<id>/tmp/<nazwa>-<n>`) — ten katalog
  sprząta środowisko.
- **Jedna prosta komenda na wywołanie.** Bez pętli łączących `cp`, `sed -i`, `git`
  i testy w jednej linii; złożoną procedurę zapisz jako skrypt w katalogu tymczasowym
  i uruchom go osobno.
- **Bez `sed -i`** na plikach repozytorium — zmiany w kodzie przez edycję pliku;
  `sed -i -E` zostawia artefakty `*-E` (HAR-14).
- **Zapis tylko w swoim worktree** i w katalogu tymczasowym zadania — nie w pamięci
  sesji głównej ani w katalogach użytkownika.

## Pułapka

W trybie `auto` każda z tych operacji kończy się prośbą o zgodę. Sesja w tle nie ma
okna, więc stoi do rana, a pętla sprintu nie startuje nowych ticketów, gdy dwie sesje
czekają na człowieka (`MONOLYNX_SPRINT_MAX_WAITING`).

## Co sprawdzić w review

- Raport agenta wymienia komendę z `rm -rf` albo `sed -i` na pliku repozytorium = **-10**.
- Artefakty narzędzi (`*-E`, `*.orig`, katalogi `mut*`) w diffie = **-25**.

## Reguły z review — komentarz ticketu, nie `.claude/` (retro M1.3)

Źródło: HAR-35, HAR-38 — sesje stały na prośbie o zgodę przy zapisie do
`.claude/agent-memory/*/MEMORY.md`; HAR-35 zostawił niezacommitowaną zmianę tego pliku.

- Sesja w tle **nie edytuje plików w `.claude/`** (reguły, pamięć agentów, ustawienia).
  W trybie `auto` każdy taki zapis czeka na człowieka.
- Regułę z review zapisuje w komentarzu podsumowania ticketu jako linię `REGULA: …` —
  retro zbiera ją stamtąd i przenosi do `.claude/rules/` przez osobny PR.
- Komendy bez `cd … &&` i bez przeszukiwania cudzych worktree — każda taka komenda
  także czeka na zgodę (HAR-45, HAR-52).

## Decyzje w sesji w tle: strażnik zamiast człowieka (2026-11-10)

Źródło: HAR-219 (sesja stała na AskUserQuestion o pełne testy), HAR-180 (49 min na pytaniu). Właściciel
przekazał decyzje strażnikowi decyzji (`.git/monolynx/straznik`), który ocenia je według CLAUDE.md,
`.claude/rules` i ticketu.

- Sesja w tle **nie używa AskUserQuestion** (hook `.claude/hooks/pytania_w_tle.py` odmawia).
- Gdy decyzja nie wynika z ticketu, CLAUDE.md ani reguł: zakończ turę wiadomością, której pierwsza linia
  to `DECYZJA-POTRZEBNA: <czego dotyczy>`, dalej dowody (plik:linia, wynik komendy), opcje
  `- <etykieta>: <skutek>` i rekomendacja. Nie wykonuj żadnej opcji przed odpowiedzią.
- Odpowiedź przychodzi jako wiadomość „Decyzja strażnika …” — wykonujesz ją jak decyzję właściciela.
- PR ticketu po zielonym CI ocenia strażnik. `rework` wraca komentarzem „Strażnik decyzji: rework” z
  rekomendacjami: poprawiasz na tej samej gałęzi, bez `--force`/`--amend`, ten sam PR.

Co sprawdzić w review: AskUserQuestion albo pytanie bez linii `DECYZJA-POTRZEBNA` w sesji w tle = **-10**.
