# -*- coding: utf-8 -*-
"""Motore del sistema "Carry Neutrale" (solo numpy + libreria standard: gira anche sul PC con Smart App Control).

Idea: long spot + short perpetuo sulla stessa moneta, stessa quantita'. Il prezzo si annulla,
resta il funding che i long pagano agli short (piu' il piccolo effetto del basis). Si entra solo
quando il funding medio recente e' alto e si esce quando diventa negativo.
"""
import csv, os, datetime as dt
import numpy as np

DAY_MS = 86_400_000
UNIVERSI = {
    "BTCETH": ["BTC", "ETH"],
    "5monete": ["BTC", "ETH", "SOL", "XRP", "DOGE"],
    "11monete": ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "LTC", "AVAX", "DOT", "BCH"],
}
MAJORS = {"BTC", "ETH"}


def giorno(ts_ms):
    return int((int(ts_ms) - 1) // DAY_MS)  # il funding pagato alle 00:00 appartiene al giorno prima


def data_da_giorno(g):
    return dt.date(1970, 1, 1) + dt.timedelta(days=int(g))


def _leggi(path):
    with open(path, newline="") as f:
        r = csv.reader(f); next(r)
        return [row for row in r]


def funding_giornaliero(dati, venue, coin):
    """Somma dei funding di ogni giorno UTC (frazione del nozionale incassata dallo short)."""
    p = os.path.join(dati, f"{venue}_funding_{coin}.csv")
    if not os.path.exists(p):
        return {}
    out = {}
    hourly = {}
    for row in _leggi(p):
        if venue == "kraken":
            t = dt.datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%S%z").timestamp() * 1000
        else:
            t = float(row[0])
        g = giorno(t)
        if venue == "kraken":
            slot = int(t)
            day = hourly.setdefault(g, {})
            value = float(row[1])
            if slot in day and day[slot] != value:
                raise ValueError("Conflicting funding records")
            day[slot] = value
        else:
            out[g] = out.get(g, 0.0) + float(row[1])
    if venue == "kraken":
        out = {g: sum(v.values()) for g, v in hourly.items() if len(v) == 24}
    return out


def chiusure(dati, nome):
    p = os.path.join(dati, nome)
    if not os.path.exists(p):
        return {}
    return {int(float(r[0]) // DAY_MS): float(r[2]) for r in _leggi(p)}


def basis_giornaliero(dati, venue, coin):
    """basis = prezzo perp / prezzo spot (o indice) - 1, sulla chiusura giornaliera; ritorna anche lo spot."""
    if venue == "binance":
        s, p = chiusure(dati, f"binance_spot_1d_{coin}.csv"), chiusure(dati, f"binance_perp_1d_{coin}.csv")
    elif venue == "kraken":
        s, p = chiusure(dati, f"kraken_index_1d_{coin}.csv"), chiusure(dati, f"kraken_trade_1d_{coin}.csv")
    else:
        return {}, {}
    b = {g: p[g] / s[g] - 1 for g in s if g in p and s[g] > 0}
    return b, s


def matrici(dati, venue, coins, g0=None, g1=None):
    """Matrici giornaliere allineate: F funding, B basis, S spot. NaN dove la moneta non c'e'."""
    fs = {c: funding_giornaliero(dati, venue, c) for c in coins}
    tutti = sorted(set().union(*[set(f) for f in fs.values()]))
    # scarta l'ultimo giorno (incompleto)
    oggi = giorno(dt.datetime.now(dt.timezone.utc).timestamp() * 1000 + 1)
    tutti = [g for g in tutti if g < oggi]
    if g0 is not None:
        tutti = [g for g in tutti if g >= g0]
    if g1 is not None:
        tutti = [g for g in tutti if g <= g1]
    giorni = np.arange(tutti[0], tutti[-1] + 1)
    idx = {g: i for i, g in enumerate(giorni)}
    n, k = len(giorni), len(coins)
    F = np.full((n, k), np.nan); B = np.full((n, k), np.nan); S = np.full((n, k), np.nan)
    for j, c in enumerate(coins):
        for g, v in fs[c].items():
            if g in idx:
                F[idx[g], j] = v
        b, s = basis_giornaliero(dati, venue, c)
        for g, v in b.items():
            if g in idx:
                B[idx[g], j] = v
        for g, v in s.items():
            if g in idx:
                S[idx[g], j] = v
    # il funding orario di Kraken/Hyperliquid va sommato solo su giorni completi: ok per costruzione
    return giorni, F, B, S


# ---------------------------------------------------------------- tasso sui depositi BCE (alternativa senza rischio)
BCE = [  # (data di decorrenza, tasso sui depositi)
    ("2019-01-01", -0.0040), ("2019-09-18", -0.0050), ("2022-07-27", 0.0000), ("2022-09-14", 0.0075),
    ("2022-11-02", 0.0150), ("2022-12-21", 0.0200), ("2023-02-08", 0.0250), ("2023-03-22", 0.0300),
    ("2023-05-10", 0.0325), ("2023-06-21", 0.0350), ("2023-08-02", 0.0375), ("2023-09-20", 0.0400),
    ("2024-06-12", 0.0375), ("2024-09-18", 0.0350), ("2024-10-23", 0.0325), ("2024-12-18", 0.0300),
    ("2025-02-05", 0.0275), ("2025-03-12", 0.0250), ("2025-04-23", 0.0225), ("2025-06-11", 0.0200),
    ("2026-06-17", 0.0225), ("2026-09-16", 0.0250),
]


def tasso_bce(giorni):
    """Tasso annuo sui depositi BCE in vigore in ogni giorno (array)."""
    passi = [((dt.date.fromisoformat(d) - dt.date(1970, 1, 1)).days, r) for d, r in BCE]
    out = np.zeros(len(giorni))
    for i, g in enumerate(giorni):
        r = passi[0][1]
        for g0, v in passi:
            if g >= g0:
                r = v
        out[i] = r
    return out


# ---------------------------------------------------------------- simulazione
DEFAULT = dict(
    L=14,            # giorni di media del funding per il segnale
    E=0.10,          # entra se funding medio annualizzato > 10%
    X=0.00,          # esci se funding medio annualizzato < 0%
    delay=0,         # 0 = esecuzione subito dopo il segnale delle 00:00 UTC; 1 = un giorno dopo
    spot_fee=0.0040, # Kraken Pro spot, livello 1, ordine limite (maker)
    perp_fee=0.0005, # Kraken derivati UE, taker
    slip_major=0.0002, slip_alt=0.0005,  # slippage per lato
    band=0.20,       # ribilanciamento se il nozionale devia oltre il 20%
    lev=1.0,         # nozionale per unita' di capitale (spot usato come collaterale)
    basis=True,      # includi l'effetto del basis perp/spot
    cash=0.0,        # rendimento annuo della liquidita' ferma (0 = prudente; "bce" = tasso BCE del giorno)
    rel_rf=False,    # True: soglie E e X misurate SOPRA il tasso BCE del giorno
    min_obs=None,    # osservazioni minime per il segnale (default = L)
)


def segnale(F, L, min_obs=None):
    """Media mobile del funding giornaliero su L giorni, annualizzata (x365)."""
    n, k = F.shape
    m = min_obs or L
    out = np.full((n, k), np.nan)
    for j in range(k):
        x = F[:, j]
        for t in range(n):
            w = x[max(0, t - L + 1): t + 1]
            v = w[~np.isnan(w)]
            if len(v) >= m and not np.isnan(x[t]):
                out[t, j] = v.mean() * 365
    return out


def simula(giorni, F, B, S, coins, p=None, sig=None):
    q = dict(DEFAULT); q.update(p or {})
    n, k = F.shape
    if sig is None:
        sig = segnale(F, q["L"], q["min_obs"])
    fee = np.array([q["spot_fee"] + q["perp_fee"] + (q["slip_major"] if c in MAJORS else q["slip_alt"]) for c in coins])
    active = np.zeros(k, bool); nz = np.zeros(k)
    R = np.zeros(n); ATT = np.zeros((n, k), bool); trades = 0; costi = np.zeros(n)
    d = 1 + q["delay"]
    rf = tasso_bce(giorni)
    cash = rf if q["cash"] == "bce" else np.full(n, float(q["cash"]))
    for t in range(1, n):
        r = np.zeros(k)
        for j in range(k):
            s = sig[t - d, j] if t - d >= 0 else np.nan
            if np.isnan(F[t, j]):
                if active[j]:  # moneta non piu' quotata: esci
                    r[j] -= nz[j] * fee[j]; active[j] = False; trades += 1
                r[j] += cash[t] / 365
                continue
            rft = rf[max(0, t - d)] if q["rel_rf"] else 0.0
            if not active[j] and not np.isnan(s) and s > q["E"] + rft:
                active[j] = True; nz[j] = q["lev"]; r[j] -= nz[j] * fee[j]; trades += 1
            elif active[j] and (np.isnan(s) or s < q["X"] + rft):
                r[j] -= nz[j] * fee[j]; active[j] = False; trades += 1
                r[j] += cash[t] / 365
                continue
            if active[j]:
                g = nz[j] * F[t, j]
                if q["basis"] and not np.isnan(B[t, j]) and not np.isnan(B[t - 1, j]):
                    g -= nz[j] * (B[t, j] - B[t - 1, j])
                r[j] += g
                if not np.isnan(S[t, j]) and not np.isnan(S[t - 1, j]) and S[t - 1, j] > 0:
                    nz[j] = nz[j] * S[t, j] / S[t - 1, j] / (1 + r[j])
                    if abs(nz[j] - q["lev"]) > q["band"] * q["lev"]:
                        r[j] -= abs(nz[j] - q["lev"]) * fee[j]; nz[j] = q["lev"]
                ATT[t, j] = True
            else:
                r[j] += cash[t] / 365
        R[t] = r.mean()
    return dict(R=R, ATT=ATT, trades=trades, giorni=giorni, sig=sig, rf=rf)


def metriche(R, giorni=None, rf=0.0):
    R = np.asarray(R, float)
    if len(R) < 2:
        return {}
    eq = np.cumprod(1 + R)
    anni = len(R) / 365
    cagr = eq[-1] ** (1 / anni) - 1 if eq[-1] > 0 else -1
    vol = R.std(ddof=1) * np.sqrt(365)
    dd = (eq / np.maximum.accumulate(np.r_[1.0, eq])[1:] - 1).min()
    sh = (R.mean() * 365 - rf) / vol if vol > 0 else np.nan
    return dict(cagr=cagr, media_ann=R.mean() * 365, vol=vol, sharpe=sh, maxdd=dd,
                peggior_giorno=R.min(), anni=anni, finale=eq[-1])
