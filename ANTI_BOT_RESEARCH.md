# Anti-Bot Protection & Award Flight Scraping Research

## Date: February 19, 2026

---

## Airline-by-Airline Analysis

### 1. Southwest Airlines (southwest.com) — Rapid Rewards Points

| Attribute | Details |
|---|---|
| **Anti-Bot Protection** | **Akamai Bot Manager** — First script loaded is Akamai's sensor script (`/resources/6ce97dd726548ff55d184615dd91bee22130011c85bd8`). This is one of the hardest bot protections to bypass. |
| **Architecture** | React SPA. The booking page (`/air/booking/select.html`) loads `app.js`, `vendor.js` bundles. Flight results are fetched via XHR/API calls after the React app initializes. All client-side rendered. |
| **Award Search Auth** | **No login required**. You can toggle "Points" on the search form without logging in. Southwest shows Rapid Rewards point prices publicly. |
| **API** | Internal API endpoints exist (called by the React app), but they're protected behind Akamai's sensor cookie validation. The API requires valid `_abck` and `bm_sz` cookies from Akamai. |
| **Mobile App API** | Southwest has a mobile app with its own API. Historically this was easier to access, but Southwest has progressively locked it down with certificate pinning and additional bot checks. |
| **Community Reports** | Multiple GitHub scrapers exist (6+ repos: `techtricity/swatcher`, `Erik-Debye/SWA-Scraper`, etc.). The SWA-Scraper project explicitly notes it uses `puppeteer-extra-plugin-stealth` and `ghost-cursor` to "trick SW's bot checkers" and admits it still gets detected ~15% of the time. **Southwest is considered one of the hardest airlines to scrape.** |
| **Difficulty** | **VERY HARD** (9/10) |

### 2. Alaska Airlines (alaskaair.com) — Mileage Plan Miles

| Attribute | Details |
|---|---|
| **Anti-Bot Protection** | **Akamai Bot Manager** + **AppDynamics** monitoring. The HTML loads `adrum.js` (AppDynamics Real User Monitoring). Also loads Tealium tag manager and Optimizely. No evidence of PerimeterX or Cloudflare. |
| **Architecture** | **Next.js** SPA (visible from `_next/static/chunks/` paths and `data-next-head` attributes). Flight search results are client-side rendered via React/Next.js with data fetched from internal APIs. |
| **Award Search Auth** | **Login required** for award/miles searches. You must be authenticated with a Mileage Plan account to see award pricing. This is a significant barrier. |
| **API** | Internal APIs behind authentication + Akamai. Also uses Airtrfx (`em-frontend-assets.airtrfx.com`) for some frontend features. |
| **Mobile App API** | Alaska has a mobile app. The API uses OAuth-style auth and is somewhat documented in community reverse-engineering efforts, but also protected. |
| **Community Reports** | Limited scraper repos on GitHub. The login requirement makes it significantly harder for casual scraping. |
| **Aggregator Coverage** | **seats.aero tracks Alaska Mileage Plan** (source code: `alaska`). AwardFares also lists "Alaska Mileage Plan" as a supported program. This is likely the best path. |
| **Difficulty** | **HARD** (8/10) — login wall + Akamai |

### 3. JetBlue (jetblue.com) — TrueBlue Points

| Attribute | Details |
|---|---|
| **Anti-Bot Protection** | **Minimal / Standard** — No Akamai, Cloudflare, or PerimeterX detected in the HTML. Uses Okta-based auth (`accounts.jetblue.com/oauth2/`), GTM, DynamicYield personalization, and TrustArc consent. No heavy bot defense layer visible. |
| **Architecture** | **Next.js** SPA (visible from `/_next/static/chunks/` paths, Turbopack references). Modern React app with client-side rendering. The booking form at `jetblue.com/booking/flights` has a "Use TrueBlue points" checkbox built into the public search form. |
| **Award Search Auth** | **No login required** to search. The "Use TrueBlue points" toggle is available on the public booking page. Points prices appear to be visible without authentication. |
| **API** | Internal Next.js API routes. The form action is `https://www.jetblue.com/booking/flights` which suggests server-side form submission + redirect pattern. |
| **Mobile App API** | JetBlue has a mobile app. The API may be accessible but community documentation is thin. |
| **Community Reports** | Only 2 scraper repos on GitHub, both old/abandoned. The relative lack of anti-bot protection makes this a reasonable target. |
| **Aggregator Coverage** | **seats.aero tracks JetBlue TrueBlue** (source code: `jetblue`). **AwardFares also supports JetBlue TrueBlue.** This is likely the easiest path. |
| **Difficulty** | **MODERATE** (5/10) — No login wall, lighter bot protection |

### 4. Frontier Airlines (flyfrontier.com) — FRONTIER Miles

| Attribute | Details |
|---|---|
| **Anti-Bot Protection** | **Minimal** — Traditional server-rendered site (ASP.NET MVC). No Akamai, Cloudflare, or PerimeterX detected. Uses Ketch for consent management and ClickTripz for comparison ads. Uses Azure Front Door CDN (`azurefd.net`). |
| **Architecture** | **Server-side rendered** (ASP.NET MVC / jQuery). The booking widget is a traditional form with jQuery, not a heavy SPA. The search redirects to `booking.flyfrontier.com`. This is the most "old-school" architecture of all five airlines. |
| **Award Search Auth** | **No login required** to search by miles. The search form has "Dollars" / "Miles" radio buttons (`searchDollars` / `searchPoints`) visible on the public homepage. |
| **API** | The booking engine at `booking.flyfrontier.com` is a Navitaire-based booking system (common for ULCCs). The form posts search parameters to this domain. |
| **Mobile App API** | Frontier has a mobile app. The Navitaire backend may have its own API patterns. |
| **Community Reports** | Very few scraper repos. The server-rendered architecture and standard form submission make this relatively approachable. |
| **Aggregator Coverage** | **NOT tracked by seats.aero or AwardFares.** Frontier Miles is not in the supported program list for either aggregator. Must scrape directly. |
| **Difficulty** | **EASY-MODERATE** (3/10) — Server-rendered, no login, light bot protection |

### 5. Spirit Airlines (spirit.com) — Free Spirit Points

| Attribute | Details |
|---|---|
| **Anti-Bot Protection** | **PerimeterX (HUMAN Security)** — Clearly visible in HTML: `window.PXkp4CLSb5_asyncInit` and `/kp4CLSb5/init.js`. Also loads **Akamai** fingerprinting (`/FeOjXGZsrl/...` obfuscated paths are Akamai Bot Manager patterns). **Dual protection: PerimeterX + Akamai.** Also uses Dynatrace RUM monitoring. |
| **Architecture** | **Angular SPA** (visible from `<app-root>` tag, chunk-based JS loading, Angular-style file names like `main-HNSMV6H2.js`). Heavy client-side rendering. |
| **Award Search Auth** | Spirit requires searching for "points" pricing, unclear if login is needed for initial search. The Free Spirit program is integrated into the booking flow. |
| **API** | Internal Angular API calls, heavily protected behind both PerimeterX and Akamai sensor validation. |
| **Mobile App API** | Spirit has a mobile app. Certificate pinning likely present. |
| **Community Reports** | Virtually no public scrapers. The dual PerimeterX + Akamai protection makes this extremely difficult. |
| **Aggregator Coverage** | **NOT tracked by seats.aero or AwardFares.** Free Spirit is not in the supported program list. Must scrape directly. |
| **Difficulty** | **EXTREMELY HARD** (10/10) — Dual PerimeterX + Akamai |

---

## Aggregators & Alternative Data Sources

### Google Flights
| Attribute | Details |
|---|---|
| **Award/Points Prices** | **NO.** Google Flights only shows cash prices. It does not display award/points pricing for any airline. |
| **Public API** | **NO.** Google deprecated the QPX Express API years ago. There is no official Google Flights API. |
| **Scraping** | Google Flights uses heavy anti-bot (reCAPTCHA, fingerprinting). Not a viable path. |
| **Verdict** | **Not useful for award flight data.** |

### seats.aero
| Attribute | Details |
|---|---|
| **Public API** | **YES!** Well-documented REST API at `https://seats.aero/partnerapi/`. Requires Pro subscription ($10/month). |
| **Coverage** | Tracks **25+ mileage programs** including: Alaska Mileage Plan, JetBlue TrueBlue, Delta SkyMiles, United MileagePlus, American AAdvantage. |
| **Missing** | **Does NOT track Southwest, Frontier, or Spirit.** |
| **Data Type** | Cached availability data (not real-time for Pro users). Popular routes have good coverage. |
| **Rate Limits** | Reasonable for Pro tier. Partner API has higher limits. |
| **Verdict** | **BEST option for Alaska and JetBlue award data.** |

### point.me
| Attribute | Details |
|---|---|
| **Public API** | **NO.** No public API. Subscription-based web service only. |
| **Coverage** | Claims to search 100+ airlines and 30+ loyalty programs. |
| **Data Type** | Real-time search via their platform (likely using browser automation internally). |
| **Verdict** | **Not usable programmatically** unless reverse-engineering their API. |

### AwardFares
| Attribute | Details |
|---|---|
| **Public API** | **NO official public API.** Their website has internal APIs but no documented external access. |
| **Coverage** | Supports 20+ programs including JetBlue TrueBlue, Alaska Mileage Plan. Does NOT cover Southwest, Frontier, or Spirit. |
| **Verdict** | **Web-only, not usable programmatically.** |

---

## Comparison Matrix

| Airline | Bot Protection | Architecture | Login Required? | Aggregator Available? | Scraping Difficulty | Best Approach |
|---|---|---|---|---|---|---|
| **Southwest** | Akamai Bot Manager | React SPA | No | NO | 9/10 (Very Hard) | Headless browser + stealth plugins; unreliable |
| **Alaska** | Akamai + AppDynamics | Next.js SPA | YES | YES (seats.aero, AwardFares) | 8/10 (Hard) | **Use seats.aero API** |
| **JetBlue** | Minimal (GTM/Okta) | Next.js SPA | No | YES (seats.aero, AwardFares) | 5/10 (Moderate) | **Use seats.aero API** |
| **Frontier** | Minimal (Azure FD) | ASP.NET MVC + jQuery | No | NO | 3/10 (Easiest) | Direct scraping (form POST to Navitaire) |
| **Spirit** | PerimeterX + Akamai | Angular SPA | Unclear | NO | 10/10 (Hardest) | Headless browser; very unreliable |

---

## Recommended Strategy: Path of Least Resistance

### Tier 1 — Use seats.aero API (Easiest, most reliable)
- **Alaska Airlines** and **JetBlue**: Use the seats.aero Partner API (`$10/mo Pro plan`).
- Cached data, well-documented, no bot-fighting needed.
- Endpoints: `/search`, `/availability`, `/trips/{id}`
- Source codes: `alaska` for Alaska, `jetblue` for JetBlue.

### Tier 2 — Direct scraping (Moderate effort)
- **Frontier Airlines**: Server-rendered site with minimal protection. Standard HTTP requests with form POST to `booking.flyfrontier.com`. The Navitaire booking engine may respond with structured data. Easiest airline to scrape directly. Toggle the "Miles" radio button in the form POST data.

### Tier 3 — Headless browser with stealth (Hard, unreliable)
- **Southwest Airlines**: Requires Puppeteer/Playwright with stealth plugins, user-agent randomization, residential proxies, and mouse movement simulation. Expect 15-30% failure rate even with best-in-class evasion. The SWA-Scraper project on GitHub is a reference implementation.

### Tier 4 — Avoid if possible
- **Spirit Airlines**: Dual PerimeterX + Akamai makes this nearly impossible to scrape reliably. Consider whether Spirit award data is critical to the product. If needed, a CAPTCHA-solving service + residential proxies + stealth browser may work occasionally.

---

## Technical Implementation Notes

### For seats.aero API:
```bash
# Search JetBlue availability from JFK to LAX
curl -H "Partner-Authorization: YOUR_API_KEY" \
  "https://seats.aero/partnerapi/search?origin_airport=JFK&destination_airport=LAX&source=jetblue&start_date=2026-03-01&end_date=2026-03-31"

# Search Alaska availability
curl -H "Partner-Authorization: YOUR_API_KEY" \
  "https://seats.aero/partnerapi/search?origin_airport=SEA&destination_airport=LAX&source=alaska"
```

### For Frontier (direct scraping):
The search form POSTs to `https://booking.flyfrontier.com` with parameters:
- `origin` / `destination` (airport codes)
- `departureDate` / `returnDate`
- `searchType=searchPoints` (for miles pricing)
- `tripType=roundtrip|oneway`

### For Southwest (headless browser):
```javascript
// Reference: Erik-Debye/SWA-Scraper approach
// Requires: puppeteer-extra, puppeteer-extra-plugin-stealth, ghost-cursor, random-useragent
// ~85% success rate per the author
```

---

## Key Takeaways

1. **seats.aero is the single best resource** — it covers Alaska and JetBlue with a clean API, eliminating the need to fight their bot protection.

2. **Frontier is the easiest to scrape directly** — minimal protection, server-rendered, no login required, public miles search.

3. **Southwest is a known hard target** — community has active scrapers but they break frequently. Budget for maintenance.

4. **Spirit should be deprioritized** — the dual bot protection stack makes reliable scraping nearly impossible.

5. **Google Flights is useless** for award/points data — cash prices only, no API.

6. **No aggregator covers all five airlines** — Southwest, Frontier, and Spirit are notably absent from all major aggregator platforms, likely because they are either hard to scrape (Southwest, Spirit) or use proprietary ULCC booking systems (Frontier, Spirit).
