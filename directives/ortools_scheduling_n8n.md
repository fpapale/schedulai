# Direttiva: Elaborazione Turni tramite OR-Tools e n8n

## Obiettivo
Configurare e gestire in autonomia l'assegnazione e l'elaborazione dei turni del personale in base a constraints e necessità, inviando un Payload in input all'infrastruttura OR-Tools preesistente, per poi salvare il risultato pulito su Google Sheets, mediante l'infrastruttura di automazione n8n.

## Input & Trigger
- **Formato Input:** JSON che rispetta lo standard `dsl.schema.json`. Al suo interno è preente l'elenco dipendenti, competenze, "shifts" richiesti per vari "sites" (incluso `SITE_DEFAULT`) e i constraint "hard" e "soft".
- **Trigger:** Chiamata Webhook verso un endpoint esposto da n8n. Assicurarsi di copiare i dati JSON in `$json.body` dall'input del Webhook.

## Strumenti e Componenti
- **Docker/Server API:** Il server `solver-api` in esecuzione in Docker (in genere disponibile su `192.168.0.72:8001`).
- **N8N Workflow File:** Il file `execution/n8n/n8n_ortools_workflow.json` contiene la definizione del workflow da importare sull'istanza `192.168.0.72`.

## Flusso Esecutivo (Orchestrazione su N8N)
Quando c'è necessità di implementare/rielaborare questo workflow, il flusso è il seguente:

1. **Ricezione del webhook POST:** Prendi in input il JSON di configurazione `spec`.
2. **Validazione (HTTP Request):** Invio `spec` tramite una POST request al microservizio di validazione `http://192.168.0.72:8001/validate`.
3. **Controllo Validità (IF Node):** Verificare la proprietà `$json.valid == true`. 
   > **ATTENZIONE AI NODI N8N:** Se si opera su versioni di N8N < `3.x` (es. `2.9.2`), assicurarsi di usare il Node `n8n-nodes-base.if` in `typeVersion: 1` per utilizzare il formato classico `{ boolean: [{value1, value2}] }` dato che le versioni recenti (v2 o v3.2) non sono retrocompatibili.
4. **Gestione Errori (Stop And Error Node):** Se `$json.valid` è `false`, il flusso si deve interrompere immediatamente restituendo `$json.errors.join(', ')`. Questo protegge il solver API da input impossibili e ci dà un feedback tempestivo.
5. **Esecuzione Solver API (HTTP Request):** Inoltro dell'input POST originale alla rotta `http://192.168.0.72:8001/solve`. Essendo sincrona, è necessario configurare un tempo di `timeout` elevato sul nodo n8n (almeno 60-120s o coerente col campo `max_time_seconds` settato a `30`).
6. **Flatting (Code Node):** La risposta di OR-Tools presenta una gerarchia rigida in `schedule -> data -> sito -> turno -> [dipendenti]`. Bisogna eseguire un unrolling in un array di flat objects di formato `{ date, site, shift, employee }`. Da ricordare sempre di trattare il turno `OFF` escludendolo o contrassegnandolo in modo speciale senza inserirlo nei conteggi del site.
7. **Storage o Output:** Mappatura dei flat objects su `Google Sheets` come *Append Row*. Ogni run produrrà "N" righe per "N" assegnazioni.

## Casi Limite & Errori Comuni
- **Se gli endpoint docker sono irraggiungibili:** Verifica che `docker-compose up -d` sia in esecusione in `/execution/ortools-api`.
- **Compatibilità Versioni IF:** Vedi la *versione 1* in JSON al punto 3.
- **Se un nuovo constraint va applicato:** Deve essere aggiornato in `ALLOWED_KINDS` nell'`api.py`, quindi ricompilata l'immagine docker. Non può essere solo inserito nel workflow JSON.
