import { useCallback, useEffect, useRef, useState } from "react";
import { fetchZSesja } from "./Konto";
import type { DecyzjaOdbioru, DecyzjaOdbioruIn, DowodyRunu } from "./api";

const KOMUNIKAT_SIEC_DOWODOW = "Brak połączenia z kokpitem.";
const KOMUNIKAT_ODCZYT_DOWODOW = "Nie udało się odczytać dowodów odbioru.";
const KOLOR_SZARY = "#58616D";
const KOLOR_CZERWONY = "#A5201A";

const OPIS_DECYZJI: Record<DecyzjaOdbioru["decyzja"], string> = {
  accept: "przyjęto",
  send_back: "odesłano do poprawy",
};

// Czas czytelnie; nieparsowalny zostaje surowy (nigdy pusto, nigdy "Invalid Date").
function czasDecyzji(at: string): string {
  const d = new Date(at);
  return Number.isNaN(d.getTime()) ? at : d.toLocaleString("pl-PL");
}

// detail z JSON, a gdy go brak albo nie jest napisem: kod HTTP. Nigdy cisza.
async function detailLubKod(r: Response): Promise<string> {
  const detail = await r.json().then(
    (j) => (typeof j?.detail === "string" && j.detail ? (j.detail as string) : null),
    () => null,
  );
  return detail ?? `błąd ${r.status}`;
}

// null to brak pomiaru (niezmierzone), nigdy zero; zmierzone zero to $0.0000.
function napisKosztu(cost: number | null): string {
  return cost === null ? "niezmierzone" : `$${cost.toFixed(4)}`;
}

export function OdbiorDowody({ runId, odswiez }: { runId: string; odswiez: () => void }) {
  const [dowody, setDowody] = useState<DowodyRunu | null>(null);
  const [ladowanie, setLadowanie] = useState(true);
  const [bladOdczytu, setBladOdczytu] = useState<string | null>(null);
  const [uzasadnienie, setUzasadnienie] = useState("");
  const [wysylam, setWysylam] = useState(false);
  const [bladDecyzji, setBladDecyzji] = useState<string | null>(null);
  const zywy = useRef(true);
  useEffect(() => {
    zywy.current = true;
    return () => {
      zywy.current = false;
    };
  }, []);

  const adres = `/api/runs/${encodeURIComponent(runId)}`;

  const pobierz = useCallback(async () => {
    setBladOdczytu(null);
    try {
      const r = await fetchZSesja(`${adres}/dowody`);
      if (!zywy.current) return;
      if (!r.ok) {
        const powod = await detailLubKod(r);
        if (!zywy.current) return;
        setBladOdczytu(`${KOMUNIKAT_ODCZYT_DOWODOW} ${powod}`);
        return;
      }
      const dane: DowodyRunu = await r.json();
      if (!zywy.current) return;
      setDowody(dane);
    } catch {
      if (zywy.current) setBladOdczytu(KOMUNIKAT_SIEC_DOWODOW);
    } finally {
      if (zywy.current) setLadowanie(false);
    }
  }, [adres]);

  useEffect(() => {
    setLadowanie(true);
    void pobierz();
  }, [pobierz]);

  async function zdecyduj(decyzja: DecyzjaOdbioruIn["decyzja"]) {
    setWysylam(true);
    setBladDecyzji(null);
    try {
      const tresc: DecyzjaOdbioruIn = { decyzja, uzasadnienie: uzasadnienie.trim() || null };
      const r = await fetchZSesja(`${adres}/decyzja-odbioru`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(tresc),
      });
      if (!zywy.current) return;
      if (!r.ok) {
        const powod = await detailLubKod(r);
        if (!zywy.current) return;
        setBladDecyzji(powod);
        return;
      }
      await r.json().catch(() => null);
      if (!zywy.current) return;
      await pobierz();
      if (!zywy.current) return;
      odswiez();
    } catch {
      if (zywy.current) setBladDecyzji(KOMUNIKAT_SIEC_DOWODOW);
    } finally {
      if (zywy.current) setWysylam(false);
    }
  }

  if (ladowanie && dowody === null) return <p data-testid="dowody-ladowanie">Wczytuję dowody odbioru…</p>;
  if (dowody === null) {
    return (
      <div>
        <p role="alert" data-testid="dowody-blad" style={{ color: KOLOR_CZERWONY }}>
          {bladOdczytu}
        </p>
        <button
          onClick={() => {
            setLadowanie(true);
            void pobierz();
          }}
        >
          Spróbuj ponownie
        </button>
      </div>
    );
  }

  const { braki, decyzja, ryzyka } = dowody;
  const przyjmijZablokowane = wysylam || (braki.length > 0 && uzasadnienie.trim() === "");

  return (
    <div data-testid="odbior-dowody">
      {/* Po udanej decyzji nieudany ponowny odczyt nie może wyglądać jak brak decyzji: dane na ekranie są stare. */}
      {bladOdczytu !== null && (
        <p role="alert" data-testid="blad-odczytu-dowodow" style={{ color: KOLOR_CZERWONY }}>
          {bladOdczytu}
        </p>
      )}
      <section aria-label="Twierdzenia">
        <h3>Twierdzenia</h3>
        {dowody.twierdzenia.length === 0 ? (
          <p>brak twierdzeń</p>
        ) : (
          <ul>
            {dowody.twierdzenia.map((t, i) => (
              <li key={`${t.step_id}:${t.artefakt}:${i}`} data-testid="twierdzenie">
                {t.step_id}: {t.artefakt}
              </li>
            ))}
          </ul>
        )}
      </section>
      <section aria-label="Dowody">
        <h3>Dowody</h3>
        {dowody.dowody.length === 0 ? (
          <p>brak dowodów</p>
        ) : (
          <ul>
            {dowody.dowody.map((d, i) => (
              <li
                key={`${d.step_id}:${d.capability}:${i}`}
                data-testid="dowod"
                style={d.source === "agent" ? { color: KOLOR_SZARY } : undefined}
              >
                {d.step_id}: {d.capability} · kod wyjścia: {d.exit_code === null ? "brak" : d.exit_code} · {d.plik} ·{" "}
                <i>{d.source === "agent" ? "deklaracja agenta" : "dowód silnika"}</i>
              </li>
            ))}
          </ul>
        )}
        {dowody.werdykty.length > 0 && (
          <ul>
            {dowody.werdykty.map((w, i) => (
              <li key={`${w.step_id}:${i}`} data-testid="werdykt">
                {w.step_id}: werdykt {w.verdict} ({w.zaliczony ? "zaliczony" : "niezaliczony"})
              </li>
            ))}
          </ul>
        )}
        {dowody.pomiary.length > 0 && (
          <ul>
            {dowody.pomiary.map((p, i) => (
              <li key={`${p.step_id}:${i}`} data-testid="pomiar">
                {p.step_id}: koszt {napisKosztu(p.cost_usd)}
              </li>
            ))}
          </ul>
        )}
      </section>
      <section aria-label="Braki">
        <h3>Braki</h3>
        {braki.length === 0 ? (
          <p>brak braków</p>
        ) : (
          <ul>
            {braki.map((b, i) => (
              <li key={`${b.rodzaj}:${b.step_id ?? ""}:${i}`} data-testid="brak" role="alert" style={{ color: KOLOR_CZERWONY }}>
                {b.powod}
              </li>
            ))}
          </ul>
        )}
      </section>
      <section aria-label="Ryzyko">
        <h3>Ryzyko</h3>
        {ryzyka.kotwice === null ? (
          <p>kotwice: brak danych</p>
        ) : ryzyka.kotwice.length === 0 ? (
          <p>kotwice: brak</p>
        ) : (
          <>
            <p>kotwice:</p>
            <ul>
              {ryzyka.kotwice.map((k, i) => (
                <li key={`${k}:${i}`}>{k}</li>
              ))}
            </ul>
          </>
        )}
        {ryzyka.mutanty === null ? (
          <p>mutanty: brak danych</p>
        ) : ryzyka.mutanty.length === 0 ? (
          <p>mutanty: brak</p>
        ) : (
          <>
            <p>mutanty:</p>
            <ul>
              {ryzyka.mutanty.map((m, i) => (
                <li key={`${m}:${i}`}>{m}</li>
              ))}
            </ul>
          </>
        )}
      </section>
      {decyzja !== null ? (
        <p data-testid="dowody-decyzja">
          {`Decyzja: ${OPIS_DECYZJI[decyzja.decyzja] ?? decyzja.decyzja} — ${decyzja.email}, ${czasDecyzji(decyzja.at)}, uzasadnienie: ${decyzja.uzasadnienie ?? "brak"}`}
        </p>
      ) : (
        <div>
          <label>
            Uzasadnienie{" "}
            <textarea aria-label="Uzasadnienie" value={uzasadnienie} onChange={(e) => setUzasadnienie(e.target.value)} />
          </label>{" "}
          <button onClick={() => void zdecyduj("accept")} disabled={przyjmijZablokowane}>
            Przyjmij
          </button>{" "}
          <button onClick={() => void zdecyduj("send_back")} disabled={wysylam}>
            Odeślij do poprawy
          </button>
          {bladDecyzji && (
            <p role="alert" data-testid="dowody-decyzja-blad" style={{ color: KOLOR_CZERWONY }}>
              {bladDecyzji}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
