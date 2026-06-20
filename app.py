import os
import time
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, make_response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "3600"))
FMP_API_KEY = os.getenv("FMP_API_KEY", "").strip()

CACHE = {
    "timestamp": 0,
    "data": None,
}

FMP_STABLE_URL = "https://financialmodelingprep.com/stable/economic-calendar"
FMP_LEGACY_URL = "https://financialmodelingprep.com/api/v3/economic_calendar"

COUNTRY_TO_CURRENCY = {
    "united states": "USD",
    "usa": "USD",
    "us": "USD",
    "euro area": "EUR",
    "eurozone": "EUR",
    "european union": "EUR",
    "germany": "EUR",
    "france": "EUR",
    "italy": "EUR",
    "spain": "EUR",
    "netherlands": "EUR",
    "united kingdom": "GBP",
    "uk": "GBP",
    "great britain": "GBP",
    "japan": "JPY",
    "australia": "AUD",
    "new zealand": "NZD",
    "canada": "CAD",
    "switzerland": "CHF",
    "china": "CNY",
}

G10 = {"USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF"}


def clean(value):
    if value is None:
        return ""
    return str(value).strip()


def currency_from_country(country):
    c = clean(country).lower()
    if len(c) == 3 and c.upper() in G10:
        return c.upper()
    return COUNTRY_TO_CURRENCY.get(c, clean(country).upper() if len(clean(country)) == 3 else "")


def normalize_impact(value):
    v = clean(value)
    l = v.lower()
    if "high" in l or l in {"3", "red"}:
        return "High"
    if "medium" in l or "moderate" in l or l in {"2", "orange"}:
        return "Medium"
    if "low" in l or l in {"1", "yellow"}:
        return "Low"
    return v


def split_date_time(date_value):
    raw = clean(date_value)
    if not raw:
        return "", ""

    # Keep date usable even when provider returns ISO datetime.
    if "T" in raw:
        left, right = raw.split("T", 1)
        return left, right[:5]

    if " " in raw:
        left, right = raw.split(" ", 1)
        return left, right[:5]

    return raw, ""


def normalize_event(item):
    country = clean(
        item.get("country")
        or item.get("region")
        or item.get("area")
        or item.get("economy")
    )

    currency = clean(item.get("currency") or item.get("ccy") or currency_from_country(country)).upper()

    date_raw = clean(
        item.get("date")
        or item.get("releaseDate")
        or item.get("releasedAt")
        or item.get("calendarDate")
    )

    date_part, time_part = split_date_time(date_raw)

    return {
        "date": date_part,
        "time": clean(item.get("time") or time_part),
        "currency": currency,
        "country": country,
        "impact": normalize_impact(item.get("impact") or item.get("importance") or item.get("level")),
        "event": clean(item.get("event") or item.get("name") or item.get("title")),
        "actual": clean(item.get("actual") or item.get("actualValue")),
        "forecast": clean(item.get("forecast") or item.get("estimate") or item.get("consensus")),
        "previous": clean(item.get("previous") or item.get("prior")),
        "source_raw": item,
    }


def request_fmp(url, start_date, end_date):
    params = {
        "from": start_date,
        "to": end_date,
        "apikey": FMP_API_KEY,
    }

    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()

    data = response.json()

    if isinstance(data, dict):
        if "Error Message" in data:
            raise RuntimeError(data["Error Message"])
        if "error" in data:
            raise RuntimeError(str(data["error"]))
        if "data" in data and isinstance(data["data"], list):
            return data["data"]
        if "historical" in data and isinstance(data["historical"], list):
            return data["historical"]

    if isinstance(data, list):
        return data

    return []


def fetch_calendar():
    if not FMP_API_KEY:
        raise RuntimeError("Missing FMP_API_KEY environment variable in Render.")

    today = datetime.now(timezone.utc).date()
    start_date = today.isoformat()
    end_date = (today + timedelta(days=30)).isoformat()

    last_error = None

    for url in (FMP_STABLE_URL, FMP_LEGACY_URL):
        try:
            raw_events = request_fmp(url, start_date, end_date)
            events = [normalize_event(item) for item in raw_events if isinstance(item, dict)]

            # Keep G10 FX-relevant events first, but do not destroy data if the provider omits currency.
            g10_events = [e for e in events if e.get("currency") in G10]
            final_events = g10_events if g10_events else events

            return {
                "status": "ok",
                "source": "Financial Modeling Prep",
                "source_url": url,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "from": start_date,
                "to": end_date,
                "count": len(final_events),
                "events": final_events,
            }

        except Exception as exc:
            last_error = exc

    raise RuntimeError(f"FMP calendar request failed: {last_error}")


@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "message": "TSA calendar server is running",
        "endpoint": "/calendar",
        "provider": "Financial Modeling Prep",
        "has_api_key": bool(FMP_API_KEY),
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

        return jsonify(data)

    except Exception as exc:
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
