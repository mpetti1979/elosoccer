# Soccer Rating Scraper

Scarica regolarmente i rating eLo (Total/Home/Away) da soccer-rating.com per le
partite elencate in `matches.csv`, ed accumula uno storico in `data/results_history.csv`.

## Setup (una tantum)

1. Crea un nuovo repository su GitHub (può essere privato).
2. Carica tutti questi file mantenendo la struttura:
   ```
   .github/workflows/scrape.yml
   soccer_rating_scraper.py
   requirements.txt
   matches.csv
   .gitignore
   ```
3. Modifica `matches.csv` con le partite che vuoi tracciare (una riga per
   partita, colonne `home,away`, nomi come compaiono sul sito o simili —
   il fuzzy match nello script tollera piccole differenze).
4. Vai su **Settings > Actions > General** del repo e assicurati che
   "Workflow permissions" sia impostato su **Read and write permissions**
   (serve perché il workflow deve fare commit/push dei risultati).
5. Fatto. Il workflow gira da solo ogni giorno alle 08:00 UTC (modificabile
   nel file `.github/workflows/scrape.yml`, riga `cron:`).

## Primo test manuale

Prima di aspettare il cron, testalo a mano:
- Vai nel tab **Actions** del repo su GitHub
- Seleziona "Soccer Rating Scraper" nella lista a sinistra
- Clicca **Run workflow**
- Controlla i log dello step "Esegui lo scraper" per vedere se estrae
  correttamente i rating (se qualcosa non torna, incollami i log e sistemiamo)

## Dove trovi i risultati

Dopo ogni run, il workflow fa commit di:
- `data/results_history.csv` — storico di tutte le partite processate nel tempo
- `data/team_index.json` — cache nome squadra -> URL (si rinfresca ogni 7 giorni)
- `data/rating_cache.json` — cache rating per squadra (si rinfresca ogni 12 ore)

## Note

- Rispetta un rate limit interno (4-8s tra richieste + backoff sui 429),
  quindi non sovraccarica il sito.
- Se aggiungi molte partite in `matches.csv`, la prima esecuzione sarà più
  lenta (deve scaricare l'indice di ~600 squadre la prima volta); le
  successive saranno rapide grazie alla cache persistita nel repo.
- Se i runner cloud di GitHub dovessero venire bloccati/rallentati dal sito
  (IP di datacenter), l'alternativa è un self-hosted runner su una tua
  macchina sempre accesa (es. Raspberry Pi) — stessa configurazione del
  workflow, cambia solo `runs-on: self-hosted`.
