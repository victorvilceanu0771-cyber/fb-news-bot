"""
Bot de știri financiare pentru Pagina de Facebook „Victor Vîlceanu finanțe”.

La fiecare rulare:
  1. citește fluxurile RSS din feeds.json;
  2. păstrează doar știrile noi (nepublicate și mai recente de MAX_AGE_HOURS);
  3. cere lui Gemini să aleagă cea mai importantă știre și să scrie un rezumat în română;
  4. publică pe Pagină prin Graph API (mesaj + link);
  5. salvează linkul în posted.json, ca să nu fie publicat de două ori.

Variabile de mediu:
  FB_PAGE_ID        ID-ul Paginii (implicit "me" = Pagina tokenului)
  FB_PAGE_TOKEN     Page Access Token (SECRET)
  GEMINI_API_KEY    cheia Gemini (SECRET)
  GEMINI_MODEL      implicit gemini-2.5-flash
  GRAPH_VERSION     implicit v24.0
  POSTS_PER_RUN     implicit 1
  MAX_AGE_HOURS     implicit 6
  DRY_RUN           "1" = afișează postarea, nu publică nimic
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import feedparser
import requests

BASE = Path(__file__).resolve().parent
FEEDS_FILE = BASE / "feeds.json"
STATE_FILE = BASE / "posted.json"

PAGE_ID = os.getenv("FB_PAGE_ID", "me")  # cu un Page token, "me" = Pagina
PAGE_TOKEN = os.getenv("FB_PAGE_TOKEN", "")
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v24.0")
POSTS_PER_RUN = int(os.getenv("POSTS_PER_RUN", "1"))
MAX_AGE_HOURS = float(os.getenv("MAX_AGE_HOURS", "6"))
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"
STATE_KEEP = 2000  # câte linkuri publicate păstrăm în istoric
UA = "Mozilla/5.0 (compatible; VVFinanteNewsBot/1.0)"

DISCLAIMER = "ℹ️ Informație cu caracter general, nu reprezintă recomandare de investiții."


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------- stare ----------

def load_state() -> list[str]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("posted.json corupt – pornesc cu istoric gol")
    return []


def save_state(keys: list[str]) -> None:
    STATE_FILE.write_text(json.dumps(keys[-STATE_KEEP:], ensure_ascii=False, indent=0), encoding="utf-8")


def _h(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def item_key(link: str, title: str) -> str:
    return _h((link or title).split("?")[0].strip().lower())


def title_key(title: str) -> str:
    return "t" + _h(re.sub(r"\W+", " ", title.lower()).strip()[:80])


# ---------- RSS ----------

def clean(text: str, limit: int = 600) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def fetch_items(feeds: list[dict], seen: set[str]) -> list[dict]:
    now = time.time()
    items: list[dict] = []
    for feed in feeds:
        try:
            resp = requests.get(feed["url"], headers={"User-Agent": UA}, timeout=20)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except Exception as exc:  # o sursă căzută nu oprește botul
            log(f"[sursă ignorată] {feed['name']}: {exc}")
            continue
        count = 0
        for e in parsed.entries[:30]:
            link = e.get("link", "")
            title = clean(e.get("title", ""), 300)
            if not title or not link:
                continue
            ts_struct = e.get("published_parsed") or e.get("updated_parsed")
            ts = calendar.timegm(ts_struct) if ts_struct else now
            if now - ts > MAX_AGE_HOURS * 3600:
                continue
            key = item_key(link, title)
            if key in seen or title_key(title) in seen:
                continue
            items.append({
                "key": key,
                "source": feed["name"],
                "category": feed.get("category", ""),
                "title": title,
                "summary": clean(e.get("summary", "")),
                "link": link,
                "ts": ts,
            })
            count += 1
        log(f"{feed['name']}: {count} știri noi")
    # eliminăm dublurile de titlu între surse
    unique, titles = [], set()
    for it in sorted(items, key=lambda x: x["ts"], reverse=True):
        t = it["title"].lower()[:80]
        if t not in titles:
            titles.add(t)
            unique.append(it)
    return unique


# ---------- Gemini ----------

PROMPT = """Ești editorul Paginii de Facebook „Victor Vîlceanu finanțe”, a unui broker român cu 24 de ani de experiență pe piața de capital.
Mai jos ai o listă numerotată de știri financiare recente (piața românească, piețe internaționale, forex și macro).

Sarcina ta:
1. Alege cele mai relevante {n} știri pentru investitori români (impact asupra piețelor, BVB, EUR/RON, dobânzi, mari companii). Evită știrile minore, publicitare sau duplicate.
2. Pentru fiecare, scrie o postare în limba română:
   - primul rând: un titlu scurt, cu un emoji potrivit (📈 📉 🏦 💶 🛢️ 🌍);
   - 2–4 propoziții de rezumat, factuale, cu cifrele din știre, scrise cu cuvintele tale (nu copia textul sursei);
   - fără recomandări de cumpărare/vânzare, fără predicții de preț, fără hashtag-uri în exces (maximum 3);
   - maximum 600 de caractere.
3. Folosește DOAR informațiile din știre. Nu inventa cifre.

Răspunde STRICT în JSON, fără alt text:
[{{"index": <numărul știrii>, "text": "<postarea>"}}]

Știri:
{items}
"""


def ask_gemini(items: list[dict], n: int) -> list[dict]:
    listing = "\n".join(
        f"{i}. [{it['source']}] {it['title']} — {it['summary']}" for i, it in enumerate(items)
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {
        "contents": [{"parts": [{"text": PROMPT.format(n=n, items=listing)}]}],
        "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json"},
    }
    resp = None
    for attempt in range(3):  # Gemini mai are întârzieri ocazionale
        try:
            resp = requests.post(url, headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=90)
            if resp.status_code in (429, 500, 503) and attempt < 2:
                raise requests.HTTPError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            break
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
            if attempt == 2:
                raise
            log(f"Gemini – încercarea {attempt + 1} eșuată ({exc}), reîncerc...")
            time.sleep(10 * (attempt + 1))
    raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip()).strip()
    picks = json.loads(raw)
    result = []
    for p in picks[:n]:
        idx = int(p["index"])
        if 0 <= idx < len(items) and p.get("text"):
            result.append({**items[idx], "post": p["text"].strip()})
    return result


def fallback_posts(items: list[dict], n: int) -> list[dict]:
    """Fără Gemini sau dacă Gemini dă eroare: titlu + sursă."""
    return [{**it, "post": f"📰 {it['title']}\n\nSursa: {it['source']}"} for it in items[:n]]


# ---------- Facebook ----------

def publish(message: str, link: str) -> str:
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PAGE_ID}/feed"
    resp = requests.post(url, data={"message": message, "link": link, "access_token": PAGE_TOKEN}, timeout=30)
    data = resp.json()
    if resp.status_code != 200 or "id" not in data:
        raise RuntimeError(f"Graph API {resp.status_code}: {data.get('error', data)}")
    return data["id"]


def log_token_expiry() -> None:
    """Afișează în log când expiră tokenul Paginii (0 = nu expiră)."""
    try:
        r = requests.get(
            f"https://graph.facebook.com/{GRAPH_VERSION}/debug_token",
            params={"input_token": PAGE_TOKEN, "access_token": PAGE_TOKEN}, timeout=20,
        ).json().get("data", {})
        exp = r.get("expires_at")
        when = "nu expiră" if exp == 0 else time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(exp)) if exp else "necunoscut"
        log(f"Token Pagină: tip={r.get('type')}, valid={r.get('is_valid')}, expiră: {when}")
    except Exception as exc:
        log(f"Nu am putut verifica tokenul: {exc}")


# ---------- main ----------

def main() -> int:
    if not DRY_RUN and not PAGE_TOKEN:
        log("Lipsește FB_PAGE_TOKEN (sau rulează cu DRY_RUN=1).")
        return 1

    if PAGE_TOKEN:
        log_token_expiry()

    feeds = json.loads(FEEDS_FILE.read_text(encoding="utf-8"))
    posted = load_state()
    items = fetch_items(feeds, set(posted))
    log(f"Total candidați: {len(items)}")
    if not items:
        log("Nicio știre nouă. Ies.")
        return 0

    candidates = items[:40]
    posts: list[dict]
    if GEMINI_KEY:
        try:
            posts = ask_gemini(candidates, POSTS_PER_RUN)
        except Exception as exc:
            # fără selecția AI nu publicăm știri la întâmplare
            log(f"Gemini a eșuat ({exc}); nu public nimic la această rulare.")
            return 1
    elif DRY_RUN:
        posts = fallback_posts(candidates, POSTS_PER_RUN)
    else:
        log("Lipsește GEMINI_API_KEY – nu public știri neselectate.")
        return 1

    errors = 0
    for p in posts:
        message = f"{p['post']}\n\n{DISCLAIMER}"
        if DRY_RUN:
            log("----- DRY RUN -----\n" + message + f"\n🔗 {p['link']}\n-------------------")
            continue
        try:
            post_id = publish(message, p["link"])
            log(f"Publicat: {post_id} ← {p['title']}")
            posted.extend([p["key"], title_key(p["title"])])
        except Exception as exc:
            errors += 1
            log(f"EROARE la publicare: {exc}")

    if not DRY_RUN:
        save_state(posted)
    return 1 if errors and errors == len(posts) else 0


if __name__ == "__main__":
    sys.exit(main())
