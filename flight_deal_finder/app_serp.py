"""
Hub Hop Engine - SerpAPI Edition

Flask web app that searches Google Flights via SerpAPI for split-ticket
"Hub Hop" deals. Compares flying from MKE vs driving to ORD.

Usage:
    python app_serp.py
"""

import os
import json
import time
import queue
import threading
from pathlib import Path
from datetime import datetime

from flask import Flask, render_template, request, jsonify, Response
from dotenv import load_dotenv

from serp_flights import (
    search_one_way,
    search_round_trip,
    search_return_flights,
    get_cache_stats,
    clear_cache,
    SerpFlightError,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

ORIGIN = os.getenv("ORIGIN", "MKE")
DESTINATION = os.getenv("DESTINATION", "ATH")
DEPART_DATE = os.getenv("DEPART_DATE", "2026-05-22")
RETURN_DATE = os.getenv("RETURN_DATE", "2026-05-30")

# Full list of hub airports available for user selection in the UI
ALL_HUBS = [h.strip() for h in os.getenv("ALL_HUBS", "JFK,EWR,ORD,IAD,ATL,YYZ,DFW,BOS,LAX,MIA,SEA,SFO,IAH").split(",") if h.strip()]

# Default checked hubs (smaller list to conserve API calls)
SERP_HUBS = [h.strip() for h in os.getenv("SERP_HUBS", "JFK,ORD,ATL").split(",") if h.strip()]

# Minimum connection time (minutes) between separate tickets at a hub
MIN_CONNECTION_MINUTES = int(os.getenv("MIN_CONNECTION_MINUTES", "60"))

# Drive-to-hub config
REGIONAL_AIRPORT = os.getenv("REGIONAL_AIRPORT", "MKE")
HUB_AIRPORT = os.getenv("HUB_AIRPORT", "ORD")
DRIVING_COST = float(os.getenv("DRIVING_COST", "60.00"))
PARKING_RATE_PER_DAY = float(os.getenv("PARKING_RATE_PER_DAY", "20.00"))

# Trip duration for parking calculation
_d = datetime.strptime(DEPART_DATE, "%Y-%m-%d").date()
_r = datetime.strptime(RETURN_DATE, "%Y-%m-%d").date()
TRIP_DAYS = (_r - _d).days

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

# Active search streams
active_searches = {}

# ---------------------------------------------------------------------------
# Community Deals — shared across all users
# ---------------------------------------------------------------------------

COMMUNITY_DEALS_FILE = Path(__file__).parent / "cache" / "community_deals.json"


def load_community_deals():
    """Load all community deals from JSON file on disk."""
    if not COMMUNITY_DEALS_FILE.exists():
        return []
    try:
        with open(COMMUNITY_DEALS_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def save_community_deal(results):
    """
    Save a deal summary from a completed search.
    Deduplicates by origin-destination-depart_date-return_date:
    if the same route+dates already exists, update it instead of adding.
    """
    baseline_price = results.get("baseline", {}).get("price") if results.get("baseline") else None
    best_hacker = None
    best_hub = None
    if results.get("hacker_fares"):
        top = results["hacker_fares"][0]  # already sorted by price
        best_hacker = top.get("total")
        best_hub = top.get("hub")

    # Use the cheapest option as the "best price"
    prices = [p for p in [baseline_price, best_hacker] if p is not None]
    if not prices:
        return  # nothing to save

    best_price = min(prices)
    savings = None
    if baseline_price and best_hacker and best_hacker < baseline_price:
        savings = round(baseline_price - best_hacker, 2)

    deal = {
        "origin": results.get("search_origin", ""),
        "destination": results.get("destination", ""),
        "depart_date": results.get("depart_date", ""),
        "return_date": results.get("return_date", ""),
        "best_price": round(best_price, 2),
        "baseline_price": round(baseline_price, 2) if baseline_price else None,
        "best_hub": best_hub,
        "savings": savings,
        "searched_at": results.get("searched_at", datetime.now().isoformat()),
    }

    # Dedup key
    dedup_key = f"{deal['origin']}-{deal['destination']}-{deal['depart_date']}-{deal['return_date']}"

    deals = load_community_deals()

    # Check for existing entry with same route+dates
    found = False
    for i, existing in enumerate(deals):
        existing_key = f"{existing['origin']}-{existing['destination']}-{existing['depart_date']}-{existing['return_date']}"
        if existing_key == dedup_key:
            deals[i] = deal  # update with fresh data
            found = True
            break

    if not found:
        deals.append(deal)

    # Persist
    try:
        COMMUNITY_DEALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(COMMUNITY_DEALS_FILE, "w") as f:
            json.dump(deals, f, indent=2)
    except IOError as e:
        print(f"  [WARNING] Could not save community deal: {e}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template(
        "dashboard_serp.html",
        origin=ORIGIN,
        hub_airport=HUB_AIRPORT,
        destination=DESTINATION,
        depart_date=DEPART_DATE,
        return_date=RETURN_DATE,
        hubs=SERP_HUBS,
        all_hubs=ALL_HUBS,
        min_connection=MIN_CONNECTION_MINUTES,
        driving_cost=DRIVING_COST,
        parking_rate=PARKING_RATE_PER_DAY,
        trip_days=TRIP_DAYS,
    )


@app.route("/search", methods=["POST"])
def start_search():
    data = request.get_json() or {}
    search_origin = data.get("origin", ORIGIN).upper().strip()
    destination = data.get("destination", DESTINATION).upper().strip()
    depart_date = data.get("depart_date", DEPART_DATE).strip()
    return_date = data.get("return_date", RETURN_DATE).strip()

    # Validate airport codes
    for code, label in [(search_origin, "origin"), (destination, "destination")]:
        if len(code) != 3 or not code.isalpha():
            return jsonify({"error": f"Invalid {label} airport code: {code}"}), 400

    # Validate dates
    try:
        d = datetime.strptime(depart_date, "%Y-%m-%d").date()
        r = datetime.strptime(return_date, "%Y-%m-%d").date()
        if r <= d:
            return jsonify({"error": "Return date must be after departure date"}), 400
        trip_days = (r - d).days
    except ValueError:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400

    # Hub comparison parameters
    hub_airport = data.get("hub_airport", HUB_AIRPORT).upper().strip()
    driving_cost = float(data.get("driving_cost", DRIVING_COST))
    parking_rate = float(data.get("parking_rate", PARKING_RATE_PER_DAY))

    is_hub_search = data.get("is_hub_search", False)

    # Client-selected hubs (validated against ALL_HUBS whitelist)
    client_hubs = data.get("hubs", None)
    if client_hubs and isinstance(client_hubs, list):
        selected_hubs = [
            h.upper().strip() for h in client_hubs
            if isinstance(h, str) and len(h.strip()) == 3
            and h.strip().isalpha() and h.upper().strip() in ALL_HUBS
        ]
        if not selected_hubs:
            selected_hubs = SERP_HUBS
    else:
        selected_hubs = SERP_HUBS

    search_params = {
        "origin": search_origin,
        "destination": destination,
        "depart_date": depart_date,
        "return_date": return_date,
        "trip_days": trip_days,
        "hub_airport": hub_airport,
        "driving_cost": driving_cost,
        "parking_rate": parking_rate,
        "is_hub_search": is_hub_search,
        "hubs": selected_hubs,
    }

    search_id = f"serp-{search_origin}-{destination}-{id(request)}"
    msg_queue = queue.Queue()
    active_searches[search_id] = msg_queue

    t = threading.Thread(
        target=run_hacker_fare_search,
        args=(msg_queue, search_params),
        daemon=True,
    )
    t.start()

    return jsonify({"search_id": search_id})


@app.route("/stream/<search_id>")
def stream(search_id):
    msg_queue = active_searches.get(search_id)
    if not msg_queue:
        return jsonify({"error": "Search not found"}), 404

    def generate():
        start_time = time.time()
        max_wait = 600  # 10-minute absolute maximum
        try:
            while time.time() - start_time < max_wait:
                try:
                    event = msg_queue.get(timeout=30)
                except queue.Empty:
                    yield ": heartbeat\n\n"
                    continue

                event_type = event.get("type", "status")
                payload = json.dumps(event.get("data", {}))
                yield f"event: {event_type}\ndata: {payload}\n\n"

                if event_type == "done":
                    break
            else:
                yield "event: done\ndata: {}\n\n"
        finally:
            active_searches.pop(search_id, None)

    return Response(generate(), mimetype="text/event-stream")


@app.route("/clear-cache", methods=["POST"])
def clear_server_cache():
    clear_cache()
    return jsonify({"status": "ok"})


@app.route("/api/deals")
def api_deals():
    """Return all community deals sorted by most recent first."""
    deals = load_community_deals()
    deals.sort(key=lambda d: d.get("searched_at", ""), reverse=True)
    return jsonify(deals)


# ---------------------------------------------------------------------------
# Hub Hop Search Pipeline
# ---------------------------------------------------------------------------

def format_minutes(mins):
    """Convert minutes to readable string."""
    if mins <= 0:
        return "N/A"
    h = mins // 60
    m = mins % 60
    if h > 0 and m > 0:
        return f"{h}h {m}m"
    elif h > 0:
        return f"{h}h"
    return f"{m}m"


def emit(q, event_type, data):
    """Push an SSE event onto the queue."""
    q.put({"type": event_type, "data": data})


def run_hacker_fare_search(q, params):
    """
    Full Hub Hop search pipeline using SerpAPI.
    """
    try:
        search_origin = params["origin"]
        destination = params["destination"]
        depart_date = params["depart_date"]
        return_date = params["return_date"]
        trip_days = params["trip_days"]
        hub_airport = params["hub_airport"]
        hubs = params["hubs"]

        # Snapshot cache stats before this search
        stats_before = get_cache_stats()

        # Determine which hubs to search (skip if hub == origin)
        active_hubs = [h for h in hubs if h != search_origin]

        # Hub-origin search (driving to hub) — only when explicitly flagged
        is_hub_search = params.get("is_hub_search", False)
        ground_cost = 0.0
        if is_hub_search:
            ground_cost = params["driving_cost"] + (params["parking_rate"] * trip_days)

        origin_label = search_origin
        if is_hub_search:
            origin_label = f"{search_origin} (drive to hub)"

        hub_total = len(active_hubs)

        emit(q, "status", {
            "step": "start",
            "message": f"Starting Hub Hop search from {origin_label}",
            "hub_total": hub_total,
            "hubs": active_hubs,
        })
        emit(q, "status", {
            "step": "info",
            "message": f"Searching {hub_total} hubs: {', '.join(active_hubs)}",
        })

        # ---------------------------------------------------------------
        # Step 1: Baseline - direct round-trip search
        # ---------------------------------------------------------------
        emit(q, "status", {
            "step": "baseline",
            "message": f"Searching baseline: {search_origin} -> {destination} (round-trip)...",
            "phase": "baseline",
        })

        baseline_results = search_round_trip(
            search_origin, destination, depart_date, return_date, max_results=5,
        )

        baseline = None
        baseline_flights = []
        if baseline_results:
            # Build full list of baseline flights for Direct Flights tab
            for bf in baseline_results:
                flight_entry = {
                    "price": bf["price"],
                    "carrier_display": bf["carrier_display"],
                    "flight_number": bf["flight_number"],
                    "airlines": bf["airlines"],
                    "airline_logos": bf["airline_logos"],
                    "stops": bf["stops"],
                    "total_duration": bf["total_duration"],
                    "layover_details": bf["layover_details"],
                    "connections": bf["connections"],
                    "departure_time": bf["departure_time"],
                    "arrival_time": bf["arrival_time"],
                    "segments": bf["segments"],
                    "price_insights": bf.get("price_insights", {}),
                    "flight_type": bf["flight_type"],
                }
                if is_hub_search:
                    flight_entry["ground_cost"] = ground_cost
                    flight_entry["total_with_ground"] = bf["price"] + ground_cost
                baseline_flights.append(flight_entry)

            # Keep single cheapest as "baseline" for backward compat
            baseline = baseline_flights[0]

            b = baseline_results[0]
            emit(q, "status", {
                "step": "baseline_done",
                "message": f"Baseline: ${b['price']:.2f} ({b['carrier_display']}, {b['stops']} stop{'s' if b['stops'] != 1 else ''}) — {len(baseline_results)} options found",
            })
        else:
            emit(q, "status", {
                "step": "baseline_done",
                "message": "No direct flights found for baseline.",
            })

        # Price insights from baseline search
        price_insights = {}
        if baseline_results:
            price_insights = baseline_results[0].get("price_insights", {})

        # ---------------------------------------------------------------
        # Step 2: Search positioning round-trips (origin <-> each hub)
        # ---------------------------------------------------------------
        positioning_offers = {}
        for hi, hub in enumerate(active_hubs):
            emit(q, "status", {
                "step": f"positioning_{hub}",
                "message": f"Searching positioning: {search_origin} <-> {hub} (round-trip)...",
                "hub": hub, "phase": "domestic", "hub_index": hi, "hub_total": hub_total,
            })

            results = search_round_trip(search_origin, hub, depart_date, return_date, max_results=3)

            if results:
                positioning_offers[hub] = results
                best = results[0]
                emit(q, "status", {
                    "step": f"positioning_{hub}_done",
                    "message": f"  {search_origin}<->{hub}: ${best['price']:.2f} RT ({best['carrier_display']})",
                    "hub": hub, "phase": "domestic", "hub_index": hi, "hub_total": hub_total,
                })
            else:
                emit(q, "status", {
                    "step": f"positioning_{hub}_done",
                    "message": f"  {search_origin}<->{hub}: No flights found",
                    "hub": hub, "phase": "domestic", "hub_index": hi, "hub_total": hub_total,
                })

        # ---------------------------------------------------------------
        # Step 3: Search international round-trips (each hub <-> dest)
        # ---------------------------------------------------------------
        international_rt_offers = {}
        for hi, hub in enumerate(active_hubs):
            emit(q, "status", {
                "step": f"intl_{hub}",
                "message": f"Searching international: {hub} <-> {destination} (round-trip)...",
                "hub": hub, "phase": "intl", "hub_index": hi, "hub_total": hub_total,
            })

            results = search_round_trip(hub, destination, depart_date, return_date, max_results=3)

            if results:
                international_rt_offers[hub] = results
                best = results[0]
                emit(q, "status", {
                    "step": f"intl_{hub}_done",
                    "message": f"  {hub}<->{destination}: ${best['price']:.2f} RT ({best['carrier_display']})",
                    "hub": hub, "phase": "intl", "hub_index": hi, "hub_total": hub_total,
                })
            else:
                emit(q, "status", {
                    "step": f"intl_{hub}_done",
                    "message": f"  {hub}<->{destination}: No flights found",
                    "hub": hub, "phase": "intl", "hub_index": hi, "hub_total": hub_total,
                })

        # ---------------------------------------------------------------
        # ARCHIVED: One-way search approach (4 calls per hub)
        # Kept for future use — uncomment to restore granular leg data.
        # ---------------------------------------------------------------
        # domestic_offers = {}
        # for hi, hub in enumerate(active_hubs):
        #     results = search_one_way(search_origin, hub, depart_date, max_results=3)
        #     if results: domestic_offers[hub] = results
        #
        # international_offers = {}
        # for hi, hub in enumerate(active_hubs):
        #     results = search_one_way(hub, destination, depart_date, max_results=3)
        #     if results: international_offers[hub] = results
        #
        # return_international_offers = {}
        # for hi, hub in enumerate(active_hubs):
        #     results = search_one_way(destination, hub, return_date, max_results=3)
        #     if results: return_international_offers[hub] = results
        #
        # return_domestic_offers = {}
        # for hi, hub in enumerate(active_hubs):
        #     results = search_one_way(hub, search_origin, return_date, max_results=3)
        #     if results: return_domestic_offers[hub] = results
        # ---------------------------------------------------------------

        # ---------------------------------------------------------------
        # Step 4: Assemble valid Hub Hop pairings (round-trip mode)
        # ---------------------------------------------------------------
        emit(q, "status", {
            "step": "assembly",
            "message": "Assembling Hub Hop routes (positioning RT + international RT)...",
            "phase": "assembly",
        })

        hacker_fares = []
        global_warnings = []
        skipped_bad_connection = 0

        for hub in active_hubs:
            pos_list = positioning_offers.get(hub, [])
            intl_list = international_rt_offers.get(hub, [])

            # Need both round-trip legs to have results
            if not pos_list or not intl_list:
                continue

            # Combine all positioning x international round-trips,
            # filter for valid connections, then keep best by price
            for pos in pos_list:
                for intl in intl_list:
                    # Validate outbound connection: positioning must arrive
                    # at the hub BEFORE the international flight departs
                    conn_minutes = calculate_connection_time(
                        pos.get("arrival_time"), intl.get("departure_time")
                    )
                    if conn_minutes is not None and conn_minutes < MIN_CONNECTION_MINUTES:
                        skipped_bad_connection += 1
                        continue
                    # conn_minutes is None when times can't be parsed or
                    # the connection is impossible (negative); skip those too
                    if conn_minutes is None and pos.get("arrival_time") and intl.get("departure_time"):
                        skipped_bad_connection += 1
                        continue

                    total = pos["price"] + intl["price"]
                    total_with_ground = total + ground_cost

                    savings = None
                    if baseline:
                        baseline_compare = (
                            baseline.get("total_with_ground", baseline["price"])
                            if is_hub_search
                            else baseline["price"]
                        )
                        savings = baseline_compare - total_with_ground

                    hacker_fares.append({
                        "hub": hub,
                        "positioning": pos,
                        "international": intl,
                        "positioning_price": pos["price"],
                        "international_price": intl["price"],
                        "total": total,
                        "ground_cost": ground_cost,
                        "total_with_ground": total_with_ground,
                        "savings_vs_baseline": savings,
                        "warnings": [],
                    })

        if skipped_bad_connection:
            global_warnings.append(
                f"Filtered out {skipped_bad_connection} route(s) with impossible or too-short outbound connections (minimum {MIN_CONNECTION_MINUTES} min)."
            )

        # Deduplicate
        seen = set()
        unique_fares = []
        for hf in hacker_fares:
            key = (
                hf["hub"],
                hf["positioning"]["carrier_display"],
                hf["positioning"]["price"],
                hf["international"]["carrier_display"],
                hf["international"]["price"],
            )
            if key not in seen:
                seen.add(key)
                unique_fares.append(hf)

        # Sort by total price (with ground cost) and cap results
        unique_fares.sort(key=lambda x: x["total_with_ground"])
        MAX_RESULTS = 30
        unique_fares = unique_fares[:MAX_RESULTS]

        # Assign ranks
        for i, hf in enumerate(unique_fares):
            hf["rank"] = i + 1

        emit(q, "status", {
            "step": "assembly_done",
            "message": f"Found {len(unique_fares)} valid Hub Hop routes.",
        })

        # ---------------------------------------------------------------
        # Step 4.5: Fetch return leg details using departure_tokens
        # ---------------------------------------------------------------
        MAX_RETURN_LEG_FETCHES = int(os.getenv("MAX_RETURN_LEG_FETCHES", "12"))

        emit(q, "status", {
            "step": "return_legs",
            "message": "Fetching return flight details...",
            "phase": "return_legs",
        })

        # Collect unique departure_tokens from assembled Hub Hop fares
        tokens_to_fetch = {}  # dedup key -> (origin, dest, depart, return, token)

        for hf in unique_fares:
            pos = hf["positioning"]
            intl = hf["international"]

            pos_token = pos.get("departure_token", "")
            if pos_token:
                tkey = ("pos", search_origin, hf["hub"], pos_token[:60])
                if tkey not in tokens_to_fetch:
                    tokens_to_fetch[tkey] = (
                        search_origin, hf["hub"], depart_date, return_date, pos_token
                    )

            intl_token = intl.get("departure_token", "")
            if intl_token:
                tkey = ("intl", hf["hub"], destination, intl_token[:60])
                if tkey not in tokens_to_fetch:
                    tokens_to_fetch[tkey] = (
                        hf["hub"], destination, depart_date, return_date, intl_token
                    )

            if len(tokens_to_fetch) >= MAX_RETURN_LEG_FETCHES:
                break

        # Fetch return legs with progress
        return_leg_cache = {}   # token_short -> best return flight dict
        fetch_count = 0
        total_tokens = len(tokens_to_fetch)
        return_leg_api_calls = 0

        for tk_key, (orig, dest, dd, rd, token) in tokens_to_fetch.items():
            fetch_count += 1
            emit(q, "status", {
                "step": f"return_leg_{fetch_count}",
                "message": f"Fetching return leg {fetch_count}/{total_tokens}: {dest} → {orig}",
                "phase": "return_legs",
            })

            ret_flights = search_return_flights(orig, dest, dd, rd, token, max_results=1)
            return_leg_api_calls += 1

            if ret_flights:
                return_leg_cache[token[:60]] = ret_flights[0]

        emit(q, "status", {
            "step": "return_legs_done",
            "message": f"Fetched {len(return_leg_cache)} return legs from {total_tokens} tokens.",
        })

        # Attach return leg data to each Hub Hop fare
        for hf in unique_fares:
            pos_token = hf["positioning"].get("departure_token", "")[:60]
            intl_token = hf["international"].get("departure_token", "")[:60]
            hf["positioning_return"] = return_leg_cache.get(pos_token, None)
            hf["international_return"] = return_leg_cache.get(intl_token, None)

        # Filter out return connections that are physically impossible
        # (connecting flight departs before the first leg arrives) and
        # warn about tight but possible connections.
        valid_fares = []
        skipped_bad_return = 0
        for hf in unique_fares:
            intl_ret = hf.get("international_return")
            pos_ret = hf.get("positioning_return")
            if intl_ret and pos_ret:
                ret_conn = calculate_connection_time(
                    intl_ret.get("arrival_time"), pos_ret.get("departure_time")
                )
                if ret_conn is None and intl_ret.get("arrival_time") and pos_ret.get("departure_time"):
                    # Negative or unparseable — impossible connection, drop it
                    skipped_bad_return += 1
                    continue
                if ret_conn is not None and ret_conn < MIN_CONNECTION_MINUTES:
                    hf["warnings"].append(f"Return connection at hub is only {ret_conn} min — consider alternate return flights")
            valid_fares.append(hf)
        unique_fares = valid_fares
        if skipped_bad_return:
            global_warnings.append(f"Dropped {skipped_bad_return} combo(s) with impossible return connections")

        # ---------------------------------------------------------------
        # Step 5: Send results
        # ---------------------------------------------------------------
        api_calls = 1 + len(active_hubs) * 2 + return_leg_api_calls
        stats_after = get_cache_stats()
        search_hits = stats_after["hits"] - stats_before["hits"]
        search_misses = stats_after["misses"] - stats_before["misses"]

        emit(q, "status", {
            "step": "cache_summary",
            "message": f"API calls: {search_misses} new, {search_hits} cached",
        })

        results = {
            "search_origin": search_origin,
            "searched_at": datetime.now().isoformat(),
            "origin_label": origin_label,
            "is_hub_search": is_hub_search,
            "ground_cost": ground_cost,
            "destination": destination,
            "depart_date": depart_date,
            "return_date": return_date,
            "hubs_searched": active_hubs,
            "min_connection_minutes": MIN_CONNECTION_MINUTES,
            "baseline": baseline,
            "baseline_flights": baseline_flights,
            "hacker_fares": unique_fares,
            "global_warnings": global_warnings,
            "price_insights": price_insights,
            "api_calls_used": api_calls,
            "cache_stats": {
                "search_hits": search_hits,
                "search_misses": search_misses,
            },
        }

        emit(q, "results", results)

        # Save to community deals (shared across all users)
        try:
            save_community_deal(results)
        except Exception as e:
            print(f"  [WARNING] Could not save community deal: {e}")

        emit(q, "status", {"step": "done", "message": "Search complete."})
        emit(q, "done", {})

    except SerpFlightError as e:
        emit(q, "error", {"message": str(e)})
        emit(q, "done", {})
    except Exception as e:
        emit(q, "error", {"message": f"Unexpected error: {str(e)}"})
        emit(q, "done", {})


def calculate_connection_time(arrival_time_str, departure_time_str):
    """
    Calculate minutes between domestic arrival and international departure.

    SerpAPI times are like "2026-05-22 14:30".
    Returns minutes or None if times can't be parsed.
    """
    if not arrival_time_str or not departure_time_str:
        return None
    try:
        arr = datetime.strptime(arrival_time_str, "%Y-%m-%d %H:%M")
        dep = datetime.strptime(departure_time_str, "%Y-%m-%d %H:%M")
        diff = (dep - arr).total_seconds() / 60
        if diff < 0:
            return None
        return int(diff)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n  Hub Hop Engine - SerpAPI Edition")
    print(f"  Trip: {ORIGIN} -> {DESTINATION} | {DEPART_DATE} to {RETURN_DATE}")
    print(f"  Hubs ({len(SERP_HUBS)}): {', '.join(SERP_HUBS)}")
    print(f"  API calls per search: ~{1 + len(SERP_HUBS) * 2}")
    print(f"  Drive option: {HUB_AIRPORT} (+${DRIVING_COST + PARKING_RATE_PER_DAY * TRIP_DAYS:.0f} ground)")
    port = int(os.getenv("PORT", 5000))
    print(f"  Server starting at http://localhost:{port}\n")

    app.run(debug=False, port=port)
