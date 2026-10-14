#!/usr/bin/env python3
"""
Crypto trend (Zarattini, Pagani, Barbon 2025, SSRN 5209907) adattato a Kraken Prop con esecuzione manuale una volta al giorno.

Regole del paper (non ottimizzate): ensemble di 9 canali di Donchian sulle chiusure giornaliere, lookback
{5,10,20,30,60,90,150,250,360}; entra quando la chiusura è la più alta degli ultimi L giorni; esce quando la chiusura
scende sotto uno stop che sale e non scende mai: max(stop precedente, metà del canale). Peso per moneta = quota di
lookback attivi x (vol obiettivo / vol realizzata a 90 giorni); long-only; leva totale max 1.

Aggiunte per la prop (Kraken Prop: perdita giornaliera max 3% ripartita alle 00:30 UTC, drawdown statico 3/5/6%):
  - vol obiettivo bassa (parametro), stop di protezione sul server a una distanza tale che, se scattano tutti,
    la perdita resti sotto `loss_cap` del capitale;
  - ribilanciamento solo se il peso cambia più di `band` (meno ordini a mano);
  - costi Kraken Prop: commissione 0,04% per lato, finanziamento del margine 0,033% al giorno sul nozionale,
    più slippage.
Dati: candele spot a 30 minuti di Binance (scarica_binance.py), orario UTC.
"""
import numpy as np, pandas as pd
import numba as nb

LBS = np.array([5, 10, 20, 30, 60, 90, 150, 250, 360])
BAR = pd.Timedelta(minutes=30)


# ----------------------------------------------------------------------------- dati
def load(coins, cartella="dati"):
    """Griglia comune a 30 minuti; barre mancanti = prezzo precedente; NaN prima della quotazione."""
    fr = {c: pd.read_parquet(f"{cartella}/{c}.parquet").set_index("ts") for c in coins}
    t0 = min(f.index.min() for f in fr.values()); t1 = max(f.index.max() for f in fr.values())
    idx = pd.date_range(t0, t1, freq="30min", tz="UTC")
    A = {}
    for k in ["open", "high", "low", "close"]:
        A[k] = np.column_stack([fr[c][k].reindex(idx).values for c in coins])
    C = pd.DataFrame(A["close"]).ffill().values
    miss = np.isnan(A["close"]) & ~np.isnan(C)
    for k in ["open", "high", "low"]:
        A[k] = np.where(miss, C, A[k])
    A["close"] = C
    return dict(ts=idx, O=A["open"], H=A["high"], L=A["low"], C=C, coins=list(coins))


def daily_index(ts, cut_hour, lag_min):
    """Per ogni giorno: indice della barra la cui chiusura è la chiusura giornaliera (ore cut_hour UTC) e barra di esecuzione."""
    t0 = ts[0].normalize() + pd.Timedelta(hours=cut_hour)
    cuts = pd.date_range(t0 + pd.Timedelta(days=1), ts[-1] + BAR, freq="1D")
    pos = ts.get_indexer(cuts - BAR)                       # barra che termina al cut
    ok = pos >= 0
    cuts, pos = cuts[ok], pos[ok]
    ex = pos + 1 + int(lag_min // 30)
    ok = ex < len(ts)
    return cuts[ok], pos[ok], ex[ok]


def daily_features(Cd, vol_win=90):
    """Massimi/minimi mobili delle chiusure per ogni lookback (inclusa la chiusura di oggi) e vol annua a vol_win giorni."""
    D, K = Cd.shape
    hi = np.full((D, K, len(LBS)), np.nan); lo = np.full((D, K, len(LBS)), np.nan)
    df = pd.DataFrame(Cd)
    for j, L in enumerate(LBS):
        hi[:, :, j] = df.rolling(L, min_periods=L).max().values
        lo[:, :, j] = df.rolling(L, min_periods=L).min().values
    lr = np.log(df).diff()
    vol = (lr.rolling(vol_win, min_periods=vol_win).std() * np.sqrt(365)).values
    return hi, lo, vol


# ----------------------------------------------------------------------------- simulazione
@nb.njit(cache=True)
def _sim(O, H, L, C, pos, ex, Cd, hi, lo, vol, eligible, target_vol, lev_cap, band, loss_cap, max_dist,
         comm, slip, stop_slip, margin_bar, vol_floor, miss_mask, use_stop, allow_short):
    N, K = C.shape; D = len(pos); NL = hi.shape[2]
    cash = 1.0
    q = np.zeros(K); inpos = np.zeros((K, NL), np.int8); stp = np.zeros((K, NL)); pst = np.zeros(K)
    E = np.full(N, np.nan); El = np.full(N, np.nan)
    W_d = np.zeros(D); trades = 0; fees = 0.0; fund = 0.0; stops = 0; turn = 0.0
    wt = np.zeros(K)
    for d in range(D):
        # --- segnali alla chiusura giornaliera
        ne = 0
        for i in range(K):
            if eligible[d, i]:
                ne += 1
        for i in range(K):
            c = Cd[d, i]
            s = 0
            if eligible[d, i] and not np.isnan(c):
                for l in range(NL):
                    if np.isnan(hi[d, i, l]):
                        continue
                    mid = 0.5 * (hi[d, i, l] + lo[d, i, l])
                    if inpos[i, l] == 1:
                        if mid > stp[i, l]:
                            stp[i, l] = mid
                        if c < stp[i, l]:
                            inpos[i, l] = 0
                    elif inpos[i, l] == -1:
                        if mid < stp[i, l]:
                            stp[i, l] = mid
                        if c > stp[i, l]:
                            inpos[i, l] = 0
                    if inpos[i, l] == 0:
                        if c >= hi[d, i, l]:
                            inpos[i, l] = 1; stp[i, l] = mid
                        elif allow_short and c <= lo[d, i, l]:
                            inpos[i, l] = -1; stp[i, l] = mid
                    s += inpos[i, l]
            v = vol[d, i]
            if s == 0 or np.isnan(v) or ne == 0:
                wt[i] = 0.0
            else:
                wt[i] = (s / NL) * target_vol / max(v, vol_floor) / ne
        tot = np.abs(wt).sum()
        if tot > lev_cap:
            wt *= lev_cap / tot
        W_d[d] = np.abs(wt).sum()
        # --- esecuzione (a mano: se il giorno è saltato, si tengono le posizioni e gli stop del giorno prima)
        b0 = ex[d]; b1 = ex[d + 1] if d + 1 < D else N
        if not miss_mask[d]:
            Edec = cash
            for i in range(K):
                if q[i] != 0.0:
                    Edec += q[i] * Cd[d, i]
            for i in range(K):
                if np.isnan(Cd[d, i]):
                    continue
                qt = wt[i] * Edec / Cd[d, i]
                big = max(abs(qt), abs(q[i]))
                go = (qt == 0.0 and q[i] != 0.0) or (qt != 0.0 and q[i] == 0.0) or (big > 0 and abs(qt - q[i]) > band * big)
                if go:
                    px = O[b0, i]
                    dq = qt - q[i]
                    fill = px * (1 + slip) if dq > 0 else px * (1 - slip)
                    cash -= dq * fill; f = abs(dq) * fill * comm; cash -= f; fees += f; turn += abs(dq) * fill
                    q[i] = qt; trades += 1
            Wn = 0.0
            for i in range(K):
                if q[i] != 0:
                    Wn += abs(q[i]) * O[b0, i]
            Wn /= max(Edec, 1e-12)
            dist = max_dist if Wn <= 0 else min(max_dist, loss_cap / Wn)
            for i in range(K):
                if use_stop and q[i] > 0:
                    pst[i] = O[b0, i] * (1 - dist)
                elif use_stop and q[i] < 0:
                    pst[i] = O[b0, i] * (1 + dist)
                else:
                    pst[i] = 0.0
        # --- barre fino alla prossima esecuzione
        for b in range(b0, b1):
            low_extra = 0.0
            for i in range(K):
                hit = False; fill = 0.0
                if q[i] > 0 and pst[i] > 0 and L[b, i] <= pst[i]:
                    fill = min(pst[i], O[b, i]) * (1 - stop_slip); hit = True
                elif q[i] < 0 and pst[i] > 0 and H[b, i] >= pst[i]:
                    fill = max(pst[i], O[b, i]) * (1 + stop_slip); hit = True
                if hit:
                    cash += q[i] * fill; f = abs(q[i]) * fill * comm; cash -= f; fees += f; turn += abs(q[i]) * fill
                    q[i] = 0.0; pst[i] = 0.0; stops += 1; trades += 1
                    for l in range(NL):
                        inpos[i, l] = 0
            notional = 0.0; eq = cash; eql = cash
            for i in range(K):
                if q[i] != 0.0:
                    notional += abs(q[i]) * C[b, i]
                    eq += q[i] * C[b, i]
                    eql += q[i] * (L[b, i] if q[i] > 0 else H[b, i])
            m = margin_bar * notional; cash -= m; fund += m; eq -= m; eql -= m
            E[b] = eq; El[b] = min(eql, eq)
    return E, El, W_d, trades, fees, fund, stops, turn


def run(X, cut_hour=17, lag_min=30, target_vol=0.10, lev_cap=1.0, band=0.20, loss_cap=0.02, max_dist=0.25,
        comm=0.0004, slip=0.0005, stop_slip=0.001, margin_day=0.00033, vol_win=90, vol_floor=0.20,
        miss=0.0, seed=0, use_stop=True, min_hist=365, lbs=None, allow_short=False):
    global LBS
    lbs_save = LBS
    if lbs is not None:
        LBS = np.array(lbs)
    try:
        cuts, pos, ex = daily_index(X["ts"], cut_hour, lag_min)
        Cd = X["C"][pos]
        hi, lo, vol = daily_features(Cd, vol_win)
        first = np.array([np.argmax(~np.isnan(Cd[:, i])) for i in range(Cd.shape[1])])
        eligible = (np.arange(len(pos))[:, None] - first[None, :]) >= min_hist   # moneta quotata da almeno min_hist giorni
        rng = np.random.default_rng(seed)
        mm = rng.random(len(pos)) < miss
        E, El, W, tr, fees, fund, stops, turn = _sim(X["O"], X["H"], X["L"], X["C"], pos, ex, Cd, hi, lo, vol, eligible,
                                                     target_vol, lev_cap, band, loss_cap, max_dist, comm, slip, stop_slip,
                                                     margin_day / 48, vol_floor, mm, use_stop, allow_short)
    finally:
        LBS = lbs_save
    ts = X["ts"]
    s = pd.DataFrame({"E": E, "El": El}, index=ts).dropna()
    return dict(bars=s, W=pd.Series(W, index=cuts), trades=tr, fees=fees, funding=fund, stops=stops, turnover=turn)


def mdl_days(bars, reset_min=30):
    """Giorni della prop con azzeramento del limite giornaliero alle reset_min minuti UTC (Kraken 00:30 = 30,
    HyroTrader 00:00 = 0, FTMO mezzanotte CEST = -120): per ogni giorno rendimento r, minimo intragiorno m e
    massimo calo dal picco intragiorno dd (tutti rispetto all'equity all'azzeramento)."""
    E, El = bars.E, bars.El
    key = (bars.index - pd.Timedelta(minutes=reset_min)).floor("1D")
    last = E.groupby(key).last(); mn = El.groupby(key).min()
    ref = last.shift(1)
    peak = E.groupby(key).cummax()
    peak = pd.concat([peak, ref.reindex(key).set_axis(peak.index)], axis=1).max(axis=1)
    ddin = (peak - El).groupby(key).max()
    d = pd.DataFrame({"ref": ref, "last": last, "min": mn, "dd": ddin}).dropna()
    return pd.DataFrame({"r": d["last"] / d["ref"] - 1, "m": d["min"] / d["ref"] - 1, "dd": d["dd"] / d["ref"]}, index=d.index)


def stats(dd, t0=None, t1=None):
    x = dd.loc[t0:t1]
    r = x.r
    eq = (1 + r).cumprod()
    low = eq.shift(1).fillna(1) * (1 + x.m)
    peak = eq.cummax()
    mdd = min((eq / peak - 1).min(), (low / peak.shift(1).fillna(1) - 1).min())
    yrs = len(r) / 365
    return dict(giorni=len(r), CAGR=eq.iloc[-1] ** (1 / yrs) - 1, vol=r.std() * np.sqrt(365),
                Sharpe=r.mean() / r.std() * np.sqrt(365), MaxDD=mdd, peggior_giorno=x.m.min(),
                giorni_sotto_3=(x.m <= -0.03).mean())


def per_year(dd):
    g = dd.r.groupby(dd.index.year)
    return pd.DataFrame({"rend": g.apply(lambda x: (1 + x).prod() - 1), "Sharpe": g.mean() / g.std() * np.sqrt(365),
                         "peggior_giorno": dd.m.groupby(dd.index.year).min()})
