#!/usr/bin/env python3
"""
Noise Area bot — esecuzione automatica della strategia Zarattini-Aziz-Barbon su futures (MNQ/MES) via ProjectX
(TopstepX) oppure su SPY via Alpaca paper (fase 0, gratis). Logica del segnale IDENTICA al backtest:
usa frame_to_arrays / features / signal_at di noise_area_topstep.py (stessa cartella).

Regole (ora di New York, sessione 9:30-16:00):
  - controlli alle 10:00, 10:30 ... 15:30 sulla chiusura della barra a 1 minuto appena chiusa;
  - long se close > UB e > VWAP, short se close < LB e < VWAP, altrimenti flat (la posizione segue il segnale);
  - ordine a mercato subito dopo il controllo; stop catastrofico lato server a 1,5 x distanza dalla banda
    (minimo min_stop_pts punti); dopo uno stop si rientra solo al controllo successivo;
  - flat alle 15:55 (12:55 nelle mezze giornate), cancellazione di tutti gli stop.

Comandi:
  python noise_bot.py selftest                    # test offline: logica bot == backtest, broker finto ProjectX
  python noise_bot.py replay --data DIR --start 2024-01-01 --end 2024-03-31   # replay su SPY storico
  python noise_bot.py run --config config.json    # live (broker: projectx | alpaca | dry)

Segreti solo da variabili d'ambiente: PX_USER, PX_API_KEY (TopstepX) oppure ALPACA_KEY, ALPACA_SECRET.
Avviare da una cartella dedicata. Topstep: il bot deve girare sul tuo computer (VPS/server remoti vietati).
"""
import argparse, csv, json, math, os, sys, time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import noise_area_topstep as N  # noqa: E402

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
CHECK_EVERY = 30          # minuti tra i controlli
FLAT_BEFORE_CLOSE = 5     # minuti prima della chiusura


# ============================================================================= motore del segnale
class Engine:
    """Calcola segnale e distanza dello stop per la barra appena chiusa, con la stessa matematica del backtest."""

    def __init__(self, cat_stop=1.5, min_stop_pts=32.0):
        self.cat_stop, self.min_stop_pts = cat_stop, min_stop_pts

    def evaluate(self, bars: pd.DataFrame, now_et: datetime):
        """bars: timestamp (ET naive, inizio barra), open, high, low, close, volume — almeno 15 sedute + oggi.
        Ritorna dict con m (indice barra chiusa), check (bool), signal, close, ub, lb, vwap, stop_dist, flat (bool)."""
        today = pd.Timestamp(now_et.date())
        closed = bars[bars.timestamp + pd.Timedelta(minutes=1) <= pd.Timestamp(now_et.replace(tzinfo=None))]
        X = N.frame_to_arrays(closed)
        if len(X["days"]) == 0 or X["days"][-1] != today:
            return None
        F = N.features(X)
        d = len(X["days"]) - 1
        L = int(X["L"][d])
        m = (now_et.hour * 60 + now_et.minute) - 570 - 1          # ultima barra chiusa
        out = dict(date=str(today.date()), m=m, L=L, valid=bool(F["valid"][d]), check=False, signal=0,
                   flat=m + 1 >= L - FLAT_BEFORE_CLOSE)
        if m < 0 or m >= L:
            return out
        C = X["C"]
        out.update(close=float(C[d, m]), ub=float(F["ub"][d, m]), lb=float(F["lb"][d, m]), vwap=float(F["vwap"][d, m]))
        is_check = (m + 1) % CHECK_EVERY == 0 and m + 1 < L - FLAT_BEFORE_CLOSE
        if not (is_check and F["valid"][d]):
            return out
        s = N.signal_at(F, C, d, m)
        lvl = max(out["ub"], out["vwap"]) if s > 0 else min(out["lb"], out["vwap"])
        dist = max(self.cat_stop * abs(out["close"] - lvl), self.min_stop_pts) if s else 0.0
        out.update(check=True, signal=int(s), stop_dist=float(dist))
        return out


def round_tick(x, tick, direction):
    q = x / tick
    return (math.floor(q + 1e-9) if direction < 0 else math.ceil(q - 1e-9)) * tick


# ============================================================================= broker ProjectX (TopstepX)
class ProjectX:
    """Client REST minimale per ProjectX Gateway (TopstepX). Endpoint: /Auth/loginKey, /Account/search,
    /Contract/search, /History/retrieveBars, /Order/place|cancel|searchOpen, /Position/searchOpen|closeContract."""

    def __init__(self, base="https://api.topstepx.com/api", user=None, api_key=None, session=None, live_contracts=False):
        import requests
        self.base, self.user, self.key = base.rstrip("/"), user, api_key
        self.s = session or requests.Session()
        self.token, self.token_time, self.live = None, 0.0, live_contracts

    def _post(self, path, payload, auth=True, retries=3):
        for k in range(retries):
            try:
                if auth:
                    self._ensure_token()
                h = {"Content-Type": "application/json", "accept": "text/plain"}
                if auth:
                    h["Authorization"] = f"Bearer {self.token}"
                r = self.s.post(self.base + path, json=payload, headers=h, timeout=15)
                if r.status_code == 401 and auth:
                    self.token = None
                    continue
                r.raise_for_status()
                j = r.json()
                if isinstance(j, dict) and j.get("success") is False:
                    raise RuntimeError(f"{path}: {j.get('errorMessage') or j}")
                return j
            except Exception:
                if k == retries - 1:
                    raise
                time.sleep(1.5 * (k + 1))

    def _ensure_token(self):
        if self.token and time.time() - self.token_time < 20 * 3600:
            return
        j = self._post("/Auth/loginKey", {"userName": self.user, "apiKey": self.key}, auth=False)
        self.token, self.token_time = j["token"], time.time()

    def accounts(self):
        return self._post("/Account/search", {"onlyActiveAccounts": True}).get("accounts", [])

    def contract(self, symbol):
        cs = self._post("/Contract/search", {"searchText": symbol, "live": self.live}).get("contracts", [])
        cs = [c for c in cs if c.get("activeContract")] or cs
        if not cs:
            raise RuntimeError(f"contratto {symbol} non trovato")
        return cs[0]

    def bars(self, contract_id, start_utc, end_utc):
        out, end = [], end_utc
        for _ in range(10):
            j = self._post("/History/retrieveBars", {"contractId": contract_id, "live": self.live,
                                                     "startTime": start_utc.isoformat(), "endTime": end.isoformat(),
                                                     "unit": 2, "unitNumber": 1, "limit": 20000, "includePartialBar": False})
            b = j.get("bars", [])
            if not b:
                break
            out += b
            first = min(pd.to_datetime(x["t"], utc=True) for x in b)
            if len(b) < 20000 or first <= pd.Timestamp(start_utc):
                break
            end = first.to_pydatetime()
        if not out:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame(out).drop_duplicates("t")
        ts = pd.to_datetime(df["t"], utc=True).dt.tz_convert(ET).dt.tz_localize(None)
        return pd.DataFrame(dict(timestamp=ts, open=df.o.astype(float), high=df.h.astype(float), low=df.l.astype(float),
                                 close=df.c.astype(float), volume=df.v.astype(float))).sort_values("timestamp")

    def position(self, account_id, contract_id):
        ps = self._post("/Position/searchOpen", {"accountId": account_id}).get("positions", [])
        for p in ps:
            if p.get("contractId") == contract_id and p.get("size"):
                return int(p["size"]) * (1 if p.get("type") == 1 else -1), float(p.get("averagePrice") or 0)
        return 0, 0.0

    def open_orders(self, account_id):
        return self._post("/Order/searchOpen", {"accountId": account_id}).get("orders", [])

    def market(self, account_id, contract_id, qty):
        side = 0 if qty > 0 else 1
        return self._post("/Order/place", {"accountId": account_id, "contractId": contract_id, "type": 2,
                                           "side": side, "size": abs(int(qty))}).get("orderId")

    def stop(self, account_id, contract_id, qty, stop_price):
        side = 0 if qty > 0 else 1
        return self._post("/Order/place", {"accountId": account_id, "contractId": contract_id, "type": 4,
                                           "side": side, "size": abs(int(qty)), "stopPrice": float(stop_price)}).get("orderId")

    def cancel(self, account_id, order_id):
        return self._post("/Order/cancel", {"accountId": account_id, "orderId": order_id})

    def close(self, account_id, contract_id):
        return self._post("/Position/closeContract", {"accountId": account_id, "contractId": contract_id})

    def balance(self, account_id):
        for a in self.accounts():
            if a.get("id") == account_id:
                return float(a.get("balance", 0.0))
        return float("nan")


# ============================================================================= broker Alpaca paper (fase 0, SPY)
class AlpacaPaper:
    """Conto paper Alpaca (gratuito). Dati IEX in tempo reale. Serve solo a verificare il codice."""

    def __init__(self, key, secret, session=None):
        import requests
        self.s = session or requests.Session()
        self.h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self.trade, self.data = "https://paper-api.alpaca.markets", "https://data.alpaca.markets"

    def accounts(self):
        a = self.s.get(self.trade + "/v2/account", headers=self.h, timeout=15).json()
        return [{"id": "alpaca", "name": "alpaca-paper", "balance": float(a.get("equity", 0))}]

    def contract(self, symbol):
        return {"id": symbol, "name": symbol, "tickSize": 0.01, "tickValue": 0.01}

    def bars(self, symbol, start_utc, end_utc):
        out, tok = [], None
        while True:
            p = {"symbols": symbol, "timeframe": "1Min", "start": start_utc.isoformat(), "end": end_utc.isoformat(),
                 "feed": "iex", "limit": 10000}
            if tok:
                p["page_token"] = tok
            j = self.s.get(self.data + "/v2/stocks/bars", headers=self.h, params=p, timeout=20).json()
            out += j.get("bars", {}).get(symbol, [])
            tok = j.get("next_page_token")
            if not tok:
                break
        if not out:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame(out)
        ts = pd.to_datetime(df["t"], utc=True).dt.tz_convert(ET).dt.tz_localize(None)
        return pd.DataFrame(dict(timestamp=ts, open=df.o, high=df.h, low=df.l, close=df.c, volume=df.v))

    def position(self, account_id, symbol):
        r = self.s.get(self.trade + f"/v2/positions/{symbol}", headers=self.h, timeout=15)
        if r.status_code == 404:
            return 0, 0.0
        j = r.json()
        return int(float(j["qty"])), float(j["avg_entry_price"])

    def open_orders(self, account_id):
        return [dict(id=o["id"], type=4 if o["type"] == "stop" else 2) for o in
                self.s.get(self.trade + "/v2/orders", headers=self.h, params={"status": "open"}, timeout=15).json()]

    def market(self, account_id, symbol, qty):
        return self.s.post(self.trade + "/v2/orders", headers=self.h, timeout=15, json={
            "symbol": symbol, "qty": abs(int(qty)), "side": "buy" if qty > 0 else "sell", "type": "market",
            "time_in_force": "day"}).json().get("id")

    def stop(self, account_id, symbol, qty, stop_price):
        return self.s.post(self.trade + "/v2/orders", headers=self.h, timeout=15, json={
            "symbol": symbol, "qty": abs(int(qty)), "side": "buy" if qty > 0 else "sell", "type": "stop",
            "stop_price": round(float(stop_price), 2), "time_in_force": "day"}).json().get("id")

    def cancel(self, account_id, order_id):
        self.s.delete(self.trade + f"/v2/orders/{order_id}", headers=self.h, timeout=15)

    def close(self, account_id, symbol):
        self.s.delete(self.trade + f"/v2/positions/{symbol}", headers=self.h, timeout=15)

    def balance(self, account_id):
        return self.accounts()[0]["balance"]


# ============================================================================= esecuzione
class Trader:
    """Allinea ogni conto al segnale: stesso segnale, taglia diversa per conto (sostituisce il copier)."""

    def __init__(self, broker, accounts, contract, log_dir, dry=False):
        self.b, self.accs, self.c, self.dry = broker, accounts, contract, dry
        self.tick = float(contract.get("tickSize", 0.25))
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)

    def _log(self, name, row):
        f = os.path.join(self.log_dir, name)
        new = not os.path.exists(f)
        with open(f, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(row.keys()))
            if new:
                w.writeheader()
            w.writerow(row)

    def cancel_stops(self, acc):
        for o in self.b.open_orders(acc["id"]):
            if o.get("contractId", self.c["id"]) == self.c["id"] and o.get("type") == 4:
                self.b.cancel(acc["id"], o["id"])

    def apply(self, target, stop_dist, ref_price, now):
        for acc in self.accs:
            size = int(acc["size"])
            pos, avg = self.b.position(acc["id"], self.c["id"])
            want = target * size
            row = dict(time=now.isoformat(), account=acc["name"], target=target, pos_before=pos, action="hold")
            if (pos > 0) - (pos < 0) == target and target != 0:
                self._log("orders.csv", row)
                continue
            if self.dry:
                row["action"] = f"DRY -> {want}"
                self._log("orders.csv", row)
                continue
            self.cancel_stops(acc)
            if pos != 0:
                self.b.close(acc["id"], self.c["id"])
                row["action"] = "close"
            if want != 0:
                self.b.market(acc["id"], self.c["id"], want)
                p2, fill = self.b.position(acc["id"], self.c["id"])
                entry = fill or ref_price
                stop_px = round_tick(entry - target * stop_dist, self.tick, -target)
                self.b.stop(acc["id"], self.c["id"], -want, stop_px)
                row.update(action=row["action"] + f"+entry {want}", entry=entry, stop=stop_px)
            self._log("orders.csv", row)

    def flatten(self, now):
        for acc in self.accs:
            if self.dry:
                continue
            self.cancel_stops(acc)
            pos, _ = self.b.position(acc["id"], self.c["id"])
            if pos != 0:
                self.b.close(acc["id"], self.c["id"])
            self._log("orders.csv", dict(time=now.isoformat(), account=acc["name"], target=0, pos_before=pos, action="flat EOD"))


def fetch_history(broker, contract_id, now_et, sessions_days=32):
    end = now_et.astimezone(UTC)
    start = (now_et - timedelta(days=sessions_days)).astimezone(UTC)
    return broker.bars(contract_id, start, end)


def run_live(cfg, broker=None, clock=None, sleeper=None, stop_after=None):
    """Ciclo principale. clock/sleeper/broker iniettabili per i test; stop_after = datetime ET di fine test."""
    clock = clock or (lambda: datetime.now(ET))
    sleeper = sleeper or time.sleep
    kind = cfg.get("broker", "dry")
    if broker is not None:
        pass
    elif kind in ("projectx", "dry"):
        broker = ProjectX(cfg.get("api_url", "https://api.topstepx.com/api"), os.environ.get("PX_USER"),
                          os.environ.get("PX_API_KEY"), live_contracts=cfg.get("live_contracts", False))
    elif kind == "alpaca":
        broker = AlpacaPaper(os.environ["ALPACA_KEY"], os.environ["ALPACA_SECRET"])
    else:
        raise SystemExit("broker sconosciuto")
    contract = broker.contract(cfg["symbol"])
    accs = []
    avail = broker.accounts()
    for a in cfg["accounts"]:
        match = [x for x in avail if str(x.get("id")) == str(a.get("id")) or x.get("name") == a.get("name")]
        if not match:
            raise SystemExit(f"conto {a} non trovato tra {[x.get('name') for x in avail]}")
        accs.append(dict(id=match[0]["id"], name=match[0]["name"], size=int(a["size"])))
    eng = Engine(cfg.get("cat_stop", 1.5), cfg.get("min_stop_pts", 32.0))
    tr = Trader(broker, accs, contract, cfg.get("log_dir", "logs"), dry=(kind == "dry"))
    print(f"[bot] contratto {contract.get('name')} | conti {[(a['name'], a['size']) for a in accs]} | modalità {kind}")
    done_checks, flat_done, cache = set(), None, {}
    while True:
        now = clock()
        if stop_after and now >= stop_after:
            return tr
        wake = (now + timedelta(minutes=1)).replace(second=5, microsecond=0)
        mins = now.hour * 60 + now.minute
        if now.weekday() >= 5 or mins < 570 or mins > 961:
            sleeper(max(5, (wake - now).total_seconds()))
            continue
        try:
            # storico di 32 giorni scaricato una volta al giorno, poi solo le barre di oggi (meno chiamate all'API)
            if cache.get("date") != now.date():
                cache.update(date=now.date(), hist=fetch_history(broker, contract["id"], now.replace(hour=9, minute=0)))
            start_today = now.replace(hour=9, minute=25, second=0, microsecond=0).astimezone(UTC)
            today = broker.bars(contract["id"], start_today, now.astimezone(UTC))
            bars = pd.concat([cache["hist"], today]).drop_duplicates("timestamp", keep="last").sort_values("timestamp")
            ev = eng.evaluate(bars, now)
        except Exception as e:
            print(f"[bot] {now:%H:%M:%S} errore dati: {e}")
            sleeper(10)
            continue
        if ev is None:
            sleeper(max(5, (wake - now).total_seconds()))
            continue
        tr._log("decisions.csv", dict(time=now.isoformat(), **{k: ev.get(k) for k in
                                      ("m", "valid", "check", "signal", "close", "ub", "lb", "vwap", "stop_dist", "flat")}))
        key = (ev["date"], ev["m"])
        if ev["flat"] and flat_done != ev["date"]:
            tr.flatten(now)
            flat_done = ev["date"]
            print(f"[bot] {now:%H:%M} flat di fine giornata")
        elif ev["check"] and key not in done_checks:
            done_checks.add(key)
            print(f"[bot] {now:%H:%M} controllo: segnale {ev['signal']:+d} close {ev['close']:.2f} "
                  f"UB {ev['ub']:.2f} LB {ev['lb']:.2f} VWAP {ev['vwap']:.2f}")
            tr.apply(ev["signal"], ev["stop_dist"], ev["close"], now)
        sleeper(max(5, (wake - clock()).total_seconds()))


# ============================================================================= replay e test offline
class SimBroker:
    """Broker simulato su barre storiche: ordini a mercato all'apertura della barra successiva, stop sulla barra."""

    def __init__(self, day_bars: pd.DataFrame):
        self.bars = day_bars.set_index("timestamp")
        self.pos, self.avg, self.stop_px, self.now = 0, 0.0, None, None
        self.fills = []

    def set_time(self, now):
        # processa gli stop sulle barre tra l'ultimo istante e ora
        prev = self.now
        self.now = now
        if prev is None or self.pos == 0 or self.stop_px is None:
            return
        a = pd.Timestamp(prev.replace(tzinfo=None)).floor("min")
        b = pd.Timestamp(now.replace(tzinfo=None)).floor("min")
        seg = self.bars[(self.bars.index >= a) & (self.bars.index < b)]      # barre chiuse dall'ultimo passaggio
        for ts, b in seg.iterrows():
            if self.pos > 0 and b.low <= self.stop_px:
                self._fill(min(self.stop_px, b.open), -self.pos, ts, "stop"); break
            if self.pos < 0 and b.high >= self.stop_px:
                self._fill(max(self.stop_px, b.open), -self.pos, ts, "stop"); break

    def _fill(self, px, qty, ts, why):
        self.fills.append(dict(time=ts, qty=qty, px=px, why=why))
        self.pos += qty
        self.avg = px if self.pos else 0.0
        if self.pos == 0:
            self.stop_px = None

    def _next_open(self):
        t = pd.Timestamp(self.now.replace(tzinfo=None)).floor("min")
        b = self.bars[self.bars.index >= t]
        return float(b.iloc[0].open), b.index[0]

    def position(self, a, c): return self.pos, self.avg
    def open_orders(self, a): return [dict(id=1, type=4, contractId=c) for c in ["X"]] if self.stop_px else []
    def cancel(self, a, oid): self.stop_px = None
    def close(self, a, c):
        px, ts = self._next_open(); self._fill(px, -self.pos, ts, "close")
    def market(self, a, c, qty):
        px, ts = self._next_open(); self._fill(px, qty, ts, "entry")
    def stop(self, a, c, qty, px): self.stop_px = px
    def bars_hist(self): return self.bars


def replay(data_dir, start, end, loader="spy", level=None, min_stop_pts=None, verbose=False):
    """Esegue il bot minuto per minuto su giorni storici e confronta i trade con il backtest (mes_backtest)."""
    X = N.load(data_dir) if loader == "spy" else N.load_oanda(data_dir)
    F = N.features(X)
    days = X["days"]
    raw = []
    # ricostruisco le barre grezze (già pulite dal loader) per dare al bot lo stesso input del backtest
    for d_i, d in enumerate(days):
        ts = d + pd.to_timedelta(570 + np.arange(X["L"][d_i]), unit="m")
        raw.append(pd.DataFrame(dict(timestamp=ts, open=X["O"][d_i, :X["L"][d_i]], high=X["H"][d_i, :X["L"][d_i]],
                                     low=X["Lo"][d_i, :X["L"][d_i]], close=X["C"][d_i, :X["L"][d_i]],
                                     volume=X["V"][d_i, :X["L"][d_i]])))
    allbars = pd.concat(raw, ignore_index=True)
    # scala: il bot lavora in unità del sottostante; per confronto uso min_stop in unità SPY
    lvl = level or N.ES_LEVEL
    sel = [i for i, d in enumerate(days) if pd.Timestamp(start) <= d <= pd.Timestamp(end) and F["valid"][i]]
    mism, n_bot, n_bt = [], 0, 0
    T_bt, _, _ = N.mes_backtest(X, F, slip_ticks=0, comm_rt=0, cat_stop=1.5, level=lvl,
                                min_stop_pts=min_stop_pts if min_stop_pts else 8.0)
    for i in sel:
        d = days[i]
        k = lvl / F["day_open"][i]
        eng = Engine(1.5, (min_stop_pts if min_stop_pts else 8.0) / k)
        hist = allbars[(allbars.timestamp >= d - pd.Timedelta(days=32)) & (allbars.timestamp < d + pd.Timedelta(days=1))]
        sim = SimBroker(allbars[(allbars.timestamp >= d) & (allbars.timestamp < d + pd.Timedelta(days=1))])
        tr = Trader(sim, [dict(id=1, name="sim", size=1)], dict(id="X", tickSize=1e-9), "/tmp/claude-0/replay_logs", dry=False)
        L = int(X["L"][i])
        for m in range(29, L, 1):
            now = datetime(d.year, d.month, d.day, tzinfo=ET) + timedelta(minutes=570 + m + 1, seconds=5)
            sim.set_time(now)
            if (m + 1) % CHECK_EVERY != 0 and m + 1 != L - FLAT_BEFORE_CLOSE:
                continue
            ev = Engine.evaluate(eng, hist, now)
            if ev is None:
                break
            if ev["flat"]:
                if sim.pos:
                    tr.flatten(now)
                break
            if ev["check"]:
                tr.apply(ev["signal"], ev["stop_dist"], ev["close"], now)
        # trade del bot: coppie entrata/uscita in unità di prezzo -> $ per contratto come nel backtest
        f = sim.fills
        bot_trades = []
        for a, b in zip(f[0::2], f[1::2]):
            bot_trades.append(round(np.sign(a["qty"]) * (b["px"] - a["px"]) * k * N.MES_MULT, 6))
        bt = [round(x, 6) for x in T_bt[T_bt.d == i].pnl.tolist()]
        n_bot += len(bot_trades); n_bt += len(bt)
        if len(bot_trades) != len(bt) or not np.allclose(bot_trades, bt, atol=1e-4):
            mism.append((str(d.date()), bot_trades, bt))
            if verbose:
                print("DIFF", d.date(), bot_trades, bt)
    return dict(giorni=len(sel), trade_bot=n_bot, trade_backtest=n_bt, giorni_diversi=len(mism), esempi=mism[:5])


class FakeProjectXSession:
    """Finto server ProjectX in memoria per testare il client REST senza credenziali."""

    def __init__(self, bars):
        self.bars, self.orders, self.pos, self.calls = bars, {}, {}, []

    class R:
        def __init__(self, j, code=200): self._j, self.status_code = j, code
        def json(self): return self._j
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.status_code)

    def post(self, url, json=None, headers=None, timeout=None):
        p = url[url.rindex("/api/") + 4:]
        self.calls.append((p, json))
        if p == "/Auth/loginKey":
            assert json["userName"] and json["apiKey"]
            return self.R({"success": True, "token": "tok"})
        assert headers.get("Authorization") == "Bearer tok"
        if p == "/Account/search":
            return self.R({"success": True, "accounts": [{"id": 7, "name": "50KTC-TEST", "balance": 50000, "canTrade": True}]})
        if p == "/Contract/search":
            return self.R({"success": True, "contracts": [{"id": "CON.F.US.MNQ.Z26", "name": "MNQZ6", "tickSize": 0.25,
                                                           "tickValue": 0.5, "activeContract": True}]})
        if p == "/History/retrieveBars":
            s, e = pd.Timestamp(json["startTime"]), pd.Timestamp(json["endTime"])
            b = self.bars[(self.bars.t >= s) & (self.bars.t <= e)]
            return self.R({"success": True, "bars": [dict(t=x.t.isoformat(), o=x.o, h=x.h, l=x.l, c=x.c, v=x.v) for x in b.itertuples()]})
        if p == "/Position/searchOpen":
            q = self.pos.get(json["accountId"], 0)
            return self.R({"success": True, "positions": [] if q == 0 else
                           [{"contractId": "CON.F.US.MNQ.Z26", "type": 1 if q > 0 else 2, "size": abs(q), "averagePrice": 100.0}]})
        if p == "/Order/searchOpen":
            return self.R({"success": True, "orders": [dict(id=k, type=v["type"], contractId=v["contractId"]) for k, v in self.orders.items()]})
        if p == "/Order/place":
            oid = len(self.calls)
            if json["type"] == 2:
                self.pos[json["accountId"]] = self.pos.get(json["accountId"], 0) + (json["size"] if json["side"] == 0 else -json["size"])
            else:
                assert json["type"] == 4 and "stopPrice" in json
                self.orders[oid] = json
            return self.R({"success": True, "orderId": oid})
        if p == "/Order/cancel":
            self.orders.pop(json["orderId"], None)
            return self.R({"success": True})
        if p == "/Position/closeContract":
            self.pos[json["accountId"]] = 0
            return self.R({"success": True})
        return self.R({"success": False, "errorMessage": "endpoint sconosciuto"}, 404)


def selftest(data_dir):
    print("1) Replay del bot su SPY storico contro il backtest (gen-mar 2024)")
    r = replay(data_dir, "2024-01-02", "2024-03-28")
    print("  ", {k: v for k, v in r.items() if k != "esempi"})
    if r["esempi"]:
        print("   esempi di differenze:", r["esempi"][:3])
    print("2) Client ProjectX contro server finto: login, conti, contratto, barre, ordini, stop, flat")
    X = N.load(data_dir)
    F = N.features(X)
    T_bt, _, _ = N.mes_backtest(X, F, slip_ticks=0, comm_rt=0, cat_stop=1.5)
    busy = T_bt.groupby("d").size()
    last = int(busy[busy >= 2].index.max())                 # ultimo giorno con almeno 2 trade (entrata, flip o uscita)
    d = X["days"][last]
    rows = []
    for i in range(last - 20, last + 1):
        dd = X["days"][i]
        for m in range(X["L"][i]):
            t = pd.Timestamp(dd + pd.Timedelta(minutes=570 + m)).tz_localize(ET).tz_convert("UTC")
            rows.append(dict(t=t, o=X["O"][i, m], h=X["H"][i, m], l=X["Lo"][i, m], c=X["C"][i, m], v=X["V"][i, m]))
    fake = FakeProjectXSession(pd.DataFrame(rows))
    px = ProjectX(user="u", api_key="k", session=fake)
    c = px.contract("MNQ")
    acc = [dict(id=7, name="50KTC-TEST", size=3)]
    tr = Trader(px, acc, c, "/tmp/claude-0/selftest_logs")
    eng = Engine(1.5, 0.05)
    signals = []
    for mm in range(29, 390, 30):
        now = datetime(d.year, d.month, d.day, tzinfo=ET) + timedelta(minutes=570 + mm + 1, seconds=5)
        ev = eng.evaluate(fetch_history(px, c["id"], now), now)
        if ev and ev["check"]:
            tr.apply(ev["signal"], ev["stop_dist"], ev["close"], now)
            signals.append(ev["signal"])
    tr.flatten(datetime(d.year, d.month, d.day, 15, 55, 5, tzinfo=ET))
    kinds = pd.Series([p for p, _ in fake.calls]).value_counts().to_dict()
    print(f"   giorno {d.date()}: segnali {signals}; chiamate {kinds}; posizione finale {fake.pos.get(7, 0)}; stop aperti {len(fake.orders)}")
    assert fake.pos.get(7, 0) == 0 and not fake.orders, "a fine giornata devono restare zero posizioni e zero stop"
    print("   OK")
    print("3) Ciclo live completo con orologio virtuale e server finto (stesso giorno, dalle 9:00 alle 16:05)")
    fake2 = FakeProjectXSession(pd.DataFrame(rows))
    px2 = ProjectX(user="u", api_key="k", session=fake2)
    vt = {"t": datetime(d.year, d.month, d.day, 9, 0, 5, tzinfo=ET)}
    def clk(): return vt["t"]
    def slp(sec): vt["t"] = vt["t"] + timedelta(seconds=sec)
    cfg = {"broker": "projectx", "symbol": "MNQ", "cat_stop": 1.5, "min_stop_pts": 0.05,
           "accounts": [{"name": "50KTC-TEST", "size": 3}], "log_dir": "/tmp/claude-0/selftest_live"}
    run_live(cfg, broker=px2, clock=clk, sleeper=slp, stop_after=datetime(d.year, d.month, d.day, 16, 5, tzinfo=ET))
    kinds2 = pd.Series([p for p, _ in fake2.calls]).value_counts().to_dict()
    same = [c for c in fake2.calls if c[0] == "/Order/place"] == [c for c in fake.calls if c[0] == "/Order/place"]
    print(f"   chiamate {kinds2}; posizione finale {fake2.pos.get(7, 0)}; stop aperti {len(fake2.orders)}; ordini identici al test 2: {same}")
    assert fake2.pos.get(7, 0) == 0 and not fake2.orders and same
    print("   OK")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("run"); a.add_argument("--config", required=True)
    b = sub.add_parser("replay"); b.add_argument("--data", required=True); b.add_argument("--start"); b.add_argument("--end")
    c = sub.add_parser("selftest"); c.add_argument("--data", default="spy/spy_contents/spy")
    args = ap.parse_args()
    if args.cmd == "run":
        raise SystemExit("AVVIO ORDINI BLOCCATO: audit 2026-10-14, retry non idempotente e stop non garantito. Usare solo replay/selftest.")
        run_live(json.load(open(args.config)))
    elif args.cmd == "replay":
        print(replay(args.data, args.start, args.end, verbose=True))
    else:
        selftest(args.data)


if __name__ == "__main__":
    main()
