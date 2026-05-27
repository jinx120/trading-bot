import { useEffect, useState } from "react";
import { useLive } from "../hooks/useLive";
import { apiGet, apiPost } from "../lib/api";
import { fmtUSD, fmtPrice, fmtPct, fmtHeld, fmtTimeLA, tone, cls } from "../lib/format";
import { Play, FlaskConical, Shield } from "lucide-react";

export function Bot() {
  const { data } = useLive();
  const [trades, setTrades] = useState<any[]>([]);
  const [signals, setSignals] = useState<any[]>([]);
  const [risk, setRisk] = useState<any>(null);
  const [riskEvents, setRiskEvents] = useState<any[]>([]);
  const [shadow, setShadow] = useState<any[]>([]);
  const [labEvents, setLabEvents] = useState<any[]>([]);
  const [tickOutput, setTickOutput] = useState<string>("");

  useEffect(() => {
    apiGet("/api/trades?limit=30").then(d => setTrades(d.trades || []));
    apiGet("/api/signals?limit=30").then(d => setSignals(d.signals || []));
    apiGet("/api/risk-state").then(d => setRisk(d.state));
    apiGet("/api/risk-events?limit=10").then(d => setRiskEvents(d.events || []));
    apiGet("/api/shadow-scores?days=7").then(d => setShadow(d.candidates || []));
    apiGet("/api/lab-events?limit=10").then(d => setLabEvents(d.events || []));
  }, []);

  const account = data.account || {};
  const positions = data.positions || [];
  const scoreSymbols = data.scores?.symbols || [];

  // Drawdown math
  const equity = Number(account.equity || 0);
  const peak = Number(risk?.peak_equity || equity || 0);
  const ddPct = peak > 0 ? ((1 - equity / peak) * 100) : 0;

  const runTick = async (dryRun: boolean) => {
    setTickOutput("Running…");
    try {
      const r = await apiPost(`/api/run-tick?dry_run=${dryRun}`);
      setTickOutput((r.stdout || "") + (r.stderr ? "\n" + r.stderr : ""));
    } catch (e: any) {
      setTickOutput(String(e));
    }
  };

  return (
    <div className="space-y-6">
      {/* Top stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Stat label="Equity" value={fmtUSD(equity)} />
        <Stat label="Cash" value={fmtUSD(Number(account.cash || 0))} />
        <Stat label="Buying Power" value={fmtUSD(Number(account.buying_power || 0))} />
        <Stat label="Open Positions" value={String(positions.length)} />
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <Stat label="Peak equity" value={fmtUSD(peak)}
              sub={`Drawdown ${fmtPct(-ddPct, 2)}`}
              accent={ddPct > 5 ? "warn" : ddPct > 10 ? "neg" : "muted"} />
        <Stat
          label="Lockdown"
          value={risk?.lockdown ? "ACTIVE" : "OK"}
          accent={risk?.lockdown ? "neg" : "pos"}
          sub={risk?.lockdown ? String(risk.lockdown_reason || "") : "Within all limits"}
        />
        <Stat label="Live tick"
              value={data.ts ? fmtTimeLA(data.ts) : "—"}
              sub="Updated via WebSocket"
              accent="muted" />
      </div>

      {/* Positions */}
      <Card title="Open positions">
        {positions.length === 0 ? (
          <Empty>No open positions.</Empty>
        ) : (
          <div className="overflow-x-auto">
          <table className="w-full text-sm table-row-hover">
            <thead className="text-muted text-xs uppercase">
              <tr>
                <Th>Symbol</Th><Th>Side</Th>
                <Th className="text-right">Qty</Th>
                <Th className="text-right">Entry</Th>
                <Th className="text-right">Price</Th>
                <Th className="text-right">SL</Th>
                <Th className="text-right">TP</Th>
                <Th className="text-right">→ SL</Th>
                <Th className="text-right">→ TP</Th>
                <Th className="text-right">PnL $</Th>
                <Th className="text-right">PnL %</Th>
                <Th>Held</Th>
                <Th>Strategy</Th>
              </tr>
            </thead>
            <tbody className="tabular">
              {positions.map((p: any) => {
                const t = tone(p.unrealized_pl);
                return (
                  <tr key={p.symbol} className="border-t border-border">
                    <Td className="font-semibold">{p.symbol}</Td>
                    <Td><span className={p.side === "long" ? "pill-pos" : "pill-neg"}>{p.side}</span></Td>
                    <Td className="text-right">{Number(p.qty).toFixed(4)}</Td>
                    <Td className="text-right">{fmtPrice(p.entry_price)}</Td>
                    <Td className="text-right">{fmtPrice(p.current_price)}</Td>
                    <Td className="text-right text-neg">{p.sl != null ? fmtPrice(p.sl) : "—"}</Td>
                    <Td className="text-right text-pos">{p.tp != null ? fmtPrice(p.tp) : "—"}</Td>
                    <Td className="text-right">
                      {p.sl_dist_pct != null
                        ? <span className={p.sl_dist_pct < 2 ? "pill-warn" : "pill-muted"}>{fmtPct(-p.sl_dist_pct, 2)}</span>
                        : "—"}
                    </Td>
                    <Td className="text-right">
                      {p.tp_dist_pct != null
                        ? <span className="pill-pos">{fmtPct(p.tp_dist_pct, 2)}</span>
                        : "—"}
                    </Td>
                    <Td className={cls("text-right", t === "pos" && "text-pos", t === "neg" && "text-neg")}>
                      {fmtUSD(p.unrealized_pl)}
                    </Td>
                    <Td className={cls("text-right", t === "pos" && "text-pos", t === "neg" && "text-neg")}>
                      {fmtPct(Number(p.unrealized_plpc) * 100)}
                    </Td>
                    <Td>{fmtHeld(p.held_seconds)}</Td>
                    <Td className="text-muted">{p.dominant_strategy || (p.bot_managed ? "—" : <span className="text-warn">pre-bot</span>)}</Td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          </div>
        )}
      </Card>

      {/* Ensemble + scores */}
      <Card title="Ensemble (live composite per symbol)">
        {scoreSymbols.length === 0 ? (
          <Empty>No score rows yet — bot needs one tick.</Empty>
        ) : (
          <div className="overflow-x-auto">
          <table className="w-full text-sm table-row-hover">
            <thead className="text-muted text-xs uppercase">
              <tr>
                <Th>Symbol</Th>
                {Object.keys(scoreSymbols[0].scores).map(s => <Th key={s} className="text-right">{s}</Th>)}
                <Th className="text-right">composite</Th>
                <Th>verdict</Th>
              </tr>
            </thead>
            <tbody className="tabular">
              {scoreSymbols.map((s: any) => (
                <tr key={s.symbol} className="border-t border-border">
                  <Td className="font-semibold">{s.symbol}</Td>
                  {Object.entries(s.scores).map(([name, v]) => (
                    <Td key={name} className={cls(
                      "text-right",
                      (v as number) > 0.3 && "text-pos",
                      (v as number) < -0.3 && "text-neg",
                    )}>
                      {(v as number).toFixed(3)}
                    </Td>
                  ))}
                  <Td className={cls(
                    "text-right font-semibold",
                    s.composite > s.threshold * 0.7 && "text-pos",
                    s.composite < -s.threshold * 0.7 && "text-neg",
                  )}>
                    {s.composite >= 0 ? "+" : ""}{s.composite.toFixed(3)}
                  </Td>
                  <Td>
                    {s.entry_eligible
                      ? <span className={s.composite > 0 ? "pill-pos" : "pill-neg"}>
                          {s.composite > 0 ? "LONG" : "SHORT"}
                        </span>
                      : <span className="pill-muted">wait</span>}
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </Card>

      {/* Recent trades */}
      <Card title="Recent bot trades">
        {trades.length === 0 ? (
          <Empty>No trades yet.</Empty>
        ) : (
          <div className="overflow-x-auto">
          <table className="w-full text-sm table-row-hover">
            <thead className="text-muted text-xs uppercase">
              <tr>
                <Th>Entered</Th><Th>Sym</Th><Th>Side</Th>
                <Th className="text-right">Entry</Th>
                <Th className="text-right">Exit</Th>
                <Th>Reason</Th>
                <Th className="text-right">PnL %</Th>
                <Th>Regime</Th>
                <Th>Composite</Th>
              </tr>
            </thead>
            <tbody className="tabular">
              {trades.map((t: any) => {
                const p = Number(t.pnl_pct || 0) * 100;
                return (
                  <tr key={t.id} className="border-t border-border">
                    <Td className="text-muted">{fmtTimeLA(t.entry_ts)}</Td>
                    <Td className="font-medium">{t.symbol}</Td>
                    <Td><span className={t.side === "long" ? "pill-pos" : "pill-neg"}>{t.side}</span></Td>
                    <Td className="text-right">{fmtPrice(Number(t.entry_price))}</Td>
                    <Td className="text-right">{t.exit_price ? fmtPrice(Number(t.exit_price)) : "—"}</Td>
                    <Td className="text-muted">{t.exit_reason || "open"}</Td>
                    <Td className={cls("text-right",
                      p > 0 && "text-pos", p < 0 && "text-neg")}>
                      {t.pnl_pct != null ? fmtPct(p) : "—"}
                    </Td>
                    <Td className="text-muted">{t.regime || "—"}</Td>
                    <Td className="text-muted">
                      {t.composite ? Number(t.composite).toFixed(3) : "—"}
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          </div>
        )}
      </Card>

      {/* Shadow lab + risk events */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Card title="Lab — shadow candidates (7d)">
          {shadow.length === 0 ? (
            <Empty>Nothing yet.</Empty>
          ) : (
            <table className="w-full text-sm">
              <thead className="text-muted text-xs uppercase">
                <tr><Th>Candidate</Th><Th className="text-right">N</Th><Th className="text-right">Hit rate</Th><Th className="text-right">Avg score</Th></tr>
              </thead>
              <tbody className="tabular">
                {shadow.map((c: any) => (
                  <tr key={c.candidate} className="border-t border-border">
                    <Td>{c.candidate}</Td>
                    <Td className="text-right">{c.n}</Td>
                    <Td className="text-right">{c.hit_rate != null ? fmtPct(Number(c.hit_rate) * 100, 1, false) : "—"}</Td>
                    <Td className="text-right">{c.avg_score != null ? Number(c.avg_score).toFixed(3) : "—"}</Td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
        <Card title="Risk events">
          {riskEvents.length === 0 ? (
            <Empty>No circuit-breaker activity.</Empty>
          ) : (
            <ul className="text-sm space-y-2">
              {riskEvents.map((e: any, i: number) => (
                <li key={i} className="flex items-center gap-3">
                  <Shield size={14} className="text-warn" />
                  <span className="text-muted text-xs tabular w-28">{fmtTimeLA(e.ts)}</span>
                  <span className="font-medium">{e.kind}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      {/* Controls */}
      <Card title="Controls">
        <div className="flex gap-3 mb-3">
          <button className="btn" onClick={() => runTick(true)}>
            <FlaskConical size={14} /> Run tick (dry-run)
          </button>
          <button className="btn-danger" onClick={() => runTick(false)}>
            <Play size={14} /> Run tick LIVE
          </button>
        </div>
        {tickOutput && (
          <pre className="font-mono text-xs bg-bg p-3 rounded border border-border max-h-64 overflow-auto whitespace-pre-wrap">
            {tickOutput}
          </pre>
        )}
      </Card>
    </div>
  );
}

// ---- Tiny presentational helpers ----------------------------------------

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="card">
      <div className="card-header"><h3 className="text-sm font-semibold tracking-wide uppercase text-muted">{title}</h3></div>
      <div className="card-body">{children}</div>
    </div>
  );
}

function Stat({ label, value, sub, accent = "muted" }:
  { label: string; value: string; sub?: string; accent?: "pos" | "neg" | "warn" | "muted" }) {
  const accentCls = {
    pos: "text-pos", neg: "text-neg", warn: "text-warn", muted: "text-muted",
  }[accent];
  return (
    <div className="card card-body">
      <div className="text-xs text-muted uppercase tracking-wide">{label}</div>
      <div className="text-2xl font-semibold mt-1 tabular">{value}</div>
      {sub && <div className={cls("text-xs mt-1 tabular", accentCls)}>{sub}</div>}
    </div>
  );
}

function Th({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <th className={cls("text-left font-medium px-3 py-2 whitespace-nowrap", className)}>{children}</th>;
}
function Td({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <td className={cls("px-3 py-2 whitespace-nowrap", className)}>{children}</td>;
}
function Empty({ children }: { children: React.ReactNode }) {
  return <div className="text-muted text-sm text-center py-6">{children}</div>;
}
