#!/usr/bin/env python3
"""
scraper_firestore.py
====================
Versione dello scraper che salva i commenti direttamente su Google Firestore,
per alimentare in tempo reale la dashboard "Market Sentiment Cockpit".

Ogni commento viene:
1. Estratto dalla pagina (Quattroruote.it / AlVolante.it)
2. Classificato automaticamente (categoria: Tecnico/Estetico/Social, sentiment)
3. Scritto su Firestore nella collezione "comments" con un ID deterministico
   (hash di autore+testo) per evitare duplicati tra esecuzioni successive.

La dashboard ascolta la collezione con onSnapshot() e mostra i nuovi commenti
in diretta, senza bisogno di alcun backend intermedio.

SETUP (una tantum):
    pip install playwright firebase-admin
    playwright install chromium

    1. Vai su https://console.firebase.google.com -> il tuo progetto
       -> Impostazioni progetto -> Account di servizio
       -> "Genera nuova chiave privata" -> salva il file come
       serviceAccountKey.json NELLA STESSA CARTELLA di questo script.
       (NON condividere mai questo file e non caricarlo su repository pubblici!)

    2. In Firestore crea (o lascia creare allo script) la collezione "comments".

USO:
    python scraper_firestore.py --model "BMW X5" --urls urls.txt
    python scraper_firestore.py --model "BMW iX3" --url "https://www.quattroruote.it/..."

ESECUZIONE PERIODICA (per il live continuo):
    - Windows: Utilità di pianificazione -> nuova attività ogni 15 minuti
    - Mac/Linux: crontab -e ->  */15 * * * * cd /percorso && python scraper_firestore.py --model "BMW X5" --urls urls.txt
"""

import argparse
import hashlib
import json
import random
import sys
import time
import urllib.robotparser
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    print("[ERRORE] Manca firebase-admin. Installa con: pip install firebase-admin")
    sys.exit(1)

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# =============================================================================
# CONFIGURAZIONE
# =============================================================================

SERVICE_ACCOUNT_FILE = "serviceAccountKey.json"
FIRESTORE_COLLECTION = "comments"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

MIN_DELAY_SEC = 2.5
MAX_DELAY_SEC = 5.5
DEFAULT_MAX_SCROLLS = 15
DEFAULT_TIMEOUT_MS = 15000

# NOTA: verificare/aggiornare i selettori ispezionando il DOM reale delle pagine.
SITE_CONFIG = {
    "quattroruote.it": {
        "engine": "disqus",
        "disqus_iframe_selector": "iframe[src*='disqus.com']",
        "load_more_selector": "text=/carica altri|load more/i",
        "comment_selector": "[id^='comment-']",
        "author_selector": ".author .fullname, .author",
        "text_selector": ".post-message, .post-body",
        "time_selector": "time, .publication-time",
    },
    "alvolante.it": {
        "engine": "generic",
        "load_more_selector": "text=/mostra altri commenti|carica altri|vedi altri/i",
        "comment_selector": "[class*='comment-item'], [class*='comment_item'], article.comment",
        "author_selector": "[class*='comment-author'], [class*='author']",
        "text_selector": "[class*='comment-text'], [class*='comment-body'], p",
        "time_selector": "time, [class*='comment-date']",
    },
}


# =============================================================================
# CLASSIFICATORE (identico a quello della dashboard)
# =============================================================================

TECNICO_WORDS = ['motore','autonomia','batteri','ricaric','kw','kwh','coppia','consum','prestazion','cambio','trazione','sospension','telaio','cruscotto','infotainment','elettric','ibrid','diesel','benzin','potenza','peso','piattaforma','powertrain','propuls','freni','impianto','navigatore','display']
ESTETICO_WORDS = ['design','fanal','fari','muso','frontale','rene','calandr','linee','forme','stile','cerchi','posteriore','coda','proporzion','esterni','estetic','carrozzeria','profilo','look']
POS_WORDS = ['bello','bellissim','stupend','top','capolavoro','ottim','perfett','ador','meravigli','favolos','incredibil','wow','promoss','azzeccat','riuscit','elegante','sublime','geniale','pazzesc','fenomenal','magnific']
NEG_WORDS = ['brutt','orrend','orribil','inguardabil','schifo','vergogn','delusion','deluso','peggio','terribl','disastr','ridicol','improponibil','agghiacciant','indecent','nauseante','pessim','fallimento','copiat','cinesat','scandal','orrore','oscen','scempio']
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


def source_from_domain(domain: str) -> str:
    if "quattroruote" in domain:
        return "Quattroruote"
    if "alvolante" in domain:
        return "AlVolante"
    return domain


# =============================================================================
# FIRESTORE
# =============================================================================

def init_firestore():
    try:
        cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
        firebase_admin.initialize_app(cred)
        return firestore.client()
    except FileNotFoundError:
        print(f"[ERRORE] File '{SERVICE_ACCOUNT_FILE}' non trovato.\n"
              f"Scaricalo da: Console Firebase -> Impostazioni progetto -> "
              f"Account di servizio -> Genera nuova chiave privata.")
        sys.exit(1)
    except Exception as e:
        print(f"[ERRORE] Inizializzazione Firebase fallita: {e}")
        sys.exit(1)


def comment_doc_id(author: str, text: str) -> str:
    """ID deterministico: stesso commento -> stesso documento -> nessun duplicato."""
    return hashlib.sha1(f"{author}|{text}".encode("utf-8")).hexdigest()


def save_comment(db, model: str, author: str, text: str,
                 timestamp: Optional[str], url: str, domain: str) -> bool:
    """Salva su Firestore. Ritorna True se il commento è NUOVO."""
    doc_id = comment_doc_id(author, text)
    doc_ref = db.collection(FIRESTORE_COLLECTION).document(doc_id)

    if doc_ref.get().exists:
        return False  # già presente, niente duplicati

    doc_ref.set({
        "model": model,
        "author": author or "utente_anonimo",
        "text": text,
        "category": classify_category(text),
        "sentiment": classify_sentiment(text),
        "source": source_from_domain(domain),
        "source_url": url,
        "original_timestamp": timestamp,
        "scraped_at": firestore.SERVER_TIMESTAMP,
    })
    return True


# =============================================================================
# ROBOTS.TXT + SCRAPING (come nella versione JSON)
# =============================================================================

def is_scraping_allowed(url: str, user_agent: str = "*") -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(robots_url)
    try:
        rp.read()
    except Exception as e:
        print(f"[ATTENZIONE] Impossibile leggere {robots_url} ({e}).")
        return True
    allowed = rp.can_fetch(user_agent, url)
    if not allowed:
        print(f"[BLOCCATO DA ROBOTS.TXT] {url}")
    return allowed


def random_delay(min_s=MIN_DELAY_SEC, max_s=MAX_DELAY_SEC):
    time.sleep(random.uniform(min_s, max_s))


def get_domain_config(url: str):
    domain = urlparse(url).netloc.replace("www.", "")
    for key, cfg in SITE_CONFIG.items():
        if key in domain:
            return cfg, domain
    return None, domain


def safe_text(locator) -> str:
    try:
        return locator.inner_text().strip()
    except Exception:
        return ""


def extract_comments_from_container(container, cfg, url, domain):
    """Estrae i commenti da un frame Disqus o dalla pagina stessa."""
    results = []
    try:
        container.wait_for_selector(cfg["comment_selector"], timeout=DEFAULT_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        print(f"[INFO] Nessun commento trovato su {url}.")
        return results

    for node in container.locator(cfg["comment_selector"]).all():
        try:
            author = safe_text(node.locator(cfg["author_selector"]).first)
            text = safe_text(node.locator(cfg["text_selector"]).first)
            timestamp = None
            tnode = node.locator(cfg["time_selector"]).first
            if tnode.count() > 0:
                timestamp = tnode.get_attribute("datetime") or safe_text(tnode)
            if text:
                results.append((author, text, timestamp))
        except Exception as e:
            print(f"[WARN] Parsing commento fallito su {url}: {e}")
            continue
    return results


def click_load_more(container, cfg):
    clicks = 0
    while clicks < DEFAULT_MAX_SCROLLS:
        try:
            btn = container.locator(cfg["load_more_selector"]).first
            if btn.is_visible(timeout=2000):
                btn.click()
                clicks += 1
                random_delay(1.0, 2.5)
            else:
                break
        except Exception:
            break


def accept_cookie_banner(page):
    """Prova a chiudere il banner GDPR: senza consenso, gli script di terze
    parti (Disqus incluso) spesso non vengono nemmeno caricati."""
    consent_selectors = [
        "button:has-text('Accetta e chiudi')",
        "button:has-text('Accetta tutto')",
        "button:has-text('ACCETTA')",
        "button:has-text('Accetta')",
        "button:has-text('Accept all')",
        "button:has-text('Accept')",
        "#onetrust-accept-btn-handler",
        "button#didomi-notice-agree-button",
        ".iubenda-cs-accept-btn",
        "[class*='consent'] button[class*='accept']",
    ]
    # 1) banner nel documento principale
    for sel in consent_selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1500):
                btn.click()
                print("[COOKIE] Banner consensi accettato (pagina principale).")
                random_delay(1.0, 2.0)
                return True
        except Exception:
            continue
    # 2) banner dentro un iframe (es. Sourcepoint/TCF usato da molti editori)
    for frame in page.frames:
        furl = (frame.url or "").lower()
        if any(k in furl for k in ("consent", "sourcepoint", "privacy", "cmp")):
            for sel in consent_selectors + ["button[title*='Accetta']", "button[title*='ACCETTA']"]:
                try:
                    btn = frame.locator(sel).first
                    if btn.is_visible(timeout=1500):
                        btn.click()
                        print("[COOKIE] Banner consensi accettato (iframe CMP).")
                        random_delay(1.0, 2.0)
                        return True
                except Exception:
                    continue
    print("[COOKIE] Nessun banner consensi rilevato (o già accettato).")
    return False


def scroll_to_bottom(page, steps=12):
    """Scroll progressivo fino in fondo: molti siti caricano Disqus in lazy-load
    solo quando la sezione commenti entra nella viewport."""
    for _ in range(steps):
        page.mouse.wheel(0, 1600)
        random_delay(0.4, 0.9)
    # se esiste il contenitore Disqus, portalo esplicitamente in vista
    try:
        thread = page.locator("#disqus_thread").first
        if thread.count() > 0:
            thread.scroll_into_view_if_needed(timeout=3000)
            random_delay(1.0, 2.0)
    except Exception:
        pass


def open_comments_section(page):
    """Su Quattroruote (e siti simili) l'iframe Disqus NON è nel DOM iniziale:
    va aperto cliccando esplicitamente il link 'LEGGI TUTTI I COMMENTI' /
    'COMMENTA', che dinamicamente inietta il widget nella pagina."""
    selectors = [
        "text=/LEGGI TUTTI I COMMENTI/i",
        "a:has-text('LEGGI TUTTI I COMMENTI')",
        "text=/COMMENTA/i",
        "a:has-text('COMMENTA')",
    ]
    for sel in selectors:
        try:
            link = page.locator(sel).first
            if link.is_visible(timeout=2000):
                link.scroll_into_view_if_needed()
                link.click()
                print("[COMMENTI] Cliccato il link per aprire la sezione commenti.")
                random_delay(1.5, 2.5)
                return True
        except Exception:
            continue
    print("[COMMENTI] Nessun link 'apri commenti' trovato (forse già aperti o pagina senza sezione commenti).")
    return False


def scrape_url(browser, db, model: str, url: str) -> int:
    """Ritorna il numero di commenti NUOVI salvati su Firestore."""
    cfg, domain = get_domain_config(url)
    if cfg is None:
        print(f"[SKIP] Dominio non configurato: {url}")
        return 0
    if not is_scraping_allowed(url):
        return 0

    context = browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        viewport={"width": 1366, "height": 900},
        locale="it-IT",
    )
    page = context.new_page()
    new_count = 0

    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        random_delay(1.5, 2.5)

        # 1) chiudi il banner GDPR: senza consenso Disqus spesso non si carica
        accept_cookie_banner(page)

        # 2) scroll progressivo fino alla sezione commenti (attiva il lazy-load)
        scroll_to_bottom(page, steps=14)

        # 3) apri esplicitamente la sezione commenti (su Quattroruote l'iframe
        #    Disqus non esiste finché non si clicca "LEGGI TUTTI I COMMENTI")
        open_comments_section(page)
        random_delay(1.0, 2.0)

        if cfg["engine"] == "disqus":
            frame = None
            try:
                iframe_el = page.wait_for_selector(cfg["disqus_iframe_selector"],
                                                    timeout=DEFAULT_TIMEOUT_MS)
                frame = iframe_el.content_frame()
            except PlaywrightTimeoutError:
                pass

            if frame is None:
                # Retry: alcuni siti caricano Disqus solo al secondo giro di
                # scroll, o il banner è comparso di nuovo dopo un redirect.
                print(f"[RETRY] Iframe Disqus non trovato al primo tentativo su {url}, riprovo...")
                accept_cookie_banner(page)
                scroll_to_bottom(page, steps=10)
                open_comments_section(page)
                random_delay(1.5, 2.5)
                try:
                    iframe_el = page.wait_for_selector(cfg["disqus_iframe_selector"],
                                                        timeout=DEFAULT_TIMEOUT_MS)
                    frame = iframe_el.content_frame()
                except PlaywrightTimeoutError:
                    print(f"[INFO] Nessun iframe Disqus su {url} (nessun commento o thread disattivato).")
                    return 0

            if frame is None:
                print(f"[ATTENZIONE] Iframe Disqus individuato ma inaccessibile su {url}")
                return 0

            click_load_more(frame, cfg)
            raw = extract_comments_from_container(frame, cfg, url, domain)
        else:
            click_load_more(page, cfg)
            # scroll infinito di sicurezza
            prev = 0
            for _ in range(DEFAULT_MAX_SCROLLS):
                page.mouse.wheel(0, 2000)
                random_delay(0.8, 1.8)
                h = page.evaluate("document.body.scrollHeight")
                if h == prev:
                    break
                prev = h
            raw = extract_comments_from_container(page, cfg, url, domain)

        for author, text, timestamp in raw:
            if save_comment(db, model, author, text, timestamp, url, domain):
                new_count += 1

        print(f"[OK] {url} -> {len(raw)} commenti letti, {new_count} NUOVI salvati su Firestore")

    except PlaywrightTimeoutError:
        print(f"[TIMEOUT] {url}")
    except Exception as e:
        print(f"[ERRORE] {url}: {e}")
    finally:
        context.close()

    return new_count


# =============================================================================
# MAIN
# =============================================================================

def parse_args():
    p = argparse.ArgumentParser(description="Scraper -> Firestore per dashboard live")
    p.add_argument("--model", required=True,
                   help='Nome del modello come appare nella dashboard, es: "BMW X5"')
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--url", help="Singolo URL")
    g.add_argument("--urls", help="File con un URL per riga")
    p.add_argument("--no-headless", action="store_true", help="Mostra il browser (debug)")
    p.add_argument("--loop", type=int, default=0, metavar="MINUTI",
                   help="Se > 0, ripete lo scraping ogni N minuti (live continuo)")
    return p.parse_args()


def load_urls(args):
    if args.url:
        return [args.url]
    with open(args.urls, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.startswith("#")]


def run_once(db, model, urls, headless):
    total_new = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        for i, url in enumerate(urls):
            print(f"\n--- [{i+1}/{len(urls)}] {url} ---")
            try:
                total_new += scrape_url(browser, db, model, url)
            except Exception as e:
                print(f"[ERRORE CRITICO] URL saltato: {url} ({e})")
            if i < len(urls) - 1:
                random_delay()
        browser.close()
    print(f"\n✔ Ciclo completato: {total_new} nuovi commenti su Firestore "
          f"(modello: {model})")
    return total_new


def main():
    args = parse_args()
    urls = load_urls(args)
    if not urls:
        print("[ERRORE] Nessun URL da elaborare.")
        sys.exit(1)

    db = init_firestore()
    print(f"[FIREBASE] Connesso. Collezione target: '{FIRESTORE_COLLECTION}'")

    if args.loop > 0:
        print(f"[LIVE] Modalità continua: scraping ogni {args.loop} minuti. CTRL+C per fermare.")
        while True:
            run_once(db, args.model, urls, headless=not args.no_headless)
            print(f"[LIVE] In attesa {args.loop} minuti...\n")
            time.sleep(args.loop * 60)
    else:
        run_once(db, args.model, urls, headless=not args.no_headless)


if __name__ == "__main__":
    main()
