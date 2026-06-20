import os
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, make_response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "3600"))
CACHE = {
    "timestamp": 0,
    "data": None,
    "error": None,
}

FOREX_FACTORY_URL = "https://www.forexfactory.com/calendar"


def _impact_from_class(classes):
    text = " ".join(classes or []).lower()
    if "high" in text:
        return "High"
    if "medium" in text:
        return "Medium"
    if "low" in text:
        return "Low"
    if "holiday" in text:
        return "Holiday"
    return ""


def fetch_forex_factory_calendar():
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    response = requests.get(FOREX_FACTORY_URL, headers=headers, timeout=20)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    rows = soup.select("tr.calendar__row")

    events = []
    current_date = ""

    for row in rows:
        date_cell = row.select_one(".calendar__date")
        if date_cell and date_cell.get_text(strip=True):
            current_date = date_cell.get_text(" ", strip=True)

        time_cell = row.select_one(".calendar__time")
        currency_cell = row.select_one(".calendar__currency")
        event_cell = row.select_one(".calendar__event")
        actual_cell = row.select_one(".calendar__actual")
        forecast_cell = row.select_one(".calendar__forecast")
        previous_cell = row.select_one(".calendar__previous")
        impact_cell = row.select_one(".calendar__impact")

        currency = currency_cell.get_text(" ", strip=True) if currency_cell else ""
        title = event_cell.get_text(" ", strip=True) if event_cell else ""

        if not currency and not title:
            continue

        impact = ""
        if impact_cell:
            impact = impact_cell.get("title") or _impact_from_class(impact_cell.get("class"))

        events.append({
            "date": current_date,
            "time": time_cell.get_text(" ", strip=True) if time_cell else "",
            "currency": currency,
            "impact": impact,
            "event": title,
            "actual": actual_cell.get_text(" ", strip=True) if actual_cell else "",
            "forecast": forecast_cell.get_text(" ", strip=True) if forecast_cell else "",
            "previous": previous_cell.get_text(" ", strip=True) if previous_cell else "",
        })

    return {
        "source": "Forex Factory",
        "source_url": FOREX_FACTORY_URL,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "count": len(events),
        "events": events,
    }


@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "message": "TSA calendar server is running",
        "endpoint": "/calendar",
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
        data = fetch_forex_factory_calendar()
        data["cached"] = False
        CACHE["timestamp"] = now
        CACHE["data"] = data
        CACHE["error"] = None
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
