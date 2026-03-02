"""
SerpAPI Google Flights - Flight Search Module

Searches Google Flights via SerpAPI for live pricing data.
Used by app_serp.py for the hacker fare engine.
"""

import os
import json
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")
SERPAPI_BASE = "https://serpapi.com/search"


class SerpFlightError(Exception):
    """Raised when a SerpAPI flight search fails."""
    pass


# ---------------------------------------------------------------------------
# Response Cache
# ---------------------------------------------------------------------------

_cache = {}
_cache_stats = {"hits": 0, "misses": 0}
_cache_timestamps = {}  # key_tuple -> float (unix timestamp)

CACHE_DIR = os.getenv("CACHE_DIR", str(Path(__file__).parent / "cache"))
CACHE_FILE = Path(CACHE_DIR) / "serp_cache.json"
CACHE_MAX_AGE_SECONDS = 24 * 60 * 60  # 24 hours


def _cache_key_to_str(key_tuple):
    return "|".join(key_tuple)


def _str_to_cache_key(key_str):
    return tuple(key_str.split("|"))


def _load_cache():
    """Load cache from JSON file on disk. Discards entries older than max age."""
    global _cache
    if not CACHE_FILE.exists():
        return
    try:
        with open(CACHE_FILE, "r") as f:
            raw = json.load(f)
        now = time.time()
        loaded = 0
        for key_str, entry in raw.items():
            cached_at = entry.get("cached_at", 0)
            if now - cached_at > CACHE_MAX_AGE_SECONDS:
                continue
            key_tuple = _str_to_cache_key(key_str)
            _cache[key_tuple] = entry["data"]
            _cache_timestamps[key_tuple] = cached_at
            loaded += 1
        print(f"  [CACHE] Loaded {loaded} entries from {CACHE_FILE.name}")
    except (json.JSONDecodeError, IOError, KeyError) as e:
        print(f"  [CACHE] Could not load cache file: {e}")


def _save_cache():
    """Persist current in-memory cache to JSON file."""
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        serializable = {}
        for key_tuple, data in _cache.items():
            key_str = _cache_key_to_str(key_tuple)
            serializable[key_str] = {
                "data": data,
                "cached_at": _cache_timestamps.get(key_tuple, time.time()),
            }
        with open(CACHE_FILE, "w") as f:
            json.dump(serializable, f)
    except (IOError, TypeError) as e:
        print(f"  [CACHE] Could not save cache file: {e}")


_load_cache()


def get_cache_stats():
    """Return cache hit/miss counts."""
    return dict(_cache_stats)


def clear_cache():
    """Clear all cached responses and delete cache file."""
    _cache.clear()
    _cache_timestamps.clear()
    _cache_stats["hits"] = 0
    _cache_stats["misses"] = 0
    if CACHE_FILE.exists():
        CACHE_FILE.unlink()


# ---------------------------------------------------------------------------
# Search Functions
# ---------------------------------------------------------------------------

def search_one_way(origin, destination, date, max_results=5):
    """
    Search for one-way flights via SerpAPI Google Flights.

    Args:
        origin: Origin IATA code (e.g. "MKE").
        destination: Destination IATA code (e.g. "JFK").
        date: Departure date as YYYY-MM-DD string.
        max_results: Max number of offers to return.

    Returns a list of parsed flight dicts sorted by price.
    """
    cache_key = ("ow", origin, destination, date)
    if cache_key in _cache:
        _cache_stats["hits"] += 1
        print(f"  [CACHE HIT] {origin}->{destination} on {date}")
        return _cache[cache_key][:max_results]

    _cache_stats["misses"] += 1

    params = {
        "engine": "google_flights",
        "api_key": SERPAPI_KEY,
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": date,
        "type": "2",           # one-way
        "currency": "USD",
        "hl": "en",
        "gl": "us",
        "adults": "1",
        "sort_by": "2",        # sort by price
    }

    resp = requests.get(SERPAPI_BASE, params=params, timeout=45)

    if resp.status_code != 200:
        print(f"  [WARNING] SerpAPI search {origin}->{destination} failed (HTTP {resp.status_code})")
        return []

    data = resp.json()

    if "error" in data:
        print(f"  [WARNING] SerpAPI error for {origin}->{destination}: {data['error']}")
        return []

    # Combine best_flights and other_flights
    best = data.get("best_flights", [])
    other = data.get("other_flights", [])
    all_flights = best + other

    if not all_flights:
        print(f"  No flights found {origin}->{destination} on {date}")
        _cache[cache_key] = []
        _cache_timestamps[cache_key] = time.time()
        _save_cache()
        return []

    parsed = []
    for flight_group in all_flights:
        offer = parse_offer(flight_group, origin, destination)
        if offer:
            parsed.append(offer)

    # Sort by price and cache full results
    parsed.sort(key=lambda f: f["price"])
    _cache[cache_key] = parsed
    _cache_timestamps[cache_key] = time.time()
    _save_cache()
    return parsed[:max_results]


def search_round_trip(origin, destination, depart_date, return_date, max_results=5):
    """
    Search for round-trip flights via SerpAPI Google Flights.

    Returns outbound flight options (we don't chase the departure_token
    for return flights to save API calls - the price shown is round-trip).

    Returns a list of parsed flight dicts sorted by price.
    """
    cache_key = ("rt", origin, destination, depart_date, return_date)
    if cache_key in _cache:
        _cache_stats["hits"] += 1
        print(f"  [CACHE HIT] {origin}->{destination} RT on {depart_date}")
        return _cache[cache_key][:max_results]

    _cache_stats["misses"] += 1

    params = {
        "engine": "google_flights",
        "api_key": SERPAPI_KEY,
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": depart_date,
        "return_date": return_date,
        "type": "1",           # round-trip
        "currency": "USD",
        "hl": "en",
        "gl": "us",
        "adults": "1",
        "sort_by": "2",        # sort by price
    }

    resp = requests.get(SERPAPI_BASE, params=params, timeout=45)

    if resp.status_code != 200:
        print(f"  [WARNING] SerpAPI search {origin}->{destination} failed (HTTP {resp.status_code})")
        return []

    data = resp.json()

    if "error" in data:
        print(f"  [WARNING] SerpAPI error for {origin}->{destination}: {data['error']}")
        return []

    best = data.get("best_flights", [])
    other = data.get("other_flights", [])
    all_flights = best + other

    if not all_flights:
        print(f"  No flights found {origin}->{destination} on {depart_date}")
        _cache[cache_key] = []
        _cache_timestamps[cache_key] = time.time()
        _save_cache()
        return []

    # Extract price insights if available
    price_insights = data.get("price_insights", {})

    parsed = []
    for flight_group in all_flights:
        offer = parse_offer(flight_group, origin, destination)
        if offer:
            offer["price_insights"] = price_insights
            parsed.append(offer)

    parsed.sort(key=lambda f: f["price"])
    _cache[cache_key] = parsed
    _cache_timestamps[cache_key] = time.time()
    _save_cache()
    return parsed[:max_results]


# ---------------------------------------------------------------------------
# Offer Parser
# ---------------------------------------------------------------------------

def parse_offer(flight_group, origin, destination):
    """
    Parse a SerpAPI flight group into our standard format.

    A flight_group looks like:
    {
        "flights": [ {segment1}, {segment2}, ... ],
        "layovers": [ {layover1}, ... ],
        "total_duration": 1309,
        "price": 2512,
        "type": "One way" / "Round trip",
        "airline_logo": "...",
        "departure_token": "...",
        ...
    }
    """
    try:
        price = flight_group.get("price")
        if price is None:
            return None
        price = float(price)

        flights = flight_group.get("flights", [])
        if not flights:
            return None

        total_duration = flight_group.get("total_duration", 0)

        # First segment info
        first = flights[0]
        dep_airport = first.get("departure_airport", {})
        departure_time = dep_airport.get("time", "")
        departure_id = dep_airport.get("id", origin)

        # Last segment info
        last = flights[-1]
        arr_airport = last.get("arrival_airport", {})
        arrival_time = arr_airport.get("time", "")
        arrival_id = arr_airport.get("id", destination)

        # Carrier info
        airlines = []
        airline_logos = []
        for seg in flights:
            airline = seg.get("airline", "")
            logo = seg.get("airline_logo", "")
            if airline and airline not in airlines:
                airlines.append(airline)
            if logo and logo not in airline_logos:
                airline_logos.append(logo)

        carrier_display = airlines[0] if airlines else "Unknown"
        if len(airlines) > 1:
            carrier_display = ", ".join(airlines)

        # Flight number
        flight_number = first.get("flight_number", "")

        # Stops = number of segments - 1
        stops = max(0, len(flights) - 1)

        # Layover details from SerpAPI (already computed for us)
        layovers_raw = flight_group.get("layovers", [])
        layover_details = []
        total_layover = 0
        for lay in layovers_raw:
            minutes = lay.get("duration", 0)
            airport = lay.get("id", "?")
            airport_name = lay.get("name", airport)
            overnight = lay.get("overnight", False)
            total_layover += minutes
            layover_details.append({
                "airport": airport,
                "airport_name": airport_name,
                "minutes": minutes,
                "overnight": overnight,
            })

        # Connection airports
        connections = [ld["airport"] for ld in layover_details]

        # Segment details (for richer display)
        segments = []
        for seg in flights:
            segments.append({
                "airline": seg.get("airline", ""),
                "airline_logo": seg.get("airline_logo", ""),
                "flight_number": seg.get("flight_number", ""),
                "airplane": seg.get("airplane", ""),
                "travel_class": seg.get("travel_class", ""),
                "legroom": seg.get("legroom", ""),
                "departure_airport": seg.get("departure_airport", {}),
                "arrival_airport": seg.get("arrival_airport", {}),
                "duration": seg.get("duration", 0),
                "overnight": seg.get("overnight", False),
                "extensions": seg.get("extensions", []),
            })

        # Carbon emissions
        carbon = flight_group.get("carbon_emissions", {})

        return {
            "price": price,
            "carrier_display": carrier_display,
            "flight_number": flight_number,
            "airlines": airlines,
            "airline_logos": airline_logos,
            "stops": stops,
            "total_duration": total_duration,
            "layover_minutes": total_layover,
            "layover_details": layover_details,
            "connections": connections,
            "departure_time": departure_time,
            "arrival_time": arrival_time,
            "departure_id": departure_id,
            "arrival_id": arrival_id,
            "segments": segments,
            "carbon_emissions": carbon,
            "departure_token": flight_group.get("departure_token", ""),
            "booking_token": flight_group.get("booking_token", ""),
            "flight_type": flight_group.get("type", ""),
            "is_multi_airline": len(airlines) > 1,
            "price_insights": {},
        }

    except (KeyError, ValueError, IndexError, TypeError) as e:
        print(f"  [WARNING] Could not parse SerpAPI offer: {e}")
        return None
