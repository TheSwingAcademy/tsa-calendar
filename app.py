import os
import time
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

import requests
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

FF_XML_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"


def text_of(node, tag):
    child = node.find(tag)
    if child is None or child.text is None:
        return ""
    return child.text.strip()


def normalize_impact(value):
    value = (value or "").strip()
    lower = value.lower()

    if "high" in lower:
        return "High"
    if "medium" in lower or "med" in lower:
        return "Medium"
    if "low" in lower:
        return "Low"
    if "holiday" in lower:
        return "Holiday"

    return value


def fetch_calendar_xml():
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
        ),
        "Accept": "application/xml,text/xml,*/*",
    }

    response = requests.get(FF_XML_URL, headers=headers, timeout=20)
    response.raise_for_status()

    root = ET.fromstring(response.content)

    events = []

    for event in root.findall(".//event"):
        title = text_of(event, "title")
        currency = text_of(event, "country")

        if not title and not currency:
            continue

        events.append({
            "date": text_of(event, "date"),
            "time": text_of(event, "time"),
            "currency": currency,
            "impact": normalize_impact(text_of(event, "impact")),
            "event": title,
            "actual": text_of(event, "actual"),
            "forecast": text_of(event, "forecast"),
            "previous": text_of(event, "previous"),
            "url": text_of(event, "url"),
        })

    return {
        "status": "ok",
        "source": "Forex Factory XML",
        "source_url": FF_XML_URL,
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
        data = fetch_calendar_xml()
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
