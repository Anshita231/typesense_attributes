import re
import os
import pandas as pd

UNIT_WORDS = {
    "%", "A", "ADC", "AH", "AMP", "AMPS", "BPM", "DPI", "GB", "GRM", "HZ",
    "INCH", "INCHES", "KA", "KG", "KGS", "KN", "KV", "KVA", "KW", "LTR",
    "MAH", "MB", "METER", "METERS", "MICRON", "MIN", "ML", "MM", "MP",
    "MTR", "MVAR", "NM", "RPM", "SQFT", "SQMM", "TON", "V", "W", "WP",
    "DB", "PCS", "PC", "NOS", "DEGREE", "DEGREES", "CELSIUS", "PHASE",
    "HP", "VOLT", "VOLTS", "WATT", "WATTS", "BAR", "PSI", "LBS", "LB",
    "PAIR", "PAIRS", "TRIAD", "TRIADS", "TRAID", "TRAIDS",
}

ATTR_FIELD_SUFFIX = "_attr"

def attr_field_name(slug):
    if not slug:
        return slug
    return f"{slug}{ATTR_FIELD_SUFFIX}"

def strip_attr_field_suffix(field_name):
    if field_name and field_name.endswith(ATTR_FIELD_SUFFIX):
        return field_name[: -len(ATTR_FIELD_SUFFIX)]
    return field_name

def slugify_attr(name):
    if not name:
        return None
    s = str(name).strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    if not s:
        return None
    if s[0].isdigit():
        s = "a_" + s
    return s

_LEADING_NUM_RE = re.compile(r"^([\d.,]+)\s*(.*)$")
_NUM_ONLY_RE = re.compile(r"^[\d.,]+$")

NOT_APPLICABLE_VALUES = {
    "not applicable", "n/a", "na", "none", "nil", "null", "-", "n.a.", "n.a",
}

VALUE_ALIASES = {
    "pvc (tape / extruded)": "pvc",
    "pvc (tape/extruded)": "pvc",
    "pvc tape/pvc extruded": "pvc",
    "pvc tape / pvc extruded": "pvc",
    "pvc tape/extruded": "pvc",
    "pvc tape / extruded": "pvc",
}

def _canonical_number(num_str):
    s = num_str.replace(",", "")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
        if s in ("", "-"):
            s = "0"
    sign = "-" if s.startswith("-") else ""
    body = s[1:] if sign else s
    int_part, dot, frac_part = body.partition(".")
    int_part = int_part.lstrip("0") or "0"
    s = sign + int_part + (dot + frac_part if dot else "")
    return s

def normalize_attr_value(value, unit=None):
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() == "null":
        return None
    s = re.sub(r"\s+", " ", s)

    lowered = s.lower()
    if lowered in NOT_APPLICABLE_VALUES:
        return None
    if lowered in VALUE_ALIASES:
        return VALUE_ALIASES[lowered]

    if unit:
        core = s.replace(",", "")
        if _NUM_ONLY_RE.match(core):
            return _canonical_number(core)
        return core.lower()

    m = _LEADING_NUM_RE.match(s.upper())
    if m:
        num, rest = m.groups()
        rest_clean = re.sub(r"[\s.]+", "", rest)
        if rest_clean in UNIT_WORDS:
            return _canonical_number(num)

    if _NUM_ONLY_RE.match(s):
        return _canonical_number(s)

    return s.lower()

def load_product_attr_schema(xlsx_path):
    df = pd.read_excel(xlsx_path)
    schema = {}
    for _, row in df.iterrows():
        pname = row.get("productName")
        attr = row.get("attribute")
        mtype = row.get("materialSpecificationMandatoryTypeId")
        if pname is None or attr is None:
            continue
        pname_key = str(pname).strip().lower()
        attr_slug = slugify_attr(attr)
        if not attr_slug:
            continue
        try:
            mtype_int = int(mtype) if pd.notna(mtype) else 2  # unknown -> treat as optional
        except (ValueError, TypeError):
            mtype_int = 2
        schema.setdefault(pname_key, {})[attr_slug] = mtype_int
    return schema

_SCHEMA_CACHE = None
_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "attributesDetails.xlsx")

def get_product_attr_schema(xlsx_path=None):
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is None:
        _SCHEMA_CACHE = load_product_attr_schema(xlsx_path or _SCHEMA_PATH)
    return _SCHEMA_CACHE