"""Claude helpers for Stash: identify an item from photos, read a receipt, judge recall matches.

Key: ANTHROPIC_API_KEY in the environment, else the first login Keychain item named in
STASH_KEYCHAIN_SERVICES (comma separated, default `stash-anthropic`). Store one with:
security add-generic-password -a "$USER" -s stash-anthropic -w '<key>' -U
The SDK needs Python 3.10+, so it is imported lazily: on the laptop's 3.9 these features report
themselves unavailable and the rest of Stash runs normally.
"""
import base64
import io
import json
import logging
import os
import subprocess

log = logging.getLogger("stash.ai")

MODEL = "claude-opus-5-5"          # photos, receipts, recall judging
SEARCH_MODEL = "claude-haiku-5-5"  # web lookups: same answers at about 5% of Opus's cost (measured 2026-10-11)
KEYCHAIN_SERVICES = tuple(s.strip() for s in os.environ.get("STASH_KEYCHAIN_SERVICES", "stash-anthropic").split(",")
                          if s.strip())
IMAGE_EDGE = 1568          # long edge sent to the model; larger only costs tokens
MAX_PDF_BYTES = 30 * 1024 * 1024

_key_cache = {"checked": False, "key": None}


class AIError(Exception):
    """Shown to the family as-is, so messages stay plain."""


def api_key():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    if not _key_cache["checked"]:
        _key_cache["checked"] = True
        for service in KEYCHAIN_SERVICES:
            try:
                out = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                                     capture_output=True, text=True, timeout=5)
            except Exception:
                continue
            if out.stdout.strip():
                _key_cache["key"] = out.stdout.strip()
                break
    return _key_cache["key"]


def available():
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return bool(api_key())


def _client():
    try:
        import anthropic
    except ImportError:
        raise AIError("The Claude features need the anthropic package on the server.")
    key = api_key()
    if not key:
        raise AIError("Claude is not set up yet: there is no API key on the server.")
    return anthropic.Anthropic(api_key=key, timeout=90.0, max_retries=1)


# Basic search, few uses: one lookup cost $0.012 on Haiku vs $0.22 with Opus and the filtering search.
def web_search(uses=2):
    return {"type": "web_search_20250305", "name": "web_search", "max_uses": uses,
            "user_location": {"type": "approximate", "country": "US"}}


def _ask(system, content, schema, max_tokens=4000, tools=None, timeout=90.0, model=MODEL, effort="medium"):
    """One structured-output call (web search allowed when tools are given). Returns the parsed JSON."""
    import anthropic
    client = _client().with_options(timeout=timeout)
    messages = [{"role": "user", "content": content}]
    extra = {"tools": tools} if tools else {}
    if model == MODEL:  # Haiku has no server-side refusal fallback
        extra.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    try:
        for _ in range(4):  # a long web search can pause the turn; resume it
            resp = client.beta.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
                **extra)
            if resp.stop_reason != "pause_turn":
                break
            messages = messages + [{"role": "assistant", "content": resp.content}]
    except anthropic.RateLimitError:
        raise AIError("Claude is busy right now. Try again in a minute.")
    except anthropic.APITimeoutError:  # subclass of APIConnectionError, so it goes first
        raise AIError("Claude took too long. Try again.")
    except anthropic.APIConnectionError:
        raise AIError("Could not reach Claude. Check the server's internet connection.")
    except anthropic.APIStatusError as exc:
        log.warning("claude api error %s: %s", exc.status_code, exc)
        raise AIError("Claude returned an error (%s). Try again, or fill the fields in by hand." % exc.status_code)
    if resp.stop_reason == "refusal":
        raise AIError("Claude declined to read this one. Fill the fields in by hand.")
    if resp.stop_reason == "max_tokens":
        raise AIError("Claude's answer was cut off. Try again with fewer photos.")
    texts = [b.text for b in resp.content if b.type == "text"]
    try:
        return json.loads(texts[-1] if texts else "")
    except ValueError:
        raise AIError("Claude's answer could not be read. Try again.")


def facts_text(item):
    keys = (("name", "Name"), ("manufacturer", "Brand"), ("model", "Model"), ("description", "Description"),
            ("purchase_date", "Bought"), ("purchase_price", "Paid"), ("notes", "Notes"))
    return "\n".join("%s: %s" % (label, item[k]) for k, label in keys if item.get(k) not in (None, ""))


def image_block(data):
    """Downscale any image Pillow can open (HEIC too, when pillow-heif is installed) to a JPEG block."""
    from PIL import Image, ImageOps
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((IMAGE_EDGE, IMAGE_EDGE))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
    except Exception:
        raise AIError("One of those files is not a photo Stash can read.")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.b64encode(buf.getvalue()).decode("ascii")}}


def pdf_block(data):
    if len(data) > MAX_PDF_BYTES:
        raise AIError("That PDF is too large to read.")
    return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                           "data": base64.b64encode(data).decode("ascii")}}


# ---------- identify an item from photos ----------

IDENTIFY_SYSTEM = (
    "You catalog things in a family's home inventory from photos. You may get one photo of the item and "
    "others of its label, rating plate, box, or tags. Read printed text carefully. Report only what the "
    "photos show or what is plainly recognizable; use an empty string when you cannot tell. Never invent "
    "a serial or model number. The name is short and plain, the way a person would say it "
    "(\"Cordless drill\", \"KitchenAid stand mixer\"). The description is one sentence on what it is and "
    "anything notable such as color or size. No em dashes."
)


def identify(images, label_names):
    """images: list of raw bytes. Returns dict with name, manufacturer, model, serial, description,
    labels (subset of label_names) and confidence."""
    if not images:
        raise AIError("Pick at least one photo first.")
    props = {
        "name": {"type": "string", "description": "Short everyday name"},
        "manufacturer": {"type": "string", "description": "Brand or maker, empty if unknown"},
        "model": {"type": "string", "description": "Model name or number as printed, empty if not visible"},
        "serial": {"type": "string", "description": "Serial number exactly as printed, empty if not visible"},
        "description": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    }
    required = list(props)
    if label_names:
        props["labels"] = {"type": "array", "items": {"type": "string", "enum": list(label_names)},
                           "description": "Existing labels that clearly fit; often none"}
        required.append("labels")
    schema = {"type": "object", "properties": props, "required": required, "additionalProperties": False}
    content = [image_block(b) for b in images[:6]]
    hint = "Identify this item for the inventory."
    if label_names:
        hint += " Labels in use: " + ", ".join(label_names) + "."
    content.append({"type": "text", "text": hint})
    out = _ask(IDENTIFY_SYSTEM, content, schema)
    out.setdefault("labels", [])
    return out


# ---------- read a receipt ----------

RECEIPT_SYSTEM = (
    "You read store receipts, invoices, and warranty cards for a home inventory. Extract facts for one "
    "item. If the receipt lists several products, use the line that best matches the item name you are "
    "given and quote that line. Use the line price for that product (per unit, before tax), not the "
    "receipt total, unless the receipt has only that one product. Dates are YYYY-MM-DD. Fill "
    "warranty_until only when the document states a warranty or protection plan length or end date; "
    "compute it from the purchase date when it gives a length. Use empty strings for anything not on the "
    "document. Never guess a serial or model number. No em dashes."
)

RECEIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "store": {"type": "string", "description": "Where it was bought"},
        "purchase_date": {"type": "string", "description": "YYYY-MM-DD or empty"},
        "price": {"type": "string", "description": "Per-unit price as digits, like 129.99, or empty"},
        "quantity": {"type": "string", "description": "Units of this product bought, or empty"},
        "manufacturer": {"type": "string"},
        "model": {"type": "string"},
        "serial": {"type": "string"},
        "warranty_until": {"type": "string", "description": "YYYY-MM-DD or empty"},
        "warranty_notes": {"type": "string", "description": "Warranty or protection plan wording, short"},
        "line": {"type": "string", "description": "The receipt line used for this item"},
    },
    "required": ["store", "purchase_date", "price", "quantity", "manufacturer", "model", "serial",
                 "warranty_until", "warranty_notes", "line"],
    "additionalProperties": False,
}


def read_receipt(data, is_pdf, item_name):
    block = pdf_block(data) if is_pdf else image_block(data)
    content = [block, {"type": "text", "text": "The item in the inventory is called: %s" % item_name}]
    return _ask(RECEIPT_SYSTEM, content, RECEIPT_SCHEMA)


# ---------- recall matching ----------

RECALL_SYSTEM = (
    "You check whether product recalls from the US Consumer Product Safety Commission cover a specific "
    "item a family owns. You get the item's facts and a short list of candidate recalls that share its "
    "brand or model. A recall matches only when the brand and the product type agree and, if the recall "
    "lists model numbers and the item has one, the model is among them. Say 'possible' when the brand "
    "and product type agree but the item lacks the model, date, or other detail needed to be sure. "
    "Everything else is 'no'. Keep each reason to one plain sentence. No em dashes."
)

RECALL_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "recall_number": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["match", "possible", "no"]},
                    "reason": {"type": "string"},
                },
                "required": ["recall_number", "verdict", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}


def judge_recalls(item, candidates):
    """item: dict of facts. candidates: list of dicts with recall_number, title, products, date.
    Returns {recall_number: (verdict, reason)}."""
    facts = "\n".join("%s: %s" % (k, v) for k, v in item.items() if v)
    lines = []
    for c in candidates:
        lines.append("Recall %s (%s): %s\nProducts: %s" % (c["recall_number"], c["recall_date"], c["title"],
                                                          c["products"][:1500]))
    content = [{"type": "text", "text": "Item:\n%s\n\nCandidate recalls:\n\n%s" % (facts, "\n\n".join(lines))}]
    out = _ask(RECALL_SYSTEM, content, RECALL_SCHEMA)
    wanted = {c["recall_number"] for c in candidates}
    return {v["recall_number"]: (v["verdict"], v["reason"]) for v in out.get("verdicts", [])
            if v.get("recall_number") in wanted}


# ---------- room walkthrough ----------

WALK_SYSTEM = (
    "You help a family inventory a room from a batch of photos. List each distinct thing worth tracking: "
    "electronics, appliances, tools, furniture, sports gear, instruments, art, collectibles, anything of "
    "value or that someone might search for later. Skip trash, food, loose papers, and fixtures built "
    "into the house. When the same thing appears in several photos, list it once. Group identical small "
    "things into one entry with a quantity. For each entry give the number of the photo that shows it "
    "best. Read brand and model only when printed or unmistakable; otherwise leave them empty. Names are "
    "short and plain. No em dashes."
)


def walkthrough(images, place, label_names):
    item_props = {
        "name": {"type": "string"},
        "manufacturer": {"type": "string"},
        "model": {"type": "string"},
        "description": {"type": "string", "description": "One sentence"},
        "quantity": {"type": "integer"},
        "photo": {"type": "integer", "description": "Number of the photo that shows it best, starting at 1"},
    }
    if label_names:
        item_props["labels"] = {"type": "array", "items": {"type": "string", "enum": list(label_names)}}
    schema = {"type": "object", "properties": {"items": {"type": "array", "items": {
        "type": "object", "properties": item_props, "required": list(item_props), "additionalProperties": False}}},
        "required": ["items"], "additionalProperties": False}
    content = []
    for n, data in enumerate(images, 1):
        content.append({"type": "text", "text": "Photo %d" % n})
        content.append(image_block(data))
    content.append({"type": "text", "text": "These photos are of: %s. List what is here." % (place or "a room")})
    out = _ask(WALK_SYSTEM, content, schema, max_tokens=16000, timeout=240.0)
    return out.get("items", [])


# ---------- replacement cost ----------

REPLACE_SYSTEM = (
    "You price household items for an insurance inventory. Find what it would cost today to buy the same "
    "item new from a mainstream US retailer or the maker. If it is no longer sold, price the closest "
    "current equivalent (same type, similar size and quality) and say so. Use the regular price, not a "
    "short sale, when you can tell them apart. Search the web; never guess a price from memory. If you "
    "cannot find a credible price, set found to false. Notes are one or two plain sentences. No em dashes."
)

REPLACE_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "price": {"type": "number", "description": "US dollars for one unit, 0 if not found"},
        "match": {"type": "string", "enum": ["exact", "equivalent", "none"]},
        "product": {"type": "string", "description": "The product priced"},
        "retailer": {"type": "string"},
        "url": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": ["found", "price", "match", "product", "retailer", "url", "note"],
    "additionalProperties": False,
}


def replacement_cost(item):
    content = [{"type": "text", "text": "Item:\n" + facts_text(item)}]
    return _ask(REPLACE_SYSTEM, content, REPLACE_SCHEMA, max_tokens=8000, tools=[web_search(2)], timeout=120.0,
                model=SEARCH_MODEL, effort="low")


# ---------- manual finder ----------

MANUAL_SYSTEM = (
    "You find the owner's manual or user guide for a product. Prefer the maker's own site and a direct "
    "PDF link. A manual for the same model family is fine when the exact model has none; say so in the "
    "note. If you only find a web page that hosts the manual, give that as page_url and leave pdf_url "
    "empty. Never invent a URL: every link must come from your search results. No em dashes."
)

MANUAL_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "pdf_url": {"type": "string", "description": "Direct link to a PDF, or empty"},
        "page_url": {"type": "string", "description": "Page that hosts the manual, or empty"},
        "title": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": ["found", "pdf_url", "page_url", "title", "note"],
    "additionalProperties": False,
}


def find_manual(item):
    content = [{"type": "text", "text": "Find the manual for:\n" + facts_text(item)}]
    return _ask(MANUAL_SYSTEM, content, MANUAL_SCHEMA, max_tokens=8000, tools=[web_search(2)], timeout=120.0,
                model=SEARCH_MODEL, effort="low")


# ---------- eBay prep ----------

EBAY_SYSTEM = (
    "You help a family get ready to sell a used item on eBay. Search eBay for the same or very similar "
    "items, favoring sold listings over active ones because sold prices show what buyers actually pay. "
    "Give up to eight comparable listings with their real prices and links from your search results; "
    "never invent a listing, price, or URL. From those, suggest a list price and a lower quick-sale price "
    "for a used one in good working order. Write an eBay title of at most 80 characters that buyers would "
    "search for (brand, model, key specs), the best eBay category, item specifics, a short honest "
    "description in plain sentences that leaves a blank for the seller to note condition and what is "
    "included, and a shipping note with an estimated packed weight. No em dashes, no hype words."
)

EBAY_SCHEMA = {
    "type": "object",
    "properties": {
        "suggested_price": {"type": "number"},
        "quick_sale_price": {"type": "number"},
        "low": {"type": "number", "description": "Lowest comparable sold price"},
        "high": {"type": "number", "description": "Highest comparable sold price"},
        "comps": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "price": {"type": "number"},
            "status": {"type": "string", "enum": ["sold", "active"]},
            "condition": {"type": "string"}, "url": {"type": "string"}},
            "required": ["title", "price", "status", "condition", "url"], "additionalProperties": False}},
        "title": {"type": "string"},
        "category": {"type": "string"},
        "specifics": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "value": {"type": "string"}},
            "required": ["name", "value"], "additionalProperties": False}},
        "description": {"type": "string"},
        "shipping": {"type": "string"},
        "note": {"type": "string", "description": "Anything the seller should know, like how much the price depends on condition"},
    },
    "required": ["suggested_price", "quick_sale_price", "low", "high", "comps", "title", "category",
                 "specifics", "description", "shipping", "note"],
    "additionalProperties": False,
}


def ebay_research(item, photo=None):
    content = []
    if photo:
        content.append(image_block(photo))
    content.append({"type": "text", "text": "Item to sell:\n" + facts_text(item)})
    return _ask(EBAY_SYSTEM, content, EBAY_SCHEMA, max_tokens=16000, tools=[web_search(3)], timeout=180.0,
                model=SEARCH_MODEL, effort="low")


# ---------- stock photos ----------

STOCK_SYSTEM = (
    "You find a product photo for an item in a family's home inventory. Search for web pages that show "
    "this exact product: the maker's own product page first, then large retailers. Skip Amazon, eBay, "
    "Pinterest, forums, and image aggregators. If the item is generic (no brand or model), give pages "
    "for a typical example. Every URL must come from your search results. Also give a short generic "
    "phrase for the kind of thing it is, like 'cordless drill' or 'floor lamp', for a fallback photo "
    "search. No em dashes."
)

STOCK_SCHEMA = {
    "type": "object",
    "properties": {
        "pages": {"type": "array", "items": {"type": "object", "properties": {
            "url": {"type": "string"},
            "match": {"type": "string", "enum": ["exact", "similar"]}},
            "required": ["url", "match"], "additionalProperties": False}},
        "generic": {"type": "string"},
    },
    "required": ["pages", "generic"],
    "additionalProperties": False,
}


def stock_photo_pages(item):
    content = [{"type": "text", "text": "Item:\n" + facts_text(item)}]
    return _ask(STOCK_SYSTEM, content, STOCK_SCHEMA, max_tokens=4000, tools=[web_search(2)], timeout=90.0,
                model=SEARCH_MODEL, effort="low")


PICK_SCHEMA = {"type": "object", "properties": {"choice": {"type": "integer"}},
               "required": ["choice"], "additionalProperties": False}


def pick_commons(generic, titles):
    """Index of the Wikimedia file title that best shows the thing on its own, or -1."""
    listing = "\n".join("%d. %s" % (i, t) for i, t in enumerate(titles))
    content = [{"type": "text", "text": "Which file most likely shows a typical %s by itself, as a clear product "
                "photo? Avoid people, diagrams, and close-ups of parts. Answer -1 if none fit.\n\n%s"
                % (generic, listing)}]
    out = _ask("You choose photos. Answer with the number only.", content, PICK_SCHEMA, max_tokens=500,
               timeout=30.0, model=SEARCH_MODEL, effort="low")
    c = out.get("choice", -1)
    return c if isinstance(c, int) and 0 <= c < len(titles) else -1
