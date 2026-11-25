"""
Algoritm de relevanță a știrilor financiare, din perspectiva unui investitor.
Port în Python al motorului de scoring din FinAnalytix Pro (src/scoring.ts), adaptat pentru bot.

Pași:
  1. filtre de intrare: strict financiar + respingere teasere promoționale
  2. clasificare: categorie (rating/fed/macro/bvb/tech/oil/energy/equity/general) și impact (ro/global/both)
  3. grupare pe subiect (cluster) -> confirmare din mai multe surse + viteza de propagare
  4. scor de importanță 0–100 cu motive explicite; nivel CRITIC (>=80) / RIDICAT (>=60) / MODERAT
"""

from __future__ import annotations

import re
import time
from urllib.parse import urlparse

# ---------------------------------------------------------------- potrivire pe cuvânt întreg

WORD_CHARS = "a-z0-9ăâîșțşţáéíóúàèäöü&"
_word_cache: dict[str, re.Pattern] = {}
_prefix_cache: dict[str, re.Pattern] = {}


def _norm(text: str) -> str:
    # unificăm diacriticele cu sedilă (ş ţ) cu cele cu virgulă (ș ț)
    return (text or "").lower().replace("ş", "ș").replace("ţ", "ț")


def contains_word(text: str, term: str) -> bool:
    t = term.lower().strip()
    if not t or t not in text:
        return False
    rx = _word_cache.get(t)
    if rx is None:
        rx = re.compile(rf"(^|[^{WORD_CHARS}]){re.escape(t)}([^{WORD_CHARS}]|$)", re.I)
        _word_cache[t] = rx
    return bool(rx.search(text))


def any_word(text: str, terms) -> bool:
    return any(contains_word(text, t) for t in terms)


def contains_prefix(text: str, stem: str) -> bool:
    t = stem.lower().strip()
    if not t or t not in text:
        return False
    rx = _prefix_cache.get(t)
    if rx is None:
        rx = re.compile(rf"(^|[^{WORD_CHARS}]){re.escape(t)}[{WORD_CHARS}]*", re.I)
        _prefix_cache[t] = rx
    return bool(rx.search(text))


def any_prefix(text: str, stems) -> bool:
    return any(contains_prefix(text, s) for s in stems)


def domain_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


# ---------------------------------------------------------------- emitenți (tickere)

TICKERS: dict[str, list[str]] = {
    # BVB
    "H2O": ["hidroelectrica"], "SNG": ["romgaz"], "SNP": ["omv petrom", "petrom"],
    "TLV": ["banca transilvania"], "BRD": ["brd"], "SNN": ["nuclearelectrica"],
    "FP": ["fondul proprietatea"], "DIGI": ["digi communications", "digi"], "TRP": ["teraplast"],
    "TGN": ["transgaz"], "TEL": ["transelectrica"], "EL": ["electrica"], "AQ": ["aquila"],
    "ONE": ["one united"], "WINE": ["purcari"], "SFG": ["sphera"], "M": ["medlife"],
    "BNET": ["bittnet"], "COTE": ["conpet"], "ATB": ["antibiotice"],
    # globale
    "AAPL": ["apple"], "MSFT": ["microsoft"], "NVDA": ["nvidia"], "AMZN": ["amazon"],
    "GOOGL": ["alphabet", "google"], "META": ["meta platforms", "meta"], "ASML": ["asml"],
    "TSM": ["tsmc"], "TSLA": ["tesla"], "JPM": ["jpmorgan"], "AVGO": ["broadcom"],
    "INTC": ["intel"], "AMD": ["amd"], "PLTR": ["palantir"], "QCOM": ["qualcomm"],
    "MU": ["micron"], "GS": ["goldman sachs"], "MS": ["morgan stanley"], "BAC": ["bank of america"],
    "XOM": ["exxonmobil", "exxon"], "CVX": ["chevron"], "MRNA": ["moderna"], "PFE": ["pfizer"],
    "LLY": ["eli lilly"], "NVO": ["novo nordisk"], "2222.SR": ["saudi aramco", "aramco"],
}
BVB_SYMBOLS = {"H2O", "SNG", "SNP", "TLV", "BRD", "SNN", "FP", "DIGI", "TRP", "TGN", "TEL",
               "EL", "AQ", "ONE", "WINE", "SFG", "M", "BNET", "COTE", "ATB"}


def extract_tickers(text: str) -> list[str]:
    found = [sym for sym, names in TICKERS.items() if any_word(text, names)]
    # simboluri scrise explicit: $NVDA, (NASDAQ: AAPL), (BVB: TLV)
    for m in re.finditer(r"\$([a-z]{1,5})\b|\((?:nasdaq|nyse|bvb)\s*:\s*([a-z0-9.]{1,6})\)", text):
        sym = (m.group(1) or m.group(2) or "").upper()
        if sym and sym not in found:
            found.append(sym)
    return found


# ---------------------------------------------------------------- 1. filtre de intrare

STRONG_FIN = [
    "bursă", "bursa", "bvb", "indice bet", "bnr", "fitch", "moody", "s&p", "dividend", "inflație",
    "inflatie", "pib", "fed", "ecb", "bce", "titluri de stat", "fidelis", "tezaur", "acțiuni", "actiuni",
    "obligațiuni", "obligatiuni", "profit", "rezultate financiare", "fuziune", "achiziție", "achizitie",
    "listare", "emisiune", "dobândă", "dobanda", "bancă", "banca", "bănci", "banci",
    "insider trading", "hidroelectrica", "romgaz", "petrom", "omv", "banca transilvania", "brd",
    "nuclearelectrica",
    # echivalente în engleză pentru sursele internaționale
    "stocks", "shares", "earnings", "federal reserve", "interest rate", "treasury", "inflation", "nasdaq",
    "wall street", "s&p 500", "dow jones", "central bank",
]
NON_FIN = [
    "fotbal", "meci", "liga 1", "superliga", "fcsb", "showbiz", "vedeta", "vedete", "horoscop", "zodiac",
    "meteo", "vremea", "prognoza meteo", "crimă", "crima", "accident", "accidente", "film", "filme", "serial",
    "muzică", "muzica", "concert", "divorț", "divort", "nuntă", "nunta", "rețetă", "reteta", "sport",
    "campionat", "cazare", "vacanță", "vacanta", "sejur", "turiști", "turisti", "plajă", "plaja",
    "restaurant", "gastronomie", "donație", "donatie", "voluntariat", "caritabil", "editorial", "opinie",
    "satiră", "satira", "pamflet", "percheziții", "perchezitii", "poliția", "politia", "arestat",
    "reținut", "retinut", "dosar penal", "furt", "jaf", "omor", "trafic de droguri",
    "football", "celebrity", "horoscope", "recipe", "weather forecast",
]
CORPORATE_EVENT = [
    "raportare", "raport financiar", "dividend", "emisiune de obligațiuni", "emisiune de obligatiuni",
    "majorare de capital", "adunare generală", "adunare generala", "profit net", "cifra de afaceri",
]
PRIMARY_FIN = [
    "capitalizare", "delistare", "ipo", "randament", "buyback", "răscumpărare", "rascumparare", "preluare",
    "trimestrial", "semestrial", "evaluare", "credit", "credite", "creditare", "împrumut", "imprumut",
    "dobânzi", "dobanzi", "rezerve", "curs valutar", "valută", "valuta", "finanțe", "finante", "financiar",
    "investiții", "investitii", "investitor", "economie", "buget", "bugetar", "deficit", "datorie", "șomaj",
    "somaj", "tva", "acciză", "acciza", "fiscal", "impozit", "anaf", "rating", "venituri", "vânzări", "vanzari",
    "pierdere", "miliard", "milion", "petrol", "țiței", "titei", "brent", "gaze naturale", "energie",
    "stock", "market", "markets", "equity", "bond", "bonds", "yield", "spread", "revenue", "sales",
    "guidance", "forecast", "outlook", "valuation", "quarterly", "merger", "acquisition", "payout",
    "default", "downgrade", "upgrade", "tariff", "tariffs", "rate cut", "rate hike", "billion", "trillion",
    "commodity", "crypto", "bitcoin", "fund", "fonduri", "gdp", "debt", "loan", "fomc", "sec", "fda",
]
FIN_STEMS = ["acțiun", "actiun", "obligațiun", "obligatiun", "dividend", "dobând", "doband", "inflați",
             "inflati", "randament", "titlur", "bugetar", "fiscal", "bancari", "valutar", "bursier",
             "monetar", "creditar"]
PROMO = ["profit insider", "abonament", "citește mai mult la", "citeste mai mult la", "dreptul de autor",
         "legii 8/1996", "abonează-te", "aboneaza-te", "conținut premium", "continut premium",
         "sponsored", "advertorial", "publicitate", "parteneriat"]


def is_strictly_financial(text: str, source_tier: str, has_tickers: bool) -> bool:
    strong_count = sum(1 for t in STRONG_FIN if contains_word(text, t))
    corporate_override = has_tickers and (strong_count >= 2 or any_word(text, CORPORATE_EVENT))
    if not corporate_override and any_word(text, NON_FIN):
        return False
    if has_tickers or source_tier == "official":
        return True
    if strong_count:
        return True
    matched = sum(1 for k in PRIMARY_FIN if contains_word(text, k))
    if source_tier == "high_rep" and matched >= 1:
        return True
    if matched >= 2:
        return True
    return any_prefix(text, FIN_STEMS)


def is_promo(text: str) -> bool:
    return any(p in text for p in PROMO)


# ---------------------------------------------------------------- 2. clasificare

def determine_category(text: str) -> str:
    if any_word(text, ["fitch", "moody", "moody's", "s&p", "rating suveran", "rating de țară", "rating de tara",
                       "credit rating", "downgrade", "upgrade", "sovereign rating", "rating agency"]):
        return "rating"
    if any_word(text, ["hidroelectrica", "romgaz", "omv petrom", "banca transilvania", "brd", "nuclearelectrica",
                       "fondul proprietatea", "digi communications", "teraplast", "transgaz", "aquila",
                       "one united", "purcari", "sphera", "medlife", "bittnet", "bvb", "indicele bet",
                       "bursa de valori bucurești", "bursa de valori bucuresti"]):
        return "bvb"
    monetary = ["federal reserve", "fed", "powell", "ecb", "bce", "lagarde", "banca centrala", "bancă centrală",
                "bnr", "dobânda cheie", "dobanda cheie", "politica monetara", "politică monetară",
                "rate decision", "interest rate", "central bank", "monetary policy", "fomc"]
    if any_word(text, monetary):
        if any_word(text, ["fed", "federal reserve", "ecb", "bce", "powell", "lagarde", "fomc"]):
            return "fed"
        return "macro"
    if any_word(text, ["apple", "microsoft", "nvidia", "amazon", "alphabet", "google", "meta", "asml", "tsmc",
                       "semiconductor", "broadcom", "intel", "amd", "chipmakers", "chips"]):
        return "tech"
    if any_word(text, ["saudi aramco", "aramco", "brent", "crude oil", "crude", "barrel", "petrol", "opec",
                       "rafinărie", "rafinarie", "motorină", "motorina", "benzină", "benzina", "țiței", "titei",
                       "refinery", "wti"]):
        return "oil"
    if any_word(text, ["energie electrică", "energie electrica", "eolian", "fotovoltaic", "nuclear",
                       "transelectrica", "electrica", "gaze naturale", "natural gas", "megawați", "mw", "gwh", "twh"]):
        return "energy"
    if any_word(text, ["inflatie", "inflație", "inflation", "cpi", "ppi", "pib", "gdp", "deficit", "buget",
                       "somaj", "șomaj", "unemployment", "pmi", "payrolls", "rate cut", "rate hike",
                       "titluri de stat", "fidelis", "tezaur", "obligațiuni", "obligatiuni", "bond", "yield",
                       "randament", "eur/usd", "usd/ron", "eur/ron", "leul", "dollar", "dolarul"]):
        return "macro"
    if any_word(text, ["jpmorgan", "berkshire", "tesla", "moderna", "pfizer", "eli lilly", "novo nordisk",
                       "wall street", "s&p 500", "nasdaq", "dow jones", "stock", "stocks", "equity", "equities",
                       "acțiuni", "actiuni", "earnings", "buyback", "ipo", "merger", "acquisition"]):
        return "equity"
    return "general"


RO_STRICT = ["românia", "romania", "românesc", "românească", "românești", "români", "bnr", "bvb", "anaf",
             "asf", "guvernul", "ministerul finanțelor", "ministerul finantelor", "fidelis", "tezaur",
             "hidroelectrica", "romgaz", "omv petrom", "banca transilvania", "brd", "nuclearelectrica",
             "fondul proprietatea", "teraplast", "transgaz", "electrica", "aquila", "one united", "purcari",
             "sphera", "medlife", "bittnet", "bucurești", "bucuresti", "romanian"]
GLOBAL_KW = ["fed", "federal reserve", "ecb", "bce", "wall street", "s&p 500", "nasdaq", "dow jones", "sua",
             "usa", "america", "germania", "china", "opec", "brent", "nvidia", "apple", "microsoft", "amazon",
             "meta", "alphabet", "google", "tesla", "jpmorgan", "tsmc", "asml"]


def determine_region(text: str, feed_region: str) -> str:
    has_bet = any_word(text, ["indicele bet", "indice bet", "bet-tr", "bet-fi", "bet-ng", "bet-bk"])
    has_ro_strict = any_word(text, RO_STRICT) or has_bet
    has_global = any_word(text, GLOBAL_KW)
    if feed_region == "world":
        if has_ro_strict and has_global:
            return "both"
        return "ro" if has_ro_strict else "global"
    has_ro = has_ro_strict or any_word(text, ["leu", "leul", "lei", "ron", "eur/ron"])
    if has_ro and has_global:
        return "both"
    if has_global and not has_ro:
        return "global"
    return "ro"


# ---------------------------------------------------------------- 3. grupare pe subiect

_STOP = set("de la si și in în cu pe din pentru a al ale ai o un mai care ce sa să se este sunt the of to and "
            "in on for a an is are with as at by from its it after over".split())


def _tokens(title: str) -> set[str]:
    return {w for w in re.findall(rf"[{WORD_CHARS}]+", _norm(title)) if len(w) > 2 and w not in _STOP}


def cluster_items(items: list[dict], threshold: float = 0.45) -> None:
    """Grupează știrile cu titluri asemănătoare și completează la fiecare
    cluster_sources (alte surse) și cluster_velocity (surse/oră)."""
    toks = [_tokens(it["title"]) for it in items]
    tick = [set(extract_tickers(_norm(it["title"]))) for it in items]
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = toks[i], toks[j]
            if not (a and b):
                continue
            sim = len(a & b) / len(a | b)
            close_in_time = abs(items[i]["ts"] - items[j]["ts"]) < 12 * 3600
            if sim >= threshold or (tick[i] & tick[j] and close_in_time and sim >= 0.2):
                parent[find(i)] = find(j)
    groups: dict[int, list[int]] = {}
    for i in range(len(items)):
        groups.setdefault(find(i), []).append(i)
    for members in groups.values():
        sources = {items[m]["source"] for m in members}
        times = [items[m]["ts"] for m in members]
        span_h = max(1.0, (max(times) - min(times)) / 3600)
        for m in members:
            items[m]["cluster_sources"] = sorted(sources - {items[m]["source"]})
            items[m]["cluster_velocity"] = round(len(sources) / span_h, 2) if len(sources) > 1 else 0.0
            items[m]["cluster_id"] = members[0]


# ---------------------------------------------------------------- 4. scor de importanță

OFFICIAL_DOMAINS = ["bnr.ro", "bvb.ro", "insse.ro", "fitchratings.com", "moodys.com", "spglobal.com",
                    "reuters.com", "bloomberg.com", "ecb.europa.eu", "ft.com", "mfinante.gov.ro",
                    "federalreserve.gov", "asfromania.ro"]
HIGH_REP_DOMAINS = ["zf.ro", "economedia.ro", "profit.ro", "bursa.ro", "economica.net", "g4media.ro",
                    "digi24.ro", "yahoo.com", "marketwatch.com", "dowjones.com", "cnbc.com", "investing.com",
                    "forexlive.com", "wsj.com", "cursdeguvernare.ro"]

SPECIFIC_KW = ["hidroelectrica", "romgaz", "omv petrom", "petrom", "banca transilvania", "brd", "nuclearelectrica",
               "teraplast", "transgaz", "electrica", "aquila", "purcari", "sphera", "medlife", "bittnet", "digi",
               "fondul proprietatea", "transelectrica", "one united", "conpet", "antibiotice", "apple", "microsoft",
               "nvidia", "amazon", "alphabet", "google", "meta", "asml", "tsmc", "jpmorgan", "tesla", "aramco",
               "fidelis", "tezaur", "titluri de stat", "fitch", "moody", "moody's", "s&p", "bnr", "fed",
               "federal reserve", "bce", "ecb", "sec", "ministerul finanțelor", "ministerul finantelor", "asf",
               "anaf", "ins", "bvb", "s&p 500", "nasdaq", "dow jones", "fomc", "opec"]
GENERIC_KW = ["buget", "deficit", "profit", "dobândă", "dobanda", "inflație", "inflatie", "inflation", "pib",
              "bursă", "bursa", "oil", "petrol", "stock", "equity", "dividend", "earnings", "yield", "bond",
              "commodity", "crypto", "cpi", "ppi", "gdp", "credite", "credit", "impozit", "fiscal", "tva",
              "acciză", "acciza", "economie", "creditare", "împrumut", "imprumut", "datorie", "venituri",
              "vânzări", "vanzari", "evaluare", "trimestrial", "semestrial", "miliard", "milion",
              "curs valutar", "rezerve", "capitalizare", "listare", "șomaj", "somaj", "loan", "debt", "shares",
              "revenue", "sales", "tariff", "tariffs", "rate cut", "rate hike", "interest rate", "billion",
              "trillion", "valuation", "quarterly", "guidance", "forecast", "outlook", "merger", "acquisition",
              "buyback", "payout", "default", "downgrade", "upgrade", "spread"]
STRUCTURAL_STEMS = ["listat", "listare", "delist", "fuziun", "preluar", "achiziț", "achizit", "dividen",
                    "majorar", "aga", "ipo", "debut", "ofertă publică", "merger", "acquisit", "takeover", "buyback"]
OPERATIONAL_STEMS = ["raport", "rezultat", "emisiun", "obligați", "obligati", "contract", "oprir", "produc",
                     "conducere", "earnings", "guidance", "revenue", "results"]
OPINION = ["what i'm watching", "what i am watching", "my take", "opinion", "column", "commentary",
           "editorial", "op-ed", "podcast", "newsletter", "wrap:", "session wrap", "week ahead", "explainer",
           "analiză:", "opinie", "comentariu", "punct de vedere", "how to", "should you", "is it time to"]
LOCAL_FISCAL = ["jocuri de noroc", "păcănele", "pacanele", "taxe locale", "impozit pe clădiri",
                "impozit pe cladiri", "taxa de salubritate", "amenzi", "taxa auto"]
FIGURES_RE = re.compile(r"\b\d+([.,]\d+)?\s*(%|mld|miliard|milioane|mil\.|euro|lei|ron|usd|\$|€|bps|puncte|"
                        r"billion|million|bn|pct)", re.I)


def score_item(it: dict, now: float | None = None) -> dict:
    """Primește o știre (title, summary, link, source, ts, region) și întoarce
    {score, level, reasons, category, region, tickers} – sau score=0 dacă nu trece filtrele."""
    now = now or time.time()
    title, body = it.get("title", ""), it.get("summary", "")
    text = _norm(f"{title} {body}")
    dom = domain_of(it.get("link", ""))
    tickers = extract_tickers(text)

    if any(d in dom for d in OFFICIAL_DOMAINS):
        tier, src_pts, src_reason = "official", 30, "Sursă oficială/autoritate financiară"
    elif any(d in dom for d in HIGH_REP_DOMAINS):
        tier, src_pts, src_reason = "high_rep", 22, "Sursă financiară de reputație"
    else:
        tier, src_pts, src_reason = "general", 12, "Sursă media generală"

    category = determine_category(text)
    region = determine_region(text, it.get("region", "ro"))
    base = {"category": category, "region": region, "tickers": tickers}

    if is_promo(text):
        return {**base, "score": 0, "level": "EXCLUS", "reasons": ["Teaser promoțional/abonament"]}
    if not is_strictly_financial(text, tier, bool(tickers)):
        return {**base, "score": 0, "level": "EXCLUS", "reasons": ["Nu este știre financiară"]}

    score, reasons = src_pts, [f"{src_reason} ({dom}) +{src_pts}"]

    # emitenți listați + eveniment corporativ
    if tickers:
        tp = 16 if len(tickers) == 1 else 18 if len(tickers) == 2 else 20
        score += tp
        reasons.append(f"Emitenți listați: {', '.join(tickers[:3])} +{tp}")
        if any_prefix(text, STRUCTURAL_STEMS):
            score += 12
            reasons.append("Eveniment corporativ structural (fuziune/listare/dividend/AGA) +12")
        elif any_prefix(text, OPERATIONAL_STEMS):
            score += 8
            reasons.append("Eveniment corporativ operațional (rezultate/contract/emisiune) +8")

    if FIGURES_RE.search(title):
        score += 3
        reasons.append("Cifre concrete în titlu +3")

    spec = sorted({k for k in SPECIFIC_KW if contains_word(text, k)})
    gen = sorted({k for k in GENERIC_KW if contains_word(text, k)})
    if len(spec) >= 2:
        kp = 30
    elif len(spec) == 1:
        kp = 22
    elif len(gen) >= 3:
        kp = 15
    elif len(gen) == 2:
        kp = 10
    elif len(gen) == 1:
        kp = 6
    else:
        kp = 3
    score += kp
    reasons.append(f"Termeni cheie ({', '.join((spec or gen)[:3]) or '—'}) +{kp}")

    has_specific = bool(spec or tickers)
    if category in ("rating", "macro", "fed", "bvb"):
        cp = 15 if has_specific else 5
    elif category in ("oil", "energy", "tech", "equity"):
        cp = 10 if (has_specific or gen) else 5
    else:
        cp = 5
    score += cp
    reasons.append(f"Categorie {category} +{cp}")

    hours = max(0.0, (now - it.get("ts", now)) / 3600)
    rp = 10 if hours < 2 else 8 if hours < 6 else 6 if hours < 12 else 4 if hours < 24 else 2
    score += rp
    reasons.append(f"Recență {hours:.1f}h +{rp}")

    n_sources = len(it.get("cluster_sources", [])) + 1
    if n_sources >= 3:
        score += 8
        reasons.append(f"Confirmat de {n_sources} surse +8")
    vel = it.get("cluster_velocity", 0) or 0
    if vel > 0:
        vb = min(15, round(vel * 5))
        if vb:
            score += vb
            reasons.append(f"Propagare rapidă ({vel} surse/oră) +{vb}")

    body_len = len(re.sub(r"<[^>]+>", "", body or "").strip())
    if body_len < 40:
        title_terms = sum(1 for k in STRONG_FIN + PRIMARY_FIN if contains_word(_norm(title), k))
        pen = 5 if (tickers or tier == "official") else 10 if title_terms >= 2 else 20
        score -= pen
        reasons.append(f"Conținut lipsă (doar titlu) -{pen}")
    elif body_len <= 160:
        score -= 5
        reasons.append("Conținut scurt -5")

    title_n = _norm(title)
    if any(o in title_n for o in OPINION):
        score -= 15
        reasons.append("Opinie / comentariu / rezumat de sesiune, nu știre -15")

    local_fiscal = any_word(text, LOCAL_FISCAL)
    if local_fiscal and not tickers and not has_specific:
        score -= 25
        reasons.append("Fiscalitate locală fără impact bursier -25")

    has_instrument = bool(tickers) or any_word(text, [
        "bet", "s&p 500", "nasdaq", "dow jones", "dax", "ftse", "nikkei", "stoxx", "titluri de stat", "fidelis",
        "tezaur", "obligațiuni", "obligatiuni", "acțiuni", "actiuni", "etf", "futures", "bnr", "fed",
        "federal reserve", "ecb", "bce", "fitch", "moody", "s&p", "asf", "anaf", "sec", "treasury", "bond"])
    final = max(0, min(100, round(score)))
    exempt = tier == "official" or category in ("rating", "fed", "macro")
    if not exempt and (not has_instrument or (local_fiscal and not tickers)) and final > 65:
        final = 65
        reasons.append("Plafonat la 65 (fără instrument, emitent sau autoritate)")

    level = "CRITIC" if final >= 80 else "RIDICAT" if final >= 60 else "MODERAT"
    return {**base, "score": final, "level": level, "reasons": reasons}
