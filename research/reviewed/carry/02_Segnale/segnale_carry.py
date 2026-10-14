# -*- coding: utf-8 -*-
"""Segnale giornaliero "Carry Neutrale" su Kraken (solo libreria standard: niente pandas/numpy).

Interruttore del capitale reale: si usa capitale vero solo se la media del funding degli ultimi 90 giorni
(BTC+ETH) supera il tasso BCE + 5 punti ("REGIME ATTIVO"); altrimenti il segnale si segue in carta.
Regola ufficiale (per BTC ed ETH, meta' capitale ciascuna):
  media del funding degli ultimi 14 giorni UTC completi, annualizzata (x365)
  - se FUORI e media > tasso BCE + 5 punti  -> ENTRA: compra spot e vendi il perpetuo PF_ per la stessa quantita'
  - se DENTRO e media < tasso BCE           -> ESCI: chiudi entrambe le gambe, capitale nel deposito
  - altrimenti                              -> MANTIENI lo stato attuale
Il segnale non invia ordini. Nessuna chiave: API pubblica Kraken Futures.

Uso:
  python -u segnale_carry.py [--capitale 10000] [--bce 0.025] [--stato stato.csv] [--conferma]
  --capitale  capitale in USD per calcolare le quantita' (oppure variabile d'ambiente CARRY_CAPITALE)
  --bce       tasso sui depositi BCE in vigore (oppure CARRY_BCE; default 2,50% dal 16/9/2026)
  --stato     file con righe "BTC,DENTRO|FUORI" (default: stato.csv accanto allo script; se manca = FUORI)
  --conferma  scrive nello stato il nuovo stato suggerito (usarlo DOPO aver eseguito gli ordini)
"""
import argparse, csv, datetime as dt, json, os, time, urllib.request

QUI = os.path.dirname(os.path.abspath(__file__))
PERP = {"BTC": "PF_XBTUSD", "ETH": "PF_ETHUSD"}
L, E_SOPRA, X_SOPRA = 14, 0.05, 0.00
REGIME_SOPRA = 0.05  # interruttore del capitale reale: media 90 giorni BTC+ETH sopra BCE + 5 punti
DAY_MS = 86_400_000


def get(url):
    for k in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "carry/1.0"}), timeout=30) as r:
                return json.load(r)
        except Exception:
            if k == 3:
                raise
            time.sleep(2 + 2 * k)


def funding_giornaliero(simbolo):
    """Somme giornaliere (UTC) del funding orario relativo; stessa convenzione del backtest (carry_lib.giorno)."""
    d = get(f"https://futures.kraken.com/derivatives/api/v4/historicalfundingrates?symbol={simbolo}")
    out = {}
    hourly = {}
    for x in d["rates"]:
        t = dt.datetime.strptime(x["timestamp"], "%Y-%m-%dT%H:%M:%S%z").timestamp() * 1000
        g = int((int(t) - 1) // DAY_MS)
        day = hourly.setdefault(g, {})
        slot = int(t)
        value = float(x["relativeFundingRate"])
        if slot in day and day[slot] != value:
            raise ValueError("Conflicting funding records")
        day[slot] = value
    oggi = int((int(time.time() * 1000)) // DAY_MS)
    return {g: sum(v.values()) for g, v in hourly.items() if g < oggi and len(v) == 24}  # solo giorni completi


def leggi_stato(p):
    st = {c: "FUORI" for c in PERP}
    if os.path.exists(p):
        with open(p, newline="") as f:
            for row in csv.reader(f):
                if len(row) >= 2 and row[0].strip().upper() in st:
                    st[row[0].strip().upper()] = row[1].strip().upper()
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capitale", type=float, default=float(os.environ.get("CARRY_CAPITALE", "0") or 0))
    ap.add_argument("--bce", type=float, default=float(os.environ.get("CARRY_BCE", "0.025")))
    ap.add_argument("--stato", default=os.path.join(QUI, "stato.csv"))
    ap.add_argument("--conferma", action="store_true")
    a = ap.parse_args()
    stato = leggi_stato(a.stato)
    tick = {x["symbol"]: x for x in get("https://futures.kraken.com/derivatives/api/v3/tickers")["tickers"]}
    oggi = dt.datetime.now(dt.timezone.utc).date()
    righe, nuovo, m90s = [], {}, []
    for c, s in PERP.items():
        f = funding_giornaliero(s)
        g = sorted(f)
        yesterday = (oggi - dt.date(1970, 1, 1)).days - 1
        if not set(range(yesterday - 89, yesterday + 1)).issubset(f):
            raise SystemExit("Funding incompleto o obsoleto: nessun segnale per " + c)
        m90s.append(sum(f[k] for k in g[-90:]) / min(90, len(g)) * 365)
        m14 = sum(f[k] for k in g[-L:]) / L * 365
        m30 = sum(f[k] for k in g[-30:]) / min(30, len(g)) * 365
        m365 = sum(f[k] for k in g[-365:]) / min(365, len(g)) * 365
        ent, usc = a.bce + E_SOPRA, a.bce + X_SOPRA
        if stato[c] == "FUORI" and m14 > ent:
            az = "ENTRA"; nuovo[c] = "DENTRO"
        elif stato[c] == "DENTRO" and m14 < usc:
            az = "ESCI"; nuovo[c] = "FUORI"
        else:
            az = "MANTIENI"; nuovo[c] = stato[c]
        prezzo = float(tick.get(s, {}).get("indexPrice") or tick.get(s, {}).get("markPrice") or 0)
        qta = (a.capitale / len(PERP)) / prezzo if (a.capitale and prezzo) else 0
        righe.append(dict(moneta=c, perp=s, ultimo_giorno=str(dt.date(1970, 1, 1) + dt.timedelta(days=g[-1])),
                          funding_14g=m14, funding_30g=m30, funding_365g=m365, soglia_entrata=ent, soglia_uscita=usc,
                          stato=stato[c], azione=az, nuovo_stato=nuovo[c], prezzo_indice=prezzo, quantita=qta))
    os.makedirs(os.path.join(QUI, "segnali"), exist_ok=True)
    m90 = sum(m90s) / len(m90s)
    regime = m90 > a.bce + REGIME_SOPRA
    txt = [f"SEGNALE CARRY NEUTRALE {oggi} (dati Kraken fino al {righe[0]['ultimo_giorno']} UTC)",
           f"Tasso BCE usato: {a.bce:.2%} | entrata sopra {a.bce + E_SOPRA:.2%} | uscita sotto {a.bce + X_SOPRA:.2%}",
           f"REGIME (funding medio 90g BTC+ETH {m90:+.2%} contro soglia {a.bce + REGIME_SOPRA:.2%}): "
           + ("ATTIVO -> solo ricerca in carta, nessuna autorizzazione al capitale reale" if regime else
              "NON ATTIVO -> segui il segnale solo in carta, capitale reale nel deposito"), ""]
    for r in righe:
        txt.append(f"{r['moneta']}: funding 14g {r['funding_14g']:+.2%}/anno (30g {r['funding_30g']:+.2%}, 365g {r['funding_365g']:+.2%})"
                   f" | stato {r['stato']} -> {r['azione']}")
        if r["azione"] == "ENTRA" and r["quantita"]:
            txt.append(f"   compra {r['quantita']:.6f} {r['moneta']} spot (ordine limite) e VENDI {r['quantita']:.6f} {r['perp']}"
                       f" (prezzo indice {r['prezzo_indice']:,.2f} USD)")
        elif r["azione"] == "ENTRA":
            txt.append("   (indica --capitale per avere le quantita')")
        elif r["azione"] == "ESCI":
            txt.append(f"   chiudi lo short {r['perp']}, vendi lo spot {r['moneta']}, capitale nel deposito")
        elif r["stato"] == "FUORI":
            txt.append(f"   mancano {r['soglia_entrata'] - r['funding_14g']:.2%} punti alla soglia di entrata")
    txt.append("")
    txt.append("Stato salvato." if a.conferma else "Solo simulazione: --conferma aggiorna lo stato di carta.")
    testo = "\n".join(txt)
    print(testo)
    with open(os.path.join(QUI, "segnali", f"segnale_{oggi}.txt"), "w", encoding="utf-8") as f:
        f.write(testo + "\n")
    p = os.path.join(QUI, "segnali", "storico_segnali.csv")
    campi = ["data", "regime_90g", "regime_attivo"] + list(righe[0].keys())
    vecchie = []
    if os.path.exists(p):  # una sola riga per moneta e giorno: se rilanci nello stesso giorno, sostituisce
        with open(p, newline="", encoding="utf-8") as f:
            vecchie = [r for r in csv.DictReader(f) if r.get("data") != str(oggi)]
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campi, extrasaction="ignore")
        w.writeheader()
        w.writerows(vecchie)
        for r in righe:
            w.writerow(dict(data=str(oggi), regime_90g=f"{m90:.6f}", regime_attivo=int(regime),
                            **{k: (f"{v:.6f}" if isinstance(v, float) else v) for k, v in r.items()}))
    if a.conferma:
        with open(a.stato, "w", newline="") as f:
            csv.writer(f).writerows([[c, s] for c, s in nuovo.items()])


if __name__ == "__main__":
    main()
