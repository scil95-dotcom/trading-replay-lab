#!/usr/bin/env python3
"""
Noise Area intraday momentum (Zarattini, Aziz, Barbon 2024, SSRN 4824172)
1) replica del paper su SPY 1m  2) versione MES con costi TopstepX  3) simulazione Topstep 50K.

Dati: repo GitHub prsdro/spy, cartella spy_contents/spy (CSV mensili a 1 minuto,
orario ET, etichetta = inizio barra, prezzi NON aggiustati, extended hours inclusi).
    git clone --depth 1 --filter=blob:none --sparse https://github.com/prsdro/spy
    cd spy && git sparse-checkout set spy_contents/spy

Uso:  pip install numpy pandas pandas_market_calendars
      python noise_area_topstep.py --data spy/spy_contents/spy
"""
import argparse, glob, os
import numpy as np
import pandas as pd
import pandas_market_calendars as mcal

NB = 390                 # barre RTH 09:30..15:59
ES_LEVEL = 7750.0        # livello ES (future dic 2026, S&P 500 a 7.704 il 24/9/2026) a cui si riportano i rendimenti
MES_MULT = 5.0           # $ per punto MES
TICK = 0.25              # punti ES
MES_COMM_RT = 1.22       # TopstepX, $ a giro completo per MES (help.topstep.com, set 2026)


# ----------------------------------------------------------------------------- dati
def load(src, y0=2006):
    """SPY 1m del repo prsdro/spy (orario ET, etichetta = inizio barra)."""
    files = sorted(f for f in glob.glob(os.path.join(src, "*_SPY_1M.csv"))
                   if int(os.path.basename(f)[:4]) >= y0)
    df = pd.concat((pd.read_csv(f) for f in files), ignore_index=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return frame_to_arrays(df)


def load_oanda(src, y0=2006, y1=2100):
    """CFD Oanda 1m (repo FutureSharks/financial-data, es. NAS100_USD, SPX500_USD): orario UTC convertito in ET,
    volume = numero di aggiornamenti di prezzo (usato come peso della VWAP)."""
    files = sorted(glob.glob(os.path.join(src, "*", "*.csv")))
    files = [f for f in files if y0 <= int(os.path.basename(os.path.dirname(f))) <= y1]
    df = pd.concat((pd.read_csv(f) for f in files), ignore_index=True)
    ts = pd.to_datetime(df["time"]).dt.tz_localize("UTC").dt.tz_convert("America/New_York").dt.tz_localize(None)
    df = pd.DataFrame(dict(timestamp=ts, open=df.open, high=df.high, low=df.low, close=df.close, volume=df.volume))
    return frame_to_arrays(df)


def frame_to_arrays(df):
    df = df.drop_duplicates("timestamp")
    mins = df.timestamp.dt.hour * 60 + df.timestamp.dt.minute
    df = df[(mins >= 570) & (mins <= 959)].copy()
    df["date"] = df.timestamp.dt.normalize()
    df["m"] = (df.timestamp.dt.hour * 60 + df.timestamp.dt.minute - 570).astype(int)

    sched = mcal.get_calendar("NYSE").schedule(df.date.min(), df.date.max())
    cal = pd.DatetimeIndex(sched.index).normalize()
    close_et = sched.market_close.dt.tz_convert("America/New_York")
    sess = pd.Series((close_et.dt.hour * 60 + close_et.dt.minute - 570).values, index=cal)

    days = pd.DatetimeIndex(sorted(df.date.unique()))
    days = days[days.isin(cal)]
    df = df[df.date.isin(days)]
    D = len(days)
    L = sess.reindex(days).values.astype(int)          # 390, o 210 nelle mezze giornate
    calpos = cal.get_indexer(days)                     # posizione nel calendario (per i buchi)

    r, c = days.get_indexer(df.date), df.m.values
    A = {}
    for k in ["open", "high", "low", "close", "volume"]:
        a = np.full((D, NB), np.nan)
        a[r, c] = df[k].values
        A[k] = a
    after = np.arange(NB)[None, :] >= L[:, None]       # after-hours nelle mezze giornate: via
    for k in A:
        A[k][after] = np.nan

    # barre mancanti: close ffill, O=H=L=close precedente, volume 0
    C = pd.DataFrame(A["close"]).ffill(axis=1).bfill(axis=1).to_numpy(copy=True)
    miss = np.isnan(A["close"])
    Cprev = np.concatenate([C[:, :1], C[:, :-1]], axis=1)
    O = np.where(miss, Cprev, A["open"]); O[:, 0] = np.where(np.isnan(A["open"][:, 0]), C[:, 0], O[:, 0])
    H = np.where(miss, C, A["high"]); Lo = np.where(miss, C, A["low"])
    V = np.where(miss, 0.0, A["volume"])
    for a in (C, O, H, Lo):
        a[after] = np.nan

    # stoppini anomali (print errati del consolidato): massimo 0,4% oltre i corpi delle barre vicine
    bh = pd.DataFrame(np.fmax(O, C)).T.rolling(5, center=True, min_periods=1).max().T.values
    bl = pd.DataFrame(np.fmin(O, C)).T.rolling(5, center=True, min_periods=1).min().T.values
    Hc = np.fmin(H, bh * 1.004); Lc = np.fmax(Lo, bl * 0.996)
    n_fixed = int(np.nansum((Hc < H) | (Lc > Lo)))

    return dict(days=days, L=L, calpos=calpos, O=O, H=Hc, Lo=Lc, C=C, V=V, n_fixed=n_fixed)


# ----------------------------------------------------------------------------- feature
def features(X, lookback=14):
    O, C, H, Lo, V, L = X["O"], X["C"], X["H"], X["Lo"], X["V"], X["L"]
    D = len(L)
    day_open = O[:, 0]
    day_close = C[np.arange(D), L - 1]
    prev_close = np.r_[np.nan, day_close[:-1]]

    move = np.abs(C / day_open[:, None] - 1.0)
    ok = ~np.isnan(move)
    cs = np.vstack([np.zeros((1, NB)), np.cumsum(np.where(ok, move, 0.0), axis=0)])
    cn = np.vstack([np.zeros((1, NB)), np.cumsum(ok, axis=0)])
    sig = np.full((D, NB), np.nan)
    s = cs[lookback:D] - cs[0:D - lookback]           # somma sui 14 giorni PRECEDENTI (oggi escluso)
    n = cn[lookback:D] - cn[0:D - lookback]
    with np.errstate(invalid="ignore", divide="ignore"):
        sig[lookback:] = np.where(n >= lookback - 3, s / n, np.nan)

    ub = np.fmax(day_open, prev_close)[:, None] * (1 + sig)
    lb = np.fmin(day_open, prev_close)[:, None] * (1 - sig)

    hlc = (H + Lo + C) / 3.0
    cv = np.nancumsum(np.nan_to_num(hlc) * V, axis=1)
    cvol = np.nancumsum(V, axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        vwap = np.where(cvol > 0, cv / cvol, C)

    # giorni validi: giorno precedente presente nel calendario e 14 giorni consecutivi alle spalle
    cp = X["calpos"]
    valid = np.zeros(D, bool)
    valid[lookback:] = (cp[lookback:] - cp[:-lookback] == lookback)

    # volatilità giornaliera a 14 giorni (per il sizing a target di volatilità del paper)
    ret = day_close / prev_close - 1
    dvol = pd.Series(ret).rolling(lookback, min_periods=lookback).std().shift(1).values

    return dict(day_open=day_open, prev_close=prev_close, sig=sig, ub=ub, lb=lb, vwap=vwap,
                valid=valid, dvol=dvol)


def signal_at(F, C, d, m):
    c = C[d, m]
    if c > F["ub"][d, m] and c > F["vwap"][d, m]:
        return 1
    if c < F["lb"][d, m] and c < F["vwap"][d, m]:
        return -1
    return 0


# ----------------------------------------------------------------------------- 1) replica paper
def paper_backtest(X, F, comm=0.0035, slip=0.001):
    """Regole del paper: controlli alle mezz'ore sul close a 1m, esecuzione a quel close,
    posizione = segnale (0 = flat), uscita al close delle 16:00. Esposizione 1x del nozionale."""
    C, L = X["C"], X["L"]
    trades, daily = [], np.zeros(len(L))
    for d in np.where(F["valid"])[0]:
        o = F["day_open"][d]
        pos, entry, m_in = 0, np.nan, -1
        for m in range(29, L[d] - 1, 30):              # 10:00 ... 15:30 (15:59 = chiusura)
            s = signal_at(F, C, d, m)
            if s != pos:
                if pos != 0:
                    trades.append((d, pos, entry, C[d, m], o, m_in, m))
                if s != 0:
                    entry, m_in = C[d, m], m
                pos = s
        if pos != 0:
            trades.append((d, pos, entry, C[d, L[d] - 1], o, m_in, L[d] - 1))
    T = pd.DataFrame(trades, columns=["d", "dir", "entry", "exit", "open", "m_in", "m_out"])
    T["gross"] = T.dir * (T.exit - T.entry) / T.open          # rendimento su nozionale 1x
    T["net"] = T.gross - 2 * (comm + slip) / T.open
    daily = T.groupby("d").net.sum().reindex(range(len(L)), fill_value=0.0).values
    return T, daily


def perf(daily, days, mask=None):
    s = pd.Series(daily, index=days)
    if mask is not None:
        s = s[mask]
    eq = (1 + s).cumprod()
    yrs = len(s) / 252
    dd = (eq / eq.cummax() - 1).min()
    return dict(CAGR=eq.iloc[-1] ** (1 / yrs) - 1, vol=s.std() * np.sqrt(252),
                Sharpe=s.mean() / s.std() * np.sqrt(252), MDD=dd,
                anni=s.groupby(s.index.year).apply(lambda x: (1 + x).prod() - 1))


def trade_stats(net):
    w, l = net[net > 0], net[net <= 0]
    return dict(n=len(net), hit=len(w) / len(net), payoff=w.mean() / -l.mean())


# ----------------------------------------------------------------------------- 2) versione MES
def mes_backtest(X, F, slip_ticks=0.5, comm_rt=MES_COMM_RT, cat_stop=None, invert=False, delay=0, miss=0.0, seed=0,
                 level=ES_LEVEL, mult=MES_MULT, tick=TICK, min_stop_pts=8.0):
    """Segnale sul close della barra di controllo, esecuzione all'OPEN del minuto dopo.
    Flat all'open della barra 15:55 (12:55 nelle mezze giornate). PnL in $ per 1 MES con
    prezzi riportati al livello `level` del future. cat_stop = moltiplicatore di D (es. 1.5) con minimo min_stop_pts punti.
    delay = minuti di ritardo extra nell'esecuzione (stress test).
    miss = probabilità di saltare un controllo di mezz'ora (esecuzione manuale): la posizione resta com'è.
    Restituisce trade e, per giorno, PnL finale e PnL minimo intraday (per MLL/DLL)."""
    O, H, Lo, C, L = X["O"], X["H"], X["Lo"], X["C"], X["L"]
    D = len(L)
    cost = comm_rt + 2 * slip_ticks * tick * mult
    day_pnl, day_min = np.zeros(D), np.zeros(D)
    trades = []
    rng = np.random.default_rng(seed)
    for d in np.where(F["valid"])[0]:
        k = level / F["day_open"][d]                 # punti del future per unità di prezzo del sottostante
        pv = k * mult                                # $ per unità di prezzo, per contratto
        Fb = L[d] - 5                                # barra di flat (15:55)
        sig = {}
        for m in range(29, Fb - 1 - delay, 30):      # ultimo controllo 15:30
            if miss and rng.random() < miss:
                continue
            s = signal_at(F, C, d, m)
            sig[m] = -s if invert else s
        if not any(sig.values()):
            continue
        pos, entry, stop, realized, mn = 0, np.nan, np.nan, 0.0, 0.0
        for m in range(30 + delay, Fb + 1):
            tgt = 0 if m == Fb else sig.get(m - 1 - delay, pos)
            if tgt != pos:
                if pos != 0:
                    pnl = pos * (O[d, m] - entry) * pv - cost
                    realized += pnl; trades.append((d, pos, pnl, "segnale"))
                pos = tgt
                if pos != 0:
                    entry = O[d, m]
                    if cat_stop:
                        ms = m - 1 - delay
                        lvl = max(F["ub"][d, ms], F["vwap"][d, ms]) if pos * (1 if not invert else -1) > 0 \
                            else min(F["lb"][d, ms], F["vwap"][d, ms])
                        dist = max(cat_stop * abs(C[d, ms] - lvl) * k, min_stop_pts) / k
                        stop = entry - pos * dist
            if m == Fb:
                break
            if pos != 0 and cat_stop:
                hit = (Lo[d, m] <= stop) if pos > 0 else (H[d, m] >= stop)
                if hit:
                    fill = min(stop, O[d, m]) if pos > 0 else max(stop, O[d, m])
                    pnl = pos * (fill - entry) * pv - cost
                    realized += pnl; trades.append((d, pos, pnl, "stop"))
                    mn = min(mn, realized)
                    pos = 0
                    continue
            if pos > 0:
                u = (Lo[d, m] - entry) * pv - cost
            elif pos < 0:
                u = (entry - H[d, m]) * pv - cost
            else:
                u = 0.0
            mn = min(mn, realized + u)
        day_pnl[d], day_min[d] = realized, min(mn, realized)
    T = pd.DataFrame(trades, columns=["d", "dir", "pnl", "uscita"])
    return T, day_pnl, day_min


# ----------------------------------------------------------------------------- 3) Topstep 50K
def one_eval(p, m, i, n, target=3000.0, mll=2000.0, dll=1000.0, cons=0.55, sb=50000.0):
    """Una eval dal giorno i. MLL trailing sul saldo di fine giornata, bloccato a 50.000, violato dal
    minimo intraday (realizzato + non realizzato). DLL: chiude la giornata, non fa fallire.
    Consistency: miglior giorno <= 55% del profitto totale (altrimenti il target sale).
    Ritorna (+1 passata / -1 bruciata / 0 aperta, giorni di trading)."""
    bal = peak = sb
    best, d = 0.0, 0
    for j in range(i, len(p)):
        dist = bal - min(peak - mll, sb)
        lo, fin = n * m[j], n * p[j]
        d += 1
        if dll and dist > dll and lo <= -dll:
            r = -dll
        elif lo <= -dist:
            return -1, d
        else:
            r = fin
        bal += r
        best = max(best, r)
        peak = max(peak, bal)
        prof = bal - sb
        if prof >= target and best <= cons * prof:
            return 1, d
    return 0, d


def topstep(p, m, n, starts, **kw):
    out = [(i, *one_eval(p, m, i, n, **kw)) for i in starts]
    return pd.DataFrame(out, columns=["start", "esito", "giorni"])


def pass_rate(R):
    r = R[R.esito != 0]
    return (r.esito == 1).mean()


def serial(p, m, n, fee=49.0, act=149.0, **kw):
    """Trader seriale: parte il primo giorno, riparte il giorno dopo ogni esito.
    Costo: abbonamento $49 ogni 21 giorni di trading + $149 di attivazione a ogni pass."""
    i, ps, fs, cost = 0, 0, 0, 0.0
    while i < len(p):
        r, d = one_eval(p, m, i, n, **kw)
        if r == 0:
            break
        cost += fee * np.ceil(d / 21)
        if r == 1:
            ps += 1; cost += act
        else:
            fs += 1
        i += d
    return ps, fs, cost


def block_boot(p, m, rng, B=63):
    T = len(p)
    st = rng.integers(0, T - B, int(np.ceil(T / B)))
    ix = (st[:, None] + np.arange(B)[None, :]).ravel()[:T]
    return p[ix], m[ix]


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="spy/spy_contents/spy")
    ap.add_argument("--boot", type=int, default=200, help="storie bootstrap (0 = salta)")
    a = ap.parse_args()

    X = load(a.data)
    F = features(X)
    days = X["days"]
    print(f"Giorni: {len(days)} ({days[0].date()} - {days[-1].date()}), validi: {F['valid'].sum()}, "
          f"stoppini anomali corretti: {X['n_fixed']}")

    # 1) replica paper
    T, daily = paper_backtest(X, F)
    st = trade_stats(T.net.values)
    p = perf(daily, days, days >= "2007-01-01")
    print("\n=== 1. REPLICA PAPER (SPY 2007-2025, 1x, costi del paper, uscita 16:00) ===")
    print(f"trade {st['n']}  hit {st['hit']:.1%}  payoff {st['payoff']:.2f}  Sharpe {p['Sharpe']:.2f}  "
          f"CAGR {p['CAGR']:.1%}  MDD {p['MDD']:.1%}  bps/trade netti {T.net.mean()*1e4:.2f}")
    print("Rendimento per anno (%):", " ".join(f"{y}:{v*100:+.1f}" for y, v in p["anni"].items()))
    lev = np.clip(0.02 / F["dvol"], 0, 4); lev = np.where(np.isnan(lev), 4, lev)
    win = (days >= "2007-05-01") & (days <= "2024-04-30")
    pv = perf(daily * lev, days, win)
    print(f"Finestra e sizing del paper (mag 2007-apr 2024, vol target 2%, leva max 4): Sharpe {pv['Sharpe']:.2f}  "
          f"CAGR {pv['CAGR']:.1%}  MDD {pv['MDD']:.1%}   [paper: 1,33 / 19,6% / 25%]")

    # 2) MES
    print(f"\n=== 2. MES (ingresso all'open del minuto dopo, flat 15:55, prezzi a ES={ES_LEVEL:.0f}) ===")
    yrs = F["valid"].sum() / 252
    scen = {"Lordo": dict(slip_ticks=0, comm_rt=0),
            "$1,22 + 0,5 tick/lato (base)": dict(slip_ticks=0.5),
            "$1,22 + 1 tick/lato": dict(slip_ticks=1.0),
            "base + stop catastrofico 1,5D": dict(slip_ticks=0.5, cat_stop=1.5)}
    res = {}
    for name, kw in scen.items():
        Tm, dp, dm = mes_backtest(X, F, **kw)
        res[name] = (dp, dm)
        s = trade_stats(Tm.pnl.values)
        v = F["valid"]
        print(f"{name:32s} trade {s['n']:5d}  $/trade {Tm.pnl.mean():6.2f}  $/anno {dp.sum()/yrs:6.0f}  "
              f"hit {s['hit']:.1%}  payoff {s['payoff']:.2f}  Sharpe {dp[v].mean()/dp[v].std()*np.sqrt(252):.2f}  "
              f"peggior trade {Tm.pnl.min():5.0f}")
    dpb, dmb = res["$1,22 + 0,5 tick/lato (base)"]
    yy = pd.Series(dpb, index=days).groupby(days.year).sum()
    print("PnL per anno, 1 MES, base ($):", " ".join(f"{y}:{v:+.0f}" for y, v in yy.items()))
    print(f"media {yy.mean():.0f}  mediana {yy.median():.0f}  media senza 2008 {yy.drop(2008).mean():.0f}  "
          f"quota del 2008 sul totale {yy[2008]/yy.sum():.0%}")

    # 3) Topstep
    print("\n=== 3. TOPSTEP 50K (target 3.000, MLL 2.000 trailing EOD, DLL 1.000, consistency 55%) ===")
    v7 = (days >= "2007-01-01") & F["valid"]
    P, M = dpb[v7], dmb[v7]
    yv = days[v7].year.values
    starts = np.arange(len(P))
    _, g1, m1 = mes_backtest(X, F, slip_ticks=0.5)
    _, g2, m2 = mes_backtest(X, F, slip_ticks=0.5, invert=True)
    Z1, ZM1, Z2, ZM2 = g1[v7], m1[v7], g2[v7], m2[v7]
    rng = np.random.default_rng(7)
    picks = [rng.random(len(P)) < 0.5 for _ in range(20)]     # edge zero: strategia o inversa, a caso
    ny = len(P) / 252
    print(f"{'MES':>3} | {'pass':>5} {'07-17':>5} {'18-25':>5} | {'gg mediani al pass':>18} | "
          f"{'edge zero':>9} | {'pass/anno seriale':>17} {'costo/pass':>10} | {'edge zero: pass/anno':>20} {'costo/pass':>10}")
    for n in [1, 2, 3, 4, 6, 10]:
        R = topstep(P, M, n, starts)
        pr = pass_rate(R); p1 = pass_rate(R[yv <= 2017]); p2 = pass_rate(R[yv >= 2018])
        gd = R[R.esito == 1].giorni.median()
        z = np.mean([pass_rate(topstep(np.where(k, Z1, Z2), np.where(k, ZM1, ZM2), n, starts[::5])) for k in picks[:8]])
        ps, fs, c = serial(P, M, n)
        zs = [serial(np.where(k, Z1, Z2), np.where(k, ZM1, ZM2), n) for k in picks]
        zp = np.mean([q[0] for q in zs]) / ny; zc = np.mean([q[2] / max(q[0], 1) for q in zs])
        print(f"{n:3d} | {pr:5.1%} {p1:5.1%} {p2:5.1%} | {gd:18.0f} | {z:9.1%} | {ps/ny:17.1f} {c/ps:10.0f} | {zp:20.1f} {zc:10.0f}")

    if a.boot:
        print(f"\nBootstrap a blocchi di 63 giorni, {a.boot} storie alternative (partenze ogni 5 giorni):")
        rb = np.random.default_rng(3)
        for n in [2, 3, 4]:
            s = [pass_rate(topstep(*block_boot(P, M, rb), n, starts[::5])) for _ in range(a.boot)]
            print(f"  {n} MES: mediana {np.median(s):.1%}  intervallo 5-95%: {np.percentile(s,5):.1%} - {np.percentile(s,95):.1%}")


if __name__ == "__main__":
    main()
