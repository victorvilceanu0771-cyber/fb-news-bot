"""
Bot de știri financiare pentru Pagina de Facebook „Victor Vîlceanu finanțe”.

La fiecare rulare:
  1. citește fluxurile RSS din feeds.json;
  2. păstrează doar știrile noi (nepublicate și mai recente de MAX_AGE_HOURS);
  3. grupează știrile pe subiect și le dă un scor de relevanță 0–100 (relevance.py,
     port al motorului FinAnalytix Pro); alege cea mai bună știre peste prag,
     cu reguli de diversitate; Gemini doar redactează postarea în română;
  4. publică pe Pagină prin Graph API (mesaj + link);
  5. salvează linkul în posted.json, ca să nu fie publicat de două ori.

Variabile de mediu:
  FB_PAGE_ID        ID-ul Paginii (implicit "me" = Pagina tokenului)
  FB_PAGE_TOKEN     token de Pagină SAU token de utilizator de lungă durată (SECRET)
  FB_PAGE_NAME      o parte din numele Paginii (implicit „finan”)
  GEMINI_API_KEY    cheia Gemini (SECRET)
  GEMINI_MODEL      implicit gemini-2.5-flash
  GRAPH_VERSION     implicit v24.0
  POSTS_PER_RUN     implicit 1
  MAX_AGE_HOURS     implicit 8
  MIN_SCORE         pragul minim de relevanță pentru publicare (implicit 60)
  MAX_PER_DAY       maximum de postări pe zi (implicit 8)
  MIN_GAP_MIN       pauza minimă între două postări, în minute (implicit 30)
  URGENT_SCORE      scorul de la care o știre poate sări peste pauză (implicit 85)
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

import extras
import relevance

BASE = Path(__file__).resolve().parent
FEEDS_FILE = BASE / "feeds.json"
STATE_FILE = BASE / "posted.json"

PAGE_ID = os.getenv("FB_PAGE_ID", "me")  # cu un Page token, "me" = Pagina
PAGE_TOKEN = os.getenv("FB_PAGE_TOKEN", "")
PAGE_NAME = os.getenv("FB_PAGE_NAME", "finan")  # o parte din numele Paginii
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v24.0")
POSTS_PER_RUN = int(os.getenv("POSTS_PER_RUN", "1"))
MAX_AGE_HOURS = float(os.getenv("MAX_AGE_HOURS", "8"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "60"))  # prag de publicare (RIDICAT)
MAX_PER_DAY = int(os.getenv("MAX_PER_DAY", "8"))  # maximum de postări pe zi
MIN_GAP_MIN = int(os.getenv("MIN_GAP_MIN", "30"))  # pauză minimă între postări (minute)
URGENT_SCORE = int(os.getenv("URGENT_SCORE", "85"))
ACTIVE_FROM, ACTIVE_TO = 7, 22  # publicăm doar între 07:00 și 22:00, ora României
RO_TZ_OFFSET = 2  # ora României iarna (UTC+2); vara diferența e de 1h, fără efect important
DRY_RUN = os.getenv("DRY_RUN", "0").strip().lower() not in ("0", "", "false", "no", "nu")  # orice altă valoare = test
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
            items.append({
                "key": key,
                "tkey": title_key(title),
                "seen": key in seen or title_key(title) in seen,
                "source": feed["name"],
                "region": "world" if feed.get("category") in ("int", "fx", "macro") and not feed.get("ro") else "ro",
                "title": title,
                "summary": clean(e.get("summary", "")),
                "link": link,
                "ts": ts,
            })
            count += 1
        log(f"{feed['name']}: {count} știri în fereastra de {MAX_AGE_HOURS:g}h")
    return items


def pick_candidates(items: list[dict]) -> list[dict]:
    """Grupează pe subiect, elimină subiectele deja publicate și păstrează
    câte un reprezentant pe subiect (sursa cu cel mai bun scor)."""
    relevance.cluster_items(items)
    clusters: dict[int, list[dict]] = {}
    for it in items:
        clusters.setdefault(it["cluster_id"], []).append(it)
    reps = []
    for members in clusters.values():
        if any(m["seen"] for m in members):
            continue  # subiect deja publicat
        for m in members:
            m.update(relevance.score_item(m))
        best = max(members, key=lambda m: (m["score"], len(m["summary"])))
        best["members"] = members
        reps.append(best)
    return sorted(reps, key=lambda r: r["score"], reverse=True)


def ro_today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + RO_TZ_OFFSET * 3600))


def posting_budget(posted: list[str]) -> tuple[int, float]:
    """Câte postări mai sunt permise azi și câte minute au trecut de la ultima."""
    stamps = [float(k[5:]) for k in posted if k.startswith("post:")]
    today = ro_today()
    today_count = sum(1 for t in stamps
                      if time.strftime("%Y-%m-%d", time.gmtime(t + RO_TZ_OFFSET * 3600)) == today)
    since_last = (time.time() - max(stamps)) / 60 if stamps else 1e9
    return MAX_PER_DAY - today_count, since_last


def choose(reps: list[dict], posted: list[str], n: int) -> list[dict]:
    """Reguli peste scor: prag minim și fără două postări la rând din aceeași categorie."""
    eligible = [r for r in reps if r["score"] >= MIN_SCORE]
    if not eligible:
        return []
    last_cat = next((k[4:] for k in reversed(posted) if k.startswith("cat:")), None)
    chosen: list[dict] = []
    pool = eligible[:]
    for r in pool:
        if len(chosen) >= n:
            break
        if r["category"] == last_cat and r["category"] != "general" and any(
                x["category"] != last_cat for x in pool if x not in chosen and x is not r):
            continue  # diversitate: altă categorie disponibilă peste prag
        chosen.append(r)
        last_cat = r["category"]
    return chosen or eligible[:n]


# ---------- Gemini ----------

PROMPT = """Ești editorul Paginii de Facebook „Victor Vîlceanu finanțe”, a unui broker român cu 24 de ani de experiență pe piața de capital.
Scrie O postare în limba română despre știrea de mai jos (subiectul a fost deja ales de algoritmul de relevanță).

Reguli:
- primul rând: un titlu scurt, cu un emoji potrivit (📈 📉 🏦 💶 🛢️ 🌍);
- 2–4 propoziții de rezumat, factuale, cu cifrele din știre, scrise cu cuvintele tale (nu copia textul sursei);
- dacă e relevant, o propoziție despre de ce contează pentru un investitor român (BVB, EUR/RON, dobânzi) – fără speculații;
- conținut STRICT informativ: NU face recomandări de investiții (cumpărare/vânzare/păstrare), NU folosi imperative
  adresate cititorului, NU da prețuri-țintă și NU face predicții de preț; fără formulări promoționale sau evaluative
  („atractiv”, „oportunitate”, „potențial de randament”, „subliniază atractivitatea”); maximum 3 hashtag-uri;
- maximum 600 de caractere;
- EXACTITATE: folosește EXCLUSIV faptele din textele de mai jos. Nu adăuga cifre, date, nume, procente,
  cauze sau context pe care nu le găsești acolo (nici din ce știi tu). Păstrează cifrele exact cum apar în sursă
  (fără rotunjiri sau conversii). Dacă sursa nu spune de ce contează știrea, nu inventa o explicație.

Răspunde STRICT în JSON: {{"text": "<postarea>"}}

Știrea (din mai multe surse, dacă există):
{items}
"""


def source_text(item: dict) -> str:
    return "\n".join(f"- [{m['source']}] {m['title']} — {m['summary']}" for m in item.get("members", [item])[:5])


def ask_gemini(item: dict, strict: bool = False, fix_numbers: list[str] | None = None) -> str:
    listing = source_text(item)
    if fix_numbers:
        listing += ("\n\nATENȚIE: varianta anterioară conținea cifre care NU apar în sursă ("
                    + ", ".join(fix_numbers) + "). Folosește numai cifrele scrise exact în textele de mai sus.")
    if strict:
        listing += ("\n\nATENȚIE: varianta anterioară conținea formulări care pot fi citite ca recomandare de investiții. "
                    "Scrie strict informativ: fapte și cifre, fără verbe la imperativ, fără „oportunitate”, „potențial”, "
                    "„atractiv”, „merită”, „buy/sell”, fără prețuri-țintă și fără aprecieri despre evoluția viitoare a prețului.")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {
        "contents": [{"parts": [{"text": PROMPT.format(items=listing)}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
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
    data = json.loads(raw)
    if isinstance(data, list):
        data = data[0]
    text = (data.get("text") or "").strip()
    if not text:
        raise ValueError("Gemini a întors un text gol")
    return text


# Formulări care pot transforma o postare informativă într-o recomandare de investiții.
FORBIDDEN_IN_POST = [
    "cumpărați", "cumparati", "cumpără acum", "cumpara acum", "vindeți", "vindeti", "vinde acum",
    "recomand", "recomandăm", "recomandam", "merită cumpărat", "merita cumparat", "merită investit",
    "oportunitate", "ocazie de cumpărare", "ocazie de cumparare", "potențial de creștere", "potential de crestere",
    "potențial de randament", "potential de randament", "preț țintă", "pret tinta", "țintă de preț",
    "investiți", "investiti", "să investești", "sa investesti", "de cumpărat", "de cumparat", "atractiv",
    "subevaluat", "supraevaluat", "intrați", "intrati", "poziționați-vă", "pozitionati-va", "buy", "sell",
]


def gemini_text(prompt: str) -> str:
    """Apel Gemini generic, cu răspuns JSON {"text": ...}."""
    if not GEMINI_KEY:
        return "(rezumat indisponibil – lipsește cheia Gemini)"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"}}
    for attempt in range(3):
        try:
            r = requests.post(url, headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=90)
            r.raise_for_status()
            raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            data = json.loads(re.sub(r"^```(?:json)?|```$", "", raw.strip()).strip())
            return (data[0] if isinstance(data, list) else data).get("text", "").strip()
        except Exception:
            if attempt == 2:
                raise
            time.sleep(10 * (attempt + 1))
    return ""


_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")


def _canon(num: str) -> str:
    """3,9 / 3.9 / 3.90 -> 3.9 ; 1.500 / 1,500 -> 1500 (separator de mii)."""
    parts = re.split(r"[.,]", num)
    if len(parts) > 1 and all(len(p) == 3 for p in parts[1:]):
        return "".join(parts)
    if len(parts) > 2:
        return "".join(parts)
    n = parts[0].lstrip("0") or "0"
    if len(parts) == 2:
        dec = parts[1].rstrip("0")
        return f"{n}.{dec}" if dec else n
    return n


def unverified_numbers(post: str, source: str) -> list[str]:
    """Cifrele din postare care nu apar în textul sursă (exceptând numerele mici, ex. „10 luni”)."""
    src = {_canon(x) for x in _NUM_RE.findall(source)}
    bad = []
    for m in _NUM_RE.finditer(post):
        raw, c = m.group(0), _canon(m.group(0))
        pct = post[m.end():m.end() + 2].lstrip().startswith("%")
        if c in src or (c.isdigit() and int(c) <= 12 and not pct):
            continue
        if raw not in bad:
            bad.append(raw)
    return bad


def violates_no_advice(text: str) -> list[str]:
    low = text.lower()
    whole = {"buy", "sell", "investiți", "investiti", "intrați", "intrati"}  # doar ca cuvânt întreg (nu „buyback”)
    return [w for w in FORBIDDEN_IN_POST
            if re.search(rf"(?<![\w]){re.escape(w)}" + (r"(?![\w])" if w in whole else ""), low)]


def fallback_text(it: dict) -> str:
    """Doar pentru DRY_RUN fără cheie Gemini."""
    return f"📰 {it['title']}\n\nSursa: {it['source']}"


# ---------- Facebook ----------

def publish(message: str, link: str) -> str:
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PAGE_ID}/feed"
    data = {"message": message, "access_token": PAGE_TOKEN}
    if link:
        data["link"] = link
    resp = requests.post(url, data=data, timeout=30)
    data = resp.json()
    if resp.status_code != 200 or "id" not in data:
        raise RuntimeError(f"Graph API {resp.status_code}: {data.get('error', data)}")
    return data["id"]


def _debug(token: str) -> dict:
    return requests.get(
        f"https://graph.facebook.com/{GRAPH_VERSION}/debug_token",
        params={"input_token": token, "access_token": token}, timeout=20,
    ).json().get("data", {})


def _fmt_exp(exp) -> str:
    if exp == 0:
        return "nu expiră"
    if not exp:
        return "necunoscut"
    days = (exp - time.time()) / 86400
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(exp)) + f" (peste {days:.0f} zile)"


def resolve_page_token() -> str:
    """Acceptă fie un token de Pagină, fie un token de utilizator de lungă durată.
    Din tokenul de utilizator obține tokenul Paginii (care, derivat dintr-un token lung, nu expiră)."""
    global PAGE_TOKEN
    try:
        info = _debug(PAGE_TOKEN)
    except Exception as exc:
        log(f"Nu am putut verifica tokenul: {exc}")
        return PAGE_TOKEN
    log(f"Token din secret: tip={info.get('type')}, valid={info.get('is_valid')}, expiră: {_fmt_exp(info.get('expires_at'))}")
    exp = info.get("expires_at") or 0
    if exp and exp - time.time() < 7 * 86400:
        log("ATENȚIE: tokenul expiră în mai puțin de 7 zile – generează unul nou.")
    if info.get("type") != "USER":
        return PAGE_TOKEN
    resp = requests.get(
        f"https://graph.facebook.com/{GRAPH_VERSION}/me/accounts",
        params={"fields": "id,name,access_token", "access_token": PAGE_TOKEN}, timeout=20,
    ).json()
    pages = resp.get("data", [])
    wanted = PAGE_NAME.lower()
    for pg in pages:
        if wanted in pg.get("name", "").lower():
            log(f"Folosesc tokenul Paginii „{pg['name']}”.")
            PAGE_TOKEN = pg["access_token"]
            return PAGE_TOKEN
    raise RuntimeError(f"Pagina care conține „{PAGE_NAME}” nu a fost găsită: {[p.get('name') for p in pages] or resp}")


# ---------- main ----------

def main() -> int:
    if not DRY_RUN and not PAGE_TOKEN:
        log("Lipsește FB_PAGE_TOKEN (sau rulează cu DRY_RUN=1).")
        return 1

    if PAGE_TOKEN:
        resolve_page_token()

    hour_ro = time.gmtime(time.time() + RO_TZ_OFFSET * 3600).tm_hour
    if not (ACTIVE_FROM <= hour_ro < ACTIVE_TO) and not DRY_RUN:
        log(f"În afara intervalului de publicare ({ACTIVE_FROM}:00–{ACTIVE_TO}:00, ora României). Ies.")
        return 0

    posted = load_state()
    force = [t for t in os.getenv("EXTRAS_FORCE", "").split(",") if t.strip()]
    extras.run(posted, publish, gemini_text, violates_no_advice, PAGE_TOKEN, GRAPH_VERSION, log,
               DRY_RUN, force=force or None)
    if not DRY_RUN:
        save_state(posted)

    feeds = json.loads(FEEDS_FILE.read_text(encoding="utf-8"))
    left, since_last = posting_budget(posted)
    if left <= 0 and not DRY_RUN:
        log(f"Limita de {MAX_PER_DAY} postări pe zi a fost atinsă. Ies.")
        return 0
    items = fetch_items(feeds, set(posted))
    reps = pick_candidates(items)
    log(f"Subiecte noi: {len(reps)} (din {len(items)} articole); prag de publicare: {MIN_SCORE}")
    for r in reps[:8]:
        extra = f" +{len(r['members']) - 1} surse" if len(r["members"]) > 1 else ""
        log(f"  [{r['score']:>3} {r['level']:<7}] {r['category']:<7} {r['region']:<6} {r['title'][:90]} ({r['source']}{extra})")
    for r in reps[:5]:
        log(f"Motive [{r['score']}] {r['title'][:60]}:\n    - " + "\n    - ".join(r["reasons"]))

    chosen = choose(reps, posted, min(POSTS_PER_RUN, max(left, 1) if DRY_RUN else left))
    if not chosen:
        log(f"Niciun subiect nu atinge pragul de {MIN_SCORE}. Nu public nimic la această rulare.")
        return 0
    if since_last < MIN_GAP_MIN and not DRY_RUN:
        urgent = [c for c in chosen if c["score"] >= URGENT_SCORE]
        if not urgent:
            log(f"Ultima postare a fost acum {since_last:.0f} min (pauză minimă {MIN_GAP_MIN} min); "
                f"cel mai bun subiect are {chosen[0]['score']} < {URGENT_SCORE}. Aștept.")
            return 0
        log(f"Știre urgentă (scor {urgent[0]['score']} ≥ {URGENT_SCORE}) – sar peste pauza de {MIN_GAP_MIN} min.")
        chosen = urgent[:1]

    errors = 0
    for p in chosen:
        if GEMINI_KEY:
            try:
                text = ask_gemini(p)
                bad = violates_no_advice(text)
                if bad:
                    log(f"Textul conține formulări de tip recomandare ({', '.join(bad)}); îl regenerez.")
                    text = ask_gemini(p, strict=True)
                    bad = violates_no_advice(text)
                if bad:
                    log(f"Tot conține formulări de tip recomandare ({', '.join(bad)}); NU public.")
                    return 0
                src = source_text(p)
                wrong = unverified_numbers(text, src)
                if wrong:
                    log(f"Cifre care nu apar în sursă ({', '.join(wrong)}); regenerez.")
                    text = ask_gemini(p, fix_numbers=wrong)
                    wrong = unverified_numbers(text, src) or violates_no_advice(text)
                if wrong:
                    log(f"Textul tot nu se potrivește cu sursa ({', '.join(wrong)}); NU public.")
                    return 0
            except Exception as exc:
                log(f"Gemini a eșuat ({exc}); nu public nimic la această rulare.")
                return 1
        elif DRY_RUN:
            text = fallback_text(p)
        else:
            log("Lipsește GEMINI_API_KEY – nu public.")
            return 1
        message = f"{text}\n\n{DISCLAIMER}"
        if DRY_RUN:
            log(f"----- DRY RUN (scor {p['score']}, {p['level']}) -----\n{message}\n🔗 {p['link']}\n-------------------")
            continue
        try:
            post_id = publish(message, p["link"])
            log(f"Publicat: {post_id} ← [{p['score']}] {p['title']}")
            for m in p["members"]:
                posted.extend([m["key"], m["tkey"]])
            posted.append(f"cat:{p['category']}")
            posted.append(f"post:{time.time():.0f}")
            extras.append_log({"ts": time.time(), "kind": "news", "title": p["title"], "score": p["score"],
                               "category": p["category"], "region": p["region"], "link": p["link"],
                               "post_id": post_id})
        except Exception as exc:
            errors += 1
            log(f"EROARE la publicare: {exc}")

    if not DRY_RUN:
        save_state(posted)
    return 1 if errors and errors == len(chosen) else 0


if __name__ == "__main__":
    sys.exit(main())
