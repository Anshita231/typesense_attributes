from flask import Flask, render_template, request, jsonify
import typesense
import os
import re
from dotenv import load_dotenv
from query_attributes import search_by_attributes
from attr_normalize import attr_field_name, get_product_attr_schema
load_dotenv()
_NUMERIC_VALUE_RE = re.compile(r"^-?\d+(\.\d+)?$")

def _is_numeric_value(value):
    return bool(_NUMERIC_VALUE_RE.fullmatch(str(value).strip()))

def is_temporary(product):
    val = product.get("isTemporary", False)
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes")
    return bool(val)

def sort_priority(product):
    has_vrc = len(product.get("vendors", [])) > 0
    has_arc = len(product.get("ARCvendors", [])) > 0

    if has_vrc:
        vendor_rank = 0
    elif has_arc:
        vendor_rank = 1
    else:
        vendor_rank = 2
    unmatched_count = len(product.get("unmatchedAttributes", {}) or {})
    match_type_rank = 0 if product.get("matchType", "exact") == "exact" else 1

    return (1 if is_temporary(product) else 0, unmatched_count, match_type_rank, vendor_rank)

def attach_vmi_tags(output, client):
    if not output:
        return output

    ids = list({str(o["MaterialId"]) for o in output if o.get("MaterialId")})
    if not ids:
        return output

    try:
        vmi_hits = client.collections["vmi_tags"].documents.search({
            "q": "*",
            "filter_by": f"materialId:=[{','.join(ids)}]",
            "per_page": 250
        })

        vmi_map = {}
        for hit in vmi_hits["hits"]:
            doc = hit["document"]
            vmi_map.setdefault(doc["materialId"], []).append(doc["linkedMaterialId"])

        for o in output:
            linked = vmi_map.get(o["MaterialId"])
            if linked:
                o["VMI"] = linked

    except Exception:
        pass

    return output


app = Flask(__name__)

client = typesense.Client({
    'nodes': [{
        'host': os.getenv('TYPESENSE_HOST'),
        'port': '443',
        'protocol': 'https'
    }],
    'api_key': os.getenv('TYPESENSE_API_KEY'),
    'connection_timeout_seconds': 20
})

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/search_attributes")
def search_attributes():
    brand = (request.args.get("brand", "") or "").strip().lower()
    product_name = (request.args.get("productName", "") or "").strip().lower()
    attrs_raw = request.args.get("attrs", "") or ""

    if not brand or not product_name:
        return jsonify({"meta": {"tier": "no_match", "reason": "brand and productName are required"}, "results": []})

    attr_docs, meta = search_by_attributes(client, product_name, brand, attrs_raw)
    print(f"[search_attributes] tier={meta.get('tier')} attrs_used={meta.get('attrs_used')} "
          f"attrs_requested={meta.get('attrs_requested')} dropped={meta.get('dropped')}")

    requested = meta.get("attrs_requested", {}) or {}
    product_schema = get_product_attr_schema().get(product_name)

    def _schema_note(key):
        if product_schema is None:
            return ""
        if key not in product_schema:
            return " (not a recognized attribute for this product type)"
        return "" if product_schema.get(key) == 1 else " (non-mandatory)"

    match_info_by_mid = {}
    for d in attr_docs:
        mid = d.get("materialId")

        matched = {}
        unmatched = {}
        for key, requested_val in requested.items():
            note = _schema_note(key)
            stored_val = d.get(attr_field_name(key))

            if stored_val is None:
                unmatched[key] = f"Not Defined{note}"
            elif str(stored_val).strip().lower() == str(requested_val).strip().lower():
                matched[key] = stored_val
            elif _is_numeric_value(requested_val) or _is_numeric_value(stored_val):
                unmatched[key] = f"{stored_val} (expected: {requested_val}){note}"
            else:
                matched[key] = f"{stored_val} (expected: {requested_val}){note}"

        match_info_by_mid[mid] = {
            "matchType": d.get("_matchType", "exact"),
            "matchedAttributes": matched,
            "unmatchedAttributes": unmatched,
        }

    if not attr_docs:
        return jsonify({"meta": meta, "results": []})

    mids = [d["materialId"] for d in attr_docs if d.get("materialId") is not None]
    if not mids:
        return jsonify({"meta": meta, "results": []})

    found_docs_by_mid = {}
    is_temp_by_mid = {}
    id_list = ",".join(str(m) for m in mids[:250])
    try:
        spec_batch = client.collections["spec"].documents.search({
            "q": "*",
            "query_by": "productName",
            "filter_by": f"materialId:=[{id_list}]",
            "per_page": min(len(mids), 250)
        })
        for h in spec_batch.get("hits", []):
            d = h["document"]
            found_docs_by_mid[d["materialId"]] = d
            is_temp_by_mid[d["materialId"]] = False
    except Exception as e:
        print(f"[search_attributes] spec batch lookup failed: {e}")

    missing_mids = [m for m in mids if m not in found_docs_by_mid]
    if missing_mids:
        missing_id_list = ",".join(str(m) for m in missing_mids[:250])
        try:
            temp_batch = client.collections["temp"].documents.search({
                "q": "*",
                "query_by": "productName",
                "filter_by": f"materialId:=[{missing_id_list}]",
                "per_page": min(len(missing_mids), 250)
            })
            for h in temp_batch.get("hits", []):
                d = h["document"]
                found_docs_by_mid[d["materialId"]] = d
                is_temp_by_mid[d["materialId"]] = True
        except Exception as e:
            print(f"[search_attributes] temp batch lookup failed: {e}")

    output = []
    for mid in mids:
        doc = found_docs_by_mid.get(mid)
        if not doc:
            continue
        is_temp = is_temp_by_mid.get(mid, False)
        info = match_info_by_mid.get(mid, {})
        output.append({
            'productName': doc.get('productName', ''),
            'brandName': doc.get('brandName', ''),
            'categoryName': doc.get('categoryName', ''),
            'MaterialId': doc.get('materialId', ''),
            'productSpecification': doc.get('productSpecification', ''),
            'listPrice': doc.get('listPrice', ''),
            'shortDescription': doc.get('shortDescription', ''),
            'UOM': doc.get('UOM', ''),
            'vendors': [] if is_temp else doc.get('vendors', []),
            'ARCvendors': doc.get('ARCvendors', []),
            'isTemporary': 'true' if is_temp else 'false',
            'matchedAttributes': info.get('matchedAttributes', {}),
            'matchType': info.get('matchType', 'exact'),
            'unmatchedAttributes': info.get('unmatchedAttributes', []),
        })

    output.sort(key=sort_priority)
    return jsonify({"meta": meta, "results": attach_vmi_tags(output, client)})

if __name__ == "__main__":
    app.run(debug=True, threaded=True)