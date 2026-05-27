import { useEffect, useMemo, useRef, useState } from "react";
import { apiGet } from "../lib/api";
import { fmtPrice, fmtPct, fmtHeld, cls } from "../lib/format";
import { TrendingUp, TrendingDown, AlertTriangle, CheckCircle2 } from "lucide-react";

type SymRow = { symbol: string; active: boolean; price: number | null };
type SymData = { crypto: SymRow[]; equity: SymRow[] };

type AssetClass = "equity" | "crypto";
type Side = "buy" | "sell";
type SizeMode = "qty" | "notional";
type EntryType = "market" | "limit";

const num = (s: string): number | null => {
  if (s.trim() === "") return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
};

// ─── Tooltip ──────────────────────────────────────────────────────────────────
function Tip({ text }: { text: string }) {
  const [show, setShow] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  return (
    <span className="relative inline-block ml-1 align-middle" ref={ref}>
      <button
        type="button"
        onMouseEnter={() => setShow(true)}
        onMouseLeave={() => setShow(false)}
        className="w-[15px] h-[15px] rounded-full border border-border text-muted hover:text-accent hover:border-accent/50 text-[9px] font-bold leading-none inline-flex items-center justify-center transition-colors"
      >
        ?
      </button>
      {show && (
        <div className="absolute z-50 left-5 top-0 w-64 bg-surface border border-border rounded-lg shadow-xl p-3 text-xs text-text leading-relaxed pointer-events-none">
          {text}
        </div>
      )}
    </span>
  );
}

// ─── Field wrapper ─────────────────────────────────────────────────────────────
function Field({ label, tip, children }: { label: string; tip?: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="flex items-center text-xs text-muted uppercase tracking-wide mb-1.5">
        <span>{label}</span>
        {tip && <Tip text={tip} />}
      </div>
      {children}
    </div>
  );
}

// ─── Symbol combobox ───────────────────────────────────────────────────────────
function SymbolCombobox({
  pool, value, onChange,
}: { pool: { symbol: string }[]; value: string; onChange: (s: string) => void }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState(value);
  const containerRef = useRef<HTMLDivElement>(null);

  const filtered = useMemo(() => {
    const sq = q.toUpperCase();
    return pool.filter(p => p.symbol.includes(sq)).slice(0, 14);
  }, [pool, q]);

  useEffect(() => { setQ(value); }, [value]);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node))
        setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  return (
    <div className="relative" ref={containerRef}>
      <input
        value={q}
        onFocus={() => setOpen(true)}
        onChange={e => {
          const v = e.target.value.toUpperCase();
          setQ(v);
          onChange(v);
          setOpen(true);
        }}
        placeholder="e.g. BTC/USD or NVDA"
        className="w-full bg-bg border border-border rounded-md px-3 py-1.5 text-sm font-mono focus:outline-none focus:border-accent/60"
      />
      {open && filtered.length > 0 && (
        <ul className="absolute z-50 mt-1 w-full bg-surface border border-border rounded-md shadow-lg max-h-52 overflow-y-auto">
          {filtered.map(p => (
            <li key={p.symbol}>
              <button
                type="button"
                onMouseDown={() => { onChange(p.symbol); setQ(p.symbol); setOpen(false); }}
                className={cls(
                  "w-full px-3 py-1.5 text-sm text-left font-mono hover:bg-border/40 transition-colors",
                  p.symbol === value && "bg-accent/10 text-accent",
                )}
              >
                {p.symbol}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ─── Toggle + SideBtn ──────────────────────────────────────────────────────────
function Toggle({ active, disabled, onClick, children }:
  { active: boolean; disabled?: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={cls(
        "px-3 py-1 rounded-md text-xs border transition-colors whitespace-nowrap",
        active ? "bg-accent/15 border-accent/50 text-accent" : "border-border text-muted hover:text-text",
        disabled && "opacity-40 cursor-not-allowed",
      )}
    >
      {children}
    </button>
  );
}

function SideBtn({ active, tone, onClick, children }:
  { active: boolean; tone: "pos" | "neg"; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cls(
        "flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-md text-sm border transition-colors",
        active
          ? (tone === "pos" ? "bg-pos/15 border-pos/50 text-pos" : "bg-neg/15 border-neg/50 text-neg")
          : "border-border text-muted hover:text-text",
      )}
    >
      {children}
    </button>
  );
}

// ─── Main component ────────────────────────────────────────────────────────────
export function Trade() {
  const [symData, setSymData] = useState<SymData>({ crypto: [], equity: [] });
  const [clockOpen, setClockOpen] = useState<boolean | null>(null);
  const [manualPositions, setManualPositions] = useState<any[]>([]);

  const [assetClass, setAssetClass] = useState<AssetClass>("equity");
  const [symbol, setSymbol] = useState("");
  const [side, setSide] = useState<Side>("buy");
  const [sizeMode, setSizeMode] = useState<SizeMode>("qty");
  const [qty, setQty] = useState("");
  const [notional, setNotional] = useState("");
  const [entryType, setEntryType] = useState<EntryType>("market");
  const [limitPrice, setLimitPrice] = useState("");
  const [stopPrice, setStopPrice] = useState("");
  const [stopLimitPrice, setStopLimitPrice] = useState("");
  const [takeProfit, setTakeProfit] = useState("");

  const [quote, setQuote] = useState<number | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<any>(null);
  const [error, setError] = useState<string>("");

  const isCrypto = assetClass === "crypto";

  const pool = useMemo(() =>
    isCrypto
      ? symData.crypto.map(r => ({ symbol: r.symbol }))
      : symData.equity.map(r => ({ symbol: r.symbol })),
    [symData, isCrypto],
  );

  const loadPositions = () => {
    apiGet("/api/positions")
      .then(d => setManualPositions((d.positions || []).filter((p: any) => p.source === "manual")))
      .catch(() => {});
  };

  useEffect(() => {
    apiGet<SymData>("/api/symbols").then(d => setSymData(d)).catch(() => {});
    apiGet("/api/clock").then(d => setClockOpen(!!d.is_open)).catch(() => {});
    loadPositions();
  }, []);

  const switchClass = (c: AssetClass) => {
    setAssetClass(c);
    setSymbol("");
    setQuote(null);
    if (c === "equity") setSizeMode("qty");
  };

  useEffect(() => {
    if (!isCrypto && sizeMode === "notional") setSizeMode("qty");
  }, [isCrypto, sizeMode]);

  // Debounced live quote
  useEffect(() => {
    if (!symbol) { setQuote(null); return; }
    const id = setTimeout(() => {
      apiGet(`/api/quote?symbol=${encodeURIComponent(symbol)}`)
        .then(d => setQuote(d.price ?? null))
        .catch(() => setQuote(null));
    }, 350);
    return () => clearTimeout(id);
  }, [symbol]);

  const sp = num(stopPrice);
  const tp = num(takeProfit);
  const lp = num(limitPrice);
  const anchor = entryType === "limit" && lp != null ? lp : quote;

  const slDistPct = useMemo(() => {
    if (anchor == null || sp == null) return null;
    return (sp - anchor) / anchor * 100;
  }, [anchor, sp]);

  const tpDistPct = useMemo(() => {
    if (anchor == null || tp == null) return null;
    return (tp - anchor) / anchor * 100;
  }, [anchor, tp]);

  const rr = useMemo(() => {
    if (anchor == null || sp == null || tp == null) return null;
    const risk = Math.abs(anchor - sp);
    const reward = Math.abs(tp - anchor);
    return risk > 0 ? reward / risk : null;
  }, [anchor, sp, tp]);

  const errors = useMemo(() => {
    const e: string[] = [];
    if (!symbol) e.push("Pick a symbol.");
    if (sizeMode === "qty") {
      const q = num(qty);
      if (q == null || q <= 0) e.push("Enter a quantity > 0.");
      else if (!isCrypto && q !== Math.floor(q)) e.push("Equities need whole-share qty.");
    } else {
      const n = num(notional);
      if (n == null || n <= 0) e.push("Enter a notional $ amount > 0.");
    }
    if (entryType === "limit" && (lp == null || lp <= 0)) e.push("Enter a limit price.");
    if (sp == null || sp <= 0) e.push("Enter a stop price.");
    if (tp == null || tp <= 0) e.push("Enter a take-profit price.");
    if (anchor != null && sp != null && tp != null) {
      if (side === "buy" && !(sp < anchor && anchor < tp))
        e.push("Long: stop must be below entry, TP above.");
      if (side === "sell" && !(tp < anchor && anchor < sp))
        e.push("Short: TP must be below entry, stop above.");
    }
    const slim = num(stopLimitPrice);
    if (!isCrypto && slim != null) {
      if (side === "buy" && sp != null && slim > sp) e.push("Long stop-limit must be ≤ stop price.");
      if (side === "sell" && sp != null && slim < sp) e.push("Short stop-limit must be ≥ stop price.");
    }
    return e;
  }, [symbol, sizeMode, qty, notional, entryType, lp, sp, tp, anchor, side, stopLimitPrice, isCrypto]);

  const ready = errors.length === 0;

  const buildPayload = () => {
    const p: any = { symbol, side, entry_type: entryType, stop_price: sp, take_profit_price: tp };
    if (sizeMode === "qty") p.qty = num(qty);
    else p.notional = num(notional);
    if (entryType === "limit") p.limit_price = lp;
    const slim = num(stopLimitPrice);
    if (!isCrypto && slim != null) p.stop_limit_price = slim;
    return p;
  };

  const submit = async () => {
    setSubmitting(true);
    setError("");
    setResult(null);
    try {
      const r = await fetch("/api/order", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(buildPayload()),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        setError(body.detail || `${r.status} ${r.statusText}`);
      } else if (body.ok === false) {
        setError(body.error || "Order rejected by Alpaca.");
      } else {
        setResult(body);
        loadPositions();
      }
    } catch (e: any) {
      setError(String(e));
    } finally {
      setSubmitting(false);
      setConfirming(false);
    }
  };

  return (
    <div className="space-y-6 max-w-3xl">
      <div className="flex flex-col sm:flex-row sm:items-baseline sm:justify-between gap-1">
        <h1 className="text-xl font-semibold">Trade</h1>
        <div className="text-sm text-muted">Manual order — stop-loss + take-profit</div>
      </div>

      <div className="card">
        <div className="card-header">
          <h3 className="text-sm font-semibold uppercase tracking-wide text-muted">Order entry</h3>
          {quote != null && <span className="text-sm tabular text-muted">last ${fmtPrice(quote)}</span>}
        </div>
        <div className="card-body space-y-5">

          {/* Asset class */}
          <Field
            label="Asset class"
            tip="Equity = stocks and ETFs (NYSE/NASDAQ) traded during regular hours 9:30–16:00 ET. Crypto = digital assets traded 24/7 on Alpaca's crypto platform. Choose this first — it determines the symbol list, order type (bracket vs self-managed), and sizing rules."
          >
            <div className="flex gap-2">
              <Toggle active={assetClass === "equity"} onClick={() => switchClass("equity")}>Equity</Toggle>
              <Toggle active={assetClass === "crypto"} onClick={() => switchClass("crypto")}>Crypto</Toggle>
            </div>
            <p className="text-xs text-muted mt-1">
              {isCrypto
                ? "Crypto: no native Alpaca bracket — the bot's exit loop enforces SL/TP after entry fills."
                : "Equity: native Alpaca bracket order. SL and TP legs attach directly to your entry."}
            </p>
          </Field>

          {/* Symbol + direction */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <Field
              label="Symbol"
              tip="Type to search the active watchlist. Crypto format: COIN/USD (e.g. BTC/USD, ETH/USD, SOL/USD). Equity format: ticker (e.g. NVDA, SPY). A live quote loads automatically after you select a symbol."
            >
              <SymbolCombobox pool={pool} value={symbol} onChange={setSymbol} />
            </Field>

            <Field
              label="Direction"
              tip="Long (Buy): you buy the asset first. You profit when price rises, and lose if it falls. Short (Sell): you borrow to sell first. You profit when price falls, and lose if it rises. The bot currently only goes long; short is available here for manual use."
            >
              <div className="flex gap-2">
                <SideBtn active={side === "buy"} tone="pos" onClick={() => setSide("buy")}>
                  <TrendingUp size={14} /> Buy · Long
                </SideBtn>
                <SideBtn active={side === "sell"} tone="neg" onClick={() => setSide("sell")}>
                  <TrendingDown size={14} /> Sell · Short
                </SideBtn>
              </div>
              <p className="text-xs text-muted mt-1">
                {side === "buy"
                  ? "Long — buy now, close later at a higher price for profit."
                  : "Short — sell now, buy back later at a lower price for profit."}
              </p>
            </Field>
          </div>

          {/* Sizing + entry type */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <Field
              label="Size"
              tip="How much of the asset to trade. 'Shares / Coins' = an exact quantity (e.g. 10 shares or 0.05 BTC). '$ Amount' = a dollar value Alpaca converts to coins for you — only available for crypto because equity bracket orders require whole-share quantities."
            >
              <div className="flex gap-2 mb-2">
                <Toggle active={sizeMode === "qty"} onClick={() => setSizeMode("qty")}>Shares / Coins</Toggle>
                <Toggle
                  active={sizeMode === "notional"}
                  disabled={!isCrypto}
                  onClick={() => isCrypto && setSizeMode("notional")}
                >$ Amount</Toggle>
              </div>
              {sizeMode === "qty" ? (
                <input
                  value={qty} onChange={e => setQty(e.target.value)}
                  placeholder={isCrypto ? "e.g. 0.05" : "e.g. 10"} inputMode="decimal"
                  className="w-full bg-bg border border-border rounded-md px-3 py-1.5 text-sm tabular focus:outline-none focus:border-accent/60"
                />
              ) : (
                <input
                  value={notional} onChange={e => setNotional(e.target.value)}
                  placeholder="e.g. 500" inputMode="decimal"
                  className="w-full bg-bg border border-border rounded-md px-3 py-1.5 text-sm tabular focus:outline-none focus:border-accent/60"
                />
              )}
              {!isCrypto && <p className="text-xs text-muted mt-1">Whole shares required for equity bracket orders.</p>}
            </Field>

            <Field
              label="Entry type"
              tip="Market: executes immediately at the best available price. Fast and certain to fill, but you don't control the exact price. Limit: only executes at your specified price or better. You control the price but the order may not fill if the market never reaches it."
            >
              <div className="flex gap-2 mb-2">
                <Toggle active={entryType === "market"} onClick={() => setEntryType("market")}>Market</Toggle>
                <Toggle active={entryType === "limit"} onClick={() => setEntryType("limit")}>Limit</Toggle>
              </div>
              <input
                value={limitPrice} onChange={e => setLimitPrice(e.target.value)}
                disabled={entryType !== "limit"} inputMode="decimal"
                placeholder={entryType === "limit" ? "your entry limit price" : "—"}
                className={cls(
                  "w-full bg-bg border border-border rounded-md px-3 py-1.5 text-sm tabular focus:outline-none focus:border-accent/60",
                  entryType !== "limit" && "opacity-40",
                )}
              />
            </Field>
          </div>

          {/* SL + stop-limit + TP */}
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <Field
              label="Stop price — SL trigger"
              tip="The price that activates your stop-loss and exits the trade. For a long (Buy): set BELOW your entry — if price drops here, you exit to cap your loss. For a short (Sell): set ABOVE entry. The distance % shown is relative to your entry price."
            >
              <input
                value={stopPrice} onChange={e => setStopPrice(e.target.value)}
                inputMode="decimal" placeholder="trigger price"
                className="w-full bg-bg border border-neg/50 rounded-md px-3 py-1.5 text-sm tabular focus:outline-none focus:border-neg/70"
              />
              {slDistPct != null && (
                <p className="text-xs text-neg mt-1 tabular">{fmtPct(slDistPct)} from entry</p>
              )}
            </Field>

            <Field
              label="Stop-limit price"
              tip="Optional (equity only). Without this, the stop-loss exits at market when triggered — guaranteed exit but potentially worse price. With this set, the exit becomes a limit order at this price. Risk: if price gaps fast through both levels, you stay in the position. Leave blank for plain stop-market exits."
            >
              <input
                value={stopLimitPrice} onChange={e => setStopLimitPrice(e.target.value)}
                disabled={isCrypto} inputMode="decimal"
                placeholder={isCrypto ? "n/a for crypto" : "optional limit fill"}
                className={cls(
                  "w-full bg-bg border border-border rounded-md px-3 py-1.5 text-sm tabular focus:outline-none focus:border-accent/60",
                  isCrypto && "opacity-40",
                )}
              />
              <p className="text-xs text-muted mt-1">
                {isCrypto ? "Crypto: bot exits at market." : "Blank = plain stop-market."}
              </p>
            </Field>

            <Field
              label="Take-profit price"
              tip="Your target exit price where the position closes for a profit. For a long (Buy): set ABOVE entry — the order fills when price rises here. For a short (Sell): set BELOW entry. The % shown is the gain you're targeting relative to entry."
            >
              <input
                value={takeProfit} onChange={e => setTakeProfit(e.target.value)}
                inputMode="decimal" placeholder="target price"
                className="w-full bg-bg border border-pos/50 rounded-md px-3 py-1.5 text-sm tabular focus:outline-none focus:border-pos/70"
              />
              {tpDistPct != null && (
                <p className="text-xs text-pos mt-1 tabular">{fmtPct(tpDistPct)} from entry</p>
              )}
            </Field>
          </div>

          {/* R:R + market-hours warning */}
          <div className="flex items-center gap-4 text-sm flex-wrap">
            {rr != null && (
              <span className="text-muted flex items-center gap-1">
                Risk/Reward
                <Tip text="How much you stand to gain for every $1 you risk. Calculated as (TP distance) ÷ (SL distance). A 2:1 ratio means you gain $2 per $1 risked. Professional traders generally look for ≥ 1.5:1 — so that even a 40% win rate is profitable." />
                <span className={cls(
                  "font-semibold tabular ml-1",
                  rr >= 2 ? "text-pos" : rr >= 1 ? "text-warn" : "text-neg",
                )}>
                  {rr.toFixed(2)} : 1
                </span>
              </span>
            )}
            {!isCrypto && clockOpen === false && (
              <span className="flex items-center gap-1 text-warn text-xs">
                <AlertTriangle size={12} /> market closed — order queues for next open
              </span>
            )}
          </div>

          {errors.length > 0 && (
            <ul className="text-xs text-warn space-y-1">
              {errors.map((e, i) => (
                <li key={i} className="flex items-center gap-1">
                  <AlertTriangle size={11} /> {e}
                </li>
              ))}
            </ul>
          )}

          {/* Review / confirm / submit */}
          {!confirming ? (
            <button
              type="button"
              className="btn-primary"
              disabled={!ready}
              onClick={() => { setError(""); setResult(null); setConfirming(true); }}
            >
              Review order
            </button>
          ) : (
            <div className="border border-accent/40 rounded-md bg-accent/5 p-4 space-y-3">
              <p className="text-sm">
                <span className="font-semibold">{side === "buy" ? "BUY" : "SELL"}</span>{" "}
                {sizeMode === "qty" ? `${qty} ${symbol}` : `$${notional} ${symbol}`}{" "}
                {entryType === "limit" ? `@ limit ${limitPrice}` : "@ market"}
                {" · "}SL {stopPrice}
                {stopLimitPrice && !isCrypto ? ` (lmt ${stopLimitPrice})` : ""}
                {" · "}TP {takeProfit}
              </p>
              <div className="flex gap-2">
                <button
                  type="button"
                  className={side === "buy" ? "btn-primary" : "btn-danger"}
                  disabled={submitting}
                  onClick={submit}
                >
                  {submitting ? "Placing…" : "Place paper order"}
                </button>
                <button type="button" className="btn" disabled={submitting} onClick={() => setConfirming(false)}>
                  Back
                </button>
              </div>
            </div>
          )}

          {error && (
            <div className="border border-neg/40 rounded-md bg-neg/10 text-neg text-sm p-4 whitespace-pre-wrap">
              {error}
            </div>
          )}
          {result && (
            <div className="border border-pos/40 rounded-md bg-pos/10 text-pos text-sm p-4">
              <div className="flex items-center gap-2 font-medium">
                <CheckCircle2 size={16} /> Order accepted
              </div>
              <div className="text-xs text-muted mt-1 font-mono break-all">
                id {result.order?.id} · status {result.order?.status}
                {result.db_warning && <span className="text-warn"> · DB: {result.db_warning}</span>}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Open manual positions */}
      <div className="card">
        <div className="card-header">
          <h3 className="text-sm font-semibold uppercase tracking-wide text-muted flex items-center gap-1">
            Open manual positions
            <Tip text="Positions you placed from this Trade tab. SL and TP levels are what you set when entering. For equity bracket orders, Alpaca enforces the exits. For crypto, the bot's monitor_exits loop checks SL/TP every 60 seconds." />
          </h3>
          <button type="button" className="text-xs text-muted hover:text-text" onClick={loadPositions}>
            refresh
          </button>
        </div>
        <div className="card-body">
          {manualPositions.length === 0 ? (
            <p className="text-muted text-sm text-center py-4">No open manual positions.</p>
          ) : (
            <div className="overflow-x-auto">
            <table className="w-full min-w-[640px] text-sm table-row-hover">
              <thead className="text-muted text-xs uppercase">
                <tr>
                  <th className="text-left px-3 py-2 whitespace-nowrap">Symbol</th>
                  <th className="text-left px-3 py-2">Side</th>
                  <th className="text-right px-3 py-2">Qty</th>
                  <th className="text-right px-3 py-2">Entry</th>
                  <th className="text-right px-3 py-2">Price</th>
                  <th className="text-right px-3 py-2 text-neg">SL</th>
                  <th className="text-right px-3 py-2 text-pos">TP</th>
                  <th className="text-right px-3 py-2">→ SL</th>
                  <th className="text-right px-3 py-2">→ TP</th>
                  <th className="text-right px-3 py-2">PnL %</th>
                  <th className="px-3 py-2">Held</th>
                </tr>
              </thead>
              <tbody className="tabular">
                {manualPositions.map((p: any) => {
                  const pnl = Number(p.unrealized_plpc) * 100;
                  const pnlCls = pnl > 0 ? "text-pos" : pnl < 0 ? "text-neg" : "";
                  return (
                    <tr key={p.symbol} className="border-t border-border">
                      <td className="px-3 py-2 font-semibold">{p.symbol}</td>
                      <td className="px-3 py-2">
                        <span className={p.side === "long" ? "pill-pos" : "pill-neg"}>{p.side}</span>
                      </td>
                      <td className="px-3 py-2 text-right">{Number(p.qty).toFixed(4)}</td>
                      <td className="px-3 py-2 text-right">{fmtPrice(p.entry_price)}</td>
                      <td className="px-3 py-2 text-right">{fmtPrice(p.current_price)}</td>
                      <td className="px-3 py-2 text-right text-neg">
                        {p.sl != null ? fmtPrice(p.sl) : <span className="text-muted">—</span>}
                      </td>
                      <td className="px-3 py-2 text-right text-pos">
                        {p.tp != null ? fmtPrice(p.tp) : <span className="text-muted">—</span>}
                      </td>
                      <td className="px-3 py-2 text-right">
                        {p.sl_dist_pct != null
                          ? <span className={p.sl_dist_pct < 2 ? "pill-warn" : "pill-muted"}>{fmtPct(-p.sl_dist_pct, 2)}</span>
                          : <span className="text-muted">—</span>}
                      </td>
                      <td className="px-3 py-2 text-right">
                        {p.tp_dist_pct != null
                          ? <span className="pill-pos">{fmtPct(p.tp_dist_pct, 2)}</span>
                          : <span className="text-muted">—</span>}
                      </td>
                      <td className={cls("px-3 py-2 text-right", pnlCls)}>{fmtPct(pnl)}</td>
                      <td className="px-3 py-2 text-muted">{fmtHeld(p.held_seconds)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
