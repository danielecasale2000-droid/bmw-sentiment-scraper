#!/usr/bin/env python3
"""
ai_summaries.py
===============
Genera un riassunto AI di alta qualità per ciascun modello, usando l'API
GRATUITA di Google Gemini, e lo salva su Firestore. La dashboard legge
questi riassunti nella sezione "Approfondimento AI" — molto più accurati
della sintesi a parole chiave, perché un vero LLM capisce sarcasmo,
negazioni e contesto.

COME FUNZIONA (efficiente e gratuito):
- Una sola chiamata API per modello (non una per commento)
- Manda a Gemini un campione dei commenti + i conteggi di sentiment
- Riceve: cosa piace, cosa critica, giudizio complessivo, in italiano
- Salva su Firestore nella collezione "ai_summaries", documento = nome modello
- Usa gemini-1.5-flash (free tier: ~15 richieste/minuto, 1500/giorno)

PERCHE' GEMINI:
- Free tier permanente e generoso, ampiamente sufficiente per 66 modelli/giorno
- Stesso ecosistema Google/Firebase gia' in uso

SETUP:
    pip install google-generativeai firebase-admin
    # Chiave API GRATUITA: https://aistudio.google.com/app/apikey
    # Su GitHub: Settings -> Secrets -> GEMINI_API_KEY

USO:
    python ai_summaries.py                 # tutti i modelli (BMW + competitor)
    python ai_summaries.py --model "BMW X5"
    python ai_summaries.py --min-comments 10   # salta modelli con pochi commenti
"""

import argparse
import os
import sys
import json
import time

try:
    import google.generativeai as genai
except ImportError:
    print("[ERRORE] Manca google-generativeai. Installa con: pip install google-generativeai")
    sys.exit(1)

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    print("[ERRORE] Manca firebase-admin. Installa con: pip install firebase-admin")
    sys.exit(1)


SERVICE_ACCOUNT_FILE = "serviceAccountKey.json"
COMMENTS_COLLECTION = "comments"
SUMMARIES_COLLECTION = "ai_summaries"
GEMINI_MODEL = "gemini-1.5-flash"          # gratuito e veloce
MAX_COMMENTS_IN_PROMPT = 120               # campione max di commenti inviati per modello

BMW_MODELS = ["BMW X5", "BMW iX3", "BMW i4", "BMW i7", "BMW i3"]

COMPETITOR_MODELS = [
    "Avatr 12", "BYD Atto 2 DM-i", "BYD Atto 3", "BYD Han", "BYD Seal",
    "BYD Seal 06", "BYD Seal U", "BYD Sealion 7", "Changan Qiyuan A07",
    "Deepal L07", "Deepal S05", "Deepal S07", "Denza Z9 GT", "GWM Wey 03",
    "GWM Wey 05", "Geely EX5", "Geely Galaxy E8", "Genesis G80",
    "Genesis GV60", "Genesis GV70", "IM L7", "Jaecoo 7", "Jaecoo 8",
    "Leapmotor B05", "Leapmotor B10", "Leapmotor C10", "Lexus LBX",
    "Lexus NX", "Lexus RX", "Lexus RZ", "Lotus Eletre", "Lotus Emeya",
    "Lynk & Co 01", "Lynk & Co 02", "Lynk & Co 08", "MG HS", "MG IM5-IM6",
    "MG S5", "Mazda CX-60", "Mazda CX-80", "NIO EL6", "NIO ET5", "NIO ET7",
    "NIO ET9", "Omoda 5", "Omoda 7", "Omoda 9", "Polestar 2", "Polestar 3",
    "Polestar 4", "Polestar 5", "Smart 5", "Tesla Model 3", "Tesla Model Y",
    "Volvo ES90", "Volvo EX40", "Volvo EX60", "Volvo EX90", "Volvo XC60",
    "Volvo XC90", "Xpeng G6",
]


def init_firestore():
    try:
        cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
        firebase_admin.initialize_app(cred)
        return firestore.client()
    except FileNotFoundError:
        print(f"[ERRORE] File '{SERVICE_ACCOUNT_FILE}' non trovato.")
        sys.exit(1)
    except Exception as e:
        print(f"[ERRORE] Init Firebase fallita: {e}")
        sys.exit(1)


def fetch_comments(db, model):
    snap = db.collection(COMMENTS_COLLECTION).where("model", "==", model).limit(500).get()
    return [d.to_dict() for d in snap]


def build_prompt(model, comments):
    pos = sum(1 for c in comments if c.get("sentiment") == "positive")
    neg = sum(1 for c in comments if c.get("sentiment") == "negative")
    neu = sum(1 for c in comments if c.get("sentiment") == "neutral")
    total = len(comments)

    sample = comments[:MAX_COMMENTS_IN_PROMPT]
    texts = []
    for c in sample:
        t = (c.get("text") or "").strip().replace("\n", " ")
        if t:
            texts.append("- " + (t[:300]))
    joined = "\n".join(texts)

    prompt = f"""Sei un analista di customer experience per il settore automotive, esperto nel
sintetizzare grandi quantità di commenti in un giudizio chiaro e leggibile per un dirigente
che ha poco tempo.

Analizza i commenti reali degli utenti sul modello {model}, raccolti da video YouTube di recensioni.

Dati aggregati: {total} commenti totali ({pos} positivi, {neg} negativi, {neu} neutri).

Ecco un campione dei commenti:
{joined}

Scrivi un'analisi in ITALIANO, in formato JSON con esattamente questi campi:
{{
  "riassunto_positivo": "un paragrafo di 2-3 frasi complete e discorsive che riassume in modo specifico e concreto cosa apprezzano davvero gli utenti — non frasi generiche, cita gli aspetti reali che emergono dai commenti (es. design, prestazioni, prezzo, un dettaglio tecnico preciso). Se non ci sono commenti positivi significativi, scrivi una frase che lo dica onestamente.",
  "riassunto_negativo": "un paragrafo di 2-3 frasi complete e discorsive che riassume in modo specifico e concreto le critiche principali — stessa profondità del riassunto positivo, con dettagli reali. Se non ci sono critiche significative, scrivi una frase che lo dica onestamente.",
  "temi_ricorrenti": ["2-3 argomenti concreti che tornano spesso, frasi brevi"],
  "sentiment_reale": "positivo oppure misto oppure negativo"
}}

Regole importanti:
- I due riassunti (positivo e negativo) sono il cuore dell'analisi: devono essere specifici,
  informativi, mai vaghi o intercambiabili tra un modello e l'altro. Evita frasi da bigliettino
  come "gli utenti sono generalmente soddisfatti" senza dire di cosa.
- Se i commenti positivi e negativi toccano la STESSA categoria generale (es. entrambi parlano
  di tecnologia), va benissimo — ma specifica ASPETTI DIVERSI e concreti all'interno di quella
  categoria per ciascun lato (es. positivo: "il sistema di infotainment è reattivo"; negativo:
  "il software presenta bug nell'aggiornamento").
- Riconosci sarcasmo, ironia e negazioni (es. "non è male" è positivo).
- Basati SOLO sui commenti forniti, non inventare fatti che non ci sono.
- Rispondi unicamente con il JSON, senza altro testo, senza markdown."""
    return prompt


def generate_summary(model_client, model, comments):
    prompt = build_prompt(model, comments)
    resp = model_client.generate_content(prompt)
    text = (resp.text or "").strip()
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"[WARN] Risposta non JSON valido per {model}, salvo come testo grezzo.")
        return {"riassunto_positivo": text[:500], "riassunto_negativo": "",
                "temi_ricorrenti": [], "sentiment_reale": "misto"}


def save_summary(db, model, summary, comment_count):
    doc_id = model.replace("/", "-")
    db.collection(SUMMARIES_COLLECTION).document(doc_id).set({
        "model": model,
        "summary": summary,
        "based_on_comments": comment_count,
        "generated_at": firestore.SERVER_TIMESTAMP,
        "generated_by": GEMINI_MODEL,
    })


def parse_args():
    p = argparse.ArgumentParser(description="Genera riassunti AI per modello (Gemini) e li salva su Firestore")
    p.add_argument("--model", help="Un solo modello (default: tutti)")
    p.add_argument("--min-comments", type=int, default=5,
                   help="Salta i modelli con meno commenti di questo (default 5)")
    p.add_argument("--api-key", default=os.environ.get("GEMINI_API_KEY"),
                   help="Chiave API Gemini (o variabile GEMINI_API_KEY)")
    return p.parse_args()


def main():
    args = parse_args()
    if not args.api_key:
        print("[ERRORE] Nessuna chiave API Gemini. Passa --api-key o imposta GEMINI_API_KEY.")
        print("         Ottienila gratis su: https://aistudio.google.com/app/apikey")
        sys.exit(1)

    db = init_firestore()
    genai.configure(api_key=args.api_key)
    model_client = genai.GenerativeModel(GEMINI_MODEL)
    print(f"[SETUP] Firestore connesso. Modello AI: {GEMINI_MODEL} (Google Gemini, free tier)")

    models = [args.model] if args.model else (BMW_MODELS + COMPETITOR_MODELS)
    done, skipped = 0, 0

    for i, model in enumerate(models):
        comments = fetch_comments(db, model)
        if len(comments) < args.min_comments:
            print(f"[{i+1}/{len(models)}] {model}: solo {len(comments)} commenti, salto.")
            skipped += 1
            continue
        try:
            summary = generate_summary(model_client, model, comments)
            save_summary(db, model, summary, len(comments))
            print(f"[{i+1}/{len(models)}] {model}: riassunto generato ({len(comments)} commenti) -> {summary.get('sentiment_reale','?')}")
            done += 1
        except Exception as e:
            print(f"[{i+1}/{len(models)}] {model}: ERRORE {e}")
        # free tier: ~15 richieste/minuto -> pausa di ~4.5s per stare tranquilli
        time.sleep(4.5)

    print(f"\n✔ Completato: {done} riassunti generati, {skipped} saltati.")


main()
