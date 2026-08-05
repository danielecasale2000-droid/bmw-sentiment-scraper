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
- Usa gemini-2.5-flash (free tier: 10 richieste/minuto, 250 richieste/giorno — giu 2026)

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
import threading

# Forza l'output "non bufferizzato": senza questo, se il processo viene
# interrotto a forza (es. timeout su GitHub Actions), tutte le righe di
# log stampate ma non ancora "svuotate" su schermo vanno perse — dando
# l'illusione che lo script non abbia fatto nulla, quando magari aveva
# già superato metà dei modelli.
sys.stdout.reconfigure(line_buffering=True)

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
GEMINI_MODEL = "gemini-2.5-flash"          # free tier: 10 richieste/minuto, 250/giorno (giu 2026)
MAX_COMMENTS_IN_PROMPT = 30                # ridotto da 120: meno commenti nel prompt = molti meno token in ingresso

BMW_MODELS = ["BMW X5", "BMW iX3", "BMW i4", "BMW i7", "BMW i3", "BMW X1", "BMW X3", "BMW Serie 5", "BMW Serie 1"]

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
    "Yangwang U7", "Yangwang U8", "Yangwang U9",
    "Xiaomi SU7", "Xiaomi YU7",
    "Geely Starray SAV", "Leapmotor T03 Hatchback",
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
    # timeout esplicito: senza, un problema di rete/credenziali può far
    # ritentare la libreria in silenzio per minuti prima di arrendersi
    snap = db.collection(COMMENTS_COLLECTION).where("model", "==", model).limit(500).get(timeout=30)
    return [d.to_dict() for d in snap]


def build_prompt(model, comments):
    pos = sum(1 for c in comments if c.get("sentiment") == "positive")
    neg = sum(1 for c in comments if c.get("sentiment") == "negative")
    total = len(comments)

    # Campione ridotto e commenti troncati più corti: qui si risparmiano
    # la maggior parte dei token in ingresso (che pesano più dell'output).
    sample = comments[:MAX_COMMENTS_IN_PROMPT]
    texts = []
    for c in sample:
        t = (c.get("text") or "").strip().replace("\n", " ")
        if t:
            texts.append("- " + t[:150])
    joined = "\n".join(texts)

    prompt = f"""Analizza questi commenti YouTube sul modello auto {model} ({total} totali, {pos} positivi, {neg} negativi).

{joined}

Rispondi SOLO con questo JSON, valori brevi (max 12 parole ciascuno), in italiano:
{{
  "design": {{"apprezzato": "...", "criticato": "..."}},
  "tecnico": {{"apprezzato": "...", "criticato": "..."}},
  "tecnologia": {{"apprezzato": "...", "criticato": "..."}},
  "sentiment_reale": "positivo o misto o negativo"
}}
Se un aspetto non è citato nei commenti, scrivi "non menzionato". Nessun testo fuori dal JSON."""
    return prompt


def generate_summary(model_client, model, comments):
    prompt = build_prompt(model, comments)
    resp = model_client.generate_content(
        prompt,
        request_options={"timeout": 60}  # fallisce dopo 60s invece di restare appeso
    )
    text = (resp.text or "").strip()
    text = text.replace("```json", "").replace("```", "").strip()
    empty_aspect = {"apprezzato": "non menzionato", "criticato": "non menzionato"}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"[WARN] Risposta non JSON valido per {model}, salvo un fallback vuoto.")
        return {"design": empty_aspect, "tecnico": empty_aspect, "tecnologia": empty_aspect,
                "sentiment_reale": "misto"}


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
    done, skipped, timed_out = 0, 0, 0
    PER_MODEL_HARD_TIMEOUT = 60  # secondi: oltre questo, si abbandona e si passa oltre

    def process_one(model, outcome):
        """Gira in un thread a parte: legge Firestore, chiama Gemini, salva.
        'outcome' è un dict condiviso in cui scrive il risultato — il thread
        principale lo legge SOLO se il join rientra nel timeout."""
        try:
            comments = fetch_comments(db, model)
        except Exception as e:
            outcome["error"] = f"lettura Firestore: {e}"
            return
        outcome["comments"] = comments
        if len(comments) < args.min_comments:
            return
        try:
            summary = generate_summary(model_client, model, comments)
            save_summary(db, model, summary, len(comments))
            outcome["summary"] = summary
        except Exception as e:
            outcome["error"] = f"generazione AI: {e}"

    for i, model in enumerate(models):
        print(f"[{i+1}/{len(models)}] {model}: lettura commenti da Firestore…")
        outcome = {}
        t = threading.Thread(target=process_one, args=(model, outcome), daemon=True)
        t.start()
        t.join(timeout=PER_MODEL_HARD_TIMEOUT)

        if t.is_alive():
            # Il thread è ancora bloccato (tipicamente un problema di rete/
            # libreria che ignora i timeout interni): lo abbandoniamo e
            # proseguiamo, così un solo modello non blocca l'intero giro.
            print(f"[{i+1}/{len(models)}] {model}: BLOCCATO oltre {PER_MODEL_HARD_TIMEOUT}s, salto e proseguo.")
            timed_out += 1
        elif "error" in outcome:
            print(f"[{i+1}/{len(models)}] {model}: ERRORE {outcome['error']}")
        elif "summary" not in outcome:
            n = len(outcome.get("comments", []))
            print(f"[{i+1}/{len(models)}] {model}: solo {n} commenti, salto.")
            skipped += 1
        else:
            n = len(outcome["comments"])
            print(f"[{i+1}/{len(models)}] {model}: riassunto generato ({n} commenti) -> {outcome['summary'].get('sentiment_reale','?')}")
            done += 1

        # free tier gemini-2.5-flash: 10 richieste/minuto -> almeno 6s tra una e l'altra
        time.sleep(6.5)

    print(f"\n✔ Completato: {done} riassunti generati, {skipped} saltati, {timed_out} bloccati/abbandonati.")


main()
