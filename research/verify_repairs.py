
from pathlib import Path
import sys,importlib.util,datetime as dt,tempfile
import numpy as np,pandas as pd
root=Path(__file__).parent
def mod(n,p):
 s=importlib.util.spec_from_file_location(n,p);m=importlib.util.module_from_spec(s);sys.modules[n]=m;s.loader.exec_module(m);return m
CT=mod("crypto_trend",root/"reviewed/crypto/02_Segnale/crypto_trend.py")
ts=pd.date_range("2024-01-01","2025-03-01 16:30",freq="30min",tz="UTC")
assert CT.daily_index(ts,17,-30)[0][-1].date()==dt.date(2025,3,1)
# Ordinary simulation cannot execute beyond the last available bar.
for lag in [0,30,120]:
 c,p,e=CT.daily_index(ts,17,lag);assert np.all(e<len(ts)) and np.all(e>p)
CL=mod("carry_lib",root/"reviewed/carry/03_Analisi/carry_lib.py")
assert abs(CL.metriche([-.1,0.])["maxdd"]+.1)<1e-12
assert CL.metriche([.1,.1])["maxdd"]==0
with tempfile.TemporaryDirectory(dir=root) as d:
 f=Path(d,"kraken_funding_BTC.csv")
 f.write_text("timestamp,rate\n2026-09-29T12:00:00+0000,0.0001\n")
 assert CL.funding_giornaliero(d,"kraken","BTC")=={}
 rows=["timestamp,rate"]
 start=dt.datetime(2026,9,29,1,tzinfo=dt.timezone.utc)
 for h in range(24):rows.append((start+dt.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%S%z")+",0.0001")
 f.write_text("\n".join(rows))
 got=CL.funding_giornaliero(d,"kraken","BTC")
 assert len(got)==1 and abs(next(iter(got.values()))-.0024)<1e-12
 f.write_text("\n".join(rows+[rows[1]]))
 assert CL.funding_giornaliero(d,"kraken","BTC")==got
SC=mod("segnale_carry",root/"reviewed/carry/02_Segnale/segnale_carry.py")
# Stub public reads: stale or incomplete funding must terminate before producing a signal.
SC.get=lambda u:{"tickers":[]} if "tickers" in u else {"rates":[]}
old=sys.argv;sys.argv=["segnale_carry.py"]
try:
 try:SC.main();raise AssertionError("stale funding accepted")
 except SystemExit as e:assert "incompleto" in str(e)
finally:sys.argv=old
print("PASS: closed daily candle, valid execution indices, initial loss drawdown, complete/deduplicated funding, stale signal blocked")
