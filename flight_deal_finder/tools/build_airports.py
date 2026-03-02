"""
One-time script to download OpenFlights airport data and produce airports.json.

Usage:
    python tools/build_airports.py

Output:
    static/airports.json  (~6,000 airports with valid IATA codes)
"""

import csv
import json
import urllib.request
from pathlib import Path

URL = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airports.dat"

# OpenFlights airports.dat columns (no header row)
COLUMNS = [
    "id", "name", "city", "country", "iata", "icao",
    "lat", "lon", "alt", "tz_offset", "dst", "tz_name", "type", "source",
]


def build():
    print(f"Downloading {URL} ...")
    resp = urllib.request.urlopen(URL)
    text = resp.read().decode("utf-8")

    airports = []
    reader = csv.reader(text.splitlines())
    for row in reader:
        if len(row) < 6:
            continue
        iata = row[4].strip().strip('"')
        # Skip entries without a valid 3-letter IATA code
        if not iata or iata == "\\N" or len(iata) != 3 or not iata.isalpha():
            continue
        airports.append({
            "iata": iata.upper(),
            "city": row[2].strip().strip('"'),
            "name": row[1].strip().strip('"'),
            "country": row[3].strip().strip('"'),
        })

    # De-duplicate by IATA code (keep first occurrence)
    seen = set()
    unique = []
    for a in airports:
        if a["iata"] not in seen:
            seen.add(a["iata"])
            unique.append(a)

    # Sort by city name
    unique.sort(key=lambda a: a["city"].lower())

    out_path = Path(__file__).parent.parent / "static" / "airports.json"
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(unique, f, ensure_ascii=False, separators=(",", ":"))

    print(f"Wrote {len(unique)} airports to {out_path}")
    print(f"File size: {out_path.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    build()
