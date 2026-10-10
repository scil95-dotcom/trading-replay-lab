"""Offline, synthetic long/flat replay with auditable Decimal accounting."""

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
from typing import Iterable

D = Decimal
UNIT = D("0.000001")


def decimal(value: object, name: str) -> Decimal:
    """Parse text/numbers without admitting NaN, infinity or booleans."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite decimal")
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a finite decimal") from None
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    # Bound inputs to keep arithmetic predictable at the declared precision.
    if result and (abs(result) < D("1e-12") or abs(result) > D("1e12")):
        raise ValueError(f"{name} outside supported range [1e-12, 1e12]")
    return result


@dataclass(frozen=True)
class Config:
    initial_cash: Decimal = D("1000")
    max_units: Decimal = D("5")
    fee_bps: Decimal = D("10")
    slippage_bps: Decimal = D("5")
    stop_loss: Decimal = D("0.08")
    window: int = 3

    def __post_init__(self) -> None:
        for name in ("initial_cash", "max_units", "fee_bps", "slippage_bps", "stop_loss"):
            object.__setattr__(self, name, decimal(getattr(self, name), name))
        if self.initial_cash <= 0 or self.max_units < UNIT:
            raise ValueError("cash must be positive and max_units at least 0.000001")
        if not 0 <= self.fee_bps < 10000 or not 0 <= self.slippage_bps < 10000:
            raise ValueError("fee/slippage bps must be in [0, 10000)")
        if not 0 < self.stop_loss < 1:
            raise ValueError("stop_loss must be in (0, 1)")
        if isinstance(self.window, bool) or not isinstance(self.window, int) or self.window < 2:
            raise ValueError("window must be an integer at least 2")


def past_signal(history: list[Decimal], window: int) -> bool:
    """The caller passes only prices strictly before the execution bar."""
    return len(history) >= window and history[-1] > sum(history[-window:]) / D(window)


def replay(prices: Iterable[object], config: Config | None = None) -> dict:
    """Replay sequential quotes; no network, shorting, leverage or real orders."""
    settings = config or Config()
    quotes = [decimal(value, f"price[{i}]") for i, value in enumerate(prices)]
    if not quotes or any(price <= 0 for price in quotes):
        raise ValueError("provide at least one strictly positive price")
    with localcontext() as context:
        context.prec = 50
        return _replay(quotes, settings)


def _replay(quotes: list[Decimal], settings: Config) -> dict:
    cash, units = settings.initial_cash, D("0")
    entry = D("0")
    fee_rate = settings.fee_bps / D("10000")
    slip_rate = settings.slippage_bps / D("10000")
    history: list[Decimal] = []
    ledger: list[dict] = []
    events: list[dict] = []
    halted = False

    def fill(index: int, side: str, reason: str) -> bool:
        nonlocal cash, units, entry
        quote = quotes[index]
        execution = quote * (1 + slip_rate if side == "buy" else 1 - slip_rate)
        quantity = units
        if side == "buy":
            capacity = min(settings.max_units, cash / (execution * (1 + fee_rate)))
            quantity = capacity.quantize(UNIT, rounding=ROUND_DOWN)
        if not quantity:
            return False
        gross = quantity * execution
        fee = gross * fee_rate
        cash_delta = -(gross + fee) if side == "buy" else gross - fee
        cash += cash_delta
        units += quantity if side == "buy" else -quantity
        entry = execution if side == "buy" else D("0")
        if cash < 0 or units < 0 or units > settings.max_units:
            raise ArithmeticError("cash or position invariant violated")
        ledger.append({
            "bar": index, "side": side, "reason": reason,
            "quantity": str(quantity), "quote": str(quote),
            "execution_price": str(execution), "gross": str(gross),
            "fee": str(fee), "cash_delta": str(cash_delta),
            "cash_after": str(cash), "units_after": str(units),
        })
        return True

    for index, quote in enumerate(quotes):
        want_long = past_signal(history, settings.window)
        action = "halted" if halted else "hold"
        # A stop executes at the observed quote with slippage, not the threshold.
        if not halted and units and quote <= entry * (1 - settings.stop_loss):
            fill(index, "sell", "hard_stop")
            halted, action = True, "hard_stop"
        elif not halted and units and not want_long:
            fill(index, "sell", "past_signal_flat")
            action = "sell"
        elif not halted and not units and want_long:
            action = "buy" if fill(index, "buy", "past_signal_long") else "insufficient_cash"
        events.append({
            "bar": index, "quote": str(quote), "action": action,
            "signal_long": want_long,
            "signal_history_end": index - 1 if history else None,
            "cash": str(cash), "units": str(units),
            "equity": str(cash + units * quote), "halted": halted,
        })
        history.append(quote)
    if units:
        fill(len(quotes) - 1, "sell", "end_of_replay")
        events.append({
            "bar": len(quotes) - 1, "quote": str(quotes[-1]),
            "action": "end_of_replay", "cash": str(cash),
            "units": str(units), "equity": str(cash), "halted": halted,
        })
    return {
        "schema_version": 1, "data_kind": "synthetic_offline",
        "config": {name: str(value) if isinstance(value, Decimal) else value
                   for name, value in asdict(settings).items()},
        "assumptions": {
            "signal": "prior window mean; prior last price above mean means long",
            "execution": "current quote, adverse slippage on both sides",
            "fees": "basis points of execution notional on each fill",
            "position_cap": "absolute units, no shorting or leverage",
            "quantity_increment": str(UNIT), "decimal_precision": 50,
            "stop": "quote at/below entry execution price * (1 - stop_loss); halt entries",
            "terminal": "liquidate any remaining position at final quote with costs",
            "model_limits": "no liquidity, latency, spread, taxes or corporate actions model",
        },
        "summary": {
            "bars": len(quotes), "fills": len(ledger), "halted": halted,
            "final_cash": str(cash), "final_units": str(units),
            "net_change": str(cash - settings.initial_cash),
            "total_fees": str(sum((D(row["fee"]) for row in ledger), D("0"))),
        },
        "ledger": ledger, "events": events,
    }
