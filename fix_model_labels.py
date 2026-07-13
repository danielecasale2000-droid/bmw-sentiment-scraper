#!/usr/bin/env python3
"""
fix_model_labels.py
====================
Correzione una tantum: alcuni commenti sono stati salvati su Firestore con
il campo "model" sbagliato (es. commenti su un video BMW i7 etichettati come
"BMW X5"), perché più video erano stati passati insieme sotto lo stesso
--model nella prima esecuzione dello scraper YouTube.

Questo script NON tocca il testo/autore/sentiment dei commenti: aggiorna
solo il campo "model" dei documenti il cui source_url corrisponde a un
video che sappiamo appartenere a un altro modello.

USO:
    pip install firebase-admin   (se non già installato)
    python fix_model_labels.py
"""

import sys

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    print("[ERRORE] Manca firebase-admin. Installa con: pip install firebase-admin")
    sys.exit(1)

SERVICE_ACCOUNT_FILE = "serviceAccountKey.json"
FIRESTORE_COLLECTION = "comments"

# Mappa: ID video YouTube -> modello CORRETTO a cui appartiene davvero
VIDEO_ID_TO_CORRECT_MODEL = {
    "iNhLIrK_xoU": "BMW X5",
    "hovl1LbuTjc": "BMW iX3",
    "IBqtfZIeFK8": "BMW i7",
    "YcCCbAmf_XI": "BMW i3",
    "-SbbyrUv9tY": "BMW i4",
}


def init_firestore():
    try:
        cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
        firebase_admin.initialize_app(cred)
        return firestore.client()
    except FileNotFoundError:
        print(f"[ERRORE] File '{SERVICE_ACCOUNT_FILE}' non trovato nella cartella corrente.")
        sys.exit(1)
    except Exception as e:
        print(f"[ERRORE] Inizializzazione Firebase fallita: {e}")
        sys.exit(1)


def main():
    db = init_firestore()
    print(f"[FIREBASE] Connesso. Collezione target: '{FIRESTORE_COLLECTION}'\n")

    total_checked = 0
    total_fixed = 0

    for video_id, correct_model in VIDEO_ID_TO_CORRECT_MODEL.items():
        source_url = f"https://www.youtube.com/watch?v={video_id}"
        docs = db.collection(FIRESTORE_COLLECTION).where("source_url", "==", source_url).stream()

        video_checked = 0
        video_fixed = 0

        for doc in docs:
            video_checked += 1
            data = doc.to_dict()
            current_model = data.get("model")

            if current_model != correct_model:
                doc.reference.update({"model": correct_model})
                video_fixed += 1

        total_checked += video_checked
        total_fixed += video_fixed
        print(f"[{video_id}] -> {correct_model}: {video_checked} commenti trovati, "
              f"{video_fixed} corretti (gli altri erano già giusti)")

    print(f"\n✔️ Completato: {total_checked} commenti controllati, {total_fixed} etichette corrette.")

if _name_ == "_main_":
    main()
