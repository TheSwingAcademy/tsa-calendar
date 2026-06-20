# TSA Calendar Server v7 Hybrid

Hybrid backend for the TSA trading dashboard.

Long-range source:
- JBlanked Calendar API
- Date range: today + CALENDAR_DAYS
- Cache: JBLANKED_CACHE_TTL_SECONDS

Live source:
- ForexFactory/Faireconomy XML this-week feed
- Cache: LIVE_CACHE_TTL_SECONDS

Required Render variable:
JBLANKED_API_KEY

Recommended Render variables:
CALENDAR_DAYS=30
JBLANKED_SOURCE=mql5
JBLANKED_CACHE_TTL_SECONDS=86400
LIVE_CACHE_TTL_SECONDS=900
ENABLE_LIVE_FF=true
