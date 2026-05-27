/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // dark trading-terminal palette
        bg:      "#0a0c10",
        surface: "#11151c",
        border:  "#1e2532",
        muted:   "#6b7689",
        text:    "#e7ecf3",
        accent:  "#3b82f6",
        pos:     "#22c55e",
        neg:     "#ef4444",
        warn:    "#f59e0b",
      },
      fontFamily: {
        mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'monospace'],
      },
    },
  },
  plugins: [],
};
