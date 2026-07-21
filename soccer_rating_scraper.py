#!/usr/bin/env python3
"""
soccer_rating_scraper.py
-------------------------
Scarica i rating eLo (Total / Home / Away) da soccer-rating.com per una o più
partite, in modo regolare e rispettoso del sito (rate limiting + backoff sui 429).

USO RAPIDO (una partita):
    python soccer_rating_scraper.py --home "Juventus" --away "Fiorentina"

USO BATCH (piu' partite da CSV, colonne: home,away):
    python soccer_rating_scraper.py --batch matches.csv

I risultati vengono sia stampati a video sia accodati a data/results_history.csv,
cosi' nel tempo costruisci uno storico utilizzabile per la formula.

NOTE IMPORTANTI (leggere prima di usare regolarmente):
- Il sito ha un rate limiter attivo: richieste troppo ravvicinate rispondono 429.
  Lo script rispetta un delay minimo tra le richieste + backoff esponenziale.
- Il parsing e' basato sul TESTO visibile della pagina ("Rating Total:",
  "Rating Home:", "Rating Away:"), non su classi CSS specifiche: e' piu'
  robusto a piccoli redesign del sito, ma va comunque validato al primo uso
  reale (io non ho potuto testare le richieste HTTP dal mio ambiente: il
  dominio soccer-rating.com non e' raggiungibile dalla mia sandbox).
- L'indice squadra->URL viene costruito scaricando le pagine di ranking
  (paginate) e viene salvato in cache locale (data/team_index.json) cosi'
  non va ricostruito ad ogni esecuzione.
"""

import argparse
import csv
import json
import random
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from difflib import get_close_matches
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.soccer-rating.com"
RANKING_PATH = "/football-club-ranking/"
DATA_DIR = Path(__file__).parent / "data"
TEAM_INDEX_FILE = DATA_DIR / "team_index.json"
RATING_CACHE_FILE = DATA_DIR / "rating_cache.json"
HISTORY_FILE = DATA_DIR / "results_history.csv"

TEAM_INDEX_MAX_AGE = timedelta(days=7)     # ogni quanto ricostruire l'indice squadre
RATING_CACHE_MAX_AGE = timedelta(hours=12)  # ogni quanto ri-scaricare il rating di una squadra

MIN_DELAY_SEC = 4.0      # delay minimo tra due richieste (educazione verso il sito)
MAX_DELAY_SEC = 8.0
MAX_RETRIES = 5
BACKOFF_BASE_SEC = 15    # 15, 30, 60, 120, 240...

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}

TEAM_LINK_RE = re.compile(r'href="(/([A-Za-z0-9\-]+)/(\d+)/?)"')
RATING_TEXT_RE = re.compile(
    r"Rating Total:\s*([\d.]+).*?Rating Home:\s*([\d.]+).*?Rating Away:\s*([\d.]+)",
    re.S,
)


@dataclass
class TeamRating:
    name: str
    url: str
    rating_total: float
    rating_home: float
    rating_away: float
    fetched_at: str


class RateLimitedSession:
    """Wrapper attorno a requests con delay minimo + backoff esponenziale sui 429."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._last_request_ts = 0.0

    def get(self, url: str) -> requests.Response:
        self._respect_min_delay()
        attempt = 0
        while True:
            attempt += 1
            resp = self.session.get(url, timeout=20)
            self._last_request_ts = time.time()
            if resp.status_code == 429:
                if attempt > MAX_RETRIES:
                    raise RuntimeError(
                        f"Troppi 429 su {url}, mi fermo dopo {MAX_RETRIES} tentativi."
                    )
                wait = BACKOFF_BASE_SEC * (2 ** (attempt - 1))
                print(f"  [429] {url} -> aspetto {wait}s (tentativo {attempt}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp

    def _respect_min_delay(self):
        elapsed = time.time() - self._last_request_ts
        min_gap = random.uniform(MIN_DELAY_SEC, MAX_DELAY_SEC)
        if elapsed < min_gap:
            time.sleep(min_gap - elapsed)


def _load_json(path: Path) -> dict:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class SoccerRatingClient:
    def __init__(self):
        self.http = RateLimitedSession()
        self.team_index = _load_json(TEAM_INDEX_FILE)
        self.rating_cache = _load_json(RATING_CACHE_FILE)

    # ---------- Indice squadre (nome -> url) ----------

    def _index_is_fresh(self) -> bool:
        ts = self.team_index.get("_built_at")
        if not ts:
            return False
        return datetime.fromisoformat(ts) > datetime.now() - TEAM_INDEX_MAX_AGE

    def build_team_index(self, max_pages: int = 6, force: bool = False):
        """Scarica le pagine di ranking (100 squadre a pagina) e costruisce
        l'indice nome -> url. max_pages=6 copre circa le prime 600 squadre
        europee (piu' che sufficiente per i principali campionati)."""
        if self._index_is_fresh() and not force:
            print("Indice squadre gia' aggiornato (cache), skip.")
            return

        print("Costruisco l'indice squadre (puo' richiedere qualche minuto)...")
        teams = {}
        for page in range(max_pages):
            start = page * 100
            url = f"{BASE_URL}{RANKING_PATH}"
            if start:
                url = f"{BASE_URL}/ranking.php?start={start}"
            print(f"  Scarico pagina ranking start={start} ...")
            try:
                resp = self.http.get(url)
            except (requests.HTTPError, RuntimeError) as e:
                print(f"  Errore su {url}: {e} -- interrompo la costruzione indice qui.")
                break

            found = TEAM_LINK_RE.findall(resp.text)
            if not found:
                print("  Nessun link squadra trovato in questa pagina, mi fermo.")
                break

            # Estraggo anche il nome mostrato nel link (serve BeautifulSoup
            # per associare correttamente testo <a> -> href)
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                m = TEAM_LINK_RE.match(f'href="{a["href"]}"')
                if not m:
                    continue
                name = a.get_text(strip=True)
                if not name:
                    continue
                teams[name] = BASE_URL + m.group(1)

        teams["_built_at"] = datetime.now().isoformat()
        self.team_index = teams
        _save_json(TEAM_INDEX_FILE, teams)
        print(f"Indice squadre salvato: {len(teams) - 1} squadre.")

    def search_team(self, name: str) -> Optional[str]:
        """Ritorna l'URL della squadra il cui nome piu' si avvicina a `name`."""
        if not self.team_index or "_built_at" not in self.team_index:
            self.build_team_index()

        candidates = [k for k in self.team_index if k != "_built_at"]
        # match esatto (case-insensitive) prima
        for c in candidates:
            if c.lower() == name.lower():
                return self.team_index[c]

        matches = get_close_matches(name, candidates, n=3, cutoff=0.5)
        if not matches:
            print(f"  Nessuna squadra trovata per '{name}'. Prova un altro indice piu' ampio "
                  f"(--rebuild-index --max-pages N) o verifica il nome esatto sul sito.")
            return None
        if len(matches) > 1:
            print(f"  Piu' squadre simili a '{name}': {matches} -> uso '{matches[0]}'.")
        return self.team_index[matches[0]]

    # ---------- Rating squadra ----------

    def get_team_rating(self, team_name: str) -> Optional[TeamRating]:
        cached = self.rating_cache.get(team_name)
        if cached:
            fetched_at = datetime.fromisoformat(cached["fetched_at"])
            if fetched_at > datetime.now() - RATING_CACHE_MAX_AGE:
                return TeamRating(**cached)

        url = self.search_team(team_name)
        if not url:
            return None

        print(f"  Scarico rating per '{team_name}' -> {url}")
        resp = self.http.get(url)
        text = BeautifulSoup(resp.text, "html.parser").get_text(separator=" ", strip=True)
        m = RATING_TEXT_RE.search(text)
        if not m:
            print(f"  ATTENZIONE: non ho trovato i pattern 'Rating Total/Home/Away' "
                  f"nella pagina di {team_name}. La struttura del sito potrebbe essere "
                  f"cambiata: va aggiornata la regex RATING_TEXT_RE.")
            return None

        rating = TeamRating(
            name=team_name,
            url=url,
            rating_total=float(m.group(1)),
            rating_home=float(m.group(2)),
            rating_away=float(m.group(3)),
            fetched_at=datetime.now().isoformat(),
        )
        self.rating_cache[team_name] = asdict(rating)
        _save_json(RATING_CACHE_FILE, self.rating_cache)
        return rating

    # ---------- Match ----------

    def get_match_ratings(self, home_name: str, away_name: str) -> Optional[dict]:
        home = self.get_team_rating(home_name)
        away = self.get_team_rating(away_name)
        if not home or not away:
            return None
        return {
            "home_team": home.name,
            "away_team": away.name,
            "home_rating_home": home.rating_home,   # <- valore da usare per la formula
            "away_rating_away": away.rating_away,    # <- valore da usare per la formula
            "home_rating_total": home.rating_total,
            "away_rating_total": away.rating_total,
            "fetched_at": datetime.now().isoformat(),
        }


def append_to_history(row: dict):
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_header = not HISTORY_FILE.exists()
    with open(HISTORY_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(description="Scarica rating eLo da soccer-rating.com")
    parser.add_argument("--home", help="Nome squadra di casa")
    parser.add_argument("--away", help="Nome squadra in trasferta")
    parser.add_argument("--batch", help="CSV con colonne 'home,away' per piu' partite")
    parser.add_argument("--rebuild-index", action="store_true",
                         help="Forza la ricostruzione dell'indice squadre")
    parser.add_argument("--max-pages", type=int, default=6,
                         help="Quante pagine di ranking scaricare per l'indice (100 squadre/pagina)")
    args = parser.parse_args()

    client = SoccerRatingClient()

    if args.rebuild_index:
        client.build_team_index(max_pages=args.max_pages, force=True)

    matches = []
    if args.batch:
        with open(args.batch, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                matches.append((row["home"].strip(), row["away"].strip()))
    elif args.home and args.away:
        matches.append((args.home, args.away))
    else:
        parser.error("Specifica --home/--away oppure --batch CSV")

    for home_name, away_name in matches:
        print(f"\n=== {home_name} (H) vs {away_name} (A) ===")
        result = client.get_match_ratings(home_name, away_name)
        if not result:
            print("  -> Impossibile ottenere i rating per questa partita.")
            continue
        print(f"  {result['home_team']} - Rating Home: {result['home_rating_home']}")
        print(f"  {result['away_team']} - Rating Away: {result['away_rating_away']}")
        append_to_history(result)

    print(f"\nStorico aggiornato in: {HISTORY_FILE}")


if __name__ == "__main__":
    sys.exit(main())
