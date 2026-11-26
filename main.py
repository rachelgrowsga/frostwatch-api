"""FrostWatch starter API.

GET /frost-risk?zip=30301 returns a 3-night frost/freeze outlook built from
the National Weather Service hourly forecast.

The scoring in score_night() is a simple STARTER method. Replace it with
your own thresholds and logic.
"""
import re
from datetime import datetime, timedelta

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

# NWS asks every client to identify itself. Put your real contact info here.
USER_AGENT = "FrostWatch (contact: rachelgrowsga@gmail.com)"

app = FastAPI(title="FrostWatch API")

# Starter setting: any site may call this API. Once your Wix domain is final,
# replace ["*"] with a list such as ["https://www.myfrostwatch.com"].
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Simple in-memory cache so repeat lookups don't hammer free services.
_cache: dict = {}
CACHE_MINUTES = 30


def c_to_f(c):
    return None if c is None else c * 9 / 5 + 32


def parse_wind_mph(text):
    """NWS gives wind like '5 mph' or '5 to 10 mph'. Return the top number."""
    nums = [int(n) for n in re.findall(r"\d+", text or "")]
    return max(nums) if nums else 0


def score_night(low_f, dew_f, wind_mph):
    """Return (level, label, note) for one night.

    Levels: 0 none, 1 frost possible, 2 frost likely, 3 freeze, 4 hard freeze.
    """
    if low_f <= 28:
        return 4, "Hard freeze", "28°F or colder. Tender and many hardy plants at risk."
    if low_f <= 32:
        return 3, "Freeze", "32°F or colder. Cover or bring in tender plants."
    if low_f <= 36:
        # Frost needs a cold surface, so a dry, breezy night lowers the odds.
        spread = low_f - dew_f if dew_f is not None else 0
        if wind_mph <= 7 and spread <= 6:
            return 2, "Frost likely", "Calm, humid and near freezing. Frost can form on leaves."
        return 1, "Frost possible", "Near freezing, but wind or dry air may keep frost off."
    return 0, "No frost", "Lows stay above frost range."


async def zip_to_latlon(client, zip_code):
    r = await client.get(f"https://api.zippopotam.us/us/{zip_code}")
    if r.status_code != 200:
        raise HTTPException(404, "Zip code not found.")
    place = r.json()["places"][0]
    return float(place["latitude"]), float(place["longitude"]), (
        f'{place["place name"]}, {place["state abbreviation"]}'
    )


async def nws_hourly(client, lat, lon):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
    p = await client.get(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}", headers=headers)
    if p.status_code != 200:
        raise HTTPException(502, "The National Weather Service has no forecast for that location.")
    hourly_url = p.json()["properties"]["forecastHourly"]
    h = await client.get(hourly_url, headers=headers)
    if h.status_code != 200:
        raise HTTPException(502, "Could not load the hourly forecast. Try again shortly.")
    return h.json()["properties"]["periods"]


def build_nights(periods, max_nights=3):
    """Group hours from 6 PM to 9 AM into nights and score each one."""
    nights: dict = {}
    for p in periods:
        start = datetime.fromisoformat(p["startTime"])
        if not (start.hour >= 18 or start.hour < 9):
            continue
        key = (start - timedelta(hours=12)).date()
        nights.setdefault(key, []).append(p)

    results = []
    for key in sorted(nights)[: max_nights + 1]:
        hours = nights[key]
        if len(hours) < 6:  # skip a partial night at the very start or end
            continue
        coldest = min(hours, key=lambda x: x["temperature"])
        low_f = coldest["temperature"]
        dew_f = c_to_f((coldest.get("dewpoint") or {}).get("value"))
        wind = parse_wind_mph(coldest.get("windSpeed"))
        level, label, note = score_night(low_f, dew_f, wind)
        results.append(
            {
                "night_of": key.isoformat(),
                "low_f": round(low_f),
                "dewpoint_f": None if dew_f is None else round(dew_f),
                "wind_mph": wind,
                "level": level,
                "label": label,
                "note": note,
            }
        )
    return results[:max_nights]


@app.get("/")
def health():
    return {"status": "ok", "service": "FrostWatch API"}


@app.get("/frost-risk")
async def frost_risk(zip: str = Query(..., min_length=5, max_length=5)):
    if not zip.isdigit():
        raise HTTPException(400, "Enter a 5 digit US zip code.")

    cached = _cache.get(zip)
    if cached and datetime.utcnow() - cached[0] < timedelta(minutes=CACHE_MINUTES):
        return cached[1]

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        lat, lon, place = await zip_to_latlon(client, zip)
        periods = await nws_hourly(client, lat, lon)

    nights = build_nights(periods)
    data = {
        "zip": zip,
        "place": place,
        "worst_level": max((n["level"] for n in nights), default=0),
        "nights": nights,
        "source": "National Weather Service hourly forecast",
    }
    _cache[zip] = (datetime.utcnow(), data)
    return data
