import requests
import json
import os
import time
from bs4 import BeautifulSoup

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

# Pridaj sem dalsie CHAT_ID ak chces posielat viacerym ludom
# Oddeľ čiarkou, napr: "111111111,222222222"
EXTRA_CHAT_IDS = os.environ.get("EXTRA_CHAT_IDS", "")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36"}
SEEN_FILE = "seen_ads.json"
PSC = "81105"
OKRUH = "15"

# Vsetky hladane slova - variacie, typy veci, slova oznacujuce darovanie
SEARCH_WORDS = [
    # iPhone - všetky variacie
    "iphone", "iphon", "iphone", "i phone", "ajfon",
    "iphone 11", "iphone 12", "iphone 13", "iphone 14", "iphone 15", "iphone 16",

    # iPad
    "ipad", "i pad", "aipod", "iped",

    # MacBook
    "macbook", "mac book", "makbok", "macbok", "laptop apple",

    # Apple Watch
    "apple watch", "applewatch", "hodinky apple",

    # AirPods
    "airpods", "air pods", "airpod", "sluchadla apple",

    # Ostatné Apple
    "imac", "mac mini", "apple tv", "homepod",

    # Bicykle - všetky variacie
    "bicykel", "bicyk", "bycikel", "bicikl", "bicikle",
    "byciklo", "bicyke", "bickel", "bicykl",
    "bike", "mtb", "bmx",
    "horsky bicykel", "horský bicykel",
    "cestny bicykel", "cestný bicykel",
    "detsky bicykel", "detský bicykel",
    "damsky bicykel", "dámsky bicykel",
    "pansky bicykel", "pánsky bicykel",
    "ebike", "e-bike", "elektricky bicykel", "elektrický bicykel",
    "trek", "specialized", "giant bicykel",
]

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)

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

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    for cid in get_all_chat_ids():
        try:
            requests.post(url, data={"chat_id": cid, "text": msg}, timeout=10)
        except Exception as e:
            print(f"Telegram chyba ({cid}): {e}")

def is_free(price_text):
    p = price_text.lower().strip()
    free_values = ["0 €", "0€", "0 eur", "zdarma", "zadarmo", "", "0"]
    return p in free_values or "zdarma" in p or "zadarmo" in p

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
    seen = load_seen()
    new_count = 0

    for word in SEARCH_WORDS:
        print(f"Skenujem: {word}")
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

            r = requests.get(url, headers=HEADERS, timeout=15)
            r.encoding = "utf-8"
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

                    link = "https://www.bazos.sk" + href if not href.startswith("http") else href
                    ad_id = href.strip("/")

                    if ad_id in seen:
                        continue

                    # Cena
                    price_el = ad.select_one(".inzeratycena") or ad.select_one(".cena")
                    price = price_el.text.strip() if price_el else ""

                    if not is_free(price):
                        continue

                    # Datum
                    date_el = ad.select_one(".velikost10") or ad.select_one(".datum")
                    date = date_el.text.strip() if date_el else "?"

                    # Popis
                    desc = get_description(ad)

                    seen.add(ad_id)
                    new_count += 1

                    msg = (
                        f"NOVÝ INZERÁT  BAZOŠ 🛑\n\n"
                        f"{title}\n"
                    )
                    if desc:
                        msg += f"{desc}\n"
                    msg += (
                        f"\nCena\n0€\n\n"
                        f"Dátum pridania\n{date}\n\n"
                        f"Link\n{link}"
                    )

                    send_telegram(msg)
                    print(f"POSLANÉ: {title}")
                    time.sleep(1)

                except Exception as e:
                    print(f"Chyba pri inzeráte: {e}")
                    continue

        except Exception as e:
            print(f"Chyba pri slove '{word}': {e}")

        time.sleep(2)

    print(f"Hotovo. Nových inzerátov: {new_count}")
    save_seen(seen)

if __name__ == "__main__":
    check()
