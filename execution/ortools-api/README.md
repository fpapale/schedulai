# SchedulAI - OR-Tools Engine

Questo modulo contiene l'engine di scheduling basato su Google OR-Tools per calcolare e assegnare la turnazione del personale tenendo in considerazione le constraints ("regole" e "vincoli") aziendali definite in un file `spec.json`. L'architettura prevede diverse componenti servite via Docker, assieme ad una WebUI per la manipolazione e validazione delle specifiche.

## Struttura dell'Applicativo (Or-Tools-API)

L'ambiente è pre-configurato tramite `docker-compose.yml` e comprende:
1. **postgres**: Database (PostgreSQL 16) locale per storicizzare e mantenere informazioni inerenti ai Job di assegnazione (salvati con UUID unico).
2. **solver-api**: Servizio FastAPI (`api.py`) con Google OR-Tools. Prende in input un `spec.json` contenente dipendenti, skills, esigenze di coverage ("demand") e limiti, compilandoli nel modello OR-Tools per trovarne le possibili o le ottime soluzioni.
3. **dsl-editor**: Applicazione basata su WebUI e servita via Nginx che funge da validatore visuale e generatore di file `spec.json` a partire dal DSL (`dsl.schema.json`).

## Avviare l'Applicativo Or-Tools

Esegui questo comando all'interno della cartella `execution/ortools-api` (che contiene il docker-compose):

```bash
docker-compose up -d --build
```
* **Solver API** esposto sulla porta `8001`.
* **Editor Web** esposto sulla porta `8080`.
* **Postgres** esposto sulla porta `5433`.

## Integrazione n8n (Scheduling Automation)

Per l'automazione dei turni e il salvataggio su **Google Sheets** validato, puoi importare il workflow situato in `directives/n8n_ortools_workflow.json` all'interno dell'istanza n8n (spesso esposta su `192.168.0.72`).

1. **Trigger Base**: Usa un Webhook in POST verso l'n8n.
2. **Endpoint Mappato**: `http://192.168.0.72:8001/solve` che offre un'operazione completamente *sincrona* in caso di esecuzioni semplici (<= max_time_seconds, es. 30s).
3. Il nodo codice compresso nel workflow (`Flatten Schedule`) spiana intelligentemente la risposta di `schedule -> Date -> Site -> Shift -> Employee` ritornando un array 1D.
4. Ogni oggetto flattened viene inviato in append row sul blocco target `Google Sheets`. 

### Sviluppi GitHub
I file appartengono al repo GitHub dedicato sotto l'org di riferimento (`fpapale/schedulai`).
