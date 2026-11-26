"""
Postări programate, pe lângă știri (rulează în aceeași execuție la 5 minute, fiecare o singură dată pe zi/săptămână):

  • curs BNR             – zilnic (L–V), după ora 13:15, din fluxul XML oficial BNR
  • închidere BVB         – zilnic (L–V), după ora 18:00, dacă sursa de cotații răspunde
  • recapitularea săptămânii – vineri, după ora 18:30 (știrile publicate în săptămână + curs)
  • raport de performanță  – vineri, după ora 19:00, ca „issue” pe GitHub (primești e-mail)

Toate postările sunt strict informative; nu conțin recomandări de investiții.
"""

from __future__ import annotations

import json
import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
LOG_FILE = BASE / "posts_log.json"
UA = {"User-Agent": "Mozilla/5.0 (compatible; VVFinanteNewsBot/1.0)"}
RO_TZ = 2
DISCLAIMER = "ℹ️ Informație cu caracter general, nu reprezintă recomandare de investiții."
ZILE = ["luni", "marți", "miercuri", "joi", "vineri", "sâmbătă", "duminică"]


def _ro_now() -> time.struct_time:
    return time.gmtime(time.time() + RO_TZ * 3600)


def _today() -> str:
    return time.strftime("%Y-%m-%d", _ro_now())


def _fmt_ro(x: float, dec: int = 4) -> str:
    return f"{x:,.{dec}f}".replace(",", " ").replace(".", ",")


def _pct(new: float, old: float) -> str:
    if not old:
        return ""
    ch = (new / old - 1) * 100
    arrow = "▲" if ch > 0 else "▼" if ch < 0 else "▬"
    return f"{arrow} {ch:+.2f}%".replace(".", ",")


# ------------------------------------------------------------------ jurnal postări

def load_log() -> list[dict]:
    try:
        return json.loads(LOG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def append_log(entry: dict) -> None:
    log = load_log()
    log.append(entry)
    cutoff = time.time() - 60 * 86400
    log = [e for e in log if e.get("ts", 0) >= cutoff]
    LOG_FILE.write_text(json.dumps(log, ensure_ascii=False, indent=0), encoding="utf-8")


# ------------------------------------------------------------------ curs BNR

def bnr_rates() -> tuple[str, dict, dict]:
    """Întoarce (data, cursuri_azi, cursuri_ziua_precedentă) din XML-ul BNR pe 10 zile."""
    r = requests.get("https://www.bnr.ro/nbrfxrates10days.xml", headers=UA, timeout=20)
    r.raise_for_status()
    ns = {"b": "http://www.bnr.ro/xsd"}
    root = ET.fromstring(r.content)
    cubes = root.findall(".//b:Cube", ns)
    days = []
    for c in cubes:
        rates = {}
        for rate in c.findall("b:Rate", ns):
            mult = float(rate.get("multiplier", "1"))
            rates[rate.get("currency")] = float(rate.text) / mult
        days.append((c.get("date"), rates))
    days.sort(key=lambda d: d[0])
    if len(days) < 2:
        raise RuntimeError("XML BNR incomplet")
    return days[-1][0], days[-1][1], days[-2][1]


def bnr_post() -> str:
    date, now, prev = bnr_rates()
    if date != _today():
        raise RuntimeError(f"BNR nu a publicat încă azi (ultima dată: {date})")
    lines = [f"💶 Cursul BNR pentru {time.strftime('%d.%m.%Y', time.strptime(date, '%Y-%m-%d'))}", ""]
    for cur, flag in (("EUR", "🇪🇺"), ("USD", "🇺🇸"), ("GBP", "🇬🇧"), ("CHF", "🇨🇭")):
        if cur in now:
            lines.append(f"{flag} 1 {cur} = {_fmt_ro(now[cur])} lei  ({_pct(now[cur], prev.get(cur, 0))} față de ziua precedentă)")
    if "XAU" in now:
        lines.append(f"🥇 1 g aur = {_fmt_ro(now['XAU'], 2)} lei  ({_pct(now['XAU'], prev.get('XAU', 0))})")
    lines += ["", "Sursa: Banca Națională a României", "#CursBNR #EURRON #Leu"]
    return "\n".join(lines)


# ------------------------------------------------------------------ închidere BVB

YAHOO_INDEX_CANDIDATES = ["^BETI", "^BET", "BET.RO"]
BVB_STOCKS = {"TLV.RO": "Banca Transilvania", "H2O.RO": "Hidroelectrica", "SNP.RO": "OMV Petrom",
              "SNG.RO": "Romgaz", "BRD.RO": "BRD", "SNN.RO": "Nuclearelectrica", "TGN.RO": "Transgaz",
              "DIGI.RO": "Digi", "M.RO": "MedLife", "ONE.RO": "One United", "EL.RO": "Electrica",
              "TEL.RO": "Transelectrica", "FP.RO": "Fondul Proprietatea", "AQ.RO": "Aquila", "TRP.RO": "Teraplast"}


def yahoo_last(symbol: str) -> tuple[float, float, str]:
    """(ultimul preț de închidere, închiderea precedentă, data) din Yahoo Finance."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(symbol)}"
    r = requests.get(url, params={"range": "5d", "interval": "1d"}, headers=UA, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
    ts = [t for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]) if c is not None]
    if len(closes) < 2:
        raise RuntimeError(f"date insuficiente pentru {symbol}")
    return closes[-1], closes[-2], time.strftime("%Y-%m-%d", time.gmtime(ts[-1] + RO_TZ * 3600))


def bvb_post() -> str:
    index_line = ""
    for sym in YAHOO_INDEX_CANDIDATES:
        try:
            last, prev, d = yahoo_last(sym)
            if d == _today():
                index_line = f"📊 Indicele BET: {_fmt_ro(last, 2)} puncte ({_pct(last, prev)})"
            break
        except Exception:
            continue
    moves = []
    for sym, name in BVB_STOCKS.items():
        try:
            last, prev, d = yahoo_last(sym)
            if d == _today() and prev:
                moves.append((name, sym.split(".")[0], last, (last / prev - 1) * 100))
        except Exception:
            continue
    if len(moves) < 5:
        raise RuntimeError(f"cotații BVB indisponibile (doar {len(moves)} emitenți)")
    moves.sort(key=lambda m: m[3], reverse=True)
    up = [m for m in moves if m[3] > 0][:3]
    down = [m for m in sorted(moves, key=lambda m: m[3]) if m[3] < 0][:3]
    lines = [f"🔔 Închiderea Bursei de Valori București – {time.strftime('%d.%m.%Y', _ro_now())}", ""]
    if index_line:
        lines += [index_line, ""]
    if up:
        lines.append("Cele mai mari creșteri (din emitenții urmăriți):")
        lines += [f"  ▲ {n} ({s}): {_fmt_ro(p, 2)} lei, {c:+.2f}%".replace(".", ",") for n, s, p, c in up]
    if down:
        lines.append("Cele mai mari scăderi (din emitenții urmăriți):")
        lines += [f"  ▼ {n} ({s}): {_fmt_ro(p, 2)} lei, {c:+.2f}%".replace(".", ",") for n, s, p, c in down]
    lines += ["", "Date de închidere; pot exista mici diferențe față de datele oficiale BVB.", "#BVB #BET #Bursa"]
    return "\n".join(lines)


# ------------------------------------------------------------------ recapitularea săptămânii

def weekly_post(ask_gemini_text) -> str:
    week_ago = time.time() - 7 * 86400
    items = [e for e in load_log() if e.get("ts", 0) >= week_ago and e.get("kind", "news") == "news"]
    if len(items) < 3:
        raise RuntimeError(f"prea puține știri publicate săptămâna aceasta ({len(items)})")
    items.sort(key=lambda e: e.get("score", 0), reverse=True)
    top = items[:6]
    listing = "\n".join(f"- [{e.get('category')}] {e['title']}" for e in top)
    intro = ask_gemini_text(
        "Scrie 2 propoziții în română care rezumă, factual și neutru, tema principală a săptămânii pe piețele "
        "financiare, pe baza titlurilor de mai jos. Fără recomandări de investiții, fără predicții, fără imperative. "
        "Răspunde STRICT în JSON: {\"text\": \"...\"}\n\n" + listing)
    lines = [f"🗓️ Săptămâna pe piețe ({time.strftime('%d.%m', time.gmtime(week_ago + RO_TZ*3600))}–"
             f"{time.strftime('%d.%m.%Y', _ro_now())})", "", intro, "", "Cele mai importante subiecte:"]
    lines += [f"• {e['title']}" for e in top]
    try:
        _, now, _ = bnr_rates()
        lines += ["", f"💶 EUR/RON (BNR): {_fmt_ro(now['EUR'])} lei"]
    except Exception:
        pass
    lines += ["", "#Săptămâna #PiețeFinanciare #BVB"]
    return "\n".join(lines)


# ------------------------------------------------------------------ raport de performanță

def performance_report(page_token: str, graph_version: str) -> str:
    week_ago = time.time() - 7 * 86400
    items = [e for e in load_log() if e.get("ts", 0) >= week_ago and e.get("post_id")]
    rows, by_cat = [], {}
    for e in items:
        try:
            d = requests.get(
                f"https://graph.facebook.com/{graph_version}/{e['post_id']}",
                params={"fields": "reactions.summary(true).limit(0),comments.summary(true).limit(0),shares",
                        "access_token": page_token}, timeout=20).json()
            rx = d.get("reactions", {}).get("summary", {}).get("total_count", 0)
            cm = d.get("comments", {}).get("summary", {}).get("total_count", 0)
            sh = d.get("shares", {}).get("count", 0)
        except Exception:
            rx = cm = sh = 0
        total = rx + cm * 2 + sh * 3
        rows.append((total, rx, cm, sh, e))
        c = by_cat.setdefault(e.get("category", "?"), [0, 0])
        c[0] += 1
        c[1] += total
    rows.sort(key=lambda r: r[0], reverse=True)
    md = [f"Postări publicate în ultimele 7 zile: **{len(rows)}**", "",
          "| Interacțiune* | Reacții | Comentarii | Distribuiri | Scor | Categorie | Titlu |",
          "|---:|---:|---:|---:|---:|---|---|"]
    for total, rx, cm, sh, e in rows:
        md.append(f"| {total} | {rx} | {cm} | {sh} | {e.get('score', '')} | {e.get('category', '')} | {e['title'][:90]} |")
    md += ["", "**Pe categorii** (interacțiune medie pe postare):", ""]
    for cat, (n, tot) in sorted(by_cat.items(), key=lambda kv: -kv[1][1] / max(1, kv[1][0])):
        md.append(f"- {cat}: {tot / max(1, n):.1f} ({n} postări)")
    md += ["", "*Interacțiune = reacții + 2×comentarii + 3×distribuiri."]
    return "\n".join(md)


def open_github_issue(title: str, body: str) -> None:
    token, repo = os.getenv("GITHUB_TOKEN"), os.getenv("GITHUB_REPOSITORY")
    if not token or not repo:
        raise RuntimeError("GITHUB_TOKEN/GITHUB_REPOSITORY lipsesc")
    r = requests.post(f"https://api.github.com/repos/{repo}/issues",
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                      json={"title": title, "body": body}, timeout=20)
    r.raise_for_status()


# ------------------------------------------------------------------ orchestrare

def due_tasks(posted: list[str]) -> list[str]:
    t = _ro_now()
    wd, hm = t.tm_wday, t.tm_hour * 60 + t.tm_min
    today = _today()
    tasks = []
    if wd < 5 and 13 * 60 + 15 <= hm < 16 * 60 and f"x:bnr:{today}" not in posted:
        tasks.append("bnr")
    if wd < 5 and 18 * 60 <= hm < 21 * 60 and f"x:bvb:{today}" not in posted:
        tasks.append("bvb")
    if wd == 4 and 18 * 60 + 30 <= hm < 21 * 60 + 30 and f"x:weekly:{today}" not in posted:
        tasks.append("weekly")
    if wd == 4 and 19 * 60 <= hm < 22 * 60 and f"x:report:{today}" not in posted:
        tasks.append("report")
    return tasks


def run(posted: list[str], publish, ask_gemini_text, violates, page_token: str, graph_version: str,
        log, dry_run: bool, force: list[str] | None = None) -> None:
    """Execută sarcinile scadente. `publish(message, link)` publică pe Pagină și întoarce id-ul postării."""
    tasks = force if force is not None else due_tasks(posted)
    for task in tasks:
        today = _today()
        try:
            if task == "report":
                body = performance_report(page_token, graph_version)
                if dry_run:
                    log("----- DRY RUN raport -----\n" + body)
                else:
                    open_github_issue(f"Raport săptămânal Pagina Facebook – {today}", body)
                    log("Raport săptămânal creat ca issue pe GitHub.")
                    posted.append(f"x:report:{today}")
                continue
            text = {"bnr": bnr_post, "bvb": bvb_post}.get(task)
            text = text() if text else weekly_post(ask_gemini_text)
            bad = violates(text)
            if bad:
                log(f"[{task}] text cu formulări de tip recomandare ({', '.join(bad)}); nu public.")
                continue
            message = f"{text}\n\n{DISCLAIMER}"
            if dry_run:
                log(f"----- DRY RUN {task} -----\n{message}\n-------------------")
                continue
            post_id = publish(message, None)
            log(f"[{task}] publicat: {post_id}")
            posted.append(f"x:{task}:{today}")
            append_log({"ts": time.time(), "kind": task, "title": text.splitlines()[0], "post_id": post_id})
        except Exception as exc:
            log(f"[{task}] amânat: {exc}")
