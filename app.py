import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from difflib import SequenceMatcher

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, make_response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "900"))
ENABLE_FF_XML = os.getenv("ENABLE_FF_XML", "true").strip().lower() in {"1", "true", "yes"}
ENABLE_INVESTING_FALLBACK = os.getenv("ENABLE_INVESTING_FALLBACK", "true").strip().lower() in {"1", "true", "yes"}

FF_XML_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
INVESTING_CALENDAR_URL = "https://sslecal2.forexprostools.com/"

G10 = {"USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF"}

CACHE = {
    "timestamp": 0,
    "data": None,
    "error": None,
}


def clean(value):
    if value is None:
        return ""
    return " ".join(str(value).replace("\xa0", " ").split()).strip()


def text_of(node, tag):
    child = node.find(tag)
    if child is None or child.text is None:
        return ""
    return clean(child.text)


def normalize_impact(value):
    value = clean(value)
    lower = value.lower()

    if "high" in lower or lower in {"3", "red"}:
        return "High"
    if "medium" in lower or "med" in lower or lower in {"2", "orange"}:
        return "Medium"
    if "low" in lower or lower in {"1", "yellow"}:
        return "Low"
    if "holiday" in lower:
        return "Holiday"

    return value


def normalize_ff_date(date_text):
    raw = clean(date_text).replace("/", "-")
    if not raw:
        return ""

    formats = [
        "%m-%d-%Y",
        "%Y-%m-%d",
        "%d-%m-%Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except Exception:
            pass

    return raw


def normalize_ff_time(time_text):
    raw = clean(time_text)
    if not raw:
        return ""

    if raw.lower() in {"all day", "tentative"}:
        return raw

    compact = raw.lower().replace(" ", "")
    formats = ["%I:%M%p", "%I%p", "%H:%M"]

    for fmt in formats:
        try:
            return datetime.strptime(compact, fmt).strftime("%H:%M")
        except Exception:
            pass

    return raw


def fetch_ff_xml():
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
        ),
        "Accept": "application/xml,text/xml,*/*",
    }

    res = requests.get(FF_XML_URL, headers=headers, timeout=20)

    if res.status_code == 403:
        raise RuntimeError("ForexFactory/Faireconomy returned 403 Forbidden.")
    if res.status_code == 429:
        raise RuntimeError("ForexFactory/Faireconomy returned 429 Too Many Requests.")

    res.raise_for_status()

    root = ET.fromstring(res.content)
    events = []

    for node in root.findall(".//event"):
        event = {
            "date": normalize_ff_date(text_of(node, "date")),
            "time": normalize_ff_time(text_of(node, "time")),
            "currency": text_of(node, "country").upper(),
            "impact": normalize_impact(text_of(node, "impact")),
            "event": text_of(node, "title"),
            "actual": text_of(node, "actual"),
            "forecast": text_of(node, "forecast"),
            "previous": text_of(node, "previous"),
            "source": "forexfactory_xml",
        }

        if event["event"]:
            events.append(event)

    return events


def parse_investing_timestamp(raw):
    raw = clean(raw)
    if not raw:
        return "", ""

    formats = [
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d %H:%M",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
        except Exception:
            pass

    if " " in raw:
        date_part, time_part = raw.split(" ", 1)
        return date_part.replace("/", "-"), time_part[:5]

    return raw.replace("/", "-"), ""


def fetch_investing_fallback():
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.investing.com/economic-calendar/",
    }

    res = requests.get(INVESTING_CALENDAR_URL, headers=headers, timeout=25)

    if res.status_code == 403:
        raise RuntimeError("Investing/ForexPros returned 403 Forbidden.")
    if res.status_code == 429:
        raise RuntimeError("Investing/ForexPros returned 429 Too Many Requests.")

    res.raise_for_status()

    soup = BeautifulSoup(res.text, "html.parser")
    rows = soup.select("tr[id*='eventRowId']")
    events = []

    for row in rows:
        currency_el = row.select_one("td.flagCur")
        event_el = row.select_one("td.event")
        actual_el = row.select_one("td.act")
        forecast_el = row.select_one("td.fore")
        previous_el = row.select_one("td.prev")
        sentiment_el = row.select_one("td.sentiment")

        if not currency_el and not event_el:
            continue

        impact_count = 0
        if sentiment_el:
            impact_count = len(sentiment_el.select("i.grayFullBullishIcon"))

        date_part, time_part = parse_investing_timestamp(row.get("event_timestamp", ""))

        event = {
            "date": date_part,
            "time": time_part,
            "currency": clean(currency_el.get_text(" ", strip=True) if currency_el else "").upper(),
            "impact": normalize_impact(str(impact_count)),
            "event": clean(event_el.get_text(" ", strip=True) if event_el else ""),
            "actual": clean(actual_el.get_text(" ", strip=True) if actual_el else ""),
            "forecast": clean(forecast_el.get_text(" ", strip=True) if forecast_el else ""),
            "previous": clean(previous_el.get_text(" ", strip=True) if previous_el else ""),
            "source": "investing_forexpros_fallback",
        }

        if event["event"]:
            events.append(event)

    return events


def event_key(event):
    return (
        clean(event.get("date")),
        clean(event.get("currency")).upper(),
        re.sub(r"[^a-z0-9]+", " ", clean(event.get("event")).lower()).strip(),
    )


def event_similarity(a, b):
    a = re.sub(r"[^a-z0-9]+", " ", clean(a).lower()).strip()
    b = re.sub(r"[^a-z0-9]+", " ", clean(b).lower()).strip()

    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.9

    return SequenceMatcher(None, a, b).ratio()


def merge_events(primary, fallback):
    merged = [dict(e) for e in primary]
    used_fallback = set()

    for i, base in enumerate(merged):
        best_idx = None
        best_score = 0

        for j, fb in enumerate(fallback):
            if j in used_fallback:
                continue
            if clean(base.get("date")) != clean(fb.get("date")):
                continue
            if clean(base.get("currency")).upper() != clean(fb.get("currency")).upper():
                continue

            score = event_similarity(base.get("event"), fb.get("event"))

            if score > best_score:
                best_score = score
                best_idx = j

        if best_idx is not None and best_score >= 0.72:
            fb = fallback[best_idx]
            used_fallback.add(best_idx)

            updated = dict(base)

            for field in ("actual", "forecast", "previous"):
                if not clean(updated.get(field)) and clean(fb.get(field)):
                    updated[field] = clean(fb.get(field))

            if not clean(updated.get("time")) and clean(fb.get("time")):
                updated["time"] = clean(fb.get("time"))

            updated["sources"] = [base.get("source"), fb.get("source")]
            updated["fallback_match_score"] = round(best_score, 3)
            merged[i] = updated

    for j, fb in enumerate(fallback):
        if j not in used_fallback:
            item = dict(fb)
            item["sources"] = [fb.get("source")]
            item["fallback_only"] = True
            merged.append(item)

    return merged


def filter_sort_g10(events):
    cleaned = [e for e in events if e.get("event")]
    g10 = [e for e in cleaned if clean(e.get("currency")).upper() in G10]
    final = g10 if g10 else cleaned

    final.sort(key=lambda e: (
        clean(e.get("date")),
        clean(e.get("time")),
        clean(e.get("currency")),
        clean(e.get("event")),
    ))

    return final


def fetch_calendar():
    warnings = []
    ff_events = []
    inv_events = []

    if ENABLE_FF_XML:
        try:
            ff_events = fetch_ff_xml()
        except Exception as exc:
            warnings.append(f"FF XML failed: {exc}")

    if ENABLE_INVESTING_FALLBACK:
        try:
            inv_events = fetch_investing_fallback()
        except Exception as exc:
            warnings.append(f"Investing fallback failed: {exc}")

    if ff_events and inv_events:
        events = merge_events(ff_events, inv_events)
        source = "ForexFactory/Faireconomy XML + Investing/ForexPros fallback"
    elif ff_events:
        events = ff_events
        source = "ForexFactory/Faireconomy XML"
    elif inv_events:
        events = inv_events
        source = "Investing/ForexPros fallback"
    else:
        raise RuntimeError("All free calendar sources failed: " + " | ".join(warnings))

    events = filter_sort_g10(events)

    return {
        "status": "ok",
        "source": source,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "period": "this_week",
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "warnings": warnings,
        "count": len(events),
        "events": events,
    }


@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "message": "TSA calendar server is running",
        "endpoint": "/calendar",
        "provider": "Free this-week calendar",
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "enable_ff_xml": ENABLE_FF_XML,
        "enable_investing_fallback": ENABLE_INVESTING_FALLBACK,
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/calendar")
def calendar():
    now = time.time()

    if CACHE["data"] is not None and now - CACHE["timestamp"] < CACHE_TTL_SECONDS:
        data = dict(CACHE["data"])
        data["cached"] = True
        return jsonify(data)

    try:
        data = fetch_calendar()
        data["cached"] = False
        CACHE["timestamp"] = now
        CACHE["data"] = data
        CACHE["error"] = None
        return jsonify(data)

    except Exception as exc:
        CACHE["error"] = str(exc)

        if CACHE["data"] is not None:
            data = dict(CACHE["data"])
            data["cached"] = True
            data["warning"] = f"Live fetch failed, serving old cache: {exc}"
            return jsonify(data)

        return make_response(jsonify({
            "status": "error",
            "message": str(exc),
            "events": [],
        }), 500)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
