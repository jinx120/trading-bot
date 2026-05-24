"""Optional ScrapeMCP sentiment filter for sr_paper_bot.

Talks to the local ScrapeMCP REST API to pull recent scraped items, then
runs a tiny keyword-based sentiment score. The bot abstains from buying
if the symbol-relevant items skew strongly negative.

This is intentionally dumb. It exists to demonstrate the cross-project
plumbing; replace the scoring function with a real model when there's
enough data to train one.

Setup:
  1. In ScrapeMCP UI, create a target for a news feed
     (e.g. Google News, Yahoo Finance) with active=true.
  2. Let the scheduler populate a few items.
  3. Flag the bot with USE_SCRAPEMCP_SENTIMENT=true and ensure
     SCRAPEMCP_URL is reachable.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import requests


SYMBOL_KEYWORDS = {
    "BTC/USD": ["bitcoin", "btc"],
    "ETH/USD": ["ethereum", "eth"],
    "SOL/USD": ["solana", "sol"],
    "LINK/USD": ["chainlink", "link"],
    "AVAX/USD": ["avalanche", "avax"],
    "DOGE/USD": ["dogecoin", "doge"],
    "AAVE/USD": ["aave"],
    "DIA": ["dow jones", "djia", "dow industrial"],
    "SPY": ["s&p 500", "sp500", "spx"],
    "QQQ": ["nasdaq", "ndx", "qqq"],
}

POS_WORDS = {
    "surge", "soar", "rally", "gain", "bullish", "breakout", "all-time high",
    "record high", "outperform", "upgrade", "approval", "adoption", "buying",
    "accumulation", "support", "strong",
}
NEG_WORDS = {
    "plunge", "crash", "drop", "fall", "bearish", "sell-off", "selloff",
    "hack", "exploit", "ban", "lawsuit", "investigation", "downgrade",
    "liquidation", "rejection", "weak", "fear", "panic",
}


def _score(text: str) -> float:
    t = text.lower()
    pos = sum(1 for w in POS_WORDS if w in t)
    neg = sum(1 for w in NEG_WORDS if w in t)
    if pos + neg == 0:
        return 0.0
    return (pos - neg) / (pos + neg)


def _matches(text: str, keywords: Iterable[str]) -> bool:
    t = text.lower()
    return any(re.search(rf"\b{re.escape(k)}\b", t) for k in keywords)


def get_sentiment(
    symbol: str,
    lookback_hours: int = 6,
    base_url: Optional[str] = None,
    bearer: Optional[str] = None,
) -> Optional[float]:
    """Return sentiment in [-1, 1] for symbol-relevant items in last N hours.

    None means "no opinion" (no items found or service down) — bot should
    treat None as neutral and proceed.
    """
    base_url = base_url or os.environ.get("SCRAPEMCP_URL", "http://localhost:8000")
    bearer = bearer or os.environ.get("SCRAPEMCP_TOKEN", "")
    kws = SYMBOL_KEYWORDS.get(symbol, [symbol.split("/")[0].lower()])

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat()
    headers = {}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    try:
        r = requests.get(
            f"{base_url}/items",
            params={"since": cutoff, "limit": 100},
            headers=headers,
            timeout=5,
        )
    except Exception as exc:
        logging.debug("scrapemcp unreachable: %s", exc)
        return None
    if not r.ok:
        logging.debug("scrapemcp /items status=%s", r.status_code)
        return None

    items = r.json() if isinstance(r.json(), list) else r.json().get("items", [])
    relevant_texts: list[str] = []
    for it in items:
        text_blob = " ".join(
            str(v) for k, v in it.items()
            if isinstance(v, str) and k in {"title", "body", "text", "headline", "summary", "raw"}
        )
        if _matches(text_blob, kws):
            relevant_texts.append(text_blob)
    if not relevant_texts:
        return None
    scores = [_score(t) for t in relevant_texts]
    return sum(scores) / len(scores)


def should_skip_for_sentiment(symbol: str, side: str, threshold: float = -0.5) -> tuple[bool, Optional[float]]:
    """Decide whether to skip a trade based on sentiment.

    Skip long entries when sentiment <= -threshold (strongly negative).
    Skip short entries when sentiment >= +threshold (strongly positive).
    Neutral/missing sentiment never skips.
    """
    if not _env_flag("USE_SCRAPEMCP_SENTIMENT"):
        return False, None
    s = get_sentiment(symbol)
    if s is None:
        return False, None
    if side == "buy" and s <= -abs(threshold):
        return True, s
    if side == "sell" and s >= abs(threshold):
        return True, s
    return False, s


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes", "on")
