"""
Flight Deal Finder - Hacker Fare Engine (Flask Dashboard)

Searches for split-ticket "hacker fares" by assembling separate domestic +
international legs through intermediate hubs.

Supports searching from MKE (home) or ORD (drive to Chicago).

Run with:  python app.py
Then open: http://localhost:5000
"""

import os
import json
import queue
import threading
import webbrowser
from datetime import datetime, timedelta

from flask import Flask, render_template, request, Response, jsonify
from dotenv import load_dotenv

load_dotenv()

# Import core logic from the CLI tool
from flight_finder import (
    get_amadeus_token,
    search_flights,
    confirm_price,
    format_minutes,
    FlightFinderError,
)

# ---------------------------------------------------------------------------
# Configuration from .env
# ---------------------------------------------------------------------------
AMADEUS_API_KEY = os.getenv("AMADEUS_API_KEY")
AMADEUS_API_SECRET = os.getenv("AMADEUS_API_SECRET")

# Hardcoded trip (Sprint 1)
ORIGIN = os.getenv("ORIGIN", "MKE")
DESTINATION = os.getenv("DESTINATION", "ATH")
DEPART_DATE = os.getenv("DEPART_DATE", "2026-05-22")
RETURN_DATE = os.getenv("RETURN_DATE", "2026-05-30")

# Chicago hub (the "drive to" alternative)
HUB_AIRPORT = os.getenv("HUB_AIRPORT", "ORD")
DRIVING_COST = float(os.getenv("DRIVING_COST", "100.00"))
PARKING_RATE_PER_DAY = float(os.getenv("PARKING_RATE_PER_DAY", "20.00"))

# Hub tiers
HUBS_QUICK = [h.strip() for h in os.getenv("HUBS_QUICK", "JFK,EWR,IAD,ORD,ATL,CLT,PHL,YYZ").split(",") if h.strip()]
HUBS_DEEP = [h.strip() for h in os.getenv("HUBS_DEEP", "JFK,EWR,IAD,ORD,ATL,CLT,PHL,YYZ,BOS,DCA,MIA,DTW,MSP,DFW,IAH,SFO,LAX,YUL,IST,FRA,CDG,AMS,ZRH,FLL,MCO").split(",") if h.strip()]
# Legacy fallback (used by CLI)
HUBS = [h.strip() for h in os.getenv("HUBS", "JFK,EWR,IAD,ORD,ATL,CLT,PHL,YYZ").split(",") if h.strip()]

# Minimum connection time (minutes) between separate tickets
MIN_CONNECTION_MINUTES = int(os.getenv("MIN_CONNECTION_MINUTES", "120"))

# Trip days (computed)
_d = datetime.strptime(DEPART_DATE, "%Y-%m-%d").date()
_r = datetime.strptime(RETURN_DATE, "%Y-%m-%d").date()
TRIP_DAYS = (_r - _d).days

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

# Store for active search streams: search_id -> queue
active_searches = {}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Serve the dashboard page."""
    return render_template(
        "dashboard.html",
        origin=ORIGIN,
        hub_airport=HUB_AIRPORT,
        destination=DESTINATION,
        depart_date=DEPART_DATE,
        return_date=RETURN_DATE,
        hubs_quick=HUBS_QUICK,
        hubs_deep=HUBS_DEEP,
        min_connection=MIN_CONNECTION_MINUTES,
        driving_cost=DRIVING_COST,
        parking_rate=PARKING_RATE_PER_DAY,
        trip_days=TRIP_DAYS,
    )


@app.route("/search", methods=["POST"])
def start_search():
    """Kick off a hacker fare search in a background thread."""
    data = request.get_json() or {}
    search_origin = data.get("origin", ORIGIN).upper().strip()
    tier = data.get("tier", "quick").lower().strip()

    # Select hub list based on tier
    hub_list = HUBS_DEEP if tier == "deep" else HUBS_QUICK

    search_id = f"hf-{search_origin}-{DESTINATION}-{tier}-{id(request)}"
    msg_queue = queue.Queue()
    active_searches[search_id] = msg_queue

    t = threading.Thread(
        target=run_hacker_fare_search,
        args=(msg_queue, search_origin, hub_list, tier),
        daemon=True,
    )
    t.start()

    return jsonify({"search_id": search_id})


@app.route("/stream/<search_id>")
def stream(search_id):
    """SSE endpoint - streams status updates for a running search."""
    msg_queue = active_searches.get(search_id)
    if not msg_queue:
        return "Search not found", 404

    def generate():
        while True:
            try:
                msg = msg_queue.get(timeout=90)
                event = msg["event"]
                data = msg["data"]
                yield f"event: {event}\ndata: {data}\n\n"
                if event == "done":
                    active_searches.pop(search_id, None)
                    break
            except queue.Empty:
                yield ": keepalive\n\n"

    return Response(generate(), mimetype="text/event-stream")


@app.route("/confirm-price", methods=["POST"])
def confirm_price_endpoint():
    """Confirm the real-time price of a flight offer via the Amadeus Price API."""
    data = request.get_json() or {}
    raw_offer = data.get("raw_offer")

    if not raw_offer:
        return jsonify({"error": "No flight offer provided"}), 400

    try:
        token = get_amadeus_token()
        result = confirm_price(token, raw_offer)
        return jsonify(result)
    except FlightFinderError as e:
        return jsonify({"error": str(e), "available": False}), 200
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)}", "available": False}), 200


# ---------------------------------------------------------------------------
# Hacker Fare Search Pipeline
# ---------------------------------------------------------------------------

def emit(q, event, data):
    """Push a Server-Sent Event message onto the queue."""
    q.put({"event": event, "data": json.dumps(data)})


def serialize_flight(flight):
    """Make a flight dict JSON-serializable (sets -> lists, keep raw for price confirm)."""
    return {
        "price": flight["price"],
        "carriers": list(flight.get("carriers", [])),
        "carrier_display": flight.get("carrier_display", ""),
        "outbound_stops": flight.get("outbound_stops", 0),
        "return_stops": flight.get("return_stops", 0),
        "outbound_minutes": flight.get("outbound_minutes", 0),
        "return_minutes": flight.get("return_minutes", 0),
        "layover_minutes": flight.get("layover_minutes", 0),
        "layover_details": flight.get("layover_details", []),
        "connections": flight.get("connections", []),
        "is_hacker_fare": flight.get("is_hacker_fare", False),
        "outbound_departure_time": flight.get("outbound_departure_time", ""),
        "outbound_arrival_time": flight.get("outbound_arrival_time", ""),
        "outbound_arrival_airport": flight.get("outbound_arrival_airport", ""),
        "raw": flight.get("raw"),
    }


def run_hacker_fare_search(q, search_origin, hub_list=None, tier="quick"):
    """
    Execute the full hacker fare search pipeline for a given origin.

    When origin is ORD (or any hub airport), we skip hubs that match the origin
    since there's no domestic leg needed -- we just search direct + hacker fares
    through other hubs.

    hub_list: which hubs to search (defaults to HUBS_QUICK)
    tier: "quick" or "deep" (for display purposes)
    """
    try:
        if hub_list is None:
            hub_list = HUBS_QUICK

        # Determine which hubs to search (skip if hub == origin)
        active_hubs = [h for h in hub_list if h != search_origin]

        # For ORD searches, calculate driving/parking overhead
        is_hub_search = (search_origin == HUB_AIRPORT)
        ground_cost = 0.0
        if is_hub_search:
            ground_cost = DRIVING_COST + (PARKING_RATE_PER_DAY * TRIP_DAYS)

        origin_label = search_origin
        if is_hub_search:
            origin_label = f"{search_origin} (drive from {ORIGIN})"

        # ---------------------------------------------------------------
        # Step 1: Authenticate
        # ---------------------------------------------------------------
        emit(q, "status", {"step": "auth", "message": "Authenticating with Amadeus API..."})
        token = get_amadeus_token()
        emit(q, "status", {"step": "auth_done", "message": "Authenticated successfully."})

        # ---------------------------------------------------------------
        # Step 2: Search the baseline (direct origin -> ATH round-trip)
        # ---------------------------------------------------------------
        emit(q, "status", {
            "step": "baseline",
            "message": f"Searching baseline: {origin_label} -> {DESTINATION} (round-trip)..."
        })
        baseline_flights = search_flights(
            token, search_origin, DESTINATION, DEPART_DATE,
            return_date=RETURN_DATE, max_results=5,
        )
        baseline = None
        if baseline_flights:
            baseline = serialize_flight(baseline_flights[0])
            # Add ground transit cost for hub searches
            if is_hub_search:
                baseline["ground_cost"] = ground_cost
                baseline["total_with_ground"] = round(baseline["price"] + ground_cost, 2)
            emit(q, "status", {
                "step": "baseline_done",
                "message": f"Baseline found: ${baseline_flights[0]['price']:.2f} "
                           f"({baseline_flights[0]['carrier_display']})"
                           + (f" + ${ground_cost:.2f} ground transit" if is_hub_search else "")
            })
        else:
            emit(q, "status", {
                "step": "baseline_done",
                "message": f"No direct flights found for {search_origin} -> {DESTINATION}. "
                           f"Will still search hacker fare routes."
            })

        # ---------------------------------------------------------------
        # Step 3: Search domestic/positioning legs (origin -> each Hub)
        # ---------------------------------------------------------------
        domestic_results = {}

        for hub in active_hubs:
            emit(q, "status", {
                "step": f"domestic_{hub}",
                "message": f"Searching positioning flight: {search_origin} -> {hub} (round-trip)..."
            })
            flights = search_flights(
                token, search_origin, hub, DEPART_DATE,
                return_date=RETURN_DATE, max_results=10,
            )
            domestic_results[hub] = flights
            if flights:
                emit(q, "status", {
                    "step": f"domestic_{hub}_done",
                    "message": f"Found {len(flights)} flight(s) to {hub}. "
                               f"Cheapest: ${flights[0]['price']:.2f}"
                })
            else:
                emit(q, "status", {
                    "step": f"domestic_{hub}_done",
                    "message": f"No flights found for {search_origin} -> {hub}."
                })

        # ---------------------------------------------------------------
        # Step 4: Search international legs (each Hub -> ATH)
        # ---------------------------------------------------------------
        international_results = {}

        for hub in active_hubs:
            dom_flights = domestic_results.get(hub, [])
            if not dom_flights:
                international_results[hub] = []
                continue

            # Find earliest domestic arrival at this hub
            earliest_arrival = None
            for df in dom_flights:
                arr_str = df.get("outbound_arrival_time", "")
                if arr_str:
                    try:
                        arr_dt = datetime.fromisoformat(arr_str)
                        if earliest_arrival is None or arr_dt < earliest_arrival:
                            earliest_arrival = arr_dt
                    except ValueError:
                        pass

            depart_time_from = None
            time_note = ""
            if earliest_arrival:
                min_depart = earliest_arrival + timedelta(minutes=MIN_CONNECTION_MINUTES)
                depart_time_from = min_depart.strftime("%H:%M")
                time_note = f" (departing after {depart_time_from})"

            emit(q, "status", {
                "step": f"intl_{hub}",
                "message": f"Searching international: {hub} -> {DESTINATION}{time_note}..."
            })

            flights = search_flights(
                token, hub, DESTINATION, DEPART_DATE,
                return_date=RETURN_DATE, max_results=10,
                depart_time_from=depart_time_from,
            )
            international_results[hub] = flights

            if flights:
                emit(q, "status", {
                    "step": f"intl_{hub}_done",
                    "message": f"Found {len(flights)} international flight(s) from {hub}. "
                               f"Cheapest: ${flights[0]['price']:.2f}"
                })
            else:
                emit(q, "status", {
                    "step": f"intl_{hub}_done",
                    "message": f"No international flights found for {hub} -> {DESTINATION}."
                })

        # ---------------------------------------------------------------
        # Step 5: Assemble valid pairings
        # ---------------------------------------------------------------
        emit(q, "status", {"step": "assemble", "message": "Assembling hacker fare routes..."})

        hacker_fares = []

        for hub in active_hubs:
            dom_flights = domestic_results.get(hub, [])
            intl_flights = international_results.get(hub, [])

            if not dom_flights or not intl_flights:
                continue

            for dom in dom_flights:
                dom_arrival_str = dom.get("outbound_arrival_time", "")
                if not dom_arrival_str:
                    continue
                try:
                    dom_arrival = datetime.fromisoformat(dom_arrival_str)
                except ValueError:
                    continue

                for intl in intl_flights:
                    intl_depart_str = intl.get("outbound_departure_time", "")
                    if not intl_depart_str:
                        continue
                    try:
                        intl_depart = datetime.fromisoformat(intl_depart_str)
                    except ValueError:
                        continue

                    connection_minutes = int((intl_depart - dom_arrival).total_seconds() / 60)
                    if connection_minutes < MIN_CONNECTION_MINUTES:
                        continue

                    total_price = dom["price"] + intl["price"]

                    # Add ground transit cost for hub-origin searches
                    total_with_ground = round(total_price + ground_cost, 2)

                    warnings = []
                    if dom.get("is_hacker_fare"):
                        dom_carriers = " + ".join(sorted(dom["carriers"]))
                        warnings.append(f"Positioning leg uses multiple airlines ({dom_carriers})")
                    if intl.get("is_hacker_fare"):
                        intl_carriers = " + ".join(sorted(intl["carriers"]))
                        warnings.append(f"International leg uses multiple airlines ({intl_carriers})")

                    hacker_fares.append({
                        "hub": hub,
                        "domestic": serialize_flight(dom),
                        "international": serialize_flight(intl),
                        "total": round(total_price, 2),
                        "ground_cost": ground_cost,
                        "total_with_ground": total_with_ground,
                        "domestic_arrival": dom_arrival_str,
                        "international_departure": intl_depart_str,
                        "connection_time_minutes": connection_minutes,
                        "warnings": warnings,
                    })

        # Sort by total (including ground cost for hub searches)
        hacker_fares.sort(key=lambda x: (x["total_with_ground"], x["connection_time_minutes"]))

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
            )
            if key not in seen:
                seen.add(key)
                unique_fares.append(hf)
        hacker_fares = unique_fares

        # Add rank
        for i, hf in enumerate(hacker_fares):
            hf["rank"] = i + 1

        # Calculate savings vs baseline
        baseline_compare_price = None
        if baseline:
            baseline_compare_price = baseline.get("total_with_ground", baseline["price"])
            for hf in hacker_fares:
                hf["savings_vs_baseline"] = round(baseline_compare_price - hf["total_with_ground"], 2)
        else:
            for hf in hacker_fares:
                hf["savings_vs_baseline"] = None

        emit(q, "status", {
            "step": "assemble_done",
            "message": f"Assembled {len(hacker_fares)} valid hacker fare route(s)."
        })

        # ---------------------------------------------------------------
        # Step 6: Emit results
        # ---------------------------------------------------------------
        global_warnings = [
            "All hacker fare routes use SEPARATE TICKETS. You must re-check bags at the hub airport.",
            "If your positioning leg is delayed or cancelled, your international ticket is NOT protected.",
        ]
        if is_hub_search:
            global_warnings.insert(0,
                f"All {search_origin} prices include ${ground_cost:.2f} ground transit "
                f"(${DRIVING_COST:.0f} driving + ${PARKING_RATE_PER_DAY:.0f}/day x {TRIP_DAYS} days parking)."
            )

        tier_label = "Deep Search" if tier == "deep" else "Quick Search"
        api_calls = 1 + len(active_hubs) * 2  # baseline + domestic + international per hub

        results = {
            "search_origin": search_origin,
            "origin_label": origin_label,
            "is_hub_search": is_hub_search,
            "ground_cost": ground_cost,
            "destination": DESTINATION,
            "depart_date": DEPART_DATE,
            "return_date": RETURN_DATE,
            "hubs_searched": active_hubs,
            "min_connection_minutes": MIN_CONNECTION_MINUTES,
            "baseline": baseline,
            "hacker_fares": hacker_fares,
            "global_warnings": global_warnings,
            "tier": tier,
            "tier_label": tier_label,
            "api_calls_used": api_calls,
        }

        emit(q, "status", {"step": "done", "message": "Search complete!"})
        emit(q, "results", results)
        emit(q, "done", {})

    except FlightFinderError as e:
        emit(q, "error", {"message": str(e)})
        emit(q, "done", {})
    except Exception as e:
        emit(q, "error", {"message": f"Unexpected error: {str(e)}"})
        emit(q, "done", {})


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

def open_browser():
    """Open the dashboard in the default browser after a short delay."""
    threading.Timer(1.5, webbrowser.open, args=["http://localhost:5000"]).start()


if __name__ == "__main__":
    print("\n  Flight Deal Finder - Hacker Fare Engine")
    print(f"  Trip: {ORIGIN} -> {DESTINATION} | {DEPART_DATE} to {RETURN_DATE}")
    print(f"  Quick hubs ({len(HUBS_QUICK)}): {', '.join(HUBS_QUICK)}")
    print(f"  Deep  hubs ({len(HUBS_DEEP)}): {', '.join(HUBS_DEEP)}")
    print(f"  Also searchable from: {HUB_AIRPORT} (drive option)")
    print("  Opening http://localhost:5000 in your browser...\n")
    open_browser()
    app.run(debug=False, port=5000, threaded=True)
