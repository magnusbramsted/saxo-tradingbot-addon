"""Indikatorer og handelsregler (RSI(2)-pullback i optrend). Ren Python, ingen afhængigheder."""


def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def rsi(values, n=2):
    """Wilder-RSI på lukkekurser. Returnerer None ved for lidt data."""
    if len(values) < n + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, n + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / n, losses / n
    for i in range(n + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0)) / n
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - 100 / (1 + rs)


def entry_signal(closes, cfg):
    """Køb når lukkekurs > SMA(trend) og RSI(2) < tærskel. Returnerer (bool, rsi)."""
    trend = sma(closes, cfg["sma_trend"])
    r = rsi(closes, 2)
    if trend is None or r is None:
        return False, r
    return closes[-1] > trend and r < cfg["rsi_entry"], r


def exit_reason(closes, position, bars_held, cfg):
    """Returnerer årsag til salg eller None."""
    price = closes[-1]
    stop = position["entry_price"] * (1 - cfg["stop_loss_pct"] / 100)
    if price <= stop:
        return "nødstop"
    exit_ma = sma(closes, cfg["sma_exit"])
    if exit_ma is not None and price > exit_ma:
        return "luk over SMA%d" % cfg["sma_exit"]
    if bars_held >= cfg["max_hold_days"]:
        return "maks. %d dage" % cfg["max_hold_days"]
    return None


MIN_COMMISSION = {"DKK": 10.0, "USD": 1.0, "EUR": 3.0}


def commission_dkk(value_ccy, ccy, fx):
    """Saxo Classic: 0,08 % (min. pr. valuta) + 0,25 % veksling for ikke-DKK."""
    fee = max(MIN_COMMISSION.get(ccy, 10.0), 0.0008 * value_ccy) * fx
    if ccy != "DKK":
        fee += 0.0025 * value_ccy * fx
    return fee
