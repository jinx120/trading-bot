"""Sub-strategy registry. Each module exposes a `score(bars) -> float in [-1, 1]`.

Phase 3 is shadow-mode: bot logs scores but trades only sr_bounce. Phase 4
switches the bot to ensemble voting across all enabled strategies.

Conventions:
  - Score in [-1, +1]. Positive = long bias, negative = short bias, 0 = no edge.
  - Each module is stateless — pure function over bars.
  - `name` attribute matches the strategy column in scores / strategy_weights.
"""
from __future__ import annotations

from . import donchian, ma_crossover, sr_bounce, zscore_revert

REGISTRY = {
    "sr_bounce":      sr_bounce,
    "donchian_trend": donchian,
    "ma_crossover":   ma_crossover,
    "zscore_revert":  zscore_revert,
}
