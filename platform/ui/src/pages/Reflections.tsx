import { useEffect, useState } from "react";
import { apiGet, apiPost } from "../lib/api";
import { fmtUSD, fmtTimeLA, fmtPct } from "../lib/format";
import { ChevronDown, ChevronRight, Play, Shield } from "lucide-react";

export function Reflections() {
  const [refl, setRefl] = useState<any[]>([]);
  const [risk, setRisk] = useState<any>(null);
  const [riskEv, setRiskEv] = useState<any[]>([]);
  const [weights, setWeights] = useState<any[]>([]);
  const [labEv, setLabEv] = useState<any[]>([]);
  const [running, setRunning] = useState(false);
  const [runOutput, setRunOutput] = useState("");
  const [open, setOpen] = useState<Record<number, boolean>>({});

  const reload = () => {
    apiGet("/api/reflections?limit=20").then(d => setRefl(d.reflections || []));
    apiGet("/api/risk-state").then(d => setRisk(d.state));
    apiGet("/api/risk-events?limit=20").then(d => setRiskEv(d.events || []));
    apiGet("/api/strategy-weights").then(d => setWeights(d.weights || []));
    apiGet("/api/lab-events?limit=20").then(d => setLabEv(d.events || []));
  };
  useEffect(reload, []);

  const runReflection = async () => {
    setRunning(true);
    setRunOutput("Running…");
    try {
      const r = await apiPost("/api/run-reflection");
      setRunOutput((r.stdout || "") + (r.stderr ? "\n" + r.stderr : ""));
      reload();
    } catch (e: any) {
      setRunOutput(String(e));
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-baseline justify-between">
        <h1 className="text-xl font-semibold">Reflections</h1>
        <button onClick={runReflection} disabled={running} className="btn-primary">
          <Play size={14} /> {running ? "Running…" : "Run now"}
        </button>
      </div>

      {/* Risk status */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <Card>
          <div className="text-xs text-muted uppercase">Peak equity</div>
          <div className="text-2xl font-semibold tabular mt-1">{fmtUSD(risk?.peak_equity || 0)}</div>
        </Card>
        <Card>
          <div className="text-xs text-muted uppercase">SOD baseline</div>
          <div className="text-2xl font-semibold tabular mt-1">{fmtUSD(risk?.sod_equity || 0)}</div>
          <div className="text-xs text-muted mt-1">{risk?.sod_date ? `as of ${risk.sod_date}` : ""}</div>
        </Card>
        <Card>
          <div className="text-xs text-muted uppercase">Lockdown</div>
          <div className={`text-2xl font-semibold mt-1 ${risk?.lockdown ? "text-neg" : "text-pos"}`}>
            {risk?.lockdown ? "ACTIVE" : "OK"}
          </div>
          {risk?.lockdown_reason && (
            <div className="text-xs text-neg mt-1">{risk.lockdown_reason}</div>
          )}
        </Card>
      </div>

      {/* Strategy weights */}
      <Card title="Active strategy weights">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {weights.map((w: any) => (
            <div key={w.name} className="card-body">
              <div className="flex items-center justify-between">
                <span className="font-medium">{w.name}</span>
                <span className={w.enabled ? "pill-pos" : "pill-muted"}>
                  {w.enabled ? "live" : "off"}
                </span>
              </div>
              <div className="text-xl font-semibold tabular mt-1">
                {(Number(w.weight) * 100).toFixed(1)}%
              </div>
              <div className="text-xs text-muted mt-1">
                {w.sharpe_30d != null ? `Sharpe 30d ${Number(w.sharpe_30d).toFixed(2)}` : "—"}
              </div>
            </div>
          ))}
        </div>
      </Card>

      {/* Reflections list */}
      <Card title="Recent reflections">
        {refl.length === 0 ? (
          <div className="text-muted text-sm py-4 text-center">
            No reflections yet. Run one with the button above, or wait for
            the auto-trigger (every 6h).
          </div>
        ) : (
          <ul className="divide-y divide-border">
            {refl.map((r: any) => (
              <li key={r.id} className="py-3">
                <button
                  onClick={() => setOpen(o => ({ ...o, [r.id]: !o[r.id] }))}
                  className="w-full flex items-center gap-3 text-left"
                >
                  {open[r.id] ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                  <span className="text-xs text-muted tabular w-32">{fmtTimeLA(r.run_ts)}</span>
                  <span className="text-sm">
                    {r.n_trades_analyzed} trades · {r.n_signals_analyzed} signals
                  </span>
                  <span className="ml-auto">
                    {r.applied ? <span className="pill-pos">applied</span> :
                     r.reverted_at ? <span className="pill-warn">reverted</span> :
                     <span className="pill-muted">—</span>}
                  </span>
                </button>
                {open[r.id] && (
                  <div className="mt-2 ml-7 space-y-2">
                    <pre className="text-xs font-mono whitespace-pre-wrap text-muted">
                      {r.summary}
                    </pre>
                    {r.proposed_changes && Object.keys(r.proposed_changes).length > 0 && (
                      <div>
                        <div className="text-xs text-muted uppercase mb-1">Proposed</div>
                        <pre className="text-xs font-mono bg-bg p-2 rounded border border-border">
                          {JSON.stringify(r.proposed_changes, null, 2)}
                        </pre>
                      </div>
                    )}
                    {r.applied_changes && Object.keys(r.applied_changes).length > 0 && (
                      <div>
                        <div className="text-xs text-pos uppercase mb-1">Applied</div>
                        <pre className="text-xs font-mono bg-bg p-2 rounded border border-border">
                          {JSON.stringify(r.applied_changes, null, 2)}
                        </pre>
                      </div>
                    )}
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>

      {/* Events */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Card title="Risk events">
          {riskEv.length === 0 ? (
            <div className="text-muted text-sm py-4 text-center">No circuit-breaker activity.</div>
          ) : (
            <ul className="text-sm space-y-2">
              {riskEv.map((e: any, i: number) => (
                <li key={i} className="flex items-center gap-3">
                  <Shield size={14} className="text-warn" />
                  <span className="text-xs text-muted tabular w-32">{fmtTimeLA(e.ts)}</span>
                  <span className="font-medium">{e.kind}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
        <Card title="Lab events">
          {labEv.length === 0 ? (
            <div className="text-muted text-sm py-4 text-center">No lab activity yet.</div>
          ) : (
            <ul className="text-sm space-y-2">
              {labEv.map((e: any, i: number) => (
                <li key={i} className="flex items-center gap-3">
                  <span className="text-xs text-muted tabular w-32">{fmtTimeLA(e.ts)}</span>
                  <span className="font-medium">{e.kind}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      {runOutput && (
        <Card title="Run output">
          <pre className="font-mono text-xs whitespace-pre-wrap max-h-64 overflow-auto">
            {runOutput}
          </pre>
        </Card>
      )}
    </div>
  );
}

function Card({ title, children }: { title?: string; children: React.ReactNode }) {
  return (
    <div className="card">
      {title && (
        <div className="card-header">
          <h3 className="text-sm font-semibold uppercase tracking-wide text-muted">{title}</h3>
        </div>
      )}
      <div className="card-body">{children}</div>
    </div>
  );
}
