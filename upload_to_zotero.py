#!/usr/bin/env python3
"""
Upload CSL-JSON citations to Zotero (user library of peikoff / 7032524)
"""

import json
import requests
from pathlib import Path
from datetime import datetime

# ── Configuration ──────────────────────────────────────────────────────────
API_KEY   = "ELpNXQ1sXYmgBChEWxfwHt1a"
USER_ID   = 7032524
BASE_URL  = f"https://api.zotero.org/users/{USER_ID}"

COLLECTIONS = {
    "osi":          "TWQ5XHIJ",          # OSI / The Objective Standard
    "prometheus":   "PPRZVWXV",          # Prometheus Foundation
    "new_ideal":    "IFUGNWTQ",
    "aynrand":      "HAQ93X2X",
    "other":        "5WQ8AEX6",
}

HEADERS = {
    "Zotero-API-Key": API_KEY,
    "Content-Type": "application/json",
    "Zotero-API-Version": "3",
}

# ── CSL → Zotero type mapping ─────────────────────────────────────────────
CSL_TO_ZOTERO = {
    "article-magazine": "magazineArticle",
    "article-newspaper": "newspaperArticle",
    "article-journal": "journalArticle",
    "post-weblog": "blogPost",
    "webpage": "webpage",
    "post": "blogPost",
    "book": "book",
    "chapter": "bookSection",
    "paper-conference": "conferencePaper",
    "report": "report",
    "thesis": "thesis",
    "motion_picture": "film",
    "song": "audioRecording",
    "broadcast": "tvBroadcast",
    "interview": "interview",
    "manuscript": "manuscript",
    "map": "map",
    "patent": "patent",
    "personal_communication": "letter",
    "speech": "presentation",
    "bill": "bill",
    "legal_case": "case",
    "legislation": "statute",
    "treaty": "statute",
    "graphic": "artwork",
    "software": "computerProgram",
    "dataset": "dataset",
}

def csl_date_to_zotero(date_obj):
    """Convert CSL date-parts to Zotero date string (YYYY-MM-DD or YYYY)"""
    if not date_obj or "date-parts" not in date_obj:
        return ""
    parts = date_obj["date-parts"][0]
    if len(parts) >= 3:
        return f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}"
    if len(parts) == 2:
        return f"{parts[0]:04d}-{parts[1]:02d}"
    if len(parts) == 1:
        return f"{parts[0]:04d}"
    return ""
def csl_to_zotero_item(csl, collection_key):
    """Convert one CSL-JSON object to a Zotero item dict (fields validated against current schema)"""
    item_type = CSL_TO_ZOTERO.get(csl.get("type", "webpage"), "webpage")

    item = {
        "itemType": item_type,
        "title": csl.get("title", ""),
        "creators": [],
        "abstractNote": csl.get("abstract", ""),
        "url": csl.get("URL", csl.get("url", "")),
        "language": csl.get("language", ""),
        "tags": [],
        "collections": [collection_key],
        "relations": {},
        "extra": "",
    }

    # Creators
    for author in csl.get("author", []) + csl.get("editor", []):
        creator = {"creatorType": "author"}
        if "family" in author or "given" in author:
            creator["firstName"] = author.get("given", "")
            creator["lastName"]  = author.get("family", "")
        else:
            creator["name"] = author.get("literal", author.get("name", ""))
        item["creators"].append(creator)

    # Date
    item["date"] = csl_date_to_zotero(csl.get("issued"))

    # Access date
    access = csl_date_to_zotero(csl.get("accessed"))
    if access:
        item["accessDate"] = access + "T00:00:00Z"

    # Publication / container – only use fields that actually exist
    container = csl.get("container-title", "")
    if item_type in ("magazineArticle", "newspaperArticle", "journalArticle"):
        item["publicationTitle"] = container
    elif item_type == "blogPost":
        item["blogTitle"] = container          # ← correct field
    elif item_type == "webpage":
        item["websiteTitle"] = container
    else:
        # fallback for other types that have publicationTitle
        item["publicationTitle"] = container

    # Keywords → tags
    if "keyword" in csl:
        for kw in str(csl["keyword"]).split(","):
            kw = kw.strip()
            if kw:
                item["tags"].append({"tag": kw})

    # Extra: preserve original id, note, and any non-mappable fields (e.g. section)
    extra_parts = []
    if csl.get("id"):
        extra_parts.append(f"csl-id: {csl['id']}")
    if csl.get("section"):
        extra_parts.append(f"section: {csl['section']}")
    if csl.get("note"):
        extra_parts.append(csl["note"])
    item["extra"] = "\n".join(extra_parts)

    return item

    # Creators
    for author in csl.get("author", []) + csl.get("editor", []):
        creator = {"creatorType": "author"}
        if "family" in author or "given" in author:
            creator["firstName"] = author.get("given", "")
            creator["lastName"]  = author.get("family", "")
        else:
            creator["name"] = author.get("literal", author.get("name", ""))
        item["creators"].append(creator)

    # Date
    item["date"] = csl_date_to_zotero(csl.get("issued"))

    # Access date
    access = csl_date_to_zotero(csl.get("accessed"))
    if access:
        item["accessDate"] = access + "T00:00:00Z"

    # Publication / container
    container = csl.get("container-title", "")
    if item_type in ("magazineArticle", "newspaperArticle", "journalArticle"):
        item["publicationTitle"] = container
    elif item_type == "blogPost":
        item["blogTitle"] = container
        item["websiteTitle"] = container
    elif item_type == "webpage":
        item["websiteTitle"] = container
    else:
        item["publicationTitle"] = container

    # Section
    if "section" in csl:
        item["section"] = csl["section"]

    # Keywords → tags
    if "keyword" in csl:
        for kw in str(csl["keyword"]).split(","):
            kw = kw.strip()
            if kw:
                item["tags"].append({"tag": kw})

    # Note / extra (preserve original id and any note)
    extra_parts = []
    if csl.get("id"):
        extra_parts.append(f"csl-id: {csl['id']}")
    if csl.get("note"):
        extra_parts.append(csl["note"])
    item["extra"] = "\n".join(extra_parts)

    return item

def load_csl(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def upload_batch(items):
    """POST a list of Zotero items (max 50)"""
    if not items:
        return
    r = requests.post(
        f"{BASE_URL}/items",
        headers=HEADERS,
        json=items,
        timeout=60,
    )
    if r.status_code != 200:
        print(f"ERROR {r.status_code}: {r.text}")
        r.raise_for_status()
    result = r.json()
    success = len(result.get("success", {}))
    failed  = len(result.get("failed", {}))
    print(f"  → {success} created, {failed} failed")
    if failed:
        print("  failures:", json.dumps(result["failed"], indent=2))
    return result

def main():
    # ── Load files ─────────────────────────────────────────────────────────
    osi_items = load_csl("osi_zotero.csl.json")
    prom_items = load_csl("prometheus_zotero.csl.json")

    print(f"Loaded {len(osi_items)} OSI items")
    print(f"Loaded {len(prom_items)} Prometheus items")

    # ── Convert ────────────────────────────────────────────────────────────
    zotero_items = []

    for csl in osi_items:
        zotero_items.append(csl_to_zotero_item(csl, COLLECTIONS["osi"]))

    for csl in prom_items:
        zotero_items.append(csl_to_zotero_item(csl, COLLECTIONS["prometheus"]))

    print(f"Converted {len(zotero_items)} total items")

    # ── Upload in batches of 50 ────────────────────────────────────────────
    BATCH = 50
    for i in range(0, len(zotero_items), BATCH):
        batch = zotero_items[i : i + BATCH]
        print(f"Uploading batch {i // BATCH + 1} ({len(batch)} items)…")
        upload_batch(batch)

    print("Done.")

if __name__ == "__main__":
    main()
