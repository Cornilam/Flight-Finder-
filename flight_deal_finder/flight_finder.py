"""
Flight Deal Finder - Total Cost of Transit (TCT) Comparison Tool

Compares the true cost of flying from your regional airport (MKE)
versus driving to a major hub (ORD) and flying from there.

Usage:
    python flight_finder.py --destination LAX --depart 2026-03-15 --return 2026-03-20
    python flight_finder.py --destination LAX --depart 2026-03-15 --return 2026-03-20 --time-value 20
"""

import os
import sys
import argparse
import requests
from datetime import datetime, date
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# 1. CONFIGURATION - loads from .env file
# ---------------------------------------------------------------------------

load_dotenv()

AMADEUS_API_KEY = os.getenv("AMADEUS_API_KEY")
AMADEUS_API_SECRET = os.getenv("AMADEUS_API_SECRET")
REGIONAL_AIRPORT = os.getenv("REGIONAL_AIRPORT", "MKE")
HUB_AIRPORT = os.getenv("HUB_AIRPORT", "ORD")
DRIVING_COST = float(os.getenv("DRIVING_COST", "100.00"))
PARKING_RATE_PER_DAY = float(os.getenv("PARKING_RATE_PER_DAY", "20.00"))
DRIVE_TIME_MINUTES = int(os.getenv("DRIVE_TIME_MINUTES", "90"))
TIME_VALUE_PER_HOUR = float(os.getenv("TIME_VALUE_PER_HOUR", "0.00"))

AMADEUS_BASE_URL = "https://test.api.amadeus.com"


# ---------------------------------------------------------------------------
# 2. AMADEUS AUTHENTICATION
# ---------------------------------------------------------------------------

class FlightFinderError(Exception):
    """Raised when a critical step in the search pipeline fails."""
    pass


def get_amadeus_token():
    """Authenticate with Amadeus and return a bearer token."""
    url = f"{AMADEUS_BASE_URL}/v1/security/oauth2/token"
    payload = {
        "grant_type": "client_credentials",
        "client_id": AMADEUS_API_KEY,
        "client_secret": AMADEUS_API_SECRET,
    }
    resp = requests.post(url, data=payload, timeout=15)

    if resp.status_code != 200:
        raise FlightFinderError(
            f"Amadeus authentication failed (HTTP {resp.status_code}). "
            f"Check your API key and secret in .env."
        )

    token = resp.json().get("access_token")
    if not token:
        raise FlightFinderError("No access_token in Amadeus response.")

    return token


# ---------------------------------------------------------------------------
# 3. FLIGHT SEARCH
# ---------------------------------------------------------------------------

def search_flights(token, origin, destination, depart_date, return_date=None,
                   max_results=5, depart_time_from=None):
    """
    Search Amadeus for flight offers.

    Args:
        token: Amadeus bearer token.
        origin: Origin IATA code.
        destination: Destination IATA code.
        depart_date: Departure date (YYYY-MM-DD).
        return_date: Return date (YYYY-MM-DD) or None for one-way.
        max_results: Max offers to return (default 5).
        depart_time_from: Optional earliest departure time (HH:MM) for
                          filtering at the API level.

    Returns a list of parsed flight dicts sorted by price, or an empty list.
    """
    url = f"{AMADEUS_BASE_URL}/v2/shopping/flight-offers"
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "originLocationCode": origin,
        "destinationLocationCode": destination,
        "departureDate": depart_date,
        "adults": 1,
        "currencyCode": "USD",
        "max": max_results,
    }
    if return_date:
        params["returnDate"] = return_date

    resp = requests.get(url, headers=headers, params=params, timeout=30)

    if resp.status_code != 200:
        error_detail = ""
        try:
            errors = resp.json().get("errors", [])
            if errors:
                error_detail = errors[0].get("detail", resp.text)
        except Exception:
            error_detail = resp.text
        print(f"\n[WARNING] Flight search from {origin} failed (HTTP {resp.status_code}).")
        print(f"  Detail: {error_detail}")
        return []

    data = resp.json()
    offers = data.get("data", [])

    if not offers:
        print(f"  No flights found from {origin} to {destination} on those dates.")
        return []

    parsed = []
    for offer in offers:
        flight = parse_offer(offer)
        if flight:
            # If a departure time filter was requested, enforce it in code
            # (Amadeus test API doesn't always support departureTimeFrom)
            if depart_time_from and flight.get("outbound_departure_time"):
                try:
                    dep_dt = datetime.fromisoformat(flight["outbound_departure_time"])
                    filter_hour, filter_min = map(int, depart_time_from.split(":"))
                    filter_time = dep_dt.replace(hour=filter_hour, minute=filter_min,
                                                 second=0, microsecond=0)
                    if dep_dt < filter_time:
                        continue  # Skip: departs too early
                except (ValueError, AttributeError):
                    pass
            parsed.append(flight)

    parsed.sort(key=lambda f: f["price"])
    return parsed


def parse_offer(offer):
    """Extract useful fields from a single Amadeus flight offer."""
    try:
        price = float(offer["price"]["grandTotal"])

        itineraries = offer.get("itineraries", [])
        if not itineraries:
            return None

        # Outbound itinerary (first leg)
        outbound = itineraries[0]
        outbound_duration_raw = outbound.get("duration", "PT0H0M")
        outbound_minutes = parse_duration(outbound_duration_raw)
        outbound_segments = outbound.get("segments", [])

        # Return itinerary (second leg, if round trip)
        return_minutes = 0
        return_segments = []
        if len(itineraries) > 1:
            ret = itineraries[1]
            return_minutes = parse_duration(ret.get("duration", "PT0H0M"))
            return_segments = ret.get("segments", [])

        # Collect all unique carrier codes across all segments
        all_segments = outbound_segments + return_segments
        carriers = set()
        for seg in all_segments:
            carrier = seg.get("carrierCode", "")
            if carrier:
                carriers.add(carrier)

        # Count stops (segments - 1 per itinerary)
        outbound_stops = max(0, len(outbound_segments) - 1)
        return_stops = max(0, len(return_segments) - 1)

        # Calculate layover time for outbound
        layover_minutes = calculate_layover(outbound_segments)
        layover_details = calculate_layover_details(outbound_segments)

        # Build flight description
        first_seg = outbound_segments[0] if outbound_segments else {}
        carrier_code = first_seg.get("carrierCode", "??")
        flight_number = first_seg.get("number", "???")

        # Extract outbound departure time (first segment departs)
        outbound_departure_time = first_seg.get("departure", {}).get("at", "")

        # Extract outbound arrival time (last segment arrives)
        last_seg = outbound_segments[-1] if outbound_segments else {}
        outbound_arrival_time = last_seg.get("arrival", {}).get("at", "")

        # Arrival airport of outbound (final destination of outbound leg)
        outbound_arrival_airport = last_seg.get("arrival", {}).get("iataCode", "")

        # Connection airports for outbound
        connections = []
        for i in range(len(outbound_segments) - 1):
            conn_airport = outbound_segments[i].get("arrival", {}).get("iataCode", "?")
            connections.append(conn_airport)

        return {
            "price": price,
            "carriers": carriers,
            "carrier_display": f"{carrier_code} {flight_number}",
            "outbound_stops": outbound_stops,
            "return_stops": return_stops,
            "outbound_minutes": outbound_minutes,
            "return_minutes": return_minutes,
            "layover_minutes": layover_minutes,
            "layover_details": layover_details,
            "connections": connections,
            "is_hacker_fare": len(carriers) > 1,
            "outbound_departure_time": outbound_departure_time,
            "outbound_arrival_time": outbound_arrival_time,
            "outbound_arrival_airport": outbound_arrival_airport,
            "raw": offer,
        }
    except (KeyError, ValueError, IndexError) as e:
        print(f"  [WARNING] Could not parse a flight offer: {e}")
        return None


def parse_duration(iso_duration):
    """Convert ISO 8601 duration (e.g., 'PT4H35M') to minutes."""
    # Remove 'PT' prefix
    s = iso_duration.replace("PT", "")
    hours = 0
    minutes = 0
    if "H" in s:
        parts = s.split("H")
        hours = int(parts[0])
        s = parts[1]
    if "M" in s:
        minutes = int(s.replace("M", ""))
    return hours * 60 + minutes


def calculate_layover(segments):
    """Calculate total layover time between connecting segments."""
    if len(segments) <= 1:
        return 0

    total_layover = 0
    for i in range(len(segments) - 1):
        arrival_time = segments[i].get("arrival", {}).get("at", "")
        departure_time = segments[i + 1].get("departure", {}).get("at", "")
        if arrival_time and departure_time:
            try:
                arr = datetime.fromisoformat(arrival_time)
                dep = datetime.fromisoformat(departure_time)
                diff = (dep - arr).total_seconds() / 60
                if diff > 0:
                    total_layover += diff
            except ValueError:
                pass
    return int(total_layover)


def calculate_layover_details(segments):
    """
    Return a list of per-connection layover details.

    Each entry: {"airport": "FRA", "minutes": 180}
    Empty list if nonstop.
    """
    details = []
    if len(segments) <= 1:
        return details

    for i in range(len(segments) - 1):
        conn_airport = segments[i].get("arrival", {}).get("iataCode", "?")
        arrival_time = segments[i].get("arrival", {}).get("at", "")
        departure_time = segments[i + 1].get("departure", {}).get("at", "")
        minutes = 0
        if arrival_time and departure_time:
            try:
                arr = datetime.fromisoformat(arrival_time)
                dep = datetime.fromisoformat(departure_time)
                diff = (dep - arr).total_seconds() / 60
                if diff > 0:
                    minutes = int(diff)
            except ValueError:
                pass
        details.append({"airport": conn_airport, "minutes": minutes})

    return details


# ---------------------------------------------------------------------------
# 4. HACKER FARE DETECTION
# ---------------------------------------------------------------------------

def confirm_price(token, flight_offer_raw):
    """
    Call the Amadeus Flight Offers Price API to confirm availability and
    get the real-time final price for a specific flight offer.

    Args:
        token: Amadeus bearer token.
        flight_offer_raw: The raw flight offer object from Flight Offers Search.

    Returns a dict with confirmed price info, or raises FlightFinderError.
    """
    url = f"{AMADEUS_BASE_URL}/v1/shopping/flight-offers/pricing"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-HTTP-Method-Override": "POST",
    }
    payload = {
        "data": {
            "type": "flight-offers-pricing",
            "flightOffers": [flight_offer_raw],
        }
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=30)

    if resp.status_code != 200:
        error_detail = ""
        try:
            errors = resp.json().get("errors", [])
            if errors:
                error_detail = errors[0].get("detail", resp.text)
        except Exception:
            error_detail = resp.text
        raise FlightFinderError(
            f"Price confirmation failed (HTTP {resp.status_code}): {error_detail}"
        )

    data = resp.json()
    offers = data.get("data", {}).get("flightOffers", [])
    if not offers:
        raise FlightFinderError("No pricing data returned from Amadeus.")

    confirmed = offers[0]
    confirmed_price = float(confirmed.get("price", {}).get("grandTotal", 0))
    currency = confirmed.get("price", {}).get("currency", "USD")

    # Extract fare rules if available
    fare_rules = []
    for traveler_pricing in confirmed.get("travelerPricings", []):
        for segment_detail in traveler_pricing.get("fareDetailsBySegment", []):
            cabin = segment_detail.get("cabin", "")
            fare_basis = segment_detail.get("fareBasis", "")
            branded_fare = segment_detail.get("brandedFare", "")
            baggage = segment_detail.get("includedCheckedBags", {})
            bag_qty = baggage.get("quantity", 0)
            bag_weight = baggage.get("weight")
            bag_unit = baggage.get("weightUnit", "")

            fare_rules.append({
                "cabin": cabin,
                "fare_basis": fare_basis,
                "branded_fare": branded_fare,
                "included_bags": bag_qty,
                "bag_weight": f"{bag_weight} {bag_unit}" if bag_weight else None,
            })

    return {
        "confirmed_price": confirmed_price,
        "currency": currency,
        "fare_rules": fare_rules,
        "available": True,
    }


def get_hacker_fare_warning(flight):
    """Return a warning string if this is a hacker fare, else None."""
    if not flight["is_hacker_fare"]:
        return None

    carriers_str = " + ".join(sorted(flight["carriers"]))
    warning = (
        f"HACKER FARE ({carriers_str}): This itinerary uses multiple airlines.\n"
        f"      You will likely need to re-check your bags at connections."
    )
    return warning


# ---------------------------------------------------------------------------
# 5. COST CALCULATION ENGINE
# ---------------------------------------------------------------------------

def calculate_route_a(flight):
    """
    Route A: Fly from regional airport (MKE).
    Total cost = flight price. That's it.
    """
    return {
        "label": f"Fly from {REGIONAL_AIRPORT}",
        "flight_price": flight["price"],
        "driving_cost": 0.0,
        "parking_cost": 0.0,
        "total_cost": flight["price"],
        "flight": flight,
    }


def calculate_route_b(flight, trip_days):
    """
    Route B: Drive to hub (ORD) and fly from there.
    Total cost = flight price + $100 flat driving + $20/day parking.
    """
    parking_cost = PARKING_RATE_PER_DAY * trip_days
    total = flight["price"] + DRIVING_COST + parking_cost

    return {
        "label": f"Drive to {HUB_AIRPORT} + Fly",
        "flight_price": flight["price"],
        "driving_cost": DRIVING_COST,
        "parking_cost": parking_cost,
        "total_cost": total,
        "flight": flight,
    }


# ---------------------------------------------------------------------------
# 6. TERMINAL OUTPUT FORMATTER
# ---------------------------------------------------------------------------

def format_minutes(minutes):
    """Convert minutes to a readable string like '4h 35m'."""
    if minutes <= 0:
        return "N/A"
    h = minutes // 60
    m = minutes % 60
    if h > 0 and m > 0:
        return f"{h}h {m}m"
    elif h > 0:
        return f"{h}h"
    else:
        return f"{m}m"


def print_comparison(route_a, route_b, destination, depart_date, return_date,
                     trip_days, time_value):
    """Print a side-by-side comparison of Route A vs Route B."""

    flight_a = route_a["flight"]
    flight_b = route_b["flight"]

    w = 62  # total width

    print()
    print("=" * w)
    print("  FLIGHT DEAL FINDER - Total Cost of Transit Comparison")
    print("=" * w)
    print(f"  Trip: {REGIONAL_AIRPORT} / {HUB_AIRPORT}  -->  {destination}")
    print(f"  Dates: {depart_date} to {return_date} ({trip_days} day{'s' if trip_days != 1 else ''})")
    print("=" * w)
    print()

    # Column setup
    col = 28
    lbl_a = f"ROUTE A: Fly from {REGIONAL_AIRPORT}"
    lbl_b = f"ROUTE B: Drive to {HUB_AIRPORT} + Fly"

    print(f"  {lbl_a:<{col}}  {lbl_b}")
    print(f"  {'-' * col}  {'-' * col}")

    # Flight price
    price_a = f"  ${route_a['flight_price']:.2f}"
    price_b = f"  ${route_b['flight_price']:.2f}"
    print(f"  {'Flight Price:':<{col}}  {'Flight Price:'}")
    print(f"  {price_a:<{col}}  {price_b}")
    print()

    # Driving cost (only Route B)
    driving_str = f"Driving (flat):  + ${route_b['driving_cost']:.2f}"
    parking_str = f"Parking ({trip_days}d):    + ${route_b['parking_cost']:.2f}"
    print(f"  {'':<{col}}  {driving_str}")
    print(f"  {'':<{col}}  {parking_str}")
    print()

    # Totals
    print(f"  {'-' * col}  {'-' * col}")
    total_a_str = f"  TOTAL: ${route_a['total_cost']:.2f}"
    total_b_str = f"  TOTAL: ${route_b['total_cost']:.2f}"
    print(f"  {total_a_str:<{col}}  {total_b_str}")
    print(f"  {'-' * col}  {'-' * col}")
    print()

    # Flight details
    stops_a = f"{flight_a['outbound_stops']} stop{'s' if flight_a['outbound_stops'] != 1 else ''}"
    stops_b = f"{flight_b['outbound_stops']} stop{'s' if flight_b['outbound_stops'] != 1 else ''}"
    if flight_a["outbound_stops"] == 0:
        stops_a = "Nonstop"
    if flight_b["outbound_stops"] == 0:
        stops_b = "Nonstop"

    conn_a = f" ({', '.join(flight_a['connections'])})" if flight_a["connections"] else ""
    conn_b = f" ({', '.join(flight_b['connections'])})" if flight_b["connections"] else ""

    detail_a1 = f"  Best: {flight_a['carrier_display']}"
    detail_b1 = f"  Best: {flight_b['carrier_display']}"
    print(f"  {detail_a1:<{col}}  {detail_b1}")

    detail_a2 = f"  Stops: {stops_a}{conn_a}"
    detail_b2 = f"  Stops: {stops_b}{conn_b}"
    print(f"  {detail_a2:<{col}}  {detail_b2}")

    detail_a3 = f"  Flight Time: {format_minutes(flight_a['outbound_minutes'])}"
    detail_b3 = f"  Flight Time: {format_minutes(flight_b['outbound_minutes'])}"
    print(f"  {detail_a3:<{col}}  {detail_b3}")

    if flight_a["layover_minutes"] > 0:
        detail_a4 = f"  Layover: {format_minutes(flight_a['layover_minutes'])}"
    else:
        detail_a4 = ""
    detail_b4 = f"  Drive to {HUB_AIRPORT}: ~{format_minutes(DRIVE_TIME_MINUTES)}"
    print(f"  {detail_a4:<{col}}  {detail_b4}")

    print()

    # Winner
    diff = abs(route_a["total_cost"] - route_b["total_cost"])
    if route_a["total_cost"] < route_b["total_cost"]:
        winner = f"Route A ({REGIONAL_AIRPORT}) saves ${diff:.2f}"
    elif route_b["total_cost"] < route_a["total_cost"]:
        winner = f"Route B ({HUB_AIRPORT}) saves ${diff:.2f}"
    else:
        winner = "It's a tie!"

    print(f"  ** WINNER: {winner} **")
    print()

    # Hacker fare warnings
    for label, flight in [("Route A", flight_a), ("Route B", flight_b)]:
        warning = get_hacker_fare_warning(flight)
        if warning:
            print(f"  [!] {label} flight is a {warning}")
            print()

    # Time value analysis
    if time_value > 0:
        # Route A: layover time is the "wasted" time
        a_time_minutes = flight_a["layover_minutes"]
        # Route B: drive time is the extra time commitment
        b_time_minutes = DRIVE_TIME_MINUTES

        a_time_cost = (a_time_minutes / 60) * time_value
        b_time_cost = (b_time_minutes / 60) * time_value

        a_with_time = route_a["total_cost"] + a_time_cost
        b_with_time = route_b["total_cost"] + b_time_cost

        print(f"  Time Value Analysis (${time_value:.0f}/hr):")
        print(f"    Route A time cost: ${a_time_cost:.2f} ({format_minutes(a_time_minutes)} layover)")
        print(f"    Route B time cost: ${b_time_cost:.2f} ({format_minutes(b_time_minutes)} drive)")
        print(f"    With time value:  A = ${a_with_time:.2f}  |  B = ${b_with_time:.2f}")

        time_diff = abs(a_with_time - b_with_time)
        if a_with_time < b_with_time:
            print(f"    --> With time factored in: Route A saves ${time_diff:.2f}")
        elif b_with_time < a_with_time:
            print(f"    --> With time factored in: Route B saves ${time_diff:.2f}")
        else:
            print(f"    --> With time factored in: Still a tie!")
        print()

    print("=" * w)
    print()


def print_no_flights_message(origin, destination, depart_date, return_date):
    """Print a message when no flights were found for an origin."""
    print(f"\n  [!] No flights found from {origin} to {destination}")
    print(f"      for {depart_date} to {return_date}.")
    print(f"      Try different dates or check that the airport codes are valid.\n")


# ---------------------------------------------------------------------------
# 7. CLI ARGUMENT PARSER & MAIN
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Flight Deal Finder - Compare flying from MKE vs driving to ORD.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python flight_finder.py --destination LAX --depart 2026-03-15 --return 2026-03-20
  python flight_finder.py --destination DEN --depart 2026-06-01 --return 2026-06-07 --time-value 25
  python flight_finder.py --destination JFK --depart 2026-04-10 --return 2026-04-14 --driving-cost 120 --parking-rate 25
        """,
    )
    parser.add_argument(
        "--destination", "-d", required=True,
        help="Destination airport IATA code (e.g., LAX, DEN, JFK)",
    )
    parser.add_argument(
        "--depart", required=True,
        help="Departure date in YYYY-MM-DD format",
    )
    parser.add_argument(
        "--return", dest="return_date", required=True,
        help="Return date in YYYY-MM-DD format",
    )
    parser.add_argument(
        "--driving-cost", type=float, default=None,
        help=f"Flat driving cost to hub (default: ${DRIVING_COST:.0f} from .env)",
    )
    parser.add_argument(
        "--parking-rate", type=float, default=None,
        help=f"Daily parking rate at hub (default: ${PARKING_RATE_PER_DAY:.0f}/day from .env)",
    )
    parser.add_argument(
        "--time-value", type=float, default=None,
        help=f"Value of your time in $/hour for comparison (default: ${TIME_VALUE_PER_HOUR:.0f}/hr from .env)",
    )

    return parser.parse_args()


def validate_date(date_str):
    """Validate a date string and return a date object."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        print(f"\n[ERROR] Invalid date format: '{date_str}'. Use YYYY-MM-DD.")
        sys.exit(1)


def main():
    args = parse_args()

    # Validate credentials
    if not AMADEUS_API_KEY or not AMADEUS_API_SECRET:
        print("\n[ERROR] Missing Amadeus API credentials.")
        print("  Make sure AMADEUS_API_KEY and AMADEUS_API_SECRET are set in your .env file.")
        sys.exit(1)

    # Parse and validate dates
    depart = validate_date(args.depart)
    ret = validate_date(args.return_date)

    if ret <= depart:
        print("\n[ERROR] Return date must be after departure date.")
        sys.exit(1)

    trip_days = (ret - depart).days
    destination = args.destination.upper()

    # Apply CLI overrides
    global DRIVING_COST, PARKING_RATE_PER_DAY, TIME_VALUE_PER_HOUR
    if args.driving_cost is not None:
        DRIVING_COST = args.driving_cost
    if args.parking_rate is not None:
        PARKING_RATE_PER_DAY = args.parking_rate
    time_value = args.time_value if args.time_value is not None else TIME_VALUE_PER_HOUR

    # Authenticate with Amadeus
    print(f"\n  Authenticating with Amadeus API...")
    token = get_amadeus_token()
    print(f"  Authenticated successfully.")

    # Search flights from both airports
    print(f"\n  Searching flights from {REGIONAL_AIRPORT} to {destination}...")
    regional_flights = search_flights(token, REGIONAL_AIRPORT, destination,
                                      args.depart, args.return_date)

    print(f"  Searching flights from {HUB_AIRPORT} to {destination}...")
    hub_flights = search_flights(token, HUB_AIRPORT, destination,
                                 args.depart, args.return_date)

    # Handle no results
    if not regional_flights and not hub_flights:
        print("\n  No flights found from either airport. Try different dates or destination.")
        sys.exit(0)

    if not regional_flights:
        print_no_flights_message(REGIONAL_AIRPORT, destination, args.depart, args.return_date)
        print(f"  Only showing Route B ({HUB_AIRPORT}) results:\n")
        best_hub = hub_flights[0]
        route_b = calculate_route_b(best_hub, trip_days)
        print(f"  Route B Total: ${route_b['total_cost']:.2f}")
        print(f"    Flight: ${route_b['flight_price']:.2f} | Driving: ${route_b['driving_cost']:.2f} | Parking: ${route_b['parking_cost']:.2f}")
        warning = get_hacker_fare_warning(best_hub)
        if warning:
            print(f"  [!] {warning}")
        print()
        sys.exit(0)

    if not hub_flights:
        print_no_flights_message(HUB_AIRPORT, destination, args.depart, args.return_date)
        print(f"  Only showing Route A ({REGIONAL_AIRPORT}) results:\n")
        best_regional = regional_flights[0]
        route_a = calculate_route_a(best_regional)
        print(f"  Route A Total: ${route_a['total_cost']:.2f}")
        print(f"    Flight: ${route_a['flight_price']:.2f}")
        warning = get_hacker_fare_warning(best_regional)
        if warning:
            print(f"  [!] {warning}")
        print()
        sys.exit(0)

    # Both routes have results - compare the cheapest from each
    best_regional = regional_flights[0]
    best_hub = hub_flights[0]

    route_a = calculate_route_a(best_regional)
    route_b = calculate_route_b(best_hub, trip_days)

    print_comparison(route_a, route_b, destination, args.depart, args.return_date,
                     trip_days, time_value)


if __name__ == "__main__":
    main()
