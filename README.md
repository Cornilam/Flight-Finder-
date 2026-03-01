# Hacker Fare Engine

A flight search tool that finds **split-ticket "hacker fare" deals** by routing through hub airports. Instead of buying one expensive round-trip ticket (e.g., MKE to ATH for $1,681), it searches for cheaper combinations of separate one-way tickets through major hubs (e.g., MKE to EWR + EWR to ATH, returning the same way, for $1,375 — saving $306).

## How It Works

A "hacker fare" splits your trip into separate tickets through a connecting hub:

```
STANDARD ROUND-TRIP (1 ticket):
  MKE -----> ATH      $1,681

HACKER FARE (4 separate tickets):
  Outbound:  MKE -> EWR (positioning)    $199
             EWR -> ATH (international)   $470
  Return:    ATH -> EWR (international)   $552
             EWR -> MKE (positioning)     $154
                                   Total: $1,375  (Save $306)
```

The engine searches multiple hubs in parallel, validates connection times (minimum 2 hours for separate tickets), and ranks all viable routes by total price.

## Quick Start

### 1. Install dependencies

```bash
cd flight_deal_finder
pip install -r requirements.txt
```

### 2. Configure your trip

Copy `.env.example` to `.env` and set your values:

```env
# Your SerpAPI key (https://serpapi.com)
SERPAPI_KEY=your_key_here

# Trip details
ORIGIN=MKE
DESTINATION=ATH
DEPART_DATE=2026-05-22
RETURN_DATE=2026-05-30

# Hub airports to search through (more hubs = more API calls)
SERP_HUBS=JFK,EWR,ORD,IAD,ATL

# Drive-to-hub comparison (optional)
HUB_AIRPORT=ORD
DRIVING_COST=100.00
PARKING_RATE_PER_DAY=20.00
```

### 3. Run the dashboard

```bash
python app_serp.py
```

Opens `http://localhost:5000` with a live dashboard.

## Dashboard Features

- **Two search origins** — Compare flying from your home airport vs. driving to a nearby hub
- **Live progress streaming** — Watch each leg being searched in real-time via Server-Sent Events
- **Results table** — Ranked hacker fares with outbound/return legs, hub layover times, and savings vs. baseline
- **Price insights** — Google Flights price level indicators (low / typical / high)
- **Persistent caching** — Results survive page reloads (localStorage) and server restarts (JSON file cache)
- **Cache timestamps** — Shows when data was last fetched with "Rerun Search" buttons
- **API budget awareness** — Displays estimated API calls per search

## API Budget

SerpAPI provides **250 searches/month** on the free tier. Each hacker fare search uses approximately:

| Hubs | API Calls | Searches/Month |
|------|-----------|----------------|
| 3    | 13        | ~19            |
| 5    | 21        | ~11            |
| 8    | 33        | ~7             |

The caching system reduces this significantly:
- **Server-side cache** — Shared legs (e.g., JFK to ATH) are reused across origin searches. Switching from MKE to ORD saves ~10 API calls.
- **Client-side cache** — Switching between previously-searched origins is instant (zero API calls).
- **File persistence** — Server cache survives restarts. Entries expire after 24 hours.

## Project Structure

```
flight_deal_finder/
  app_serp.py          # Flask web app (SerpAPI / Google Flights)
  serp_flights.py      # SerpAPI search module with persistent caching
  app.py               # Alternative Flask app (Amadeus API)
  flight_finder.py     # CLI comparison tool (Amadeus API)
  requirements.txt     # Python dependencies
  .env                 # Configuration and API keys (not committed)
  .env.example         # Template for .env
  cache/
    serp_cache.json    # Persistent API response cache (auto-generated)
  templates/
    dashboard_serp.html  # SerpAPI dashboard UI
    dashboard.html       # Amadeus dashboard UI
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SERPAPI_KEY` | Yes | SerpAPI key for Google Flights searches |
| `ORIGIN` | Yes | Home airport IATA code (e.g., `MKE`) |
| `DESTINATION` | Yes | Target airport IATA code (e.g., `ATH`) |
| `DEPART_DATE` | Yes | Departure date (`YYYY-MM-DD`) |
| `RETURN_DATE` | Yes | Return date (`YYYY-MM-DD`) |
| `SERP_HUBS` | Yes | Comma-separated hub airports (e.g., `JFK,EWR,ORD,IAD,ATL`) |
| `MIN_CONNECTION_MINUTES` | No | Minimum layover at hub — default `120` |
| `HUB_AIRPORT` | No | Hub to compare driving to — default `ORD` |
| `DRIVING_COST` | No | Flat driving cost — default `100.00` |
| `PARKING_RATE_PER_DAY` | No | Daily parking at hub — default `20.00` |

## Caching Architecture

```
Browser (localStorage)          Server (Python)              Disk
  resultsCache[origin]  <-->  _cache[(type,orig,dest,date)]  <-->  cache/serp_cache.json
       |                              |                                  |
  Instant switching             Shared legs reused              Survives restarts
  between origins               across searches                 24h expiry
```

- **Layer 1 — localStorage**: Full search results per origin. Renders instantly on page load.
- **Layer 2 — In-memory dict**: Individual API responses keyed by route+date. Shared legs between MKE and ORD searches are cached.
- **Layer 3 — JSON file**: Persists Layer 2 to disk. Loaded on server startup. Entries older than 24 hours are discarded.

## Search Engines

| Engine | File | API | Best For |
|--------|------|-----|----------|
| SerpAPI | `app_serp.py` | Google Flights via SerpAPI | Primary use — real pricing, simple setup |
| Amadeus | `app.py` | Amadeus Flight Offers | Alternative — supports price confirmation |
| CLI | `flight_finder.py` | Amadeus Flight Offers | Quick terminal comparisons |

## Important Warnings

Hacker fares use **separate tickets**. This means:

- You must **re-check bags** and **clear security** at the hub airport
- If your positioning flight is delayed, your international ticket is **not protected**
- Allow at least **2 hours** between separate tickets (configurable via `MIN_CONNECTION_MINUTES`)
- Airlines have no obligation to rebook you on the other ticket if something goes wrong

## Dependencies

- **Python 3.8+**
- `flask` — Web framework and SSE streaming
- `requests` — HTTP client for API calls
- `python-dotenv` — Environment variable management
