# Testi pronti da condividere

## LinkedIn

Ho esteso il mio Trading Replay Lab con test automatici e un monitor di ricerca crypto su GitHub Actions.

Il lavoro parte da problemi concreti: una candela giornaliera esclusa dal segnale, una perdita iniziale ignorata nel drawdown e un avvio ordini da bloccare per difetti di gestione delle risposte del broker.

Le regressioni usano dati sintetici; il monitor raccoglie dati pubblici, conserva gli errori e produce un riepilogo delle ultime 24 ore. Tutto gira in Python senza chiamate a un modello IA.

Il limite e esplicito: i dataset storici recuperati sono incompleti, quindi non presento i vecchi rendimenti come nuovamente verificati. I filtri memecoin producono osservazioni, non segnali d'acquisto.

Codice, metodo e controlli: https://github.com/scil95-dotcom/trading-replay-lab

## X

Trading Replay Lab: regressioni automatiche e monitor crypto su GitHub Actions, senza chiamate IA. Dati sintetici per i test, errori dei feed visibili. Nessun rendimento o ingresso validato.
https://github.com/scil95-dotcom/trading-replay-lab
