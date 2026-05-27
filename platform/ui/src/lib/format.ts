// Small formatting helpers — keep numbers stable and readable across the app.

export const fmtUSD = (n: number | null | undefined, frac = 2): string => {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: frac,
    maximumFractionDigits: frac,
  });
};

export const fmtPrice = (n: number | null | undefined): string => {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  // Sub-dollar prices need more precision (DOGE/SHIB), big prices need less
  const frac = n < 1 ? 6 : n < 100 ? 4 : 2;
  return n.toLocaleString("en-US", {
    minimumFractionDigits: frac,
    maximumFractionDigits: frac,
  });
};

export const fmtPct = (n: number | null | undefined, frac = 2, signed = true): string => {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const sign = signed && n > 0 ? "+" : "";
  return `${sign}${n.toFixed(frac)}%`;
};

export const fmtHeld = (seconds: number | null | undefined): string => {
  if (!seconds || seconds < 0) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 24) return `${Math.floor(h / 24)}d${h % 24}h`;
  return `${h}h${m.toString().padStart(2, "0")}m`;
};

export const fmtTimeLA = (iso: string | null | undefined): string => {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString("en-US", {
      timeZone: "America/Los_Angeles",
      month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit",
      hour12: false,
    });
  } catch { return "—"; }
};

export const cls = (...xs: (string | false | undefined | null)[]): string =>
  xs.filter(Boolean).join(" ");

export const tone = (n: number | null | undefined): "pos" | "neg" | "neutral" => {
  if (n === null || n === undefined || Number.isNaN(n)) return "neutral";
  return n > 0 ? "pos" : n < 0 ? "neg" : "neutral";
};
