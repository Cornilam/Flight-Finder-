"""
Hacker Fare Engine - SerpAPI Edition

Flask web app that searches Google Flights via SerpAPI for split-ticket
"hacker fare" deals. Compares flying from MKE vs driving to ORD.

Usage:
    python app_serp.py
"""

import os
import json
import time
import queue
import threading
from datetime import datetime

from flask import Flask, render_template, request, jsonify, Response
from dotenv import load_dotenv

from serp_flights import (
    search_one_way,
    search_round_trip,
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

# Hubs for SerpAPI (smaller list to conserve searches)
SERP_HUBS = [h.strip() for h in os.getenv("SERP_HUBS", "JFK,EWR,ORD,IAD,ATL,YYZ,DFW,BOS").split(",") if h.strip()]

# Minimum connection time (minutes) between separate tickets at a hub
MIN_CONNECTION_MINUTES = int(os.getenv("MIN_CONNECTION_MINUTES", "120"))

# Drive-to-hub config
REGIONAL_AIRPORT = os.getenv("REGIONAL_AIRPORT", "MKE")
HUB_AIRPORT = os.getenv("HUB_AIRPORT", "ORD")
DRIVING_COST = float(os.getenv("DRIVING_COST", "100.00"))
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
        "hubs": SERP_HUBS,
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


# ---------------------------------------------------------------------------
# Hacker Fare Search Pipeline
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
    Full hacker fare search pipeline using SerpAPI.
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
            "message": f"Starting SerpAPI hacker fare search from {origin_label}",
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
            search_origin, destination, depart_date, return_date, max_results=3,
        )

        baseline = None
        if baseline_results:
            b = baseline_results[0]
            baseline = {
                "price": b["price"],
                "carrier_display": b["carrier_display"],
                "flight_number": b["flight_number"],
                "airlines": b["airlines"],
                "airline_logos": b["airline_logos"],
                "stops": b["stops"],
                "total_duration": b["total_duration"],
                "layover_details": b["layover_details"],
                "connections": b["connections"],
                "departure_time": b["departure_time"],
                "arrival_time": b["arrival_time"],
                "segments": b["segments"],
                "price_insights": b.get("price_insights", {}),
                "flight_type": b["flight_type"],
            }
            if is_hub_search:
                baseline["ground_cost"] = ground_cost
                baseline["total_with_ground"] = b["price"] + ground_cost

            emit(q, "status", {
                "step": "baseline_done",
                "message": f"Baseline: ${b['price']:.2f} ({b['carrier_display']}, {b['stops']} stop{'s' if b['stops'] != 1 else ''})",
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
        # Step 2: Search positioning flights (origin -> each hub)
        # ---------------------------------------------------------------
        domestic_offers = {}
        for hi, hub in enumerate(active_hubs):
            emit(q, "status", {
                "step": f"domestic_{hub}",
                "message": f"Searching positioning: {search_origin} -> {hub} (one-way)...",
                "hub": hub, "phase": "domestic", "hub_index": hi, "hub_total": hub_total,
            })

            results = search_one_way(search_origin, hub, depart_date, max_results=3)

            if results:
                domestic_offers[hub] = results
                best = results[0]
                emit(q, "status", {
                    "step": f"domestic_{hub}_done",
                    "message": f"  {search_origin}->{hub}: ${best['price']:.2f} ({best['carrier_display']})",
                    "hub": hub, "phase": "domestic", "hub_index": hi, "hub_total": hub_total,
                })
            else:
                emit(q, "status", {
                    "step": f"domestic_{hub}_done",
                    "message": f"  {search_origin}->{hub}: No flights found",
                    "hub": hub, "phase": "domestic", "hub_index": hi, "hub_total": hub_total,
                })

        # ---------------------------------------------------------------
        # Step 3: Search international flights (each hub -> destination)
        # ---------------------------------------------------------------
        international_offers = {}
        for hi, hub in enumerate(active_hubs):
            emit(q, "status", {
                "step": f"intl_{hub}",
                "message": f"Searching international: {hub} -> {destination} (one-way)...",
                "hub": hub, "phase": "intl", "hub_index": hi, "hub_total": hub_total,
            })

            results = search_one_way(hub, destination, depart_date, max_results=3)

            if results:
                international_offers[hub] = results
                best = results[0]
                emit(q, "status", {
                    "step": f"intl_{hub}_done",
                    "message": f"  {hub}->{destination}: ${best['price']:.2f} ({best['carrier_display']})",
                    "hub": hub, "phase": "intl", "hub_index": hi, "hub_total": hub_total,
                })
            else:
                emit(q, "status", {
                    "step": f"intl_{hub}_done",
                    "message": f"  {hub}->{destination}: No flights found",
                    "hub": hub, "phase": "intl", "hub_index": hi, "hub_total": hub_total,
                })

        # ---------------------------------------------------------------
        # Step 3b: Search return international flights (dest -> each hub)
        # ---------------------------------------------------------------
        return_international_offers = {}
        for hi, hub in enumerate(active_hubs):
            emit(q, "status", {
                "step": f"ret_intl_{hub}",
                "message": f"Searching return international: {destination} -> {hub} (one-way)...",
                "hub": hub, "phase": "ret_intl", "hub_index": hi, "hub_total": hub_total,
            })

            results = search_one_way(destination, hub, return_date, max_results=3)

            if results:
                return_international_offers[hub] = results
                best = results[0]
                emit(q, "status", {
                    "step": f"ret_intl_{hub}_done",
                    "message": f"  {destination}->{hub}: ${best['price']:.2f} ({best['carrier_display']})",
                    "hub": hub, "phase": "ret_intl", "hub_index": hi, "hub_total": hub_total,
                })
            else:
                emit(q, "status", {
                    "step": f"ret_intl_{hub}_done",
                    "message": f"  {destination}->{hub}: No flights found",
                    "hub": hub, "phase": "ret_intl", "hub_index": hi, "hub_total": hub_total,
                })

        # ---------------------------------------------------------------
        # Step 3c: Search return positioning flights (each hub -> origin)
        # ---------------------------------------------------------------
        return_domestic_offers = {}
        for hi, hub in enumerate(active_hubs):
            emit(q, "status", {
                "step": f"ret_dom_{hub}",
                "message": f"Searching return positioning: {hub} -> {search_origin} (one-way)...",
                "hub": hub, "phase": "ret_dom", "hub_index": hi, "hub_total": hub_total,
            })

            results = search_one_way(hub, search_origin, return_date, max_results=3)

            if results:
                return_domestic_offers[hub] = results
                best = results[0]
                emit(q, "status", {
                    "step": f"ret_dom_{hub}_done",
                    "message": f"  {hub}->{search_origin}: ${best['price']:.2f} ({best['carrier_display']})",
                    "hub": hub, "phase": "ret_dom", "hub_index": hi, "hub_total": hub_total,
                })
            else:
                emit(q, "status", {
                    "step": f"ret_dom_{hub}_done",
                    "message": f"  {hub}->{search_origin}: No flights found",
                    "hub": hub, "phase": "ret_dom", "hub_index": hi, "hub_total": hub_total,
                })

        # ---------------------------------------------------------------
        # Step 4: Assemble valid hacker fare pairings
        # ---------------------------------------------------------------
        emit(q, "status", {
            "step": "assembly",
            "message": "Assembling valid hacker fare routes (outbound + return)...",
            "phase": "assembly",
        })

        hacker_fares = []
        global_warnings = []

        for hub in active_hubs:
            dom_list = domestic_offers.get(hub, [])
            intl_list = international_offers.get(hub, [])
            ret_intl_list = return_international_offers.get(hub, [])
            ret_dom_list = return_domestic_offers.get(hub, [])

            # Need all 4 leg types to have results
            if not dom_list or not intl_list or not ret_intl_list or not ret_dom_list:
                continue

            # Phase 1: Valid outbound pairs (origin->hub + hub->dest)
            outbound_pairs = []
            for dom in dom_list:
                for intl in intl_list:
                    conn_out = calculate_connection_time(
                        dom["arrival_time"], intl["departure_time"]
                    )
                    if conn_out is None or conn_out < MIN_CONNECTION_MINUTES:
                        continue
                    outbound_pairs.append({
                        "domestic": dom,
                        "international": intl,
                        "connection_minutes": conn_out,
                        "pair_price": dom["price"] + intl["price"],
                    })

            # Phase 2: Valid return pairs (dest->hub + hub->origin)
            return_pairs = []
            for ret_intl in ret_intl_list:
                for ret_dom in ret_dom_list:
                    conn_ret = calculate_connection_time(
                        ret_intl["arrival_time"], ret_dom["departure_time"]
                    )
                    if conn_ret is None or conn_ret < MIN_CONNECTION_MINUTES:
                        continue
                    return_pairs.append({
                        "ret_international": ret_intl,
                        "ret_domestic": ret_dom,
                        "connection_minutes": conn_ret,
                        "pair_price": ret_intl["price"] + ret_dom["price"],
                    })

            if not outbound_pairs or not return_pairs:
                continue

            # Keep top 3 cheapest of each direction
            outbound_pairs.sort(key=lambda x: x["pair_price"])
            return_pairs.sort(key=lambda x: x["pair_price"])
            top_outbound = outbound_pairs[:3]
            top_return = return_pairs[:3]

            # Phase 3: Cross-combine top outbound x top return
            for ob in top_outbound:
                for rt in top_return:
                    total = ob["pair_price"] + rt["pair_price"]
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
                        "domestic": ob["domestic"],
                        "international": ob["international"],
                        "outbound_connection_minutes": ob["connection_minutes"],
                        "ret_international": rt["ret_international"],
                        "ret_domestic": rt["ret_domestic"],
                        "return_connection_minutes": rt["connection_minutes"],
                        "outbound_total": ob["pair_price"],
                        "return_total": rt["pair_price"],
                        "total": total,
                        "ground_cost": ground_cost,
                        "total_with_ground": total_with_ground,
                        "savings_vs_baseline": savings,
                        "warnings": [],
                    })

        # Deduplicate
        seen = set()
        unique_fares = []
        for hf in hacker_fares:
            key = (
                hf["hub"],
                hf["domestic"]["carrier_display"],
                hf["domestic"]["price"],
                hf["international"]["carrier_display"],
                hf["international"]["price"],
                hf["ret_international"]["carrier_display"],
                hf["ret_international"]["price"],
                hf["ret_domestic"]["carrier_display"],
                hf["ret_domestic"]["price"],
            )
            if key not in seen:
                seen.add(key)
                unique_fares.append(hf)

        # Sort by total price (with ground cost)
        unique_fares.sort(key=lambda x: x["total_with_ground"])

        # Assign ranks
        for i, hf in enumerate(unique_fares):
            hf["rank"] = i + 1

        emit(q, "status", {
            "step": "assembly_done",
            "message": f"Found {len(unique_fares)} valid hacker fare routes.",
        })

        # ---------------------------------------------------------------
        # Step 5: Send results
        # ---------------------------------------------------------------
        api_calls = 1 + len(active_hubs) * 4
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
    print("\n  Hacker Fare Engine - SerpAPI Edition")
    print(f"  Trip: {ORIGIN} -> {DESTINATION} | {DEPART_DATE} to {RETURN_DATE}")
    print(f"  Hubs ({len(SERP_HUBS)}): {', '.join(SERP_HUBS)}")
    print(f"  API calls per search: ~{1 + len(SERP_HUBS) * 4}")
    print(f"  Drive option: {HUB_AIRPORT} (+${DRIVING_COST + PARKING_RATE_PER_DAY * TRIP_DAYS:.0f} ground)")
    print("  Server starting at http://localhost:5000\n")

    app.run(debug=False, port=5000)
