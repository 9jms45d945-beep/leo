#!/usr/bin/env python3
"""
Monitor voli Roma (qualsiasi aeroporto) -> New York (qualsiasi aeroporto).
Andata/ritorno, avvisa su Telegram quando il prezzo scende sotto la soglia.

Uso:  python monitor_voli.py
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from itertools import product

try:
    from fast_flights import FlightQuery, Passengers, create_query, get_flights
except Exception as _err:
    import traceback
    traceback.print_exc()
    print("\n--- diagnostica ---")
    try:
        import fast_flights
        print("Nomi disponibili:",
              [n for n in dir(fast_flights) if not n.startswith("_")])
    except Exception as _e2:
        print("Modulo non importabile:", repr(_e2))
    sys.exit(f"\nImport fallito: {_err!r}")

import requests

# ----------------------------------------------------------------------
# CONFIGURAZIONE
# ----------------------------------------------------------------------

# ROM e NYC sono codici *città*: coprono FCO+CIA e JFK+EWR+LGA in una sola query.
ORIGINE = "ROM"
DESTINAZIONE = "NYC"

ANDATE = ["2026-12-27", "2026-12-28"]
RITORNI = ["2027-01-04", "2027-01-05"]

SOGLIA_EUR = 750
ADULTI = 1

# 0 = solo diretti, 1 = ammette un solo scalo
MAX_SCALI = 1

# Scarta i viaggi troppo lunghi. Roma-New York diretto e' circa 9 ore, quindi
# 15 ore lascia passare i diretti e gli scali brevi (2-4 h), non quelli lunghi.
DURATA_MAX_ORE = 15

# Per non ricevere 10 volte lo stesso avviso: rinotifica solo se il prezzo
# scende di almeno questo importo rispetto all'ultimo avviso mandato.
DELTA_RINOTIFICA = 20

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stato.json")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


# ----------------------------------------------------------------------
# UTILITY
# ----------------------------------------------------------------------

def parse_prezzo(raw):
    """Converte '€1.234', '1,234 EUR', '612.50' in float. None se non ci riesce."""
    s = str(raw or "")
    m = re.search(r"\d[\d.,\u00a0\u202f ]*\d|\d", s)
    if not m:
        return None
    n = re.sub(r"[\s\u00a0\u202f]", "", m.group(0))
    # separatore seguito da esattamente 2 cifre finali -> sono decimali
    if re.search(r"[.,]\d{2}$", n) and len(re.sub(r"\D", "", n)) > 2:
        interi = re.sub(r"\D", "", n[:-3]) or "0"
        return float(interi + "." + n[-2:])
    solo_cifre = re.sub(r"\D", "", n)
    return float(solo_cifre) if solo_cifre else None


def parse_durata(raw):
    """Converte '11 hr 30 min', '11 ore 30 min', '11h30' in minuti. None se fallisce."""
    s = str(raw or "").lower()
    ore = re.search(r"(\d+)\s*(?:hr|hour|hours|ore|ora|h)\b", s)
    minuti = re.search(r"(\d+)\s*(?:min|minute|minutes|m)\b", s)
    if not ore and not minuti:
        return None
    return (int(ore.group(1)) * 60 if ore else 0) + (int(minuti.group(1)) if minuti else 0)


def link_skyscanner(andata, ritorno):
    """Skyscanner usa il formato date yymmdd nell'URL."""
    a = datetime.strptime(andata, "%Y-%m-%d").strftime("%y%m%d")
    r = datetime.strptime(ritorno, "%Y-%m-%d").strftime("%y%m%d")
    return f"https://www.skyscanner.it/trasporti/voli/{ORIGINE.lower()}/{DESTINAZIONE.lower()}/{a}/{r}/"


def carica_stato():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def salva_stato(stato):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(stato, f, indent=2, ensure_ascii=False)


def notifica(testo):
    print(testo)
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        print("  [Telegram non configurato: imposta TELEGRAM_TOKEN e TELEGRAM_CHAT_ID]")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": testo,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=20,
        ).raise_for_status()
    except Exception as e:
        print(f"  [Errore invio Telegram: {e}]")


# ----------------------------------------------------------------------
# RICERCA
# ----------------------------------------------------------------------

def _attr(oggetto, *nomi, default="?"):
    """Prende il primo attributo esistente fra quelli elencati."""
    for n in nomi:
        v = getattr(oggetto, n, None)
        if v not in (None, ""):
            return v
    return default


def cerca(andata, ritorno):
    """Restituisce la lista di offerte (dict) per una coppia di date."""
    extra = {"max_stops": MAX_SCALI}
    try:
        tratte = [
            FlightQuery(date=andata, from_airport=ORIGINE,
                        to_airport=DESTINAZIONE, **extra),
            FlightQuery(date=ritorno, from_airport=DESTINAZIONE,
                        to_airport=ORIGINE, **extra),
        ]
    except TypeError:
        # versione della libreria senza filtro sugli scali
        print("  [attenzione: max_stops non supportato, includo tutti gli scali]")
        tratte = [
            FlightQuery(date=andata, from_airport=ORIGINE, to_airport=DESTINAZIONE),
            FlightQuery(date=ritorno, from_airport=DESTINAZIONE, to_airport=ORIGINE),
        ]
    base = dict(
        flights=tratte,
        trip="round-trip",
        seat="economy",
        passengers=Passengers(adults=ADULTI),
    )

    # currency/language non esistono in tutte le versioni: se falliscono, riprovo senza
    try:
        query = create_query(**base, currency="EUR", language="it-IT")
    except TypeError:
        query = create_query(**base)

    risultato = get_flights(query)

    # a seconda della versione il risultato e' una lista o ha .flights
    voli = getattr(risultato, "flights", None)
    if voli is None:
        voli = list(risultato)

    offerte = []
    scartati_lunghi = 0
    for volo in voli:
        prezzo = parse_prezzo(_attr(volo, "price", default=None))
        if prezzo is None:
            continue

        durata_raw = _attr(volo, "duration")
        minuti = parse_durata(durata_raw)
        # se la durata non e' leggibile tengo l'offerta invece di scartarla al buio
        if minuti is not None and minuti > DURATA_MAX_ORE * 60:
            scartati_lunghi += 1
            continue

        offerte.append({
            "prezzo": prezzo,
            "compagnia": _attr(volo, "airlines", "airline", "name"),
            "durata": durata_raw,
            "scali": "diretto" if MAX_SCALI == 0 else _attr(volo, "stops", "num_stops"),
        })

    if scartati_lunghi:
        print(f"  ({scartati_lunghi} opzioni scartate perche' oltre {DURATA_MAX_ORE} h)")
    return sorted(offerte, key=lambda o: o["prezzo"])


def main():
    stato = carica_stato()
    ora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"=== Controllo {ora} — soglia {SOGLIA_EUR} € ===")

    trovato_qualcosa = False

    for andata, ritorno in product(ANDATE, RITORNI):
        chiave = f"{andata}_{ritorno}"
        try:
            offerte = cerca(andata, ritorno)
        except Exception as e:
            print(f"{chiave}: errore nella ricerca ({e})")
            continue

        if not offerte:
            print(f"{chiave}: nessun risultato")
            continue

        migliore = offerte[0]
        print(f"{chiave}: miglior prezzo {migliore['prezzo']:.0f} € "
              f"({migliore['compagnia']}, {migliore['scali']})")

        # aggiorna sempre lo storico prezzi
        stato.setdefault(chiave, {})["ultimo_visto"] = migliore["prezzo"]
        stato[chiave]["aggiornato"] = ora

        if migliore["prezzo"] > SOGLIA_EUR:
            continue

        ultimo_avviso = stato[chiave].get("ultimo_avviso")
        if ultimo_avviso is not None and migliore["prezzo"] > ultimo_avviso - DELTA_RINOTIFICA:
            continue  # già segnalato a un prezzo simile

        trovato_qualcosa = True
        stato[chiave]["ultimo_avviso"] = migliore["prezzo"]

        righe = [
            f"✈️ <b>{migliore['prezzo']:.0f} €</b> — Roma → New York A/R",
            f"📅 {datetime.strptime(andata, '%Y-%m-%d').strftime('%d/%m')} → "
            f"{datetime.strptime(ritorno, '%Y-%m-%d').strftime('%d/%m')}",
            f"🛫 {migliore['compagnia']} · {migliore['scali']} · {migliore['durata']}",
            "",
            f'<a href="{link_skyscanner(andata, ritorno)}">Apri su Skyscanner</a>',
        ]
        if len(offerte) > 1:
            altre = ", ".join(f"{o['prezzo']:.0f} €" for o in offerte[1:4])
            righe.insert(3, f"Altre opzioni: {altre}")

        notifica("\n".join(righe))

    salva_stato(stato)
    if not trovato_qualcosa:
        print("Niente sotto soglia questa volta.")


if __name__ == "__main__":
    main()
