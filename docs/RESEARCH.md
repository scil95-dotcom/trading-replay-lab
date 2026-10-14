# Ricerca e automazione

Migrazione del 14 ottobre 2026. Python esegue i controlli senza chiamate a modelli IA.
I sorgenti selezionati provengono dalle copie revisionate dei progetti recuperati;
documenti personali, configurazioni dei conti, risultati storici e credenziali non sono pubblicati.

| Progetto | Controllo automatico | Limite residuo |
|---|---|---|
| Crypto | Ultima candela chiusa e indici di esecuzione | Dataset storico assente; nessun rendimento ricalcolato |
| Carry | Perdita iniziale nel drawdown, funding completo e segnale obsoleto bloccato | Standby; tassi, costi e fattibilita da riverificare |
| Noise | CLI ordini bloccata prima di leggere la configurazione | Duplicazione ordini/stop rifiutato NON risolti; funzioni interne storiche non sicure |
| Monitor | Dati mancanti/non finiti esclusi, outage parziale, salvataggio SQLite | Filtri descrittivi; contratti, holders e uscita non verificati |

## Esecuzione

```bash
python -m pip install -r research/requirements.txt
python research/run_checks.py
python research/cloud_monitor.py
```

`Research regressions` parte a ogni push/PR. Il monitor parte ogni ora al minuto 17 UTC
e puo essere avviato manualmente da Actions. Non dipende dal PC acceso.
Il riepilogo delle ultime 24 ore e aggiornato a ogni snapshot, quindi disponibile anche la mattina.
Aprire [Actions](https://github.com/scil95-dotcom/trading-replay-lab/actions), scegliere
`Crypto research monitor`, poi l'ultima esecuzione: riepilogo nel job summary;
scaricare `crypto-monitor-state` per `ultimo_report.txt`, `daily_report.md`, `latest.json` e SQLite.
Gli artifact di un repository pubblico sono accessibili agli utenti GitHub: contengono solo dati di ricerca pubblici.

Lo stato viene recuperato dall'ultimo artifact disponibile e conserva 30 giorni di snapshot.
Gli artifact scadono dopo 7 giorni: dopo una lunga interruzione lo storico puo ripartire da zero,
esplicitamente indicato nel report. Un recupero fallito produce errore e stato PARZIALE.
L'assenza totale delle fonti fa fallire il job; una fonte parziale viene conservata e dichiarata.
Un job verde non prova copertura completa: leggere stato, errori e conteggio snapshot.

CoinGecko top100 e profili DEX recenti costituiscono un campione, non tutte le crypto/memecoin.
Liquidita >=250k USD, volume24h >=100k, eta >=7 giorni e vendite24h >=10 sono
soglie provvisorie non ottimizzate. Superarle consente soltanto osservazione.
Nessun ingresso, ordine, wallet o profitto validato. Nessuna tesi di lungo termine automatizzata.

GitHub puo ritardare o saltare esecuzioni; nei repository pubblici disabilita le schedule
dopo 60 giorni senza attivita. Non e infrastruttura per esecuzione continua di ordini.
Fonti: [schedule GitHub](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule),
[CoinGecko](https://docs.coingecko.com/reference/coins-markets),
[DEX Screener](https://docs.dexscreener.com/api/reference).

## Per riprodurre i backtest

Mancano dati OHLCV/serie Crypto, dati e tabelle Noise, funding/prezzi/serie Carry e
verdetto originale del 30 settembre. Non sono stati ricreati o sostituiti con risultati inventati.
Servono snapshot con provenienza, hash, periodo e licenza; poi nuova esecuzione e confronto.
Fino ad allora i controlli sintetici attestano solo i casi verificati.
