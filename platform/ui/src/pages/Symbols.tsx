import { useEffect, useState } from "react";
import { apiGet, apiPost } from "../lib/api";
import { fmtPrice, cls } from "../lib/format";
import { Check, X } from "lucide-react";

type SymRow = { symbol: string; active: boolean; price: number | null };
type SymData = { crypto: SymRow[]; equity: SymRow[] };

export function Symbols() {
  const [data, setData] = useState<SymData | null>(null);
  const [pending, setPending] = useState<Record<string, boolean>>({});
  const [adding, setAdding] = useState({ kind: "crypto" as "crypto" | "equity", symbol: "" });
  const [savedToast, setSavedToast] = useState<string>("");

  const reload = () => apiGet<SymData>("/api/symbols").then(setData);
  useEffect(() => { reload(); }, []);

  if (!data) return <div className="text-muted">Loading…</div>;

  const toggle = async (kind: "crypto" | "equity", sym: string) => {
    const list = data[kind];
    const newActive = list.filter(r => (r.symbol === sym ? !r.active : r.active)).map(r => r.symbol);
    setPending(p => ({ ...p, [sym]: true }));
    try {
      await apiPost(`/api/symbols/${kind}`, { active: newActive });
      setSavedToast(`Saved ${kind}: ${newActive.length} active`);
      setTimeout(() => setSavedToast(""), 2000);
      reload();
    } finally {
      setPending(p => ({ ...p, [sym]: false }));
    }
  };

  const addSymbol = async () => {
    const sym = adding.symbol.trim().toUpperCase();
    if (!sym) return;
    if (adding.kind === "crypto" && !sym.includes("/")) {
      alert("Crypto needs a slash, e.g. SHIB/USD");
      return;
    }
    if (adding.kind === "equity" && sym.includes("/")) {
      alert("Equity symbols don't have a slash.");
      return;
    }
    const list = data[adding.kind];
    if (list.find(r => r.symbol === sym)) {
      alert(`${sym} already in pool`);
      return;
    }
    const newActive = [...list.filter(r => r.active).map(r => r.symbol), sym];
    await apiPost(`/api/symbols/${adding.kind}`, { active: newActive });
    setAdding({ ...adding, symbol: "" });
    reload();
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-baseline sm:justify-between gap-1">
        <h1 className="text-xl font-semibold">Symbols</h1>
        <div className="text-sm text-muted sm:max-w-md sm:text-right">
          Toggle a card on/off; bot hot-reloads within ~5 min. Existing
          positions are NOT auto-closed when disabled — they exit on SL/TP/trail.
        </div>
      </div>
      {savedToast && (
        <div className="card-body bg-pos/10 border border-pos/30 text-pos text-sm">
          {savedToast}
        </div>
      )}

      <Section title="🪙 Crypto" sub="24/7 markets">
        <Grid rows={data.crypto} pending={pending} onToggle={(s) => toggle("crypto", s)} />
      </Section>

      <Section title="📈 Equities" sub="US regular trading hours only">
        <Grid rows={data.equity} pending={pending} onToggle={(s) => toggle("equity", s)} />
      </Section>

      <div className="card">
        <div className="card-header">
          <h3 className="text-sm font-semibold uppercase tracking-wide text-muted">Add custom symbol</h3>
        </div>
        <div className="card-body flex flex-col sm:flex-row gap-3">
          <select
            value={adding.kind}
            onChange={(e) => setAdding({ ...adding, kind: e.target.value as any })}
            className="bg-bg border border-border rounded-md px-3 py-1.5 text-sm"
          >
            <option value="crypto">Crypto</option>
            <option value="equity">Equity</option>
          </select>
          <input
            placeholder={adding.kind === "crypto" ? "SHIB/USD" : "NVDA"}
            value={adding.symbol}
            onChange={(e) => setAdding({ ...adding, symbol: e.target.value })}
            onKeyDown={(e) => e.key === "Enter" && addSymbol()}
            className="flex-1 bg-bg border border-border rounded-md px-3 py-1.5 text-sm font-mono"
          />
          <button onClick={addSymbol} className="btn-primary">Add</button>
        </div>
      </div>
    </div>
  );
}

function Section({ title, sub, children }: { title: string; sub: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="flex items-baseline gap-3 mb-3">
        <h2 className="text-lg font-semibold">{title}</h2>
        <span className="text-xs text-muted">{sub}</span>
      </div>
      {children}
    </div>
  );
}

function Grid({ rows, pending, onToggle }:
  { rows: SymRow[]; pending: Record<string, boolean>; onToggle: (s: string) => void }) {
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
      {rows.map(r => (
        <button
          key={r.symbol}
          onClick={() => onToggle(r.symbol)}
          disabled={pending[r.symbol]}
          className={cls(
            "card text-left transition-all hover:border-accent/50",
            r.active ? "ring-1 ring-accent/40 border-accent/40" : "opacity-70 hover:opacity-100",
            pending[r.symbol] && "opacity-50 cursor-wait",
          )}
        >
          <div className="card-body">
            <div className="flex items-center justify-between">
              <span className="font-semibold tracking-tight">{r.symbol}</span>
              {r.active
                ? <Check size={16} className="text-pos" />
                : <X size={16} className="text-muted" />}
            </div>
            <div className="text-lg font-mono tabular mt-1">
              {r.price != null ? "$" + fmtPrice(r.price) : <span className="text-muted">—</span>}
            </div>
          </div>
        </button>
      ))}
    </div>
  );
}
