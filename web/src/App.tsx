import { useEffect, useState } from "react";
import { fetchZSesja, Konto, odswiezSesje, PanelKonta, ustawPowrotDoLogowania, ustawToken } from "./Konto";
import type { Katalog, RunnerZespolu } from "./api";
import { KOMUNIKAT_SIEC, opisOffline, PODPOWIEDZ_OFFLINE_ID, PODPOWIEDZ_OFFLINE, WidokRunu, type StanRunnera } from "./WidokRunu";

const ODSWIEZANIE_RUNNEROW_MS = 5000;
const KOMUNIKAT_TRYB_ODCZYTU = "Runner offline — tryb tylko do odczytu";
const KOMUNIKAT_KATALOG_PUSTY = "Katalog jeszcze nie odświeżony";
const KOMUNIKAT_BLAD_KATALOGU = "Nie udało się pobrać katalogu.";
const KOMUNIKAT_BLAD_ODSWIEZENIA_KATALOGU = "Nie udało się odświeżyć katalogu.";
const KOMUNIKAT_BRAK_RUNNEROW = "Brak runnerów. Zainstaluj i uruchom cockpit-runner z tokenem zespołu.";
const KOMUNIKAT_BLAD_RUNNEROW = "Nie udało się odświeżyć listy runnerów. Ponawiam; pokazana lista może być nieaktualna.";

const KOMUNIKAT_SESJA_WYGASLA = "Sesja wygasła, zaloguj się";
// UTC zamiast strefy przegladarki: ten sam stan katalogu pokazuje ten sam napis wszystkim.
function dataStanu(iso: string): string {
  const d = new Date(iso);
  const dwie = (n: number) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${dwie(d.getUTCMonth() + 1)}-${dwie(d.getUTCDate())} ${dwie(d.getUTCHours())}:${dwie(d.getUTCMinutes())} UTC`;
}

// Wlasny polling, niezalezny od PanelRunnerow: ten panel zostaje nietkniety.
function useStanRunnera(): StanRunnera {
  const [stan, setStan] = useState<StanRunnera>("nieznany");

  useEffect(() => {
    let anulowano = false;

    async function pobierz() {
      try {
        const r = await fetchZSesja("/api/runners");
        if (anulowano) return;
        if (!r.ok) {
          setStan("nieznany");
          return;
        }
        const lista: RunnerZespolu[] = await r.json();
        if (anulowano) return;
        setStan(lista.some((x) => x.online) ? "online" : "offline");
      } catch {
        if (!anulowano) setStan("nieznany");
      }
    }

    void pobierz();
    const timer = setInterval(() => void pobierz(), ODSWIEZANIE_RUNNEROW_MS);
    return () => {
      anulowano = true;
      clearInterval(timer);
    };
  }, []);

  return stan;
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
  return <WidokZalogowany email={stan.email} poWygasnieciuSesji={poWygasnieciuSesji} />;
}

// Osobny komponent, bo hook stanu runnera nie moze stac za wczesnymi return w App.
function WidokZalogowany({ email, poWygasnieciuSesji }: { email: string; poWygasnieciuSesji: () => void }) {
  const stanRunnera = useStanRunnera();
  return (
    <>
      <PanelKonta email={email} />
      {stanRunnera === "offline" && <PasekTrybuOdczytu />}
      <WidokRunu poWygasnieciuSesji={poWygasnieciuSesji} stanRunnera={stanRunnera} />
      <Definicje stanRunnera={stanRunnera} />
      <PanelRunnerow />
    </>
  );
}

// Informacja, nie awaria: neutralny kolor i role="status", nigdy alert.
function PasekTrybuOdczytu() {
  return (
    <div style={{ fontFamily: "system-ui", maxWidth: 720, margin: "40px auto 0" }}>
      <p role="status" style={{ color: "#58616D", background: "#EEF1F4", padding: "8px 12px", margin: 0 }}>
        {KOMUNIKAT_TRYB_ODCZYTU}
      </p>
      <p id={PODPOWIEDZ_OFFLINE_ID} style={{ color: "#58616D", margin: "8px 0 0" }}>
        {PODPOWIEDZ_OFFLINE}
      </p>
    </div>
  );
}

// Katalog z cache w bazie: czytelny bez runnera. Odswiezenie wymaga runnera, bo dysk jest po jego stronie.
function Definicje({ stanRunnera }: { stanRunnera: StanRunnera }) {
  const offline = stanRunnera === "offline";
  const [katalog, setKatalog] = useState<Katalog | null>(null);
  const [blad, setBlad] = useState(false);
  const [bladOdswiezenia, setBladOdswiezenia] = useState(false);
  const [odswiezam, setOdswiezam] = useState(false);

  useEffect(() => {
    let anulowano = false;
    fetchZSesja("/api/katalog")
      .then(async (r) => {
        if (!r.ok) throw new Error(String(r.status));
        const k: Katalog = await r.json();
        if (!anulowano) setKatalog(k);
      })
      .catch(() => {
        if (!anulowano) setBlad(true);
      });
    return () => {
      anulowano = true;
    };
  }, []);

  async function odswiez() {
    setOdswiezam(true);
    setBladOdswiezenia(false);
    try {
      const r = await fetchZSesja("/api/katalog/odswiez", { method: "POST" });
      if (!r.ok) {
        setBladOdswiezenia(true);
        return;
      }
      const k: Katalog = await r.json();
      setKatalog(k);
    } catch {
      setBladOdswiezenia(true);
    } finally {
      setOdswiezam(false);
    }
  }

  return (
    <section aria-label="Definicje" style={{ fontFamily: "system-ui", maxWidth: 720, margin: "40px auto" }}>
      <h2 style={{ fontSize: 16 }}>Definicje</h2>
      <button onClick={() => void odswiez()} disabled={odswiezam || offline} {...opisOffline(offline)}>
        {odswiezam ? "Odświeżam…" : "Odśwież"}
      </button>
      {bladOdswiezenia && (
        <p role="status" style={{ color: "#58616D" }}>
          {KOMUNIKAT_BLAD_ODSWIEZENIA_KATALOGU}
        </p>
      )}
      {blad && <p style={{ color: "#A5201A" }}>{KOMUNIKAT_BLAD_KATALOGU}</p>}
      {!blad && katalog === null && <p style={{ color: "#58616D" }}>Ładowanie…</p>}
      {katalog !== null && katalog.odswiezono_at === null && <p style={{ color: "#58616D" }}>{KOMUNIKAT_KATALOG_PUSTY}</p>}
      {katalog !== null && katalog.odswiezono_at !== null && (
        <>
          <p style={{ color: "#58616D" }}>stan z {dataStanu(katalog.odswiezono_at)}</p>
          <table style={{ borderCollapse: "collapse", width: "100%", textAlign: "left" }}>
            <thead>
              <tr>
                <th>Rodzaj</th>
                <th>Nazwa</th>
                <th>Rola</th>
              </tr>
            </thead>
            <tbody>
              {katalog.definicje.map((d) => (
                <tr key={`${d.runner_id}:${d.projekt}:${d.rodzaj}:${d.klucz}`}>
                  <td>{d.rodzaj}</td>
                  <td>{d.nazwa}</td>
                  <td>{d.rola ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}

// Lista runnerow zespolu. Przy chwilowym bledzie zostaje ostatnia znana lista
// (z komunikatem), bo znikajaca lista wygladalaby jak "wszystkie runnery padly".
function PanelRunnerow() {
  const [runnery, setRunnery] = useState<RunnerZespolu[] | null>(null);
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
        const lista: RunnerZespolu[] = await r.json();
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
