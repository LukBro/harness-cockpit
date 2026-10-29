import { useEffect, useState } from "react";
import { fetchZSesja, Konto, odswiezSesje, PanelKonta, ustawPowrotDoLogowania } from "./Konto";

type Zdarzenie = { seq: number; kind: string; text: string; step_id: string };
type Run = {
  id: string;
  workflow: string;
  status: string;
  reason: string | null;
  runner_kind: string;
  events: Zdarzenie[];
};

const KOMUNIKAT_OFFLINE = "Runner jest offline. Uruchom runnera na swojej maszynie.";
const KOMUNIKAT_SIEC = "Brak połączenia z kokpitem.";
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

  function poZalogowaniu() {
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
  if (stan === "gosc") return <Konto onZalogowano={poZalogowaniu} />;
  return (
    <>
      <PanelKonta email={stan.email} />
      <WidokRunu />
    </>
  );
}

function WidokRunu() {
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

  async function uruchom() {
    setUruchamiam(true);
    setBladUruchomienia(null);
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
        body: JSON.stringify({ workflow: "szkielet" }),
      });
      if (r.status === 409) {
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
      <button onClick={uruchom} disabled={uruchamiam}>
        {uruchamiam ? "Uruchamiam…" : "Uruchom"}
      </button>
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
          </li>
        ))}
      </ul>
    </main>
  );
}
