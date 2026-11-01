import { useEffect, useState } from "react";
import { fetchZSesja, Konto, odswiezSesje, PanelKonta, ustawPowrotDoLogowania, ustawToken } from "./Konto";

type Zdarzenie = {
  seq: number;
  kind: string;
  text: string;
  step_id: string;
  text_truncated: boolean;
  text_full_len: number | null;
};
type Run = {
  id: string;
  workflow: string;
  status: string;
  reason: string | null;
  runner_kind: string;
  events: Zdarzenie[];
};

type Backend = { nazwa: string; auth: string };
type Runner = {
  id: string;
  name: string;
  version: string;
  backends: Backend[];
  online: boolean;
  last_seen_at: string | null;
};

const ODSWIEZANIE_RUNNEROW_MS = 5000;
const KOMUNIKAT_BRAK_RUNNEROW = "Brak runnerów. Zainstaluj i uruchom cockpit-runner z tokenem zespołu.";
const KOMUNIKAT_BLAD_RUNNEROW = "Nie udało się odświeżyć listy runnerów. Ponawiam; pokazana lista może być nieaktualna.";

const KOMUNIKAT_OFFLINE = "Runner jest offline. Uruchom runnera na swojej maszynie.";
const KOMUNIKAT_SIEC = "Brak połączenia z kokpitem.";
const KOMUNIKAT_SESJA_WYGASLA = "Sesja wygasła, zaloguj się";
const KOMUNIKAT_STRUMIEN = "Brak połączenia ze strumieniem zdarzeń. Ponawiam; historia poniżej może być niepełna.";

// Scalanie po seq: historia z GET i strumień zbiegają się bez duplikatów.
function scal(stare: Zdarzenie[], nowe: Zdarzenie[]): Zdarzenie[] {
  const mapa = new Map<number, Zdarzenie>();
  for (const z of stare) mapa.set(z.seq, z);
  for (const z of nowe) mapa.set(z.seq, z);
  return [...mapa.values()].sort((a, b) => a.seq - b.seq);
}

function runZAdresu(): string | null {
  return new URLSearchParams(window.location.search).get("run");
}

// Bramka: bez sesji ekran logowania, z sesja dotychczasowy widok runu.
// Stan "ladowanie" nie pokazuje nic, zeby nie mignal zly ekran przed odpowiedzia "ja".
export function App() {
  const [stan, setStan] = useState<"ladowanie" | "gosc" | "blad" | { email: string }>("ladowanie");
  const [komunikatLogowania, setKomunikatLogowania] = useState<string | null>(null);

  useEffect(() => {
    ustawPowrotDoLogowania(() => setStan("gosc"));
    let anulowano = false;
    // Wspolne odswiezenie: podwojny efekt StrictMode dostaje ten sam token i ta sama odpowiedz.
    odswiezSesje().then(
      (ja) => {
        if (!anulowano && ja) setStan({ email: ja.email });
      },
      () => {
        if (!anulowano) setStan("blad");
      },
    );
    return () => {
      anulowano = true;
    };
  }, []);

  // Sesja wygasla w trakcie strumienia runu: czysty token CSRF i ekran logowania z komunikatem.
  function poWygasnieciuSesji() {
    ustawToken(null);
    setKomunikatLogowania(KOMUNIKAT_SESJA_WYGASLA);
    setStan("gosc");
  }

  function poZalogowaniu() {
    setKomunikatLogowania(null);
    setStan("ladowanie");
    odswiezSesje().then(
      (ja) => setStan(ja ? { email: ja.email } : "gosc"),
      () => setStan("blad"),
    );
  }

  if (stan === "ladowanie") return <main />;
  if (stan === "blad") {
    return (
      <main style={{ fontFamily: "system-ui", maxWidth: 720, margin: "40px auto" }}>
        <p style={{ color: "#A5201A" }}>{KOMUNIKAT_SIEC}</p>
      </main>
    );
  }
  if (stan === "gosc") {
    return (
      <>
        {komunikatLogowania && (
          <p role="alert" style={{ color: "#A5201A", fontFamily: "system-ui", maxWidth: 720, margin: "40px auto 0" }}>
            {komunikatLogowania}
          </p>
        )}
        <Konto onZalogowano={poZalogowaniu} />
      </>
    );
  }
  return (
    <>
      <PanelKonta email={stan.email} />
      <WidokRunu poWygasnieciuSesji={poWygasnieciuSesji} />
      <PanelRunnerow />
    </>
  );
}

function WidokRunu({ poWygasnieciuSesji }: { poWygasnieciuSesji: () => void }) {
  const [runId, setRunId] = useState<string | null>(runZAdresu);
  const [zdarzenia, setZdarzenia] = useState<Zdarzenie[]>([]);
  const [status, setStatus] = useState<string | null>(null);
  const [reason, setReason] = useState<string | null>(null);
  const [blad, setBlad] = useState<string | null>(null);
  // Osobno od bledu historii: udany GET nie moze zgasic komunikatu o strumieniu.
  const [bladStrumienia, setBladStrumienia] = useState<string | null>(null);
  // Blad samego uruchomienia (np. runner offline); nie nalezy do zadnego runu.
  const [bladUruchomienia, setBladUruchomienia] = useState<string | null>(null);
  const [uruchamiam, setUruchamiam] = useState(false);
  const [ticket, setTicket] = useState("");
  const [ostrzezenie, setOstrzezenie] = useState<{ aktywnyRunId: string } | null>(null);

  useEffect(() => {
    // Nowy runId (lub brak): nic z poprzedniego runu nie moze zostac na ekranie.
    setZdarzenia([]);
    setStatus(null);
    setReason(null);
    setBlad(null);
    setBladStrumienia(null);
    if (!runId) return;
    let anulowano = false;

    async function dociagnij() {
      try {
        const r = await fetch(`/api/runs/${encodeURIComponent(runId!)}`);
        if (anulowano) return;
        if (r.status === 404) {
          setBlad("Nie znaleziono runu o tym identyfikatorze.");
          return;
        }
        if (!r.ok) {
          setBlad(KOMUNIKAT_SIEC);
          return;
        }
        const run: Run = await r.json();
        if (anulowano) return;
        setBlad(null);
        setStatus(run.status);
        setReason(run.reason);
        setZdarzenia((p) => scal(p, run.events));
      } catch {
        if (!anulowano) setBlad(KOMUNIKAT_SIEC);
      }
    }

    // Strumien nie odtwarza historii, wiec GET idzie zawsze, takze gdy strumien
    // sie nie otwiera; onopen dociaga po (ponownym) polaczeniu. Scalanie po seq.
    const es = new EventSource(`/api/runs/${encodeURIComponent(runId)}/stream`);
    void dociagnij();
    es.onopen = () => {
      if (anulowano) return;
      setBladStrumienia(null);
      void dociagnij();
    };
    es.onerror = () => {
      if (!anulowano) setBladStrumienia(KOMUNIKAT_STRUMIEN);
    };
    // Serwer zamyka strumien po tym zdarzeniu; close() przed onerror, zeby nie bylo ponawiania.
    // Dane zdarzenia nie sa potrzebne (stały tekst UI), wiec nie ma parsowania.
    es.addEventListener("sesja_wygasla", () => {
      es.close();
      if (anulowano) return;
      poWygasnieciuSesji();
    });
    es.addEventListener("run", (e) => {
      if (anulowano) return;
      const z: Zdarzenie = JSON.parse((e as MessageEvent).data);
      setZdarzenia((p) => scal(p, [z]));
      if (z.kind === "step_finished" || z.kind === "step_failed") void dociagnij();
    });
    return () => {
      anulowano = true;
      es.close();
    };
  }, [runId]);

  async function wyslijStart(potwierdz: boolean) {
    setUruchamiam(true);
    setBladUruchomienia(null);
    setOstrzezenie(null);
    // Kazda porazka ma ten sam skutek: zadnego "starego" runu na ekranie ani w adresie.
    // runId=null domyka stary strumien w cleanupie efektu.
    const porzucRun = () => {
      window.history.replaceState(null, "", window.location.pathname);
      setRunId(null);
    };
    try {
      const r = await fetchZSesja("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // Pusty ticket: klucz pomijamy, bo API odrzuca pusty napis (min_length=1).
        body: JSON.stringify({
          workflow: "szkielet",
          ...(ticket.trim() ? { ticket_id: ticket.trim() } : {}),
          ...(potwierdz ? { potwierdz_rownolegly: true } : {}),
        }),
      });
      if (r.status === 409) {
        // Pole aktywny_run_id odroznia ostrzezenie o rownoleglym runie od "runner offline".
        // Nieczytelne ciało traktujemy jak offline. Biezacego widoku nie zrywamy: decyzja jest u człowieka.
        const aktywny = await r.json().then(
          (j) => (typeof j?.aktywny_run_id === "string" ? (j.aktywny_run_id as string) : null),
          () => null,
        );
        if (aktywny !== null) {
          setOstrzezenie({ aktywnyRunId: aktywny });
          return;
        }
        // Stan pusty zamiast bledu: mow, co zrobic (BRO-123).
        porzucRun();
        setBladUruchomienia(KOMUNIKAT_OFFLINE);
        return;
      }
      if (!r.ok) {
        porzucRun();
        setBladUruchomienia(`Nie udało się uruchomić (błąd ${r.status}).`);
        return;
      }
      const id: string = (await r.json()).run_id;
      window.history.pushState(null, "", `?run=${encodeURIComponent(id)}`);
      setRunId(id);
    } catch {
      porzucRun();
      setBladUruchomienia(KOMUNIKAT_SIEC);
    } finally {
      setUruchamiam(false);
    }
  }

  return (
    <main style={{ fontFamily: "system-ui", maxWidth: 720, margin: "40px auto" }}>
      <h1 style={{ fontSize: 20 }}>Harness Cockpit</h1>
      <label>
        Ticket{" "}
        <input
          type="text"
          value={ticket}
          onChange={(e) => {
            setTicket(e.target.value);
            // Ostrzezenie dotyczylo starej wartosci pola.
            setOstrzezenie(null);
          }}
        />
      </label>{" "}
      <button onClick={() => void wyslijStart(false)} disabled={uruchamiam}>
        {uruchamiam ? "Uruchamiam…" : "Uruchom"}
      </button>
      {ostrzezenie && (
        <div role="alert" style={{ color: "#8A5A00" }}>
          <p>
            Na tym tickecie trwa już run{" "}
            <a href={`?run=${encodeURIComponent(ostrzezenie.aktywnyRunId)}`}>{ostrzezenie.aktywnyRunId}</a>. Uruchomić drugi?
          </p>
          <button onClick={() => void wyslijStart(true)} disabled={uruchamiam}>
            Uruchom mimo to
          </button>{" "}
          <button onClick={() => setOstrzezenie(null)} disabled={uruchamiam}>
            Anuluj
          </button>
        </div>
      )}
      {bladUruchomienia && <p style={{ color: "#A5201A" }}>{bladUruchomienia}</p>}
      {blad && <p style={{ color: "#A5201A" }}>{blad}</p>}
      {bladStrumienia && <p style={{ color: "#A5201A" }}>{bladStrumienia}</p>}
      {runId && (
        <p style={{ color: "#58616D" }}>
          run {runId}
          {status && <> · status: {status}</>}
        </p>
      )}
      {runId && reason && <p style={{ color: "#A5201A" }}>{reason}</p>}
      <ul>
        {zdarzenia.map((z) => (
          <li key={z.seq}>
            <b>{z.kind}</b> {z.text}
            {z.text_truncated && (
              // === null, nie truthiness: brak pomiaru to "nieznana", nigdy 0 (measured_or_null).
              <span style={{ color: "#58616D" }}>
                {" "}
                obcięte, pełna długość {z.text_full_len === null ? "nieznana" : `${z.text_full_len} znaków`}
              </span>
            )}
          </li>
        ))}
      </ul>
    </main>
  );
}

// Lista runnerow zespolu. Przy chwilowym bledzie zostaje ostatnia znana lista
// (z komunikatem), bo znikajaca lista wygladalaby jak "wszystkie runnery padly".
function PanelRunnerow() {
  const [runnery, setRunnery] = useState<Runner[] | null>(null);
  const [blad, setBlad] = useState<string | null>(null);

  useEffect(() => {
    let anulowano = false;

    async function pobierz() {
      try {
        const r = await fetchZSesja("/api/runners");
        if (anulowano) return;
        if (!r.ok) {
          setBlad(KOMUNIKAT_BLAD_RUNNEROW);
          return;
        }
        const lista: Runner[] = await r.json();
        if (anulowano) return;
        setRunnery(lista);
        setBlad(null);
      } catch {
        if (!anulowano) setBlad(KOMUNIKAT_BLAD_RUNNEROW);
      }
    }

    void pobierz();
    const timer = setInterval(() => void pobierz(), ODSWIEZANIE_RUNNEROW_MS);
    return () => {
      anulowano = true;
      clearInterval(timer);
    };
  }, []);

  return (
    <section aria-label="Runnery" style={{ fontFamily: "system-ui", maxWidth: 720, margin: "40px auto" }}>
      <h2 style={{ fontSize: 16 }}>Runnery</h2>
      {blad && <p style={{ color: "#A5201A" }}>{blad}</p>}
      {runnery === null && !blad && <p style={{ color: "#58616D" }}>Ładowanie…</p>}
      {runnery !== null && runnery.length === 0 && <p style={{ color: "#58616D" }}>{KOMUNIKAT_BRAK_RUNNEROW}</p>}
      {runnery !== null && runnery.length > 0 && (
        <table style={{ borderCollapse: "collapse", width: "100%", textAlign: "left" }}>
          <thead>
            <tr>
              <th>Nazwa</th>
              <th>Wersja</th>
              <th>Backendy</th>
              <th>Stan</th>
            </tr>
          </thead>
          <tbody>
            {runnery.map((r) => (
              <tr key={r.id}>
                <td>{r.name}</td>
                <td>{r.version}</td>
                <td>{r.backends.length === 0 ? "brak" : r.backends.map((b) => `${b.nazwa} (${b.auth})`).join(", ")}</td>
                <td>
                  <span style={{ color: r.online ? "#1B7F3B" : "#58616D" }}>{r.online ? "online" : "offline"}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
