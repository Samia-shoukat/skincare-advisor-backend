"""
Scrape skincare products from five Pakistani brand stores into Supabase.

    python scripts/scrape_products.py                 # scrape, save JSON, write nothing
    python scripts/scrape_products.py --apply         # scrape and upsert into Supabase
    python scripts/scrape_products.py --brand Trego   # one brand only

Brands and the stores they sell from:

    Organic Lab      https://organiclab.store
    Nirvana Botanic  https://nirvanabotanics.com
    Jenpharm         https://jenpharm.com
    Skin Pantry      https://thehairpantry.com   (shared with Hair Pantry)
    Trego            https://trego.pk

## How it gets the data

All five are Shopify stores, and every Shopify store publishes its catalogue at
`/products.json` -- name, price, images and the description, as structured
JSON. That is far sturdier than scraping the HTML grid, which breaks whenever a
store changes its theme. No Selenium is needed: nothing here is rendered by
JavaScript.

Ingredients are the exception. Each store puts them somewhere different -- in
the description, or in an "Ingredients" tab on the product page -- so they are
found with BeautifulSoup: look for an "Ingredients" heading, take the text
after it, stop at the next section ("Benefits", "How to use", ...). The product
page is only fetched when the description has no ingredient list.

Some stores show ingredients only as an image (much of Nirvana Botanic, parts of
Jenpharm). Those rows get `ingredients = null` rather than a guess.

## Where it writes

NOT the `products` table. That table is the backend's curated catalogue: its
columns differ (slug id, price_tier, JSON ingredient keys), and the routine
engine recommends from it, so an unreviewed scraped product landing there would
be recommended to users. Scraped rows go to TABLE_NAME below -- create it with
the SQL in the instructions first.

`skin_type` is a keyword guess from the store's own text and tags ("for oily
skin", "all skin types"). Stores rarely state it, so many rows will be null.

Dry run by default, because `--apply` writes to the live Supabase project.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

TABLE_NAME = "scraped_products"

STORES = {
    "Organic Lab": "https://organiclab.store",
    "Nirvana Botanic": "https://nirvanabotanics.com",
    "Jenpharm": "https://jenpharm.com",
    "Skin Pantry": "https://thehairpantry.com",
    "Trego": "https://trego.pk",
}

USER_AGENT = "SkinsightProductScraper/1.0 (+catalogue research; contact via app support)"

# Seconds between product-page requests to the same store. Be polite: these are
# small businesses' storefronts, not an API.
REQUEST_DELAY = 1.0

# Not skin care. Matched against title, product type and tags. Skin Pantry
# shares a store with Hair Pantry, Jenpharm and Trego sell hair products too,
# and Organic Lab sells wax strips and tooth powder.
EXCLUDE = re.compile(
    r"\b(hair|shampoo|conditioner|scalp|lice|beard|anagrow|progain|rosemary oil|"
    r"wax|waxing|tooth|teeth|toothpaste|nail|perfume|perfumes|fragrance mist|comb|"
    r"frizz|dandruff|rosemarymint|case|holder|sticker)\b",
    re.I,
)

# Checked against the opening of the description. Only unambiguous words: in
# running text "set" and "case" are usually just English ("set to offer").
DESCRIPTION_EXCLUDE = re.compile(r"\b(hair|dandruff|scalp|perfumes?|bundle|kit)\b", re.I)

# Offers listed as products: "Buy 5 get 1 free", "Get 25% Off On 4 Products".
PROMO = re.compile(
    r"\bbuy \d|\bget \d+\s*%|\d+\s*% off|\bpack of \d|\bget free\b|\bcomplete\b.*\bsolution\b",
    re.I,
)

# Bundles and samples repeat products already listed on their own.
BUNDLE = re.compile(
    r"\b(bundle|bundles|kit|trio|duo|combo|deal|set|challenge|sample|samples|tester)\b", re.I
)

# Text that starts an ingredient list: a heading ("Key Ingredients",
# "INGREDIENTS:"), a toggle ("See Full Ingredients", Nirvana Botanic's), or a
# line carrying the list itself ("Ingredients: Aqua, ...").
INGREDIENT_HEADING = re.compile(
    r"^\s*((see|view|show|read)\s+)?(the\s+)?"
    r"(key\s+|active\s+|full\s+|complete\s+|all\s+|main\s+|hero\s+)?(ingredients?|inci)\b",
    re.I,
)


def _starts_ingredients(text: str | None) -> bool:
    if not text or not INGREDIENT_HEADING.match(text):
        return False
    text = text.strip()
    return len(text) <= 60 or ":" in text[:40]

# Headings that end one: the next section of a product page or description.
SECTION_HEADING = re.compile(
    r"^\s*(benefits?|key benefits|how to (use|apply)|directions?|usage|"
    r"suitable for|skin type|faqs?|description|reviews?|results?|why |what it|"
    r"who (is it|should)|caution|warning|precautions?|storage|size|quantity|"
    r"shipping|delivery|you may also|related|recently viewed|"
    # Small print and review widgets that follow a list with no heading of
    # their own -- Nirvana Botanic's run straight into "Manufactured by" and
    # then the reviews block.
    r"manufactured|made in|disclaimer|note\b|shelf life|expiry|mrp|net (wt|weight)|"
    r"customer reviews|write a review|★|☆)",
    re.I,
)

# Elements whose text is an ingredient's name rather than its description.
LABEL_TAGS = {"h2", "h3", "h4", "h5", "h6", "strong", "b", "dt", "th"}

SKIN_TYPES = [
    ("All", re.compile(r"\ball skin types?\b|\bevery skin type\b|\bsuitable for all\b", re.I)),
    ("Oily", re.compile(r"\boily\b", re.I)),
    ("Dry", re.compile(r"\bdry skin\b|\bvery dry\b|\bdry and\b|\bfor dry\b", re.I)),
    ("Combination", re.compile(r"\bcombination\b", re.I)),
    ("Sensitive", re.compile(r"\bsensitive\b", re.I)),
    ("Acne-prone", re.compile(r"\bacne[- ]prone\b", re.I)),
    ("Normal", re.compile(r"\bnormal skin\b", re.I)),
]

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def get(url: str, *, as_json: bool = False, retries: int = 5):
    """
    GET with retries.

    Shopify answers 429 when one address sends too much in a short time, across
    all its stores at once -- so a second run straight after the first can be
    refused everywhere. The limit clears in a minute or two; this waits it out,
    honouring Retry-After when the store sends one.
    """
    for attempt in range(1, retries + 1):
        try:
            # Never send the store's cookies back. Shopify sets tracking cookies
            # on the first response, and its bot protection answers 429 to any
            # non-browser client that returns them -- on every request, however
            # long you wait. Without them, the same request gets a 200.
            session.cookies.clear()
            response = session.get(url, timeout=30)
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = response.headers.get("Retry-After", "")
                error = requests.HTTPError(f"HTTP {response.status_code}")
                error.retry_after = float(retry_after) if retry_after.isdigit() else None
                raise error
            response.raise_for_status()
            return response.json() if as_json else response.text
        except (requests.RequestException, ValueError) as error:
            if attempt == retries:
                raise
            wait = getattr(error, "retry_after", None) or 15 * attempt
            print(f"    {error}; waiting {wait:.0f}s (retry {attempt}/{retries - 1})")
            time.sleep(wait)


def robots_for(base: str) -> robotparser.RobotFileParser:
    parser = robotparser.RobotFileParser()
    try:
        parser.parse(get(f"{base}/robots.txt").splitlines())
    except requests.RequestException:
        parser.parse([])  # no robots.txt: everything allowed
    return parser


def fetch_catalogue(base: str) -> list[dict]:
    """Every product in a Shopify store, 250 per page."""
    products, page = [], 1
    while True:
        batch = get(f"{base}/products.json?limit=250&page={page}", as_json=True)["products"]
        if not batch:
            return products
        products.extend(batch)
        page += 1
        time.sleep(REQUEST_DELAY)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("​", "")).strip(" |:-–")


def tags_of(product: dict) -> list[str]:
    tags = product.get("tags") or []
    return tags if isinstance(tags, list) else [t.strip() for t in tags.split(",")]


def is_skincare(product: dict) -> bool:
    # The opening of the description as well as the title: several stores give
    # a hair oil or a bundle a name like "Midnight Crush" or "Sun Breeze" and
    # only say what it is in the first sentence.
    opening = BeautifulSoup(product.get("body_html") or "", "html.parser").get_text(" ")[:160]
    labels = " ".join([product["title"], product.get("product_type") or "", *tags_of(product)])
    if EXCLUDE.search(labels) or BUNDLE.search(labels) or PROMO.search(product["title"]):
        return False
    if DESCRIPTION_EXCLUDE.search(opening):
        return False
    if product["title"].strip().lower().startswith("free"):
        return False
    return any(float(v.get("price") or 0) > 0 for v in product.get("variants", []))


def format_price(product: dict) -> str | None:
    """Lowest variant price, as the stores show it: "Rs. 1,450"."""
    prices = [float(v["price"]) for v in product.get("variants", []) if v.get("price")]
    prices = [p for p in prices if p > 0]
    if not prices:
        return None
    low = min(prices)
    return f"Rs. {low:,.0f}" if low.is_integer() else f"Rs. {low:,.2f}"


def first_image(product: dict) -> str | None:
    images = product.get("images") or []
    if not images:
        return None
    src = images[0].get("src", "")
    return "https:" + src if src.startswith("//") else src or None


def extract_ingredients(html: str) -> str | None:
    """
    Text under an "Ingredients" heading, or None.

    Every heading that matches is tried and the one followed by the most text
    wins. That skips tab strips, where "Ingredients" is a label followed
    straight away by "Benefits" and the real list sits further down the page.
    """
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for junk in soup(["script", "style", "svg", "noscript", "header", "footer", "nav", "form"]):
        junk.decompose()

    best, best_score = None, 0.0
    for heading in soup.find_all(string=_starts_ingredients):
        candidate = _ingredients_after(heading)
        score = _list_score(candidate) if candidate else 0.0
        if score > best_score:
            best, best_score = candidate, score
    return best


def _section_of(heading):
    """
    The element holding this heading's section: its nearest ancestor with more
    in it than the heading itself. Reading stops at that element's edge, so a
    "Key ingredients" block cannot run on into the reviews widget below it.
    """
    own = len(clean(heading))
    for ancestor in heading.parents:
        if ancestor.name == "[document]":
            return None
        if len(clean(ancestor.get_text(" "))) > own + 20:
            return ancestor
    return None


def _ingredients_after(heading) -> str | None:
    scope = _section_of(heading)

    # Each part is (text, is_label). A label is an ingredient name set apart
    # from its description: "Niacinamide:" or a name in its own <h3>/<strong>.
    # The list may share the heading's line: "Ingredients: Aqua, Glycerin, ..."
    inline = clean(heading.split(":", 1)[1]) if ":" in heading[:40] else ""
    parts: list[tuple[str, bool]] = [(inline, False)] if len(inline) > 3 else []

    for node in heading.find_all_next(string=True):
        if scope is not None and scope not in node.parents:
            break
        text = clean(node)
        if not text:
            continue
        if SECTION_HEADING.match(text) or _starts_ingredients(text):
            break
        is_label = len(text) < 60 and (
            node.strip().endswith(":") or node.parent.name in LABEL_TAGS
        )
        parts.append((text, is_label))
        if sum(len(p) for p, _ in parts) > 1500:
            break

    if sum(len(p) for p, _ in parts) < 8:
        return None

    # "Key ingredients" cards read "Niacinamide: | Evens tone and ...". When the
    # section is that shape -- several labels, some sentences -- keep the names
    # and drop the marketing copy.
    labels = [p for p, is_label in parts if is_label]
    has_copy = any(not is_label and len(p.split()) > 3 for p, is_label in parts)
    if len(labels) >= 2 and has_copy:
        return ", ".join(labels)
    joined = ", ".join(p for p, _ in parts)
    return re.sub(r"\s*,\s*,+\s*|,\s*\.", ", ", joined).strip(" ,")[:2000]


def _list_score(text: str) -> float:
    """
    How much `text` looks like an ingredient list rather than marketing copy.

    A real list is many short comma-separated names ("Aqua, Glycerin,
    Niacinamide, ..."); copy is long phrases ("brightens skin tone and reduces
    oxidative stress"). Picking by length alone chose the copy whenever a page
    had both.
    """
    items = [item.strip() for item in text.split(",") if item.strip()]
    names = sum(1 for item in items if len(item.split()) <= 5)
    prose = len(items) - names
    return names - 3 * prose + len(text) / 1000


def guess_skin_type(product: dict) -> str | None:
    text = " ".join([
        product["title"],
        product.get("product_type") or "",
        " ".join(tags_of(product)),
        BeautifulSoup(product.get("body_html") or "", "html.parser").get_text(" "),
    ])
    found = [label for label, pattern in SKIN_TYPES if pattern.search(text)]
    if "All" in found:
        return "All"
    return ", ".join(found) or None


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------


def scrape_brand(brand: str, base: str) -> list[dict]:
    print(f"\n{brand} ({base})")
    robots = robots_for(base)
    catalogue = fetch_catalogue(base)
    products = [p for p in catalogue if is_skincare(p)]
    print(f"  {len(catalogue)} products in store, {len(products)} are skin care")

    rows = []
    for index, product in enumerate(products, 1):
        url = f"{base}/products/{product['handle']}"
        ingredients = extract_ingredients(product.get("body_html") or "")

        if not ingredients and robots.can_fetch(USER_AGENT, url):
            time.sleep(REQUEST_DELAY)
            try:
                ingredients = extract_ingredients(get(url))
            except requests.RequestException as error:
                print(f"    could not open {url}: {error}")

        rows.append({
            "name": clean(product["title"]),
            "brand": brand,
            "price": format_price(product),
            "image_url": first_image(product),
            "skin_type": guess_skin_type(product),
            "ingredients": ingredients,
        })
        print(f"  [{index}/{len(products)}] {rows[-1]['name']}"
              f"{'' if ingredients else '  (no ingredient text found)'}")
    return rows


def dedupe(rows: list[dict]) -> list[dict]:
    """One row per (brand, name). Postgres refuses an upsert batch that hits the same key twice."""
    seen: dict[tuple[str, str], dict] = {}
    for row in rows:
        seen.setdefault((row["brand"], row["name"].lower()), row)
    return list(seen.values())


def upload(rows: list[dict]) -> None:
    from supabase import create_client  # only needed with --apply

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env")

    client = create_client(url, key)
    for start in range(0, len(rows), 100):
        chunk = rows[start:start + 100]
        # Upsert on (brand, name): re-running refreshes prices and images
        # instead of inserting duplicates. Needs the unique constraint from
        # the setup SQL.
        client.table(TABLE_NAME).upsert(chunk, on_conflict="brand,name").execute()
        print(f"  upserted {start + len(chunk)}/{len(rows)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help=f"write to Supabase table {TABLE_NAME}")
    parser.add_argument("--brand", choices=list(STORES), help="scrape one brand only")
    parser.add_argument("--out", default="scraped_products.json", help="where to save the scraped rows")
    args = parser.parse_args()

    # The backend's .env, wherever the script is run from.
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # product names contain non-ASCII

    stores = {args.brand: STORES[args.brand]} if args.brand else STORES
    rows: list[dict] = []
    for brand, base in stores.items():
        try:
            rows.extend(scrape_brand(brand, base))
        except requests.RequestException as error:
            print(f"  skipped {brand}: {error}")

    rows = dedupe(rows)
    with open(args.out, "w", encoding="utf-8") as file:
        json.dump(rows, file, ensure_ascii=False, indent=2)

    with_ingredients = sum(1 for r in rows if r["ingredients"])
    with_skin_type = sum(1 for r in rows if r["skin_type"])
    print(f"\n{len(rows)} products scraped -> {args.out}")
    print(f"  ingredients found for {with_ingredients}, skin type guessed for {with_skin_type}")

    if args.apply:
        print(f"\nWriting to Supabase table '{TABLE_NAME}'...")
        upload(rows)
        print("done")
    else:
        print("\nDry run: nothing written. Check the JSON, then re-run with --apply.")


if __name__ == "__main__":
    main()
