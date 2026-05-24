# Literature watchlist

Papers + posts to evaluate in upcoming research digests. The remote agent
should pull deeper context on these (especially openly-available preprints
or supplementary material), evaluate whether anything is implementable
inside the existing Phase 0–6 architecture, and propose concrete diffs in
its next digest.

The default position is **skeptical**: a paper makes the watchlist for
context, not because it ought to be implemented. The bot's current
ensemble (sr_bounce + donchian_trend + ma_crossover + zscore_revert) with
its guardrail stack is the baseline. Anything proposed here must clear
the same gates: walk-forward improvement, anti-runaway, retirement.

---

## 2026-05-24

### Deep learning for algorithmic trading: A systematic review of predictive models and optimization strategies
- **DOI**: 10.1016/j.array.2025.100390
- **Journal**: Array (Elsevier, 2025) · 22 citations
- **Type**: Systematic review of LSTM / RNN / CNN / hybrid models in algotrading
- **Verdict**: Survey, not a method. Cites others' work. Useful as a literature map
  to *find* primary papers worth implementing — not actionable on its own.
- **Stated challenges that match our concerns**: data noise, overfitting,
  interpretability — all three are explicitly addressed by our Phase 0/2/4
  architecture without DL.
- **Action**: agent should mine its references list (open-access preprints
  preferred) for one primary paper with: (a) concrete architecture spec,
  (b) backtest including transaction costs, (c) walk-forward across regimes.

### Algorithmic crypto trading using information-driven bars, triple barrier labeling and deep learning
- **DOI**: 10.1186/s40854-025-00866-w (Financial Innovation, open access)
- **Asset class match**: ⭐ BTC + ETH on tick data 2018–2023 — directly applicable
- **Methods of interest**:
  - **Information-driven bars** (CUSUM filter, range bars, volume bars, dollar bars)
    as alternatives to time-bar sampling. Could replace our 1H bar resolution
    with event-driven sampling, which Carver and López de Prado both argue
    is structurally more honest for non-stationary markets.
  - **Triple Barrier labeling** (López de Prado, *Advances in Financial Machine
    Learning*, 2018) — labels each entry by which barrier hits first: TP, SL,
    or time. Almost exactly what our `monitor_exits` does today; explicit ML
    label feature would let us train a meta-model on which entries to take.
  - Transformer architectures (vanilla, FEDformer, Autoformer) compared to
    other DL approaches.
- **Reported finding**: CUSUM-filtered bars + Triple Barrier labeling outperforms
  time bars + next-bar prediction, "even after accounting for transaction costs".
- **Verdict**: This is implementable in pieces. Specifically:
  - **Phase 7 candidate**: replace fixed 60s polling with a CUSUM trigger that
    only evaluates signals when |return since last evaluation| ≥ k × ATR. Bot
    runs fewer ticks, captures more meaningful price moves.
  - **Phase 8 candidate**: add a "triple-barrier meta-classifier" sub-strategy
    that doesn't predict price direction directly — instead predicts whether
    a candidate entry (from the existing ensemble composite) will hit TP
    before SL. Trains on our own historical trade outcomes, retrains weekly.
- **Action**: agent should fetch the open-access PDF and produce a concrete
  Phase 7 implementation sketch with code-level integration points.

---

## How to use this file

- Add a new dated section per research run.
- For each paper: title, DOI, type, **explicit verdict**, **action**.
- Verdicts should be one of: `IMPLEMENT NOW`, `IMPLEMENT LATER (phase X)`,
  `CONTEXT ONLY`, `REJECT`. No vague "interesting" or "promising" — those
  produce zero work.
- When implementing, link from the phase work back to the paper here.
