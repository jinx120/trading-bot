import { useEffect, useState } from "react";
import { Activity, Coins, BrainCircuit, CandlestickChart, Wifi, WifiOff } from "lucide-react";
import { useLive } from "./hooks/useLive";
import { Bot } from "./pages/Bot";
import { Trade } from "./pages/Trade";
import { Symbols } from "./pages/Symbols";
import { Reflections } from "./pages/Reflections";

type Page = "bot" | "trade" | "symbols" | "reflections";

const NAV: { id: Page; label: string; icon: any }[] = [
  { id: "bot",         label: "Bot",         icon: Activity },
  { id: "trade",       label: "Trade",       icon: CandlestickChart },
  { id: "symbols",     label: "Symbols",     icon: Coins },
  { id: "reflections", label: "Reflections", icon: BrainCircuit },
];

export default function App() {
  const [page, setPage] = useState<Page>("bot");
  const [now, setNow] = useState(() => new Date());
  const { connected } = useLive();

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  const time = now.toLocaleString("en-US", {
    timeZone: "America/Los_Angeles",
    hour12: false,
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).replace(",", "");

  const conn = connected ? (
    <><Wifi size={12} className="text-pos" /><span className="text-pos">live</span></>
  ) : (
    <><WifiOff size={12} className="text-neg" /><span className="text-neg">disconnected</span></>
  );

  return (
    <div className="flex flex-col md:flex-row h-full">
      {/* Sidebar (desktop) / top bar (mobile) */}
      <aside className="w-full md:w-56 shrink-0 bg-surface border-b md:border-b-0 md:border-r border-border flex flex-col">
        <div className="px-4 py-3 md:py-4 border-b border-border flex items-center justify-between md:block">
          <div>
            <div className="text-text font-semibold">Trading Bot</div>
            <div className="text-muted text-xs mt-0.5 md:mt-1 tabular">{time} PT</div>
          </div>
          {/* Connection — inline on mobile (no footer there) */}
          <div className="flex items-center gap-2 text-xs md:hidden">{conn}</div>
        </div>
        <nav className="flex md:flex-col md:flex-1 gap-1 p-2 overflow-x-auto">
          {NAV.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              onClick={() => setPage(id)}
              className={`shrink-0 flex items-center gap-2 md:gap-3 px-3 py-2 rounded-md
                          text-sm whitespace-nowrap transition-colors md:w-full ${
                page === id
                  ? "bg-accent/15 text-accent"
                  : "text-muted hover:text-text hover:bg-border/30"
              }`}
            >
              <Icon size={16} />
              {label}
            </button>
          ))}
        </nav>
        <div className="hidden md:block p-3 border-t border-border text-xs text-muted">
          <div className="flex items-center gap-2">{conn}</div>
        </div>
      </aside>

      {/* Main */}
      <main className="flex-1 overflow-auto">
        <div className="max-w-[1400px] mx-auto p-4 md:p-6">
          {page === "bot" && <Bot />}
          {page === "trade" && <Trade />}
          {page === "symbols" && <Symbols />}
          {page === "reflections" && <Reflections />}
        </div>
      </main>
    </div>
  );
}
