# Trading Replay Lab

## Automated research extension — October 2026

[Research method and status](docs/RESEARCH.md) · [Actions and reports](https://github.com/scil95-dotcom/trading-replay-lab/actions)

The `research/` extension adds recovered Crypto/Carry regression checks, verifies that the unsafe Noise order CLI remains blocked, and collects public crypto/DEX observations hourly. It runs without LLM calls or broker credentials. Reports explicitly distinguish partial data, descriptive watchlists and blocked historical backtests. It does not provide validated entries. The original synthetic replay below remains independent of these networked research scripts.

A personal, AI-assisted engineering demo by **Alfio Scilio**: an offline replay engine that makes signal timing, execution costs, cash limits and risk events inspectable. It uses invented prices and a deliberately small strategy so reviewers can follow every fill.

The engineering focus is reproducibility and explicit assumptions. This is a new portfolio demonstration, not a record of professional trading experience or investment performance.

## Run in under a minute

Requires Python 3.12 or newer; CI targets 3.12 and 3.13. There are no external dependencies, API keys or accounts. On systems without a `python` command, use `python3` below.

```bash
python -m trading_lab demo --output artifacts/demo.json
python -m unittest discover -s tests -v
```

`--output` defaults to `artifacts/demo.json`. The committed [sample output](artifacts/demo.json) contains 24 synthetic bars, a fill ledger, per-bar events, the configuration and model assumptions. Running the command again produces the same JSON.

## Architecture and mechanics

```text
Synthetic fixture → validated Decimal quotes → prior-history signal
                     → risk checks → cost-aware fills → JSON audit trail
```

- **Past-only signal:** the last observed price must exceed the mean of the previous three prices. Bar `i` uses history ending at `i - 1` and executes at quote `i`. The current quote affects execution and the stop, never its own strategy signal.
- **Long or flat:** at most one open position, with no shorting or leverage. Quantity is capped at five units by default and rounded down to six decimal places. Available cash must cover both the purchase and its fee.
- **Explicit costs:** every buy executes above the quote and every sell below it by the configured slippage basis points. Fees apply to the execution notional on both sides.
- **Hard stop:** a quote at or below 92% of the entry execution price forces a sale and prevents any further entry during that replay. A gap fills at the observed quote with slippage; the threshold is not a guaranteed execution price.
- **Accounting:** Decimal arithmetic uses 50-digit precision. Decimal amounts are JSON strings to preserve their representation. The engine rejects nonpositive, nonfinite and excessively small or large input values before running. Every fill records the cash delta and resulting balances; the final position is liquidated with costs.

[`trading_lab/__init__.py`](trading_lab/__init__.py) owns validation, configuration, the signal and the state machine. [`trading_lab/__main__.py`](trading_lab/__main__.py) is a thin CLI. The [fixture](trading_lab/fixtures/synthetic_prices.json) includes a reversal and a gap to exercise the stop. The [tests](tests/test_replay.py) check signal timing under altered future data, an exact cost calculation, insufficient cash, the position cap, gap stops, invalid inputs, ledger reconciliation and deterministic replay.

## Design tradeoffs

A standard-library-only implementation keeps the demonstration easy to inspect and run. A simple one-position state machine makes accounting and failure boundaries explicit. Decimal values reduce floating-point surprises, but are not rounded to a broker's currency rules; this model has no broker integration.

The replay does not model order books, liquidity, spreads beyond the fixed slippage setting, latency, taxes, corporate actions or intrabar paths. Position size is an absolute unit cap, so rising market value can increase the share of equity held. The hard stop halts entries for the full replay rather than implementing cooldowns. These choices are visible in the output and can be discussed in a code review.

The synthetic results are not evidence of profitability or suitability for real markets. There are no live connectors, real orders or investment recommendations.

## Review and extend

The next useful extensions would be a timestamped quote schema with explicit ordering, portfolio-wide exposure limits, property-based accounting tests and alternative execution models. Each would require independent fixtures and tests before being described as supported.

CI checks Python 3.12 and 3.13, runs the tests and compares the regenerated demo with the committed sample. GitHub Actions are pinned to commit hashes and receive read-only repository permission.
