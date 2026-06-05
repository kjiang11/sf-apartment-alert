import json
import os
import re
import smtplib
import time
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from playwright.sync_api import sync_playwright

# ── Config ────────────────────────────────────────────────────────────────────
SEEN_FILE    = "seen_listings.json"
PENDING_FILE = "pending_listings.json"
RECIPIENT    = "hmj.firstclass@gmail.com"
SENDER       = os.environ.get("GMAIL_ADDRESS")
APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
PRICE_MAX    = 4000

TARGET_NEIGHBORHOODS = [
    "bernal heights",
    "bernal",
    "potrero hill",
    "potrero",
    "mission",
    "the mission",
    "noe valley",
    "noe",
    "dogpatch",
]

CL_URL = (
    "https://sfbay.craigslist.org/search/sfc/apa"
    "?max_price=4000&availabilityMode=0&sale_date=all+dates"
)

# ── Daytime check ─────────────────────────────────────────────────────────────
def is_daytime() -> bool:
    return True  # TEMP: always send immediately for testing

# ── Helpers ───────────────────────────────────────────────────────────────────
def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()

def save_seen(seen: set):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=2)

def extract_price(text: str):
    for m in re.findall(r'\$[\d,]+', text):
        p = int(m.replace("$", "").replace(",", ""))
        if 500 < p < 15_000:
            return p
    return None

def extract_bedrooms(text: str):
    t = text.lower()
    if re.search(r'\b(studio|efficiency)\b', t):
        return "Studio"
    m = re.search(r'(\d)\s*(?:br|bed|bedroom)', t)
    if m:
        n = int(m.group(1))
        return f"{n}BR"
    return "?"

def in_target_neighborhood(text: str) -> bool:
    t = text.lower()
    return any(hood in t for hood in TARGET_NEIGHBORHOODS)

def parse_parking(text: str) -> str:
    t = text.lower()
    if re.search(r'\bno parking\b|\bparking not\b|\bstreet parking only\b', t):
        return "No parking"
    if re.search(r'\bparking included\b|\bparking available\b|\bgarage\b|\bcarport\b|\boff.street parking\b|\bparking space\b', t):
        return "Parking available"
    if re.search(r'\bparking\b', t):
        return "Parking (see listing)"
    return "Not mentioned"

def parse_laundry(text: str) -> str:
    t = text.lower()
    if re.search(r'in.unit\s*(w/?d|washer|laundry)|washer.{0,10}dryer.{0,20}in.unit|in.unit laundry', t):
        return "In-unit W/D"
    if re.search(r'\bw[/\-]?d\b|washer.{0,10}dryer|laundry\s*(room|in\s*building|on.site|on\s*site)', t):
        return "W/D in building"
    return "Not accessible / not mentioned"

def parse_dishwasher(text: str) -> str:
    return "Yes" if re.search(r'\bdishwasher\b', text.lower()) else "Not mentioned"

def parse_sqft(text: str) -> str:
    m = re.search(r'(\d{3,4})\s*(?:sq\.?\s*ft|sqft|square\s*feet)', text.lower())
    return f"{m.group(1)} sq ft" if m else "Not listed"

# ── Fetch detail page ─────────────────────────────────────────────────────────
def fetch_detail(page, url: str) -> dict:
    """Visit a listing page and extract detail fields."""
    detail = {
        "body":    "",
        "sqft":    "Not listed",
        "posted":  "Unknown",
        "contact": "Not listed",
    }
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        time.sleep(1)

        body_el = page.query_selector("#postingbody")
        detail["body"] = body_el.inner_text().strip() if body_el else ""

        sqft_el = page.query_selector(".housing")
        if sqft_el:
            detail["sqft"] = parse_sqft(sqft_el.inner_text())
        if detail["sqft"] == "Not listed":
            detail["sqft"] = parse_sqft(detail["body"])

        time_el = page.query_selector("time.date.timeago")
        if time_el:
            detail["posted"] = time_el.get_attribute("title") or time_el.inner_text().strip()

        reply_el = page.query_selector(".reply-button, .replylink, #replylink")
        if reply_el:
            detail["contact"] = reply_el.inner_text().strip() or "See listing reply button"

        # Try to find an email/phone in body text
        phone_match = re.search(r'(\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4})', detail["body"])
        email_match = re.search(r'[\w.\-]+@[\w.\-]+\.\w+', detail["body"])
        if phone_match or email_match:
            parts = []
            if phone_match:
                parts.append(phone_match.group(1))
            if email_match:
                parts.append(email_match.group(0))
            detail["contact"] = ", ".join(parts)

    except Exception as e:
        print(f"    Detail fetch error: {e}")
    return detail

# ── Scraping ──────────────────────────────────────────────────────────────────
def fetch_listings():
    listings = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()
        print(f"Fetching {CL_URL}")
        page.goto(CL_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

        selectors = [
            "li.cl-search-result",
            "li.gallery-card",
            ".cl-search-result",
            "li[data-pid]",
            ".result-row",
        ]
        items = []
        for sel in selectors:
            items = page.query_selector_all(sel)
            if items:
                print(f"  Found {len(items)} listings with selector '{sel}'")
                break

        for item in items:
            try:
                link_el  = item.query_selector("a.cl-app-anchor")
                title_el = item.query_selector(".label")
                price_el = item.query_selector(".priceinfo")
                hood_el  = item.query_selector(".meta")

                if not link_el or not title_el:
                    continue

                url        = link_el.get_attribute("href") or ""
                title      = title_el.inner_text().strip()
                price_text = price_el.inner_text().strip() if price_el else ""
                hood_text  = hood_el.inner_text().strip()  if hood_el  else ""
                listing_id = url.split("/")[-1].replace(".html", "")

                listings.append({
                    "id":    listing_id,
                    "url":   url,
                    "title": title,
                    "price": price_text,
                    "meta":  hood_text,
                })
            except Exception as e:
                print(f"  Error parsing item: {e}")

        # Now visit each candidate's detail page
        seen = load_seen()
        candidates = []
        for item in listings:
            lid   = item["id"]
            title = item["title"]
            meta  = item["meta"]
            price = extract_price(item["price"] + " " + title)

            if lid in seen:
                print(f"  SKIP (seen) {title[:60]}")
                continue
            if price and price > PRICE_MAX:
                print(f"  SKIP (price ${price}) {title[:60]}")
                continue
            if re.search(r'\bfurnished\b', (title + meta).lower()) and \
               not re.search(r'\bunfurnished\b', (title + meta).lower()):
                print(f"  SKIP (furnished) {title[:60]}")
                continue

            candidates.append(item)

        print(f"  Fetching detail pages for {len(candidates)} candidates...")
        for item in candidates:
            print(f"    {item['title'][:60]}")
            detail = fetch_detail(page, item["url"])
            item.update(detail)

        browser.close()

    return listings, candidates, seen

# ── Email ─────────────────────────────────────────────────────────────────────
def build_email_rows(matches: list) -> str:
    rows = ""
    for m in matches:
        title   = m.get("title", "")
        url     = m.get("url", "")
        meta    = m.get("meta", "")
        price   = m.get("price", "?")
        beds    = m.get("beds", "?")
        parking = m.get("parking", "Not mentioned")
        laundry = m.get("laundry", "Not mentioned")
        dish    = m.get("dishwasher", "Not mentioned")
        sqft    = m.get("sqft", "Not listed")
        posted  = m.get("posted", "Unknown")
        contact = m.get("contact", "Not listed")

        rows += f"""
        <tr>
          <td style="padding:14px;border-bottom:1px solid #eee;vertical-align:top">
            <a href="{url}" style="font-weight:bold;font-size:15px;color:#1a0dab;text-decoration:none">{title}</a><br>
            <span style="color:#888;font-size:12px">{meta}</span>
          </td>
          <td style="padding:14px;border-bottom:1px solid #eee;white-space:nowrap;vertical-align:top">{price}</td>
          <td style="padding:14px;border-bottom:1px solid #eee;white-space:nowrap;vertical-align:top">{beds}</td>
          <td style="padding:14px;border-bottom:1px solid #eee;vertical-align:top">
            <b>Parking:</b> {parking}<br>
            <b>Laundry:</b> {laundry}<br>
            <b>Dishwasher:</b> {dish}<br>
            <b>Sq ft:</b> {sqft}<br>
            <b>Posted:</b> {posted}<br>
            <b>Contact:</b> {contact}
          </td>
        </tr>"""
    return rows

def send_email(matches: list, subject_prefix: str = "🏠"):
    rows = build_email_rows(matches)
    count = len(matches)
    html = f"""
    <html><body style="font-family:sans-serif;max-width:960px;margin:0 auto">
      <h2 style="color:#333">{subject_prefix} {count} apartment listing{'s' if count != 1 else ''}</h2>
      <table width="100%" cellspacing="0" style="border-collapse:collapse;border:1px solid #eee">
        <thead>
          <tr style="background:#f5f5f5">
            <th style="padding:10px;text-align:left">Listing</th>
            <th style="padding:10px;text-align:left">Price</th>
            <th style="padding:10px;text-align:left">Beds</th>
            <th style="padding:10px;text-align:left">Details</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{subject_prefix} {count} new SF apartment{'s' if count != 1 else ''} found"
    msg["From"]    = SENDER
    msg["To"]      = RECIPIENT
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(SENDER, APP_PASSWORD)
        s.sendmail(SENDER, RECIPIENT, msg.as_string())
    print(f"Email sent: {count} listings.")

# ── Pending (overnight) helpers ───────────────────────────────────────────────
def load_pending() -> list:
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE) as f:
            return json.load(f)
    return []

def save_pending(listings: list):
    with open(PENDING_FILE, "w") as f:
        json.dump(listings, f, indent=2)

def clear_pending():
    if os.path.exists(PENDING_FILE):
        os.remove(PENDING_FILE)

# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    now_utc = datetime.now(timezone.utc)
    now_pt  = now_utc - timedelta(hours=7)
    print(f"UTC: {now_utc.strftime('%H:%M')} | PT: {now_pt.strftime('%I:%M %p')} | daytime={is_daytime()}")

    all_listings, candidates, seen = fetch_listings()
    new_matches = []

    for item in candidates:
        title = item["title"]
        body  = item.get("body", "")
        meta  = item.get("meta", "")
        full_text = title + " " + body + " " + meta

        if not in_target_neighborhood(full_text):
            print(f"  SKIP (neighborhood) {title[:60]}")
            continue

        beds = extract_bedrooms(full_text)
        if beds not in ("Studio", "1BR", "?"):
            print(f"  SKIP (bedrooms: {beds}) {title[:60]}")
            continue

        item["beds"]       = beds
        item["parking"]    = parse_parking(full_text)
        item["laundry"]    = parse_laundry(full_text)
        item["dishwasher"] = parse_dishwasher(full_text)
        item["sqft"]       = item.get("sqft", parse_sqft(full_text))

        print(f"  MATCH {title[:60]}")
        new_matches.append(item)

    save_seen(seen)

    if is_daytime():
        pending = load_pending()
        if pending:
            print(f"Sending overnight summary ({len(pending)} listings)...")
            send_email(pending, subject_prefix="🌙 Overnight summary:")
            clear_pending()

        if new_matches:
            send_email(new_matches)
        else:
            print("No new matching listings.")
    else:
        if new_matches:
            pending = load_pending()
            pending.extend(new_matches)
            save_pending(pending)
            print(f"Overnight: saved {len(new_matches)} to pending (total: {len(pending)})")
        else:
            print("Overnight: no new matches.")

if __name__ == "__main__":
    run()
