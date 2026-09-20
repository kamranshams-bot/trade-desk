#!/usr/bin/env python3
"""Daily US day-trade brief generator.

1. Claude (with web search) finds today's catalysts: news, earnings, IPOs, analyst
   actions, macro events and sentiment, and proposes candidate tickers.
2. yfinance supplies REAL prices. Every candidate is checked (price, liquidity,
   volatility) and entry / stop / exit levels are computed from those prices with
   fixed rules (ATR based), never taken from the language model.
3. Writes data.json (read by the web app) and optionally emails the brief.

If anything fails validation the script exits non-zero WITHOUT touching data.json,
so the site keeps showing the last good brief.
"""
import datetime as dt
import json
import os
import re
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import anthropic
import pandas as pd
import yfinance as yf

MODEL = os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-5"
SEARCH_TOOL = os.environ.get("WEB_SEARCH_TOOL") or "web_search_20250305"
FORCE = (os.environ.get("FORCE") or "").lower() in ("1", "true", "yes")
DXB, ET = ZoneInfo("Asia/Dubai"), ZoneInfo("America/New_York")
CAPS = {"core": 3, "sector": 4, "low": 3}
MIN_PICKS = 3
DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data.json")

# ---- trading-day logic -------------------------------------------------------
try:
    import holidays
    NYSE = holidays.financial_holidays("NYSE", years=range(2026, 2031))
except Exception:  # library missing: fall back to weekends only
    NYSE = {}


def is_session(d):
    return d.weekday() < 5 and d not in NYSE


def next_session(now_et):
    d = now_et.date()
    if now_et.time() >= dt.time(16, 0):
        d += dt.timedelta(days=1)
    while not is_session(d):
        d += dt.timedelta(days=1)
    return d


# ---- Claude: catalysts and candidates ----------------------------------------
SYSTEM = (
    "You are a careful US equities analyst preparing a pre-market day-trading brief. "
    "Use web search for current facts. Never invent numbers, quotes, or sources. "
    "Paraphrase sources in your own words (no long quotes). Do NOT output prices, "
    "entry, stop or target levels: those are computed separately from market data. "
    "Output a single JSON object and nothing else."
)


def build_prompt(session, open_dubai):
    return f"""Today is {dt.datetime.now(DXB):%A %d %B %Y} in Dubai. The next US session is {session:%A %d %B %Y}
(opens {open_dubai} Dubai time). Research, using web search:
- US market news since the last close, futures / pre-market moves, macro events today and this week
  (Fed, yields, oil, crypto, data releases), and overall sentiment;
- earnings reporting before the open today and this week, with notable results and guidance;
- IPOs pricing or starting to trade this week;
- analyst upgrades/downgrades, M&A, big contracts, FDA or regulatory decisions, other catalysts.

Then choose LONG day-trade candidates that have a real, recent, verifiable catalyst:
- "core": up to 3 highest-conviction ideas.
- "sector": up to 4 ideas, each from a DIFFERENT sector (tech, financials, healthcare, energy, industrials, consumer, etc.).
- "low": up to 3 low-priced stocks (under $10) listed on NASDAQ, NYSE or NYSE American with average volume above 2M shares.
  Never OTC/pink-sheet names, never stocks under $1, never names whose move is unexplained or right after a share offering.
Also list up to 5 "avoid" tickers with a specific negative catalyst.

Return ONLY this JSON:
{{
 "notes": ["3 to 6 short strings on the backdrop: macro, rates, oil, crypto, sentiment"],
 "calendar": ["3 to 6 short strings: earnings, data releases, IPOs, events for today and this week"],
 "picks": [{{"ticker":"","name":"","sector":"","group":"core|sector|low","news":"2-3 factual sentences on the catalyst","risk":"one sentence","sources":["https://..."]}}],
 "avoid": [{{"ticker":"","reason":"one factual sentence"}}]
}}"""


def find_json(text):
    dec, best = json.JSONDecoder(), None
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except ValueError:
            continue
        if isinstance(obj, dict) and "picks" in obj:
            if best is None or len(json.dumps(obj)) > len(json.dumps(best)):
                best = obj
    return best


def ask_claude(session, open_dubai):
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": build_prompt(session, open_dubai)}]
    tools = [{"type": SEARCH_TOOL, "name": "web_search", "max_uses": 15}]
    for _ in range(5):  # allow continuation if the server pauses a long turn
        resp = client.messages.create(model=MODEL, max_tokens=8000, system=SYSTEM,
                                      messages=messages, tools=tools)
        if resp.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": resp.content})
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    obj = find_json(text)
    if not obj:
        raise RuntimeError("Claude did not return usable JSON")
    return obj


# ---- real market data ---------------------------------------------------------
def quote(ticker):
    try:
        h = yf.Ticker(ticker).history(period="6mo", interval="1d", auto_adjust=False)
        h = h.dropna(subset=["Close"])
    except Exception:
        return None
    if len(h) < 30:
        return None
    tr = pd.concat([h.High - h.Low, (h.High - h.Close.shift()).abs(),
                    (h.Low - h.Close.shift()).abs()], axis=1).max(axis=1)
    close = float(h.Close.iloc[-1])
    return {
        "close": close,
        "week": (close / float(h.Close.iloc[-6]) - 1) * 100,
        "atr": float(tr.rolling(14).mean().iloc[-1]),
        "avgvol": float(h.Volume.tail(20).mean()),
        "hi5": float(h.High.tail(5).max()),
        "date": h.index[-1].date().isoformat(),
    }


def r2(x):
    return round(x + 1e-9, 2)


def levels(q):
    c, atr = q["close"], q["atr"]
    lo, hi = c - 0.5 * atr, c - 0.15 * atr
    mid = (lo + hi) / 2
    brk = max(q["hi5"], c + 0.1 * atr)
    stop, t1, t2 = mid - 0.6 * atr, mid + 1.0 * atr, mid + 1.6 * atr
    if stop <= 0:
        return None
    return {
        "entry": f"Dip to ${r2(lo):,.2f}–{r2(hi):,.2f}, or break above ${r2(brk):,.2f} on volume",
        "entryMid": r2(mid), "stop": r2(stop), "t1": r2(t1), "t2": r2(t2),
        "exit": f"${r2(t1):,.2f} (half), ${r2(t2):,.2f} (rest)",
    }


def clean(s, n):
    return re.sub(r"\s+", " ", str(s or "")).strip()[:n]


def safe_sources(urls):
    out = []
    for u in (urls or [])[:3]:
        p = urlparse(str(u))
        if p.scheme == "https" and p.netloc:
            out.append(str(u)[:300])
    return out


def build_items(picks, avoid):
    items, seen, counts, sectors = [], set(), {"core": 0, "sector": 0, "low": 0}, set()
    for p in picks:
        t = clean(p.get("ticker"), 8).upper()
        g = p.get("group")
        if not re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", t) or t in seen or g not in CAPS:
            continue
        q = quote(t)
        if not q or q["close"] < 1.0:
            continue
        atr_pct = q["atr"] / q["close"] * 100
        if not (0.5 <= atr_pct <= 15):
            continue
        # group follows the real price
        g = "low" if q["close"] < 10 else ("sector" if g == "low" else g)
        if q["avgvol"] < (2e6 if g == "low" else 1e6):
            continue
        sector = clean(p.get("sector"), 40) or "Other"
        if g == "low":
            sector = "Low-priced (under $10)"
        if counts[g] >= CAPS[g]:
            continue
        lv = levels(q)
        if not lv:
            continue
        seen.add(t)
        counts[g] += 1
        items.append({
            "t": t, "name": clean(p.get("name"), 60) or t, "sector": sector, "group": g,
            "ref": r2(q["close"]), "refNote": f"close {q['date']}", "week": round(q["week"], 1),
            "news": clean(p.get("news"), 500), "risk": clean(p.get("risk"), 240),
            "sources": safe_sources(p.get("sources")), **lv,
        })
    avoid_out = []
    for a in avoid or []:
        t = clean(a.get("ticker"), 8).upper()
        if not re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", t) or t in seen:
            continue
        q = quote(t)
        if not q:
            continue
        seen.add(t)
        reason = clean(a.get("reason"), 300)
        avoid_out.append({"t": t, "reason": reason})
        items.append({"t": t, "name": clean(a.get("name"), 60) or t, "sector": "Avoid / fade",
                      "group": "avoid", "ref": r2(q["close"]), "week": round(q["week"], 1),
                      "news": reason + " No levels given."})
    return items, avoid_out


def market_stats():
    def chg(sym):
        h = yf.Ticker(sym).history(period="10d", interval="1d").dropna(subset=["Close"])
        c, p = float(h.Close.iloc[-1]), float(h.Close.iloc[-2])
        return c, (c / p - 1) * 100
    out = []
    for label, sym, fmt in [("S&P 500 (SPY)", "SPY", "pct"), ("Nasdaq-100 (QQQ)", "QQQ", "pct"),
                            ("10-yr yield", "^TNX", "yld"), ("Oil (WTI)", "CL=F", "usd"),
                            ("Bitcoin", "BTC-USD", "usd"), ("VIX", "^VIX", "vix")]:
        try:
            c, d = chg(sym)
            if fmt == "pct":
                out.append([label, f"{d:+.2f}%", "last session"])
            elif fmt == "yld":
                y = c / 10 if c > 20 else c
                out.append([label, f"{y:.2f}%", f"{d:+.1f}% day"])
            elif fmt == "usd":
                out.append([label, f"${c:,.0f}", f"{d:+.1f}% day"])
            else:
                out.append([label, f"{c:.1f}", f"{d:+.1f}% day"])
        except Exception:
            continue
    return out


# ---- optional email -----------------------------------------------------------
def email_html(data):
    def cell(bg, fg, label, val):
        return (f'<td width="33%" bgcolor="{bg}" style="background:{bg};color:{fg};padding:8px;'
                f'border-radius:6px;font-size:13px;text-align:center"><b>{label}</b><br>{val}</td>')
    esc = lambda x: (str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    parts = [f'<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:640px;margin:auto;color:#1F2937">'
             f'<div style="background:#14213D;color:#fff;padding:18px 20px;border-radius:8px 8px 0 0">'
             f'<div style="font-size:13px;color:#9FB3D1">{esc(data["kicker"])}</div>'
             f'<div style="font-size:24px;font-weight:700">US Day-Trade Brief</div>'
             f'<div style="font-size:13px;color:#C9D6EA">{esc(data["asOf"])}</div></div>',
             '<p style="font-size:12px;background:#FFF4D6;padding:8px 10px;color:#5C4300"><b>Not investment advice.</b> '
             'Day trading and penny stocks carry a high risk of loss. Verify prices and news before acting.</p>',
             "<h3>Market backdrop</h3><ul>" + "".join(f"<li>{esc(n)}</li>" for n in data["notes"]) + "</ul>",
             "<p style='font-size:13px'>" + " · ".join(f"<b>{esc(a)}</b> {esc(b)}" for a, b, _ in data["stats"]) + "</p>"]
    for it in data["items"]:
        if "entry" not in it:
            continue
        parts.append(
            f'<div style="border:1px solid #DDE3EC;border-radius:8px;padding:12px 14px;margin:10px 0">'
            f'<div><b style="font-size:19px">{esc(it["t"])}</b> <span style="color:#6B7280">{esc(it["name"])} · {esc(it["sector"])} · ~${it["ref"]}</span></div>'
            f'<p style="font-size:14px;margin:6px 0">{esc(it["news"])}</p>'
            f'<table role="presentation" width="100%" cellspacing="4"><tr>'
            + cell("#E8F5EF", "#14603F", "Entry", esc(it["entry"]))
            + cell("#FBEAEC", "#9B2C3B", "Stop", f"${it['stop']}")
            + cell("#EAF0FB", "#1E3F7A", "Exit", esc(it["exit"]))
            + f'</tr></table><div style="font-size:12px;color:#6B7280"><b>Risk:</b> {esc(it["risk"])}</div></div>')
    if data["avoid"]:
        parts.append("<h3>Avoid or fade</h3><ul>" + "".join(
            f"<li><b>{esc(a['t'])}</b> {esc(a['reason'])}</li>" for a in data["avoid"]) + "</ul>")
    parts.append("<h3>Calendar</h3><ul>" + "".join(f"<li>{esc(c)}</li>" for c in data["calendar"]) + "</ul></div>")
    return "".join(parts)


def send_email(data):
    host, to = os.environ.get("SMTP_HOST"), os.environ.get("MAIL_TO")
    if not (host and to):
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"US Day-Trade Brief – {data['sessionDate']}"
    msg["From"] = os.environ.get("MAIL_FROM") or os.environ.get("SMTP_USER", "")
    msg["To"] = to
    msg.attach(MIMEText("Open this email in an HTML-capable client.", "plain"))
    msg.attach(MIMEText(email_html(data), "html"))
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=30) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.sendmail(msg["From"], [a.strip() for a in to.split(",")], msg.as_string())


# ---- main ---------------------------------------------------------------------
def main():
    now_et, now_dxb = dt.datetime.now(ET), dt.datetime.now(DXB)
    if not FORCE and not is_session(now_et.date()):
        print("Not a US trading day; nothing to do.")
        return 0
    session = next_session(now_et)
    open_dubai = dt.datetime.combine(session, dt.time(9, 30), ET).astimezone(DXB).strftime("%-I:%M%p").lower()

    spy = quote("SPY")
    if not spy:
        print("Could not fetch market data.", file=sys.stderr)
        return 1
    try:
        prev = json.load(open(DATA_PATH, encoding="utf-8"))
    except Exception:
        prev = {}
    if not FORCE and prev.get("barDate") == spy["date"] and prev.get("sessionDate") == session.isoformat():
        print("Already generated for this session.")
        return 0

    raw = ask_claude(session, open_dubai)
    items, avoid = build_items(raw.get("picks", []), raw.get("avoid", []))
    picks = [i for i in items if i["group"] in CAPS]
    if len(picks) < MIN_PICKS:
        print(f"Only {len(picks)} valid picks after validation; keeping previous data.json.", file=sys.stderr)
        return 1

    data = {
        "asOf": f"Updated {now_dxb:%a %d %b %Y, %H:%M} Dubai · prices from {spy['date']} close · for the {session:%a %d %b} session",
        "kicker": f"Dubai desk · US open {open_dubai} Dubai (9:30am ET)",
        "barDate": spy["date"], "sessionDate": session.isoformat(),
        "stats": market_stats(),
        "notes": [clean(n, 220) for n in raw.get("notes", [])][:6],
        "calendar": [clean(c, 220) for c in raw.get("calendar", [])][:6],
        "avoid": avoid, "items": items,
    }
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    print(f"Wrote {len(picks)} picks, {len(avoid)} avoids.")
    try:
        send_email(data)
    except Exception as e:  # email failure must not block the site update
        print("Email failed:", e, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
