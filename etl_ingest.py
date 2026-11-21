import ast
import json
import os
import sys

import pandas as pd
import typesense
from dotenv import load_dotenv

from attr_normalize import normalize_attr_value, slugify_attr, attr_field_name

load_dotenv()

_client = None

def get_client():
    global _client
    if _client is None:
        _client = typesense.Client({
            'nodes': [{
                'host': os.getenv('TYPESENSE_HOST'),
                'port': '443',
                'protocol': 'https'
            }],
            'api_key': os.getenv('TYPESENSE_API_KEY'),
            'connection_timeout_seconds': 20
        })
    return _client


COLLECTION_NAME = "AttributesJSON"
BATCH_SIZE = 50

def load_source(xlsx_path):
    df = pd.read_excel(xlsx_path)
    for _, row in df.iterrows():
        yield row.to_dict()

def parse_spec_json(raw):
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        items = ast.literal_eval(raw)
    except (ValueError, SyntaxError) as e:
        print(f"[parse] literal_eval failed: {e}")
        return []

    parsed = []
    for item in items:
        try:
            parsed.append(json.loads(item))
        except (ValueError, TypeError) as e:
            print(f"[parse] json.loads failed on item {item!r}: {e}")
    return parsed

def build_document(row):
    material_id = row.get("materialId")
    product_name = row.get("productName")
    brand_name = row.get("brandName")
    category_id = row.get("categoryId")

    if material_id is None or pd.isna(material_id):
        return None
    material_id = int(material_id)

    doc = {
        "id": str(material_id),
        "materialId": material_id,
        "materialIdStr": str(material_id),
        "productName": str(product_name).strip().lower() if pd.notna(product_name) else "",
        "brandName": str(brand_name).strip().lower() if pd.notna(brand_name) else "",
    }
    if pd.notna(category_id):
        try:
            doc["categoryId"] = int(category_id)
        except (ValueError, TypeError):
            pass

    for item in parse_spec_json(row.get("productSpecificationJSON")):
        if not isinstance(item, dict):
            continue
        attr_type = item.get("type")
        if not attr_type:
            continue
        field = slugify_attr(attr_type)
        if not field:
            continue
        if field in ("id", "materialid", "materialidstr", "productname", "brandname", "categoryid"):
            continue
        normalized = normalize_attr_value(item.get("value"), item.get("unit"))
        if normalized is None:
            continue
        doc[attr_field_name(field)] = normalized

    return doc

def run(xlsx_path):
    batch = []
    total = 0
    errors = 0

    for row in load_source(xlsx_path):
        doc = build_document(row)
        if doc is None:
            continue
        batch.append(doc)

        if len(batch) >= BATCH_SIZE:
            total += len(batch)
            _upsert_batch(batch)
            batch = []
            print(f"...{total} materials upserted")

    if batch:
        total += len(batch)
        _upsert_batch(batch)

    print(f"Done. {total} materials processed.")

def _upsert_batch(batch, max_retries=3):
    import time
    for attempt in range(1, max_retries + 1):
        try:
            results = get_client().collections[COLLECTION_NAME].documents.import_(
                batch, {"action": "upsert"}
            )
            for r in results:
                if not r.get("success", False):
                    print(f"[upsert error] {r}")
            return
        except typesense.exceptions.ObjectUnprocessable as e:
            if attempt == max_retries:
                print(f"[upsert] giving up on batch of {len(batch)} after {max_retries} attempts: {e}")
                raise
            wait = 5 * attempt
            print(f"[upsert] server busy/OOM (attempt {attempt}/{max_retries}), "
                  f"retrying batch of {len(batch)} in {wait}s: {e}")
            time.sleep(wait)

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python etl_ingest.py /path/to/productSpecificationsAll1.xlsx")
        sys.exit(1)
    run(sys.argv[1])