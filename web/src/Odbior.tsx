import { useEffect, useRef, useState } from "react";
import { fetchZSesja } from "./Konto";
import type { DiffRunu, ZdarzenieRunu as Zdarzenie } from "./api";

const KOMUNIKAT_SIEC_ODBIORU = "Brak połączenia z kokpitem.";
const KOMUNIKAT_NIECZYTELNY = "Nie udało się odczytać szczegółów tego wpisu.";
const KOMUNIKAT_PUSTY_DIFF = "Brak zmian do pokazania.";

const ETYKIETA_WPISU: Record<string, string> = {
  integrated: "Zintegrowano",
  committed: "Zacommitowano",
};

// Czas czytelnie; nieparsowalny zostaje surowy (nigdy pusto, nigdy "Invalid Date").
function czas(at: string): string {
  const d = new Date(at);
  return Number.isNaN(d.getTime()) ? at : d.toLocaleString("pl-PL");
}

// Opis wpisu historii z JSON w text. Nie-JSON albo brak email = komunikat, nie pusty wiersz.
function opisWpisu(z: Zdarzenie): string {
  let dane: unknown;
  try {
    dane = JSON.parse(z.text);
  } catch {
    return KOMUNIKAT_NIECZYTELNY;
  }
  if (typeof dane !== "object" || dane === null) return KOMUNIKAT_NIECZYTELNY;
  const rekord = dane as Record<string, unknown>;
  if (typeof rekord.email !== "string" || rekord.email === "") return KOMUNIKAT_NIECZYTELNY;
  const sha = z.kind === "committed" && typeof rekord.sha === "string" && rekord.sha ? `, ${rekord.sha}` : "";
  return `${rekord.email}${sha}`;
}

async function detailLubKod(r: Response): Promise<string> {
  const detail = await r.json().then(
    (j) => (typeof j?.detail === "string" && j.detail ? (j.detail as string) : null),
    () => null,
  );
  return detail ?? `błąd ${r.status}`;
}

export function Odbior({ runId, zdarzenia, odswiez }: { runId: string; zdarzenia: Zdarzenie[]; odswiez: () => void }) {
  const [diff, setDiff] = useState<string | null>(null);
  const [pobieramDiff, setPobieramDiff] = useState(false);
  const [potwierdzam, setPotwierdzam] = useState(false);
  const [wysylam, setWysylam] = useState(false);
  const [blad, setBlad] = useState<string | null>(null);
  const zywy = useRef(true);
  useEffect(() => {
    zywy.current = true;
    return () => {
      zywy.current = false;
    };
  }, []);

  const wpisy = zdarzenia.filter((z) => z.kind === "integrated" || z.kind === "committed");
  const zintegrowany = wpisy.some((z) => z.kind === "integrated");
  const zacommitowany = wpisy.some((z) => z.kind === "committed");
  const adres = `/api/runs/${encodeURIComponent(runId)}`;

  async function pokazDiff() {
    setPobieramDiff(true);
    setBlad(null);
    try {
      const r = await fetchZSesja(`${adres}/diff`);
      if (!zywy.current) return;
      if (!r.ok) {
        const powod = await detailLubKod(r);
        if (zywy.current) setBlad(`Nie udało się pobrać diffu: ${powod}`);
        return;
      }
      const dane: DiffRunu = await r.json();
      if (!zywy.current) return;
      setDiff(typeof dane.diff === "string" ? dane.diff : "");
    } catch {
      if (zywy.current) setBlad(`Nie udało się pobrać diffu: ${KOMUNIKAT_SIEC_ODBIORU}`);
    } finally {
      if (zywy.current) setPobieramDiff(false);
    }
  }

  async function wykonaj(akcja: "integrate" | "commit", opis: string) {
    setWysylam(true);
    setBlad(null);
    try {
      const r = await fetchZSesja(`${adres}/${akcja}`, { method: "POST" });
      if (!zywy.current) return;
      if (!r.ok) {
        const powod = await detailLubKod(r);
        if (zywy.current) setBlad(`Nie udało się ${opis}: ${powod}`);
        // Stan na ekranie mogl sie rozjechac z kokpitem (np. 409): pokaz prawde.
        odswiez();
        return;
      }
      // Poprawna odpowiedz bez JSON nie jest sukcesem, o ktorym wiemy: odswiez i tak pokaze zdarzenie.
      await r.json().catch(() => null);
      if (zywy.current) setPotwierdzam(false);
      odswiez();
    } catch {
      if (zywy.current) setBlad(`Nie udało się ${opis}: ${KOMUNIKAT_SIEC_ODBIORU}`);
    } finally {
      if (zywy.current) setWysylam(false);
    }
  }

  return (
    <section aria-label="Odbiór runu" data-testid="odbior">
      <h2 style={{ fontSize: 16 }}>Odbiór</h2>
      <button data-testid="odbior-pokaz-diff" onClick={() => void pokazDiff()} disabled={pobieramDiff}>
        {pobieramDiff ? "Pobieram diff…" : "Pokaż diff"}
      </button>
      {diff !== null &&
        (diff === "" ? (
          <p data-testid="odbior-diff-pusty">{KOMUNIKAT_PUSTY_DIFF}</p>
        ) : (
          <pre data-testid="odbior-diff" style={{ overflowX: "auto", background: "#F4F5F7", padding: 8 }}>
            {diff}
          </pre>
        ))}
      {!zintegrowany && (
        <p>
          {!potwierdzam ? (
            <button data-testid="odbior-integruj" onClick={() => setPotwierdzam(true)} disabled={wysylam}>
              Integruj
            </button>
          ) : (
            <>
              <span data-testid="odbior-pytanie-integracji">Zmiany z runu trafią do kopii roboczej repozytorium. Kontynuować?</span>{" "}
              <button data-testid="odbior-potwierdz-integracje" onClick={() => void wykonaj("integrate", "zintegrować zmian")} disabled={wysylam}>
                {wysylam ? "Integruję…" : "Potwierdź"}
              </button>{" "}
              <button data-testid="odbior-anuluj-integracje" onClick={() => setPotwierdzam(false)} disabled={wysylam}>
                Anuluj
              </button>
            </>
          )}
        </p>
      )}
      {zintegrowany && !zacommitowany && (
        <p>
          <button data-testid="odbior-commituj" onClick={() => void wykonaj("commit", "zacommitować zmian")} disabled={wysylam}>
            {wysylam ? "Commituję…" : "Commituj"}
          </button>
        </p>
      )}
      {blad && (
        <p role="alert" data-testid="odbior-blad" style={{ color: "#A5201A" }}>
          {blad}
        </p>
      )}
      {wpisy.length > 0 && (
        <ul aria-label="Historia odbioru" data-testid="odbior-historia">
          {wpisy.map((z) => (
            <li key={z.seq} data-testid="odbior-historia-wpis" data-kind={z.kind}>
              <b>{ETYKIETA_WPISU[z.kind]}</b> · {opisWpisu(z)} · <time dateTime={z.at}>{czas(z.at)}</time>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
