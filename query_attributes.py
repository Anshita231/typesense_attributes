MIN_RESULTS = 5 
CONDUCTOR_MATERIAL_KEY = "conductor_material"
CONTAINS_FALLBACK_KEYS = ["conductor_material", "armouring"]
COLLECTION_NAME = "AttributesJSON"
RESERVED_KEYS = {"productname", "brandname"}

import re
from attr_normalize import normalize_attr_value, slugify_attr, get_product_attr_schema, attr_field_name

def parse_attr_string(raw):
    result = {}
    if not raw:
        return result
    for part in raw.split("|"):
        part = part.strip()
        if not part or ":" not in part:
            continue
        key, _, value = part.partition(":")
        slug = slugify_attr(key)
        if not slug:
            continue
        normalized = normalize_attr_value(value)
        if normalized is None:
            continue
        result[slug] = normalized
    return result

_SAFE_UNQUOTED_RE = re.compile(r"^[A-Za-z0-9_\- ]+$")
_NUMERIC_VALUE_RE = re.compile(r"^-?\d+(\.\d+)?$")

def _is_text_value(value):
    return not bool(_NUMERIC_VALUE_RE.fullmatch(str(value)))

def _escape_filter_value(value):
    value = str(value)
    if _SAFE_UNQUOTED_RE.match(value) and "`" not in value:
        return value
    return f"`{value}`"

def _build_filter(product_name, brand_name, attrs, contains_keys=None):
    contains_keys = contains_keys or ()
    clauses = [
        f"productName:={_escape_filter_value(product_name)}",
        f"brandName:={_escape_filter_value(brand_name)}",
    ]
    for k, v in attrs.items():
        op = ":" if k in contains_keys else ":="
        clauses.append(f"{attr_field_name(k)}{op}{_escape_filter_value(v)}")
    return " && ".join(clauses)

def _run_search(client, filter_by, per_page=100):
    try:
        res = client.collections[COLLECTION_NAME].documents.search({
            "q": "*",
            "query_by": "materialIdStr",
            "filter_by": filter_by,
            "per_page": per_page,
        })
    except Exception as e:
        print(f"[attribute search] search failed for filter {filter_by!r}: {e}")
        return []
    return [hit["document"] for hit in res.get("hits", [])]

def _merge_docs(primary_docs, secondary_docs):
    seen_ids = {d.get("materialId") for d in primary_docs}
    merged = list(primary_docs)
    for d in secondary_docs:
        mid = d.get("materialId")
        if mid not in seen_ids:
            merged.append(d)
            seen_ids.add(mid)
    return merged

def _exact_plus_contains_search(client, product_name, brand_name, attrs, per_page=100):
    exact_docs = _run_search(client, _build_filter(product_name, brand_name, attrs), per_page)

    text_keys = {k for k, v in attrs.items() if _is_text_value(v)}
    if not text_keys:
        return exact_docs, set()

    contains_docs = _run_search(
        client,
        _build_filter(product_name, brand_name, attrs, contains_keys=text_keys),
        per_page,
    )
    merged = _merge_docs(exact_docs, contains_docs)
    return merged, text_keys

def search_by_attributes(client, product_name, brand_name, raw_attr_string, per_page=100, min_results=MIN_RESULTS):
    parsed = parse_attr_string(raw_attr_string)

    product_name = (parsed.pop("productname", None) or product_name or "").strip().lower()
    brand_name = (parsed.pop("brandname", None) or brand_name or "").strip().lower()
    attrs = parsed

    if not product_name or not brand_name:
        return [], {"tier": "no_match", "reason": "productName and brandName are required hard filters"}

    if not attrs:
        docs = _run_search(client, _build_filter(product_name, brand_name, {}), per_page)
        return docs, {"tier": "brand_product_only", "attrs_used": [], "attrs_requested": {}}

    schema = get_product_attr_schema()
    product_schema = schema.get(product_name)

    if product_schema is not None:
        recognized = {k: v for k, v in attrs.items() if k in product_schema}
        unrecognized_dropped = set(attrs) - set(recognized)

        if not recognized:
            docs = _run_search(client, _build_filter(product_name, brand_name, {}), per_page)
            return docs, {
                "tier": "brand_product_only",
                "attrs_used": [],
                "attrs_requested": attrs,
                "dropped": list(unrecognized_dropped),
                "reason": "no given attribute is recognized for this productName",
            }
        full_docs, contains_keys_used = _exact_plus_contains_search(
            client, product_name, brand_name, recognized, per_page
        )

        if len(full_docs) >= min_results:
            meta = {
                "tier": "full_match",
                "attrs_used": recognized,
                "attrs_requested": attrs,
                "count": len(full_docs),
            }
            if contains_keys_used:
                meta["contains_merged_keys"] = list(contains_keys_used)
            if unrecognized_dropped:
                meta["tier"] = "dropped_unrecognized"
                meta["dropped"] = list(unrecognized_dropped)
            return full_docs, meta
        mandatory_only = {k: v for k, v in recognized.items() if product_schema.get(k) == 1}
        optional_dropped = set(recognized) - set(mandatory_only)

        if mandatory_only and optional_dropped:
            mandatory_docs, mandatory_contains_keys_used = _exact_plus_contains_search(
                client, product_name, brand_name, mandatory_only, per_page
            )
            merged = _merge_docs(full_docs, mandatory_docs)
            merged = merged[:per_page]

            if merged:
                tier = "full_match" if not unrecognized_dropped else "dropped_unrecognized"
                if len(merged) > len(full_docs):
                    tier = "augmented_with_mandatory_only" if full_docs else "mandatory_only"
                meta = {
                    "tier": tier,
                    "attrs_used": recognized if full_docs else mandatory_only,
                    "attrs_requested": attrs,
                    "full_match_count": len(full_docs),
                    "augmented_count": len(merged) - len(full_docs),
                }
                merged_contains_keys = contains_keys_used | mandatory_contains_keys_used
                if merged_contains_keys:
                    meta["contains_merged_keys"] = list(merged_contains_keys)
                if unrecognized_dropped:
                    meta["dropped_unrecognized"] = list(unrecognized_dropped)
                if len(merged) > len(full_docs):
                    meta["optional_dropped_for_augment"] = list(optional_dropped)
                return merged, meta

        if full_docs:
            meta = {
                "tier": "full_match",
                "attrs_used": recognized,
                "attrs_requested": attrs,
                "count": len(full_docs),
            }
            if contains_keys_used:
                meta["contains_merged_keys"] = list(contains_keys_used)
            if unrecognized_dropped:
                meta["tier"] = "dropped_unrecognized"
                meta["dropped"] = list(unrecognized_dropped)
            return full_docs, meta

        base_for_drop = mandatory_only if mandatory_only else recognized

        for contains_key in CONTAINS_FALLBACK_KEYS:
            if contains_key not in base_for_drop:
                continue
            docs = _run_search(
                client,
                _build_filter(product_name, brand_name, base_for_drop, contains_keys={contains_key}),
                per_page,
            )
            if not docs:
                continue
            field = attr_field_name(contains_key)
            deduped = []
            seen_values = []
            for d in docs:
                val = d.get(field)
                if val not in seen_values:
                    seen_values.append(val)
                    deduped.append(d)
            return deduped, {
                "tier": f"contains_{contains_key}",
                "attrs_used": base_for_drop,
                "attrs_requested": attrs,
                "reason": f"no exact/contains match on the full set; retried with {contains_key} alone as a contains/word match",
                f"{contains_key}_requested": base_for_drop.get(contains_key),
                f"{contains_key}_variants_found": seen_values,
                "ambiguous": len(seen_values) > 1,
            }

        if CONDUCTOR_MATERIAL_KEY in base_for_drop:
            without_conductor_material = {k: v for k, v in base_for_drop.items() if k != CONDUCTOR_MATERIAL_KEY}
            if without_conductor_material:
                docs = _run_search(client, _build_filter(product_name, brand_name, without_conductor_material), per_page)
                if docs:
                    cm_field = attr_field_name(CONDUCTOR_MATERIAL_KEY)
                    deduped = []
                    seen_values = []
                    for d in docs:
                        val = d.get(cm_field)
                        if val not in seen_values:
                            seen_values.append(val)
                            deduped.append(d)
                    return deduped, {
                        "tier": "dropped_conductor_material",
                        "attrs_used": without_conductor_material,
                        "attrs_requested": attrs,
                        "reason": "no match with conductor_material included; retried with it dropped entirely (not loosened)",
                        "conductor_material_requested": base_for_drop.get(CONDUCTOR_MATERIAL_KEY),
                        "conductor_material_variants_found": seen_values,
                        "ambiguous": len(seen_values) > 1,
                    }

        return [], {
            "tier": "no_match",
            "reason": "no result from the full attribute set (exact+contains), the mandatory-only fallback, the single-field contains fallback, or dropping conductor_material",
            "attrs_requested": attrs,
            "dropped": list(unrecognized_dropped),
        }
    docs, contains_keys_used = _exact_plus_contains_search(client, product_name, brand_name, attrs, per_page)
    if docs:
        meta = {"tier": "full_match", "attrs_used": attrs, "attrs_requested": attrs}
        if contains_keys_used:
            meta["contains_merged_keys"] = list(contains_keys_used)
        return docs, meta

    remaining = list(attrs.items())
    while remaining:
        remaining = remaining[:-1]
        current = dict(remaining)
        docs, contains_keys_used = _exact_plus_contains_search(
            client, product_name, brand_name, current, per_page
        ) if current else (
            _run_search(client, _build_filter(product_name, brand_name, {}), per_page), set()
        )
        if docs:
            meta = {
                "tier": "unmapped_product_dropped",
                "attrs_used": current,
                "attrs_requested": attrs,
            }
            if contains_keys_used:
                meta["contains_merged_keys"] = list(contains_keys_used)
            return docs, meta

    return [], {
        "tier": "no_match",
        "reason": "unmapped productName, nothing matched even brand+productName alone",
        "attrs_requested": attrs,
    }