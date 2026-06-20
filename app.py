import os
import re
import time
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

import requests
from flask import Flask, jsonify, make_response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# Long-range source: JBlanked, intended for 30-day planning.
JBLANKED_API_KEY = os.getenv("JBLANKED_API_KEY", "").strip()
CALENDAR_DAYS = int(os.getenv("CALENDAR_DAYS", "30"))
JBLANKED_SOURCE = os.getenv("JBLANKED_SOURCE", "mql5").strip().lower()
JBLANKED_CACHE_TTL_SECONDS = int(os.getenv("JBLANKED_CACHE_TTL_SECONDS", "86400"))

# Live source: ForexFactory/Faireconomy XML, intended for same-week actual updates.
LIVE_CACHE_TTL_SECONDS = int(os.getenv("LIVE_CACHE_TTL_SECONDS", "900"))
ENABLE_LIVE_FF = os.getenv("ENABLE_LIVE_FF", "true").strip().lower() in {"1", "true", "yes"}

VALID_JBLANKED_SOURCES = {"mql5", "forex-factory", "fxstreet"}
if JBLANKED_SOURCE not in VALID_JBLANKED_SOURCES:
    JBLANKED_SOURCE = "mql5"

JBLANKED_URL = f"https://www.jblanked.com/news/api/{JBLANKED_SOURCE}/calendar/range/"
FF_XML_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

G10 = {"USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF"}

long_cache = {"timestamp": 0, "data": None, "error": None}
live_cache = {"timestamp": 0, "data": None, "error": None}


def clean(value):
    if value is None:
        return ""
    return " ".join(str(value).replace("\xa0", " ").split()).strip()


def text_of(node, tag):
    child = node.find(tag)
    if child is None or child.text is None:
        return ""
    return clean(child.text)


def parse_date_time(raw):
    raw = clean(raw)
    if not raw:
        return "", ""

    raw = raw.replace("/", "-")

    formats = [
        "%Y.%m.%d %H:%M:%S",
        "%Y.%m.%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
        "%m-%d-%Y %H:%M:%S",
        "%m-%d-%Y %H:%M",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(raw[:19], fmt)
            return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
        except Exception:
            pass

    if " " in raw:
        d, t = raw.split(" ", 1)
        return d.replace(".", "-"), t[:5]

    return raw.replace(".", "-"), ""


def normalize_impact(value):
    v = clean(value)
    l = v.lower()

    if "high" in l:
        return "High"
    if "medium" in l or "med" in l:
        return "Medium"
    if "low" in l:
        return "Low"
    if "holiday" in l:
        return "Holiday"
    if "none" in l:
        return "None"

    return v


def normalize_event_name(value):
    value = clean(value).lower()
    value = re.sub(r"\([^)]*\)", "", value)
    value = value.replace("prelim", "preliminary")
    value = value.replace("flash", "")
    value = value.replace("final", "")
    value = value.replace("m/m", "")
    value = value.replace("y/y", "")
    value = re.sub(r"[^a-z0-9 ]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def event_similarity(a, b):
    a = normalize_event_name(a)
    b = normalize_event_name(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.92
    return SequenceMatcher(None, a, b).ratio()


def normalize_jblanked_event(item):
    date_raw = (
        item.get("Date")
        or item.get("date")
        or item.get("datetime")
        or item.get("time")
        or ""
    )
    date_part, time_part = parse_date_time(date_raw)

    currency = clean(
        item.get("Currency")
        or item.get("currency")
        or item.get("Country")
        or item.get("country")
        or ""
    ).upper()

    return {
        "date": date_part,
        "time": time_part,
        "currency": currency,
        "impact": normalize_impact(item.get("Impact") or item.get("impact")),
        "event": clean(item.get("Name") or item.get("name") or item.get("Event") or item.get("event")),
        "category": clean(item.get("Category") or item.get("category")),
        "actual": clean(item.get("Actual") or item.get("actual")),
        "forecast": clean(item.get("Forecast") or item.get("forecast")),
        "previous": clean(item.get("Previous") or item.get("previous")),
        "outcome": clean(item.get("Outcome") or item.get("outcome")),
        "strength": clean(item.get("Strength") or item.get("strength")),
        "quality": clean(item.get("Quality") or item.get("quality")),
        "source": "jblanked",
    }


def normalize_ff_date(date_text):
    # ForexFactory XML date is commonly MM-DD-YYYY.
    raw = clean(date_text)
    if not raw:
        return ""

    raw = raw.replace("/", "-")

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
    if not raw or raw.lower() in {"all day", "tentative"}:
        return raw

    raw_lower = raw.lower().replace(" ", "")
    formats = ["%I:%M%p", "%I%p", "%H:%M"]

    for fmt in formats:
        try:
            return datetime.strptime(raw_lower, fmt).strftime("%H:%M")
        except Exception:
            pass

    return raw


def normalize_ff_event(node):
    return {
        "date": normalize_ff_date(text_of(node, "date")),
        "time": normalize_ff_time(text_of(node, "time")),
        "currency": text_of(node, "country").upper(),
        "impact": normalize_impact(text_of(node, "impact")),
        "event": text_of(node, "title"),
        "actual": text_of(node, "actual"),
        "forecast": text_of(node, "forecast"),
        "previous": text_of(node, "previous"),
        "source": "forexfactory_live",
    }


def fetch_jblanked():
    if not JBLANKED_API_KEY:
        raise RuntimeError("Missing JBLANKED_API_KEY environment variable in Render.")

    today = datetime.now(timezone.utc).date()
    start_date = today.isoformat()
    end_date = (today + timedelta(days=CALENDAR_DAYS)).isoformat()

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {JBLANKED_API_KEY}",
        "User-Agent": "TSA-Calendar-Server/1.0",
    }
    params = {"from": start_date, "to": end_date}

    res = requests.get(JBLANKED_URL, headers=headers, params=params, timeout=25)

    if res.status_code == 401:
        raise RuntimeError("JBlanked rejected the API key.")
    if res.status_code == 402:
        raise RuntimeError("JBlanked returned 402 Payment Required.")
    if res.status_code == 403:
        raise RuntimeError("JBlanked returned 403 Forbidden.")
    if res.status_code == 429:
        raise RuntimeError("JBlanked returned 429 Too Many Requests. Keep cache at 86400.")

    res.raise_for_status()
    raw = res.json()

    if isinstance(raw, dict):
        for key in ("data", "events", "results", "calendar"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
        else:
            if raw.get("message"):
                raise RuntimeError(str(raw.get("message")))
            raw = []

    if not isinstance(raw, list):
        raw = []

    events = [normalize_jblanked_event(x) for x in raw if isinstance(x, dict)]
    events = [e for e in events if e.get("event")]
    g10 = [e for e in events if e.get("currency") in G10]

    return {
        "from": start_date,
        "to": end_date,
        "events": g10 if g10 else events,
    }


def fetch_ff_live():
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
    events = [normalize_ff_event(e) for e in root.findall(".//event")]
    events = [e for e in events if e.get("event")]
    g10 = [e for e in events if e.get("currency") in G10]

    return g10 if g10 else events


def get_long_events():
    now = time.time()
    if long_cache["data"] is not None and now - long_cache["timestamp"] < JBLANKED_CACHE_TTL_SECONDS:
        return long_cache["data"], True, long_cache["error"]

    try:
        data = fetch_jblanked()
        long_cache["timestamp"] = now
        long_cache["data"] = data
        long_cache["error"] = None
        return data, False, None
    except Exception as exc:
        long_cache["error"] = str(exc)
        if long_cache["data"] is not None:
            return long_cache["data"], True, str(exc)
        raise


def get_live_events():
    if not ENABLE_LIVE_FF:
        return [], True, "Live FF disabled."

    now = time.time()
    if live_cache["data"] is not None and now - live_cache["timestamp"] < LIVE_CACHE_TTL_SECONDS:
        return live_cache["data"], True, live_cache["error"]

    try:
        data = fetch_ff_live()
        live_cache["timestamp"] = now
        live_cache["data"] = data
        live_cache["error"] = None
        return data, False, None
    except Exception as exc:
        live_cache["error"] = str(exc)
        if live_cache["data"] is not None:
            return live_cache["data"], True, str(exc)
        return [], False, str(exc)


def merge_events(long_events, live_events):
    merged = [dict(e) for e in long_events]
    used_live = set()

    for i, base in enumerate(merged):
        best_idx = None
        best_score = 0

        for j, live in enumerate(live_events):
            if j in used_live:
                continue
            if clean(base.get("currency")).upper() != clean(live.get("currency")).upper():
                continue
            if clean(base.get("date")) != clean(live.get("date")):
                continue

            score = event_similarity(base.get("event"), live.get("event"))

            if score > best_score:
                best_score = score
                best_idx = j

        if best_idx is not None and best_score >= 0.72:
            live = live_events[best_idx]
            used_live.add(best_idx)

            updated = dict(base)
            updated["live_source_matched"] = True
            updated["live_match_score"] = round(best_score, 3)

            # Prefer live values when available.
            for field in ("actual", "forecast", "previous"):
                if clean(live.get(field)):
                    updated[field] = clean(live.get(field))

            if not clean(updated.get("time")) and clean(live.get("time")):
                updated["time"] = clean(live.get("time"))
            if not clean(updated.get("impact")) and clean(live.get("impact")):
                updated["impact"] = clean(live.get("impact"))

            updated["sources"] = ["jblanked", "forexfactory_live"]
            merged[i] = updated

    # Add live events missing from JBlanked, so same-week releases still show.
    for j, live in enumerate(live_events):
        if j not in used_live:
            item = dict(live)
            item["sources"] = ["forexfactory_live"]
            item["live_source_only"] = True
            merged.append(item)

    merged.sort(key=lambda e: (clean(e.get("date")), clean(e.get("time")), clean(e.get("currency")), clean(e.get("event"))))
    return merged


@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "message": "TSA calendar server is running",
        "endpoint": "/calendar",
        "provider": "Hybrid: JBlanked long-range + ForexFactory/Faireconomy live",
        "has_jblanked_api_key": bool(JBLANKED_API_KEY),
        "calendar_days": CALENDAR_DAYS,
        "jblanked_source": JBLANKED_SOURCE,
        "jblanked_cache_ttl_seconds": JBLANKED_CACHE_TTL_SECONDS,
        "live_cache_ttl_seconds": LIVE_CACHE_TTL_SECONDS,
        "enable_live_ff": ENABLE_LIVE_FF,
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/calendar")
def calendar():
    try:
        long_data, long_cached, long_warning = get_long_events()
        live_events, live_cached, live_warning = get_live_events()

        events = merge_events(long_data.get("events", []), live_events)

        warnings = []
        if long_warning:
            warnings.append(f"JBlanked warning: {long_warning}")
        if live_warning:
            warnings.append(f"Live source warning: {live_warning}")

        return jsonify({
            "status": "ok",
            "source": "Hybrid: JBlanked + ForexFactory/Faireconomy",
            "from": long_data.get("from"),
            "to": long_data.get("to"),
            "calendar_days": CALENDAR_DAYS,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "jblanked_cached": long_cached,
            "live_cached": live_cached,
            "jblanked_count": len(long_data.get("events", [])),
            "live_count": len(live_events),
            "count": len(events),
            "warnings": warnings,
            "events": events,
        })

    except Exception as exc:
        return make_response(jsonify({
            "status": "error",
            "message": str(exc),
            "events": [],
        }), 500)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
