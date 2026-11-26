"""FrostWatch starter API.

GET /frost-risk?zip=30301 returns a tonight-first frost outlook plus the next
two nights, built from the National Weather Service hourly forecast.

The scoring in score_night() is a simple STARTER method. Replace it with
your own thresholds and logic.
"""
import re
from datetime import datetime, time as dtime, timedelta

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

# The "night" runs from 6 PM to 9 AM local time.
NIGHT_START_HOUR = 18
NIGHT_END_HOUR = 9
FROST_RANGE_F = 36  # hours at or below this count as the "cold window"

# level: (short answer, headline wording)
ANSWERS = {
    0: ("No", "No frost expected"),
    1: ("Low risk", "Frost possible"),
    2: ("Yes", "Frost likely"),
    3: ("Yes", "Freeze expected"),
    4: ("Yes", "Hard freeze expected"),
}


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
            return 2, "Frost likely", "Calm, humid and near freezing. Protect tender plants."
        return 1, "Frost possible", "Near freezing, but wind or dry air may keep frost off."
    return 0, "No frost", "Lows stay above frost range. No protection needed."


def fmt_hour(dt):
    return dt.strftime("%I %p").lstrip("0")


def when_phrase(night_date, today):
    days = (night_date - today).days
    if days < 0:
        return "this morning"
    if days == 0:
        return "tonight"
    if days == 1:
        return "tomorrow night"
    return night_date.strftime("%A") + " night"


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


def build_timeline(key, coldest_start, cold_starts, periods):
    """When a gardener should act, built around a normal day.

    Cover: before the cold arrives, but never later than midnight, since
    nobody is out there at 2 AM. Uncover: once it is daytime and back above
    the frost range.
    """
    now = datetime.fromisoformat(periods[0]["startTime"])
    midnight = datetime.combine(key + timedelta(days=1), dtime(0, 0), tzinfo=coldest_start.tzinfo)
    cover = min(cold_starts[0] - timedelta(hours=1), midnight)

    if cover <= now:
        mode, cover = "now", now
    elif cover == midnight:
        mode = "midnight"
    else:
        mode = "before"

    last_cold = cold_starts[-1]
    uncover = last_cold + timedelta(hours=1)
    for p in periods:
        start = datetime.fromisoformat(p["startTime"])
        if start > last_cold and p.get("isDaytime") and p["temperature"] > FROST_RANGE_F:
            uncover = start
            break

    return {
        "cover_mode": mode,
        "cover_label": fmt_hour(cover),
        "cover_at": cover.isoformat(),
        "low_at": f"{fmt_hour(coldest_start)} {coldest_start.strftime('%A')}",
        "uncover_label": f"{fmt_hour(uncover)} {uncover.strftime('%A')}",
        "uncover_at": uncover.isoformat(),
    }


def build_nights(periods, max_nights=3):
    """Group hours from 6 PM to 9 AM into nights and score each one.

    A night already underway (for example at 3 AM) is kept and labelled
    "this morning" until too little of it is left to matter.
    """
    if not periods:
        return []
    today = datetime.fromisoformat(periods[0]["startTime"]).date()

    nights: dict = {}
    for p in periods:
        start = datetime.fromisoformat(p["startTime"])
        if not (start.hour >= NIGHT_START_HOUR or start.hour < NIGHT_END_HOUR):
            continue
        key = (start - timedelta(hours=12)).date()
        nights.setdefault(key, []).append((start, p))

    results = []
    for key in sorted(nights):
        hours = nights[key]
        in_progress = key < today
        if in_progress and len(hours) < 2:
            continue  # almost over, skip it
        if not in_progress and len(hours) < 6:
            continue  # partial night at the far end of the forecast

        coldest_start, coldest = min(hours, key=lambda x: x[1]["temperature"])
        low_f = coldest["temperature"]
        dew_f = c_to_f((coldest.get("dewpoint") or {}).get("value"))
        wind = parse_wind_mph(coldest.get("windSpeed"))
        level, label, note = score_night(low_f, dew_f, wind)

        timeline = None
        cold_starts = [s for s, p in hours if p["temperature"] <= FROST_RANGE_F]
        if level >= 1 and cold_starts:
            timeline = build_timeline(key, coldest_start, cold_starts, periods)

        night_word = "morning" if in_progress else "night"
        weekday = (key + timedelta(days=1)).strftime("%A") if in_progress else key.strftime("%A")

        results.append(
            {
                "night_of": key.isoformat(),
                "when": when_phrase(key, today),
                "weekday_night": f"{weekday} {night_word}",
                "low_f": round(low_f),
                "dewpoint_f": None if dew_f is None else round(dew_f),
                "wind_mph": wind,
                "level": level,
                "label": label,
                "note": note,
                "timeline": timeline,
                "colder": False,
            }
        )
    # A night already underway with no frost risk isn't worth leading with.
    # Skip it so the first answer is about the coming night.
    if len(results) > 1 and results[0]["when"] == "this morning" and results[0]["level"] == 0:
        results.pop(0)

    return results[:max_nights]


def build_response(zip_code, place, periods):
    nights = build_nights(periods)

    summary = None
    if nights:
        first = nights[0]
        for n in nights[1:]:
            n["colder"] = n["level"] > first["level"]
        answer, wording = ANSWERS[first["level"]]
        heads_up = None
        later = [n for n in nights[1:] if n["level"] >= 2 and n["level"] > first["level"]]
        if later:
            worst = max(later, key=lambda n: n["level"])
            heads_up = f'Heads up: {worst["label"].lower()} expected {worst["when"]}.'
        summary = {
            "answer": answer,
            "headline": f'{wording} {first["when"]}',
            "heads_up": heads_up,
        }

    series = []
    for p in periods[:72]:
        dew = c_to_f((p.get("dewpoint") or {}).get("value"))
        series.append(
            {
                "t": p["startTime"],
                "temp": p["temperature"],
                "dew": None if dew is None else round(dew),
            }
        )

    return {
        "zip": zip_code,
        "place": place,
        "summary": summary,
        "worst_level": max((n["level"] for n in nights), default=0),
        "nights": nights,
        "hourly": series,
        "source": "National Weather Service hourly forecast",
    }


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

    data = build_response(zip, place, periods)
    _cache[zip] = (datetime.utcnow(), data)
    return data

    data = build_response(zip, place, periods)
    _cache[zip] = (datetime.utcnow(), data)
    return data
