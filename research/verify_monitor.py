from pathlib import Path
import sys,importlib.util,datetime as dt,tempfile,json,sqlite3
p=Path(sys.argv[1]);s=importlib.util.spec_from_file_location('watch',p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
now=dt.datetime.now(dt.timezone.utc)
assert m.inspect_pair({},now)['status']=='ESCLUSO_DAI_FILTRI'
good={'chainId':'solana','pairAddress':'p','baseToken':{'address':'a'},'priceUsd':'1','liquidity':{'usd':300000},'volume':{'h24':200000},'pairCreatedAt':int((now-dt.timedelta(days=8)).timestamp()*1000),'txns':{'h24':{'sells':20}}}
a=m.inspect_pair(good,now);assert a['status']=='OSSERVAZIONE' and a['entry']=='NESSUN_INGRESSO_VALIDATO'
good['priceUsd']='nan';assert m.inspect_pair(good,now)['status']=='ESCLUSO_DAI_FILTRI'
m.get=lambda u:(_ for _ in ()).throw(RuntimeError('offline fixture'))
d=m.collect();assert d['status']=='PARZIALE' and not d['coins'] and not d['pairs']
with tempfile.TemporaryDirectory(dir=p.parent) as tmp:
 m.save(d,Path(tmp));assert json.loads(Path(tmp,'latest.json').read_text(encoding='utf-8'))['status']=='PARZIALE';db=sqlite3.connect(Path(tmp,'market.sqlite'));assert db.execute('select count(*) from runs').fetchone()[0]==1;db.close()
print('PASS monitor: unknown fields rejected, finite prices, no entry even for passing filters, provider outage preserved as partial, SQLite saved')
