import requests
import json
import os
import re
import time
import datetime
import logging
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


class BazosFetchError(Exception):
    """Vyhadzuje sa keď fetch_with_retry vyčerpá všetky pokusy."""
    pass


TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

# Pridaj sem dalsie CHAT_ID ak chces posielat viacerym ludom
# Oddeľ čiarkou, napr: "111111111,222222222"
EXTRA_CHAT_IDS = os.environ.get("EXTRA_CHAT_IDS", "")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36"}
SEEN_FILE = "seen_ads.json"
PSC = "81105"
OKRUH = "15"
SK_TZ = ZoneInfo("Europe/Bratislava")

# Vsetky hladane slova - variacie, typy veci, slova oznacujuce darovanie
SEARCH_WORDS = [
    # iPhone
    "iphone", "iphon", "ajfon",

    # iPad
    "ipad", "iped",

    # MacBook
    "macbook", "macbok",

    # Apple Watch
    "apple watch",

    # AirPods
    "airpods", "airpod",

    # Ostatné Apple
    "imac", "mac mini", "apple tv", "homepod",

    # Bicykle
    "bicykel", "bycikel", "bicikl", "bickel",
    "bike", "mtb", "bmx", "ebike", "e-bike",
    "trek", "specialized",

    # Kolobežky
    "kolobezka", "kolobežka", "scooter",
    "elektricka kolobezka", "elektrická kolobežka",
    "e-kolobezka", "e-kolobežka",

    # IKEA
    "ikea",

    # Detské veci
    "prebalovaci stol", "prebaľovací stôl",
    "detska postielka", "detská postieľka",
    "autosedacka", "autosedačka",
    "kocarik", "kočiarik",
    "hojdacka", "hojdačka",
    "detska stolička", "detska stolicka",
]


def empty_db():
    """Vráti prázdnu DB štruktúru s dnešnými stats."""
    today = datetime.datetime.now(SK_TZ).date().isoformat()
    return {
        "ads": {},
        "last_run": None,
        "last_health_check_date": None,
        "stats": {
            "today": {
                "date": today,
                "runs": 0,
                "successful_runs": 0,
                "failed_runs": 0,
                "new_ads_found": 0,
                "notifications_sent": 0,
                "ads_pruned": 0,
                "last_error": None,
            }
        },
    }


def migrate_legacy_format(data):
    """Deteguje starý formát (list URL) a migruje na nový dict formát."""
    if not isinstance(data, list):
        return data

    logging.info(f"Migrácia: {len(data)} záznamov zo starého formátu...")
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    db = empty_db()
    skipped = 0

    for url in data:
        match = re.search(r"/inzerat/(\d+)/", url)
        if not match:
            logging.warning(f"Migrácia: preskočená URL bez /inzerat/<číslo>/ formátu: {url}")
            skipped += 1
            continue

        ad_id = match.group(1)
        db["ads"][ad_id] = {
            "title": "<migrated>",
            "url": url,
            "search_word": "<migrated>",
            "price": "zadarmo",
            "posted_date": None,
            "first_seen": now,
            "last_seen": now,
            "notified": True,
            "notified_at": now,
            "missing_count": 0,
        }

    logging.info(f"Migrácia hotová: {len(db['ads'])} migrovaných, {skipped} preskočených")
    return db


def load_db():
    """Načíta DB zo súboru, prípadne migruje zo starého formátu."""
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            data = json.load(f)
        return migrate_legacy_format(data)
    return empty_db()


def save_db(db):
    """Uloží DB do súboru."""
    with open(SEEN_FILE, "w") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def get_all_chat_ids():
    ids = []
    if CHAT_ID:
        ids.append(CHAT_ID)
    if EXTRA_CHAT_IDS:
        for cid in EXTRA_CHAT_IDS.split(","):
            cid = cid.strip()
            if cid and cid not in ids:
                ids.append(cid)
    return ids


def fetch_with_retry(url, max_retries=3):
    """GET na Bazos s retry logikou a HTML validáciou."""
    backoff = [5, 15, 45]
    last_reason = ""
    for attempt in range(max_retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            r.encoding = "utf-8"
            title_tag = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.IGNORECASE | re.DOTALL)
            title_text = title_tag.group(1) if title_tag else ""
            if "bazos" not in title_text.lower():
                last_reason = f"neplatná stránka (title: {title_text[:60]!r})"
                raise ValueError(last_reason)
            return r
        except Exception as e:
            last_reason = str(e)
            logging.warning(f"fetch_with_retry pokus {attempt + 1}/{max_retries} zlyhal: {last_reason}")
            if attempt < max_retries - 1:
                time.sleep(backoff[attempt])
    raise BazosFetchError(f"Po {max_retries} pokusoch zlyhalo: {last_reason}")


def send_telegram_with_retry(msg):
    """Pošle správu na všetky chat_id s retry logikou. Vracia True ak aspoň jeden uspel."""
    backoff = [2, 5, 10]
    tg_url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    any_ok = False
    for cid in get_all_chat_ids():
        success = False
        for attempt in range(3):
            try:
                r = requests.post(tg_url, data={"chat_id": cid, "text": msg}, timeout=10)
                r.raise_for_status()
                success = True
                break
            except Exception as e:
                logging.warning(f"Telegram pokus {attempt + 1}/3 pre {cid} zlyhal: {e}")
                if attempt < 2:
                    time.sleep(backoff[attempt])
        if success:
            any_ok = True
        else:
            logging.error(f"Telegram: všetky 3 pokusy zlyhali pre chat_id {cid}")
    return any_ok


def is_free(price_text):
    p = price_text.lower().strip()
    free_values = ["0 €", "0€", "0 eur", "zdarma", "zadarmo", "", "0"]
    return p in free_values or "zdarma" in p or "zadarmo" in p


def parse_bazos_date(date_text):
    """Parsuje dátum z Bazosu formátu '- [D.M. YYYY]' na datetime.date, alebo None."""
    if not date_text:
        logging.warning("parse_bazos_date: prázdny text")
        return None
    cleaned = date_text.strip().strip("-").strip().strip("[]").strip()
    match = re.search(r"(\d{1,2})\.(\d{1,2})\.\s*(\d{4})", cleaned)
    if not match:
        logging.warning(f"parse_bazos_date: nedá sa parsovať '{date_text}'")
        return None
    day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    try:
        return datetime.date(year, month, day)
    except ValueError:
        logging.warning(f"parse_bazos_date: neplatný dátum z textu '{date_text}'")
        return None


def should_send_health_check(db, today_sk_str, current_hour):
    """Vráti True ak je čas na denný health check a dnes ešte nebol odoslaný."""
    return current_hour >= 20 and db["last_health_check_date"] != today_sk_str


def send_health_check(db, today_sk_str):
    """Zostaví a odošle denný health check. Uloží poistku PRED odoslaním."""
    stats = db["stats"]["today"]

    if db["last_run"] is None:
        last_run_fmt = "nikdy"
    else:
        last_run_fmt = (
            datetime.datetime.fromisoformat(db["last_run"])
            .astimezone(SK_TZ)
            .strftime("%H:%M")
        )

    msg = (
        f"🟢 Bazos scanner - denný report\n\n"
        f"Dnes:\n"
        f"- {stats['runs']} behov ({stats['successful_runs']} úspešných, {stats['failed_runs']} chýb)\n"
        f"- {stats['new_ads_found']} nových inzerátov nájdených\n"
        f"- {stats['notifications_sent']} odoslaných do Telegramu\n"
        f"- {stats['ads_pruned']} vymazaných z DB\n\n"
        f"V DB aktívnych: {len(db['ads'])}\n"
        f"Posledná chyba: {stats['last_error'] or 'žiadna'}\n"
        f"Posledný beh: {last_run_fmt}"
    )

    # Poistka pred odoslaním ... aj keď Telegram zlyhá, dnes sa health check neopakuje
    db["last_health_check_date"] = today_sk_str
    save_db(db)

    send_telegram_with_retry(msg)
    logging.info("Health check odoslaný")


def get_description(ad):
    """Pokus o ziskanie popisu inzeratu."""
    desc_el = (
        ad.select_one(".popis") or
        ad.select_one(".inzeratypopis") or
        ad.select_one("div.maincontent p")
    )
    if desc_el:
        text = desc_el.get_text(strip=True)
        # Skrat popis na max 200 znakov
        return text[:200] + "..." if len(text) > 200 else text
    return ""


def check():
    db = load_db()
    new_count = 0
    today_sk = datetime.datetime.now(SK_TZ).date()
    today_sk_str = today_sk.isoformat()
    current_hour = datetime.datetime.now(SK_TZ).hour
    seen_this_run = set()

    # Reset denných stats ak prišla polnoc a dátum sa zmenil
    if db["stats"]["today"]["date"] != today_sk_str:
        db["stats"]["today"] = {
            "date": today_sk_str,
            "runs": 0,
            "successful_runs": 0,
            "failed_runs": 0,
            "new_ads_found": 0,
            "notifications_sent": 0,
            "ads_pruned": 0,
            "last_error": None,
        }

    had_fetch_error = False
    db["stats"]["today"]["runs"] += 1

    for word in SEARCH_WORDS:
        logging.info(f"Skenujem: {word}")
        try:
            url = (
                f"https://www.bazos.sk/search.php"
                f"?hledat={requests.utils.quote(word)}"
                f"&rubriky=www"
                f"&hlokalita={PSC}"
                f"&humkreis={OKRUH}"
                f"&cenaod=0"
                f"&cenado=0"
                f"&Submit=H%C4%BEada%C5%A5"
            )

            r = fetch_with_retry(url)
            soup = BeautifulSoup(r.text, "html.parser")

            ads = soup.select(".inzeraty") or soup.find_all("div", class_=lambda c: c and "inzerat" in c.split())

            for ad in ads:
                try:
                    title_el = ad.select_one("h2 a") or ad.select_one(".nadpis")
                    if not title_el:
                        continue

                    title = title_el.text.strip()
                    href = title_el.get("href", "")
                    if not href:
                        continue

                    # Extrahuj numerické ID ... URL bez čísla preskočíme
                    ad_id_match = re.search(r"/inzerat/(\d+)/", href)
                    if not ad_id_match:
                        logging.warning(f"Preskočený inzerát bez /inzerat/<číslo>/ v href: {href}")
                        continue
                    ad_id = ad_id_match.group(1)

                    # Zaznač videnie PRED všetkými filtrami ... aj odmietnutý inzerát
                    # bol reálne videný na stránke, GC ho nesmie považovať za zmiznutý
                    seen_this_run.add(ad_id)

                    if ad_id in db["ads"]:
                        continue

                    link = "https://www.bazos.sk" + href if not href.startswith("http") else href

                    # Cena
                    price_el = ad.select_one(".inzeratycena") or ad.select_one(".cena")
                    price = price_el.text.strip() if price_el else ""

                    if not is_free(price):
                        continue

                    # Datum ... preskoc inzeráty ktoré nie sú z dnešného dňa (SK čas)
                    date_el = ad.select_one(".velikost10") or ad.select_one(".datum")
                    date_text = date_el.text.strip() if date_el else ""
                    posted_date = parse_bazos_date(date_text)
                    if posted_date is None:
                        logging.warning(f"Preskočený inzerát (dátum sa nedal parsovať): {link}")
                        continue
                    if posted_date != today_sk:
                        logging.info(f"Preskočený inzerát (starý dátum {posted_date}): {link}")
                        continue

                    # Popis
                    desc = get_description(ad)

                    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

                    db["ads"][ad_id] = {
                        "title": title,
                        "url": link,
                        "search_word": word,
                        "price": price,
                        "posted_date": posted_date.isoformat(),
                        "first_seen": now,
                        "last_seen": now,
                        "notified": True,
                        "notified_at": now,
                        "missing_count": 0,
                    }
                    new_count += 1
                    db["stats"]["today"]["new_ads_found"] += 1

                    msg = (
                        f"NOVÝ INZERÁT  BAZOŠ 🛑\n\n"
                        f"{title}\n"
                    )
                    if desc:
                        msg += f"{desc}\n"
                    msg += (
                        f"\nCena\n0€\n\n"
                        f"Dátum pridania\n{posted_date.strftime('%d.%m.%Y')}\n\n"
                        f"Link\n{link}"
                    )

                    if send_telegram_with_retry(msg):
                        db["stats"]["today"]["notifications_sent"] += 1
                    logging.info(f"POSLANÉ: {title}")
                    time.sleep(1)

                except Exception as e:
                    logging.error(f"Chyba pri inzeráte: {e}")
                    continue

        except Exception as e:
            logging.error(f"Chyba pri slove '{word}': {e}")
            if isinstance(e, BazosFetchError):
                had_fetch_error = True
                db["stats"]["today"]["last_error"] = str(e)

        time.sleep(2)

    logging.info(f"Hotovo. Nových inzerátov: {new_count}")

    # GC ... preskočíme ak seen_this_run je prázdny (Bazos možno zlyhal celkom)
    if not seen_this_run:
        logging.warning("GC preskočený: žiadne inzeráty videné v tomto behu (možno Bazos zlyhal)")
    else:
        now_gc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        pruned_count = 0
        for ad_id in list(db["ads"].keys()):
            if ad_id in seen_this_run:
                db["ads"][ad_id]["missing_count"] = 0
                db["ads"][ad_id]["last_seen"] = now_gc
            else:
                db["ads"][ad_id]["missing_count"] += 1
                if db["ads"][ad_id]["missing_count"] >= 3:
                    logging.info(f"GC: vymazaný {ad_id} ({db['ads'][ad_id]['title'][:40]})")
                    del db["ads"][ad_id]
                    pruned_count += 1
                    db["stats"]["today"]["ads_pruned"] += 1
        logging.info(f"GC: {pruned_count} vymazaných, {len(db['ads'])} zostáva v DB")

    if had_fetch_error:
        db["stats"]["today"]["failed_runs"] += 1
    else:
        db["stats"]["today"]["successful_runs"] += 1

    db["last_run"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if should_send_health_check(db, today_sk_str, current_hour):
        send_health_check(db, today_sk_str)

    save_db(db)


if __name__ == "__main__":
    check()
