#!/usr/bin/env python3
"""
soccer_rating_results.py
-------------------------
Estensione di soccer_rating_scraper.py: recupera i RISULTATI ESATTI delle
partite passate dalla stessa pagina squadra che gia' usi per il rating
(colonna "Res." nella tabella storica in fondo alla pagina di ogni squadra).

Non tocca soccer_rating_scraper.py: lo importa e riusa (rate limiting, cache,
fuzzy matching squadre). Deve stare nella STESSA cartella di quel file.

ATTENZIONE - LEGGERE PRIMA DI USARE:
Non ho potuto testare questo script contro il sito vero (soccer-rating.com
non e' raggiungibile dalla mia sandbox, stessa limitazione gia' segnalata
nello scraper originale). Il parsing e' basato su un esempio reale di pagina
squadra che ho recuperato via ricerca web, ma la regex ROW_RE va validata
al primo uso con --debug su UNA sola squadra prima di lanciare il batch
completo. Se non trova nulla, quasi certamente la struttura della tabella
e' leggermente diversa da quella che ho visto io e la regex va aggiustata.

USO CONSIGLIATO (in 2 passi):

1) Validazione su una squadra sola, con debug:
     python soccer_rating_results.py --test-team "FC Barcelona" --debug

2) Se il test funziona, batch completo su un export CSV di "db home"
   (colonne richieste: data,home,away,elo_h,elo_a - altre colonne ignorate):
     python soccer_rating_results.py --input-csv db_home_export.csv --output-csv db_home_con_gol.csv

Il file di output ha le stesse colonne dell'input + gol_home, gol_away,
match_confidence (alta/bassa), match_note.
"""

import argparse
import csv
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from difflib import get_close_matches
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

# Riusa TUTTO quello che gia' funziona nello scraper originale
from soccer_rating_scraper import SoccerRatingClient, DATA_DIR

MATCH_HISTORY_CACHE_FILE = DATA_DIR / "match_history_cache.json"

# Ancora la regex sui RATING (4 cifre prima del punto, es. 2386.56) perche'
# sono l'elemento piu' distintivo e difficile da confondere con le quote
# (1-2 cifre prima del punto, es. 1.32). Il punteggio e' l'ultimo blocco N:N.
ROW_RE = re.compile(
    r"(\d{2}\.\d{2}\.\d{2})\s+"        # 1: data gg.mm.aa
    r"([A-Za-z0-9]{2,8})\s+"            # 2: codice torneo (es. ES1, CLCUP)
    r"(.+?)\s*-\s*(.+?)\s+(?=\d)"        # 3,4: squadra casa - squadra ospite
                                         #      (si ferma appena inizia un numero, cioe' le quote)
    r".*?"                              # quote e altra roba nel mezzo, salto
    r"(\d{3,4}\.\d{2})\s+"              # 5: rating casa (3-4 cifre intere)
    r"(\d{3,4}\.\d{2})\s+"              # 6: rating ospite
    r"(\d{1,2}):(\d{1,2})\b"            # 7,8: risultato H:A
)

DATE_SITE_FMT = "%d.%m.%y"  # 17.05.26


@dataclass
class MatchRecord:
    date: datetime
    league: str
    home_team: str
    away_team: str
    rating_home: float
    rating_away: float
    score_home: int
    score_away: int


class SoccerRatingResultsClient(SoccerRatingClient):
    """Aggiunge il recupero dello storico risultati alla classe esistente."""

    def get_team_match_history(self, team_name: str, debug: bool = False) -> list[MatchRecord]:
        url = self.search_team(team_name)
        if not url:
            return []

        print(f"  Scarico storico risultati per '{team_name}' -> {url}")
        resp = self.http.get(url)
        soup = BeautifulSoup(resp.text, "html.parser")

        records: list[MatchRecord] = []
        rows_checked = 0
        for tr in soup.find_all("tr"):
            row_text = tr.get_text(" ", strip=True)
            # Filtro veloce: se non c'e' un pattern tipo "N:N" nella riga,
            # quasi certamente non e' una riga di risultato -> skip subito.
            if not re.search(r"\b\d{1,2}:\d{1,2}\b", row_text):
                continue
            rows_checked += 1

            m = ROW_RE.search(row_text)
            if debug:
                print(f"    [debug] riga grezza: {row_text[:160]}")
                print(f"    [debug] match regex: {'OK' if m else 'NESSUN MATCH'}")
            if not m:
                continue

            try:
                date = datetime.strptime(m.group(1), DATE_SITE_FMT)
            except ValueError:
                continue

            records.append(MatchRecord(
                date=date,
                league=m.group(2),
                home_team=m.group(3).strip(),
                away_team=m.group(4).strip(),
                rating_home=float(m.group(5)),
                rating_away=float(m.group(6)),
                score_home=int(m.group(7)),
                score_away=int(m.group(8)),
            ))

        print(f"  -> {rows_checked} righe con un punteggio nel testo, "
              f"{len(records)} interpretate correttamente dalla regex.")
        if rows_checked and not records:
            print("  ATTENZIONE: righe con punteggio trovate ma la regex ROW_RE non "
                  "ne ha interpretata nessuna. La struttura della pagina e' probabilmente "
                  "diversa da quella prevista -> serve aggiustare ROW_RE. Rilancia con "
                  "--debug per vedere il testo grezzo delle righe e capire cosa correggere.")
        return records


# ---------------------------------------------------------------------------
# Parsing date del TUO foglio (formati visti: 22/10/2024 e 9/5/25)
# ---------------------------------------------------------------------------

def parse_sheet_date(raw: str) -> Optional[datetime]:
    raw = raw.strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Matching tra le righe del tuo db e i MatchRecord scaricati
# ---------------------------------------------------------------------------

def find_best_match(target_date: datetime, target_away: str, target_elo_h: float,
                     target_elo_a: float, candidates: list) -> tuple:
    """Tra i MatchRecord di una squadra, trova quello che meglio corrisponde
    a una riga del tuo db. Ritorna (MatchRecord|None, confidence, note)."""

    # 1) filtro per data uguale (+/- 1 giorno, per sicurezza su fusi orari)
    same_day = [c for c in candidates if abs((c.date - target_date).days) <= 1]
    if not same_day:
        return None, "n/d", "nessuna partita con quella data sulla pagina"

    # 2) tra quelle stesso giorno, squadra ospite piu' simile per nome
    away_names = [c.away_team for c in same_day]
    close = get_close_matches(target_away, away_names, n=1, cutoff=0.6)
    if not close:
        return None, "n/d", f"data giusta ma nessun avversario simile a '{target_away}'"

    pick = next(c for c in same_day if c.away_team == close[0])

    # 3) controllo incrociato sui rating: se il rating del sito e' vicino
    #    (+/- 5 punti) a quello che hai nel db, alta confidenza; altrimenti,
    #    e' probabile che sia un'altra partita tra le stesse due squadre
    #    (es. stagioni diverse) -> confidenza bassa, da controllare a mano.
    rating_diff = abs(pick.rating_home - target_elo_h) + abs(pick.rating_away - target_elo_a)
    if rating_diff <= 5:
        return pick, "alta", "rating coincide"
    return pick, "bassa", f"rating diverso di {rating_diff:.1f} punti totali - verificare a mano"


# ---------------------------------------------------------------------------
# Batch da CSV
# ---------------------------------------------------------------------------

def run_batch(input_csv: str, output_csv: str, max_teams: Optional[int] = None,
              debug: bool = False):
    with open(input_csv, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    print(f"Righe lette da {input_csv}: {len(rows)}")

    client = SoccerRatingResultsClient()

    # Un fetch per squadra "home" unica (basta la loro pagina: mostra anche
    # le partite in cui giocano in casa, con l'avversario in trasferta)
    unique_home_teams = sorted({r["home"].strip() for r in rows})
    if max_teams:
        unique_home_teams = unique_home_teams[:max_teams]
        print(f"--max-teams attivo: limito il test a {max_teams} squadre.")

    history_by_team = {}
    for i, team in enumerate(unique_home_teams, 1):
        print(f"[{i}/{len(unique_home_teams)}] {team}")
        try:
            history_by_team[team] = client.get_team_match_history(team, debug=debug)
        except Exception as e:
            print(f"  ERRORE su '{team}': {e} -- salto questa squadra.")
            history_by_team[team] = []

    out_rows = []
    n_alta, n_bassa, n_nd = 0, 0, 0
    for r in rows:
        home = r["home"].strip()
        away = r["away"].strip()
        date = parse_sheet_date(r["data"])
        def _to_float(v):
            try:
                return float(str(v).replace(",", "").strip())
            except ValueError:
                return 0.0
        elo_h = _to_float(r.get("elo_h", 0))
        elo_a = _to_float(r.get("elo_a", 0))

        out = dict(r)
        if date is None or home not in history_by_team:
            out.update(gol_home="", gol_away="", match_confidence="n/d",
                       match_note="data non valida o squadra non scaricata")
            n_nd += 1
        else:
            pick, conf, note = find_best_match(date, away, elo_h, elo_a,
                                                history_by_team[home])
            if pick:
                out.update(gol_home=pick.score_home, gol_away=pick.score_away,
                           match_confidence=conf, match_note=note)
                if conf == "alta":
                    n_alta += 1
                else:
                    n_bassa += 1
            else:
                out.update(gol_home="", gol_away="", match_confidence="n/d", match_note=note)
                n_nd += 1
        out_rows.append(out)

    fieldnames = list(out_rows[0].keys()) if out_rows else []
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"\nFatto -> {output_csv}")
    print(f"  Confidenza alta:  {n_alta}")
    print(f"  Confidenza bassa: {n_bassa}  (controllare a mano prima di usarle)")
    print(f"  Non trovate:      {n_nd}")


def main():
    parser = argparse.ArgumentParser(description="Recupera risultati esatti da soccer-rating.com")
    parser.add_argument("--test-team", help="Testa il parsing su una sola squadra, stampa cosa trova")
    parser.add_argument("--input-csv", help="CSV export di 'db home' (colonne: data,home,away,elo_h,elo_a,...)")
    parser.add_argument("--output-csv", help="Dove scrivere il CSV con gol_home/gol_away aggiunti")
    parser.add_argument("--max-teams", type=int, default=None,
                         help="Limita il batch alle prime N squadre (utile per un test veloce)")
    parser.add_argument("--debug", action="store_true", help="Stampa il dettaglio riga per riga")
    args = parser.parse_args()

    if args.test_team:
        client = SoccerRatingResultsClient()
        records = client.get_team_match_history(args.test_team, debug=args.debug)
        print(f"\nTrovate {len(records)} partite valide per '{args.test_team}':")
        for rec in records[:15]:
            print(f"  {rec.date.date()}  {rec.home_team} {rec.score_home}-{rec.score_away} {rec.away_team}")
        return

    if args.input_csv and args.output_csv:
        run_batch(args.input_csv, args.output_csv, max_teams=args.max_teams, debug=args.debug)
        return

    parser.error("Usa --test-team NOME per il test, oppure --input-csv/--output-csv per il batch")


if __name__ == "__main__":
    sys.exit(main())
