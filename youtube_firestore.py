#!/usr/bin/env python3
"""
youtube_firestore.py
=====================
Alternativa gratuita e senza blocchi allo scraping: legge i commenti reali
dai video YouTube di lancio/recensione dei modelli BMW (e dei competitor,
per il benchmarking) tramite l'API UFFICIALE di YouTube, e li salva su
Firestore con la stessa identica struttura usata da scraper_firestore.py.

Perché questa via funziona dove Quattroruote/AlVolante no:
- È un'API pensata per essere letta da programmi: nessun bot-detection,
  nessun muro anti-adblock, nessun robots.txt da rispettare
- Piano gratuito con quota giornaliera ampia (10.000 unità/giorno;
  leggere i commenti di un video costa circa 1 unità per pagina di ~100
  commenti, quindi migliaia di commenti al giorno gratis)
- Stessa pipeline: classificazione categoria/sentiment + dashboard live
  già pronte, cambia solo la fonte

SETUP (una tantum, gratuito):
    1. Vai su https://console.cloud.google.com
    2. Crea un progetto (o riusa uno esistente)
    3. Menu -> API e servizi -> Libreria -> cerca "YouTube Data API v3" -> Abilita
    4. Menu -> API e servizi -> Credenziali -> Crea credenziali -> Chiave API
    5. Copia la chiave: la userai come YOUTUBE_API_KEY (secret su GitHub,
       stesso posto dove hai messo FIREBASE_SERVICE_ACCOUNT)

INSTALL:
    pip install google-api-python-client firebase-admin

USO:
    python youtube_firestore.py --model "BMW X5" --video-ids VIDEO_ID1 VIDEO_ID2
    python youtube_firestore.py --model "BMW X5" --search "BMW X5 2026 test review" --max-videos 5

    L'ID video è la parte dopo "v=" nell'URL, es:
    https://www.youtube.com/watch?v=XXXXXXXXXXX -> ID è XXXXXXXXXXX
"""

import argparse
import hashlib
import os
import sys
import time
from typing import Optional

try:
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    print("[ERRORE] Manca google-api-python-client. Installa con:\n"
          "  pip install google-api-python-client")
    sys.exit(1)

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    print("[ERRORE] Manca firebase-admin. Installa con: pip install firebase-admin")
    sys.exit(1)


# =============================================================================
# CONFIGURAZIONE
# =============================================================================

SERVICE_ACCOUNT_FILE = "serviceAccountKey.json"
FIRESTORE_COLLECTION = "comments"
MAX_COMMENTS_PER_VIDEO = 300  # limite di sicurezza per non consumare troppa quota


# =============================================================================
# CLASSIFICATORE (identico a scraper_firestore.py e alla dashboard)
# =============================================================================

TECNICO_WORDS = ['motore','autonomia','batteri','ricaric','kw','kwh','coppia','consum','prestazion','cambio','trazione','sospension','telaio','cruscotto','infotainment','elettric','ibrid','diesel','benzin','potenza','peso','piattaforma','powertrain','propuls','freni','impianto','navigatore','display']
ESTETICO_WORDS = ['design','fanal','fari','muso','frontale','rene','calandr','linee','forme','stile','cerchi','posteriore','coda','proporzion','esterni','estetic','carrozzeria','profilo','look']
POS_WORDS = ['bello','bellissim','stupend','top','capolavoro','ottim','perfett','ador','meravigli','favolos','incredibil','wow','promoss','azzeccat','riuscit','elegante','sublime','geniale','pazzesc','fenomenal','magnific','beautiful','amazing','great','love','stunning']
NEG_WORDS = ['brutt','orrend','orribil','inguardabil','schifo','vergogn','delusion','deluso','peggio','terribl','disastr','ridicol','improponibil','agghiacciant','indecent','nauseante','pessim','fallimento','copiat','cinesat','scandal','orrore','oscen','scempio','ugly','hate','terrible','worst']
POS_EMOJI = ['😍','❤️','🔥','👏','🙌','💙','🤍','💯','😘','🥰','👍']
NEG_EMOJI = ['🤮','💩','😡','🤦']


def classify_category(text: str) -> str:
    t = text.lower()
    tec = [w for w in TECNICO_WORDS if w in t]
    est = [w for w in ESTETICO_WORDS if w in t]
    if est and not tec:
        return "Estetico"
    if tec and not est:
        return "Tecnico"
    if tec and est:
        return "Tecnico" if min(t.find(w) for w in tec) < min(t.find(w) for w in est) else "Estetico"
    return "Social"


def classify_sentiment(text: str) -> str:
    t = text.lower()
    pos = sum(1 for w in POS_WORDS if w in t) + sum(1 for e in POS_EMOJI if e in text)
    neg = sum(1 for w in NEG_WORDS if w in t) + sum(1 for e in NEG_EMOJI if e in text)
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"


# =============================================================================
# FIRESTORE
# =============================================================================

def init_firestore():
    try:
        cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
        firebase_admin.initialize_app(cred)
        return firestore.client()
    except FileNotFoundError:
        print(f"[ERRORE] File '{SERVICE_ACCOUNT_FILE}' non trovato. "
              f"Scaricalo da Console Firebase -> Account di servizio.")
        sys.exit(1)
    except Exception as e:
        print(f"[ERRORE] Inizializzazione Firebase fallita: {e}")
        sys.exit(1)


def comment_doc_id(author: str, text: str) -> str:
    return hashlib.sha1(f"{author}|{text}".encode("utf-8")).hexdigest()


def save_comment(db, model: str, author: str, text: str,
                  timestamp: Optional[str], video_id: str) -> bool:
    doc_id = comment_doc_id(author, text)
    doc_ref = db.collection(FIRESTORE_COLLECTION).document(doc_id)
    if doc_ref.get().exists:
        return False
    doc_ref.set({
        "model": model,
        "author": author or "utente_anonimo",
        "text": text,
        "category": classify_category(text),
        "sentiment": classify_sentiment(text),
        "source": "YouTube",
        "source_url": f"https://www.youtube.com/watch?v={video_id}",
        "original_timestamp": timestamp,
        "scraped_at": firestore.SERVER_TIMESTAMP,
    })
    return True


# =============================================================================
# YOUTUBE DATA API
# =============================================================================

def get_youtube_client(api_key: str):
    return build("youtube", "v3", developerKey=api_key)


def search_videos(youtube, query: str, max_results: int = 5) -> list:
    """Cerca video pubblici per query (es. 'BMW X5 2026 review') e ritorna gli ID."""
    try:
        resp = youtube.search().list(
            q=query,
            part="id",
            type="video",
            maxResults=max_results,
            relevanceLanguage="it",
        ).execute()
        return [item["id"]["videoId"] for item in resp.get("items", [])]
    except HttpError as e:
        print(f"[ERRORE] Ricerca video fallita per '{query}': {e}")
        return []


def fetch_comments_for_video(youtube, video_id: str) -> list:
    """Ritorna lista di (author, text, timestamp) per un video, gestendo la paginazione."""
    results = []
    page_token = None

    try:
        while len(results) < MAX_COMMENTS_PER_VIDEO:
            resp = youtube.commentThreads().list(
                videoId=video_id,
                part="snippet",
                maxResults=100,
                order="relevance",
                pageToken=page_token,
                textFormat="plainText",
            ).execute()

            for item in resp.get("items", []):
                top = item["snippet"]["topLevelComment"]["snippet"]
                results.append((
                    top.get("authorDisplayName", "utente_anonimo"),
                    top.get("textDisplay", ""),
                    top.get("publishedAt"),
                ))

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    except HttpError as e:
        if e.resp.status == 403:
            print(f"[INFO] Commenti disabilitati o non accessibili per il video {video_id}.")
        else:
            print(f"[ERRORE] Recupero commenti fallito per {video_id}: {e}")

    return results


# =============================================================================
# MAIN
# =============================================================================

def parse_args():
    p = argparse.ArgumentParser(description="YouTube -> Firestore per dashboard live (gratuito, API ufficiale)")
    p.add_argument("--model", required=True, help='Nome modello come appare nella dashboard, es: "BMW X5"')
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--video-ids", nargs="+", help="Uno o più ID video YouTube (spazio-separati)")
    g.add_argument("--video-ids-csv", help="ID video separati da virgola in una singola stringa "
                                            "(usare questa forma se qualche ID inizia con '-': "
                                            "es. --video-ids-csv=-uHQ8U_xEf4,Wt6HuAmPMjo — evita "
                                            "qualsiasi ambiguità con argparse)")
    g.add_argument("--search", help="Query di ricerca per trovare video automaticamente")
    p.add_argument("--max-videos", type=int, default=5, help="Numero max video da --search")
    p.add_argument("--api-key", default=os.environ.get("YOUTUBE_API_KEY"),
                    help="Chiave API YouTube (o variabile d'ambiente YOUTUBE_API_KEY)")
    return p.parse_args()


def main():
    args = parse_args()
    if not args.api_key:
        print("[ERRORE] Nessuna chiave API YouTube. Passa --api-key oppure imposta YOUTUBE_API_KEY.")
        sys.exit(1)

    db = init_firestore()
    print(f"[FIREBASE] Connesso. Collezione target: '{FIRESTORE_COLLECTION}'")

    youtube = get_youtube_client(args.api_key)

    if args.video_ids:
        video_ids = args.video_ids
    elif args.video_ids_csv:
        video_ids = [v.strip() for v in args.video_ids_csv.split(",") if v.strip()]
    else:
        print(f"[YOUTUBE] Ricerca video per: '{args.search}'")
        video_ids = search_videos(youtube, args.search, args.max_videos)
        print(f"[YOUTUBE] Trovati {len(video_ids)} video: {video_ids}")

    if not video_ids:
        print("[ERRORE] Nessun video da elaborare.")
        sys.exit(1)

    total_new = 0
    for i, vid in enumerate(video_ids):
        print(f"\n--- [{i+1}/{len(video_ids)}] Video {vid} ---")
        comments = fetch_comments_for_video(youtube, vid)
        new_count = 0
        for author, text, timestamp in comments:
            if text and save_comment(db, args.model, author, text, timestamp, vid):
                new_count += 1
        print(f"[OK] {vid} -> {len(comments)} commenti letti, {new_count} NUOVI salvati su Firestore")
        total_new += new_count
        time.sleep(1)  # cortesia verso l'API, ben sotto i limiti di quota

    print(f"\n✔ Ciclo completato: {total_new} nuovi commenti su Firestore (modello: {args.model})")


if __name__ == "__main__":
    main()
