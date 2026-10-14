
"""Monitor pubblico crypto/DEX. Ricerca e osservazione; nessun ordine e nessuna chiave wallet."""
from pathlib import Path
import argparse,datetime as dt,json,sqlite3,time,uuid
import requests
ROOT=Path(__file__).resolve().parent
STABLE={"tether","usd-coin","dai","ethena-usde","usds","first-digital-usd","paypal-usd","true-usd","frax","usual-usd","usd1-wlfi","usdd","pax-dollar"}
def get(url):
    last=None
    for attempt in range(2):
        try:
            r=requests.get(url,headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"},timeout=12)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException,ValueError) as e:
            last=e
            if attempt==0:time.sleep(1)
    raise RuntimeError(str(last))
def number(x):
    try:
        n=float(x)
        return n if n==n and abs(n)!=float("inf") else None
    except (TypeError,ValueError):return None
def inspect_pair(p,now):
    liq=number((p.get("liquidity") or {}).get("usd"))
    vol=number((p.get("volume") or {}).get("h24"))
    price=number(p.get("priceUsd"))
    created=number(p.get("pairCreatedAt"))
    age=(now.timestamp()*1000-created)/86400000 if created is not None else None
    sells=number(((p.get("txns") or {}).get("h24") or {}).get("sells"))
    reasons=[]
    if price is None or price<=0:reasons.append("prezzo mancante/non valido")
    if liq is None or liq<250000:reasons.append("liquidita sotto 250k USD o sconosciuta")
    if vol is None or vol<100000:reasons.append("volume 24h sotto 100k USD o sconosciuto")
    if age is None or age<7:reasons.append("pool con meno di 7 giorni o eta sconosciuta")
    if sells is None or sells<10:reasons.append("meno di 10 vendite 24h o dato sconosciuto")
    return dict(chain=p.get("chainId"),contract=(p.get("baseToken") or {}).get("address"),
        symbol=(p.get("baseToken") or {}).get("symbol"),pair=p.get("pairAddress"),
        price_usd=price,liquidity_usd=liq,volume_24h_usd=vol,age_days=age,
        change_24h=(p.get("priceChange") or {}).get("h24"),url=p.get("url"),
        status="OSSERVAZIONE" if not reasons else "ESCLUSO_DAI_FILTRI",
        reasons=reasons or ["supera filtri descrittivi, da validare; non dimostra un vantaggio"],
        security="NON_VERIFICATA",entry="NESSUN_INGRESSO_VALIDATO",raw=p)
def collect():
    now=dt.datetime.now(dt.timezone.utc);errors=[];sources=[];coins=[];pairs=[]
    url="https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=100&page=1&sparkline=false&price_change_percentage=7d,30d"
    try:
        data=get(url)
        if not isinstance(data,list):raise ValueError("schema CoinGecko inatteso")
        coins=[c for c in data if c.get("id") not in STABLE]
        fresh=[]
        for c in coins:
            try:
                updated=dt.datetime.fromisoformat(c["last_updated"].replace("Z","+00:00"))
                age=(now-updated).total_seconds()
                if age < -60 or age > 1200:raise ValueError("timestamp stale")
                fresh.append(c)
            except (KeyError,TypeError,ValueError):
                errors.append("CoinGecko stale/unknown timestamp: " + str(c.get("id")))
        coins=fresh
        sources.append(dict(source="CoinGecko top100",url=url,rows=len(data)))
    except Exception as e:errors.append("CoinGecko: "+str(e))
    url="https://api.dexscreener.com/token-profiles/latest/v1"
    profiles=[]
    try:
        profiles=get(url)
        if not isinstance(profiles,list):raise ValueError("schema profili DEX inatteso")
        sources.append(dict(source="DEX recent profiles",url=url,rows=len(profiles)))
    except Exception as e:errors.append("DEX discovery: "+str(e))
    groups={}
    for p in profiles:
        chain=p.get("chainId");addr=p.get("tokenAddress")
        if chain in {"solana","ethereum","base","bsc"} and isinstance(addr,str) and addr.isalnum():
            groups.setdefault(chain,set()).add(addr)
    for chain,addresses in groups.items():
        addresses=sorted(addresses)
        for i in range(0,len(addresses),30):
            batch=addresses[i:i+30]
            url="https://api.dexscreener.com/tokens/v1/"+chain+"/"+",".join(batch)
            try:
                data=get(url)
                if not isinstance(data,list):raise ValueError("schema pool DEX inatteso")
                sources.append(dict(source="DEX pools "+chain,url=url,rows=len(data)))
                for p in data:
                    if p.get("chainId")==chain and (p.get("baseToken") or {}).get("address") in batch:
                        pairs.append(inspect_pair(p,now))
            except Exception as e:errors.append("DEX "+chain+": "+str(e))
            time.sleep(.25)
    unique={(p["chain"],p["pair"]):p for p in pairs if p["pair"] and p["contract"]}
    pairs=list(unique.values())
    return dict(run_id=uuid.uuid4().hex,collected_at=now.isoformat(),coverage="top100 CoinGecko e profili DEX recenti, NON tutte le monete",
        status="PARZIALE" if errors else "SNAPSHOT_COMPLETATO",errors=errors,sources=sources,coins=coins,pairs=pairs,
        discovery_profiles=profiles,entry="NESSUN_INGRESSO_VALIDATO")
def save(data,destination):
    destination.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(destination/"market.sqlite",timeout=20)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY,collected_at TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL)")
        db.execute("INSERT INTO runs VALUES (?,?,?,?)",(data["run_id"],data["collected_at"],data["status"],json.dumps(data,ensure_ascii=False)))
        db.commit()
        count=db.execute("SELECT count(*) FROM runs WHERE collected_at >= ?",((dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=24)).isoformat(),)).fetchone()[0]
    finally:db.close()
    (destination/"latest.json").write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
    good=sorted((p for p in data["pairs"] if p["status"]=="OSSERVAZIONE"),key=lambda p:p["liquidity_usd"],reverse=True)
    lines=["MONITOR CRYPTO - "+data["collected_at"],"Stato: "+data["status"],"Copertura: "+data["coverage"],
        "Snapshot salvati nelle ultime 24h: "+str(count),
        "NESSUN INGRESSO VALIDATO. Filtri di ricerca, non segnali di acquisto.",
        "I volumi sono aggregati del fornitore, non liquidita eseguibile. Nessuna verifica contratti/holders/uscita.",
        "Breve termine: osservazione di prezzo/liquidita. Lungo termine: serve una tesi indipendente; hype non sufficiente.",
        "", "CRYPTO PER CAPITALIZZAZIONE (stablecoin principali escluse; classificazione non esaustiva)"]
    for c in data["coins"][:12]:
        lines.append(f"{c.get('symbol','?').upper()} | prezzo USD {c.get('current_price')} | 24h {c.get('price_change_percentage_24h')}% | 7g {c.get('price_change_percentage_7d_in_currency')}% | 30g {c.get('price_change_percentage_30d_in_currency')}% | volume USD {c.get('total_volume')}")
    lines+=["","TOKEN DEX CHE SUPERANO I FILTRI DESCRITTIVI"]
    for p in good[:10]:
        lines.append(f"{p['symbol']} [{p['chain']}] {p['contract']} | liquidita {p['liquidity_usd']:.0f} USD | volume24h {p['volume_24h_usd']:.0f} USD | eta pool {p['age_days']:.1f}g | sicurezza non verificata | {p['url']}")
    if not good:lines.append("Nessun candidato supera tutti i filtri. Nessun ingresso.")
    lines+=["",f"Pool osservati {len(data['pairs'])}; ammessi a osservazione {len(good)}; esclusi {len(data['pairs'])-len(good)}.",
        "Soglie provvisorie: liquidita >=250k USD, volume24h >=100k, eta pool >=7g, vendite24h >=10.",
        "Boost e pubblicita non sono un criterio positivo. I profili recenti creano un campione selezionato.",
        "Nessun rendimento strategico calcolabile: non esiste ancora una regola di ingresso/uscita validata.",
        "", "ERRORI / COPERTURA INCOMPLETA"]+data["errors"]+["","FONTI"]+[s["url"] for s in data["sources"]]
    text="\n".join(lines)+"\n"
    (destination/"ultimo_report.txt").write_text(text,encoding="utf-8")
    print(text)
def main():
    p=argparse.ArgumentParser()
    p.add_argument("--loop",action="store_true",help="snapshot ogni ora finche il processo e il PC restano attivi")
    p.add_argument("--output",type=Path,default=ROOT/"monitor_data")
    a=p.parse_args()
    while True:
        try:
            save(collect(),a.output)
        except Exception as e:
            print("ERRORE MONITOR, nessun risultato corrente: " + str(e),flush=True)
            if not a.loop:raise
        if not a.loop:break
        time.sleep(3600)
if __name__=="__main__":main()
