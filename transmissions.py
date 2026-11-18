"""Nomenclatura controlada de transmisiones de AutoValor Pro Colombia.

Cada versión del catálogo guarda:

* ``transmission``: CATEGORÍA principal. Solo admite «Mecánica», «Automática» o
  «Sin confirmar». «Sin confirmar» no es una caja de cambios: indica que no hay base
  documental para afirmar cuál tiene la versión.
* ``transmission_subtype``: subtipo técnico de un vocabulario cerrado (CVT, doble
  embrague, etc.), solo cuando está documentado.
* ``transmission_gears`` y ``transmission_detail``: número de marchas y detalle comercial
  (p. ej. «SKYACTIV-DRIVE», «con levas»), solo cuando están documentados.
* ``transmission_status``: cómo se sabe (verificada, documentada, inferida, regla técnica,
  pendiente o conflicto).
* ``transmission_original``: el valor tal como estaba antes de la primera normalización.

Las funciones de este módulo solo traducen equivalencias de texto seguras. No completan
especificaciones a partir de otro país, generación o año: eso exige una ficha técnica
registrada en ``data/fichas_tecnicas`` (ver ``transmission_audit.py``).
"""
from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# Vocabulario controlado
# ---------------------------------------------------------------------------
CATEGORY_MANUAL = "Mecánica"
CATEGORY_AUTOMATIC = "Automática"
CATEGORY_UNCONFIRMED = "Sin confirmar"
CATEGORIES = (CATEGORY_MANUAL, CATEGORY_AUTOMATIC, CATEGORY_UNCONFIRMED)
CONFIRMED_CATEGORIES = (CATEGORY_MANUAL, CATEGORY_AUTOMATIC)

# Subtipos (código interno -> nombre que se guarda en la base)
SUBTYPES = {
    "manual": "Manual",
    "convencional": "Automática convencional (convertidor de par)",
    "cvt": "CVT (variación continua)",
    "dct": "Doble embrague (DCT/DSG)",
    "amt": "Automatizada de embrague simple (AMT)",
    "ecvt": "Híbrida de división de potencia (e-CVT)",
    "dht": "Híbrida dedicada (DHT)",
    "ev": "Transmisión de vehículo eléctrico (sin caja de cambios convencional)",
}
SUBTYPE_TO_CODE = {v: k for k, v in SUBTYPES.items()}
AUTOMATIC_SUBTYPE_CODES = ("convencional", "cvt", "dct", "amt", "ecvt", "dht", "ev")

# Nombre corto del subtipo para la página pública
SUBTYPE_SHORT = {
    "manual": "",
    "convencional": "convencional",
    "cvt": "CVT",
    "dct": "doble embrague",
    "amt": "automatizada",
    "ecvt": "e-CVT híbrida",
    "dht": "híbrida DHT",
    "ev": "eléctrico",
}

# Estados de la transmisión
STATUS_VERIFIED = "verificada"        # confirmada con fuente técnica del mercado colombiano y año
STATUS_DOCUMENTED = "documentada"     # declarada de forma explícita en la descripción de la versión
STATUS_INFERRED = "inferida"          # deducida con una regla documentada (pareja automática/no marcada)
STATUS_RULE = "regla_tecnica"         # p. ej. vehículos eléctricos: no tienen caja convencional
STATUS_PENDING = "pendiente"          # no hay base para afirmar la caja
STATUS_CONFLICT = "conflicto"         # fuentes contradictorias: requiere revisión
STATUSES = (STATUS_VERIFIED, STATUS_DOCUMENTED, STATUS_INFERRED, STATUS_RULE, STATUS_PENDING, STATUS_CONFLICT)
CONFIRMED_STATUSES = (STATUS_VERIFIED, STATUS_DOCUMENTED, STATUS_INFERRED, STATUS_RULE)

STATUS_PUBLIC = {
    STATUS_VERIFIED: "Caja verificada con ficha técnica",
    STATUS_DOCUMENTED: "Caja indicada en la descripción de la versión",
    STATUS_INFERRED: "Caja deducida (sin ficha técnica verificada)",
    STATUS_RULE: "Vehículo eléctrico",
    STATUS_PENDING: "Caja sin confirmar",
    STATUS_CONFLICT: "Caja en revisión",
}


def strip_accents(value) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _fold(value) -> str:
    return strip_accents(value).lower()


# ---------------------------------------------------------------------------
# Marcas de transmisión en descripciones comerciales (listas de precios)
# ---------------------------------------------------------------------------
# token -> (código de subtipo o None, detalle). Todos indican caja automática.
# Solo se asigna subtipo cuando el nombre comercial designa un tipo concreto de caja
# (equivalencias documentadas en data/fichas_tecnicas/fuentes_tecnicas.csv, grupo GLOSARIO).
AUTOMATIC_TOKENS: dict[str, tuple[str | None, str | None]] = {
    "AUT": (None, None),
    "AUT.": (None, None),
    "AT": (None, None),
    "A/T": (None, None),
    "AUTOMATICA": (None, None),
    "AUTOMÁTICA": (None, None),
    "BVA": (None, "BVA"),
    "TIP": (None, "Tiptronic"),
    "TIP.": (None, "Tiptronic"),
    "TIPTRONIC": (None, "Tiptronic"),
    "TRONIC": (None, None),
    "CVT": ("cvt", None),
    "IVT": ("cvt", "IVT"),
    "XTRONIC": ("cvt", "Xtronic"),
    "X-TRONIC": ("cvt", "Xtronic"),
    "MULT.": ("cvt", "Multitronic"),
    "MULTITRONIC": ("cvt", "Multitronic"),
    "DSG": ("dct", "DSG"),
    "S-TRONIC": ("dct", "S tronic"),
    "STRONIC": ("dct", "S tronic"),
    "TRONIC-S": ("dct", "S tronic"),
    "POWERSHIFT": ("dct", "PowerShift"),
    "DCT": ("dct", None),
    "EDC": ("dct", "EDC"),
    "PDK": ("dct", "PDK"),
    "ASG": ("amt", "ASG"),
    "AMT": ("amt", None),
    "ECVT": ("ecvt", None),
    "E-CVT": ("ecvt", None),
    "DHT": ("dht", None),
}
# Fichas en las que la abreviatura de una palabra de dos letras es ambigua ("ST" es S tronic
# en Audi pero una versión deportiva en Ford): no se interpreta automáticamente.
AMBIGUOUS_TOKENS = {"ST"}
# Ninguna lista de precios usada marca las cajas mecánicas; se aceptan por si llegan en otras fuentes.
MANUAL_TOKENS = {"MT", "M/T", "MEC", "MEC.", "MECANICA", "MECÁNICA", "MANUAL"}


def _tokens(description: str) -> list[str]:
    return [t.strip(",;()[]") for t in str(description or "").upper().split()]


def classify_description(description: str | None) -> dict | None:
    """Clasifica una descripción comercial por sus marcas explícitas de caja.

    Devuelve None si la descripción no marca la transmisión. Nunca deduce la caja
    por la ausencia de marca.
    """
    if not description:
        return None
    toks = _tokens(description)
    auto_hits = [t for t in toks if t in AUTOMATIC_TOKENS]
    manual_hits = [t for t in toks if t in MANUAL_TOKENS]
    if auto_hits and manual_hits:
        return {"category": None, "conflict": True, "tokens": auto_hits + manual_hits}
    if manual_hits:
        return {"category": CATEGORY_MANUAL, "subtype_code": "manual", "detail": None, "tokens": manual_hits}
    if not auto_hits:
        return None
    subtype, details = None, []
    for tok in auto_hits:
        code, detail = AUTOMATIC_TOKENS[tok]
        if code and subtype and code != subtype:
            return {"category": CATEGORY_AUTOMATIC, "subtype_code": None, "detail": None,
                    "tokens": auto_hits, "subtype_conflict": True}
        subtype = subtype or code
        if detail and detail not in details:
            details.append(detail)
    return {"category": CATEGORY_AUTOMATIC, "subtype_code": subtype,
            "detail": ", ".join(details) or None, "tokens": auto_hits}


def base_description(description: str | None) -> str:
    """Descripción sin marcas de caja (para emparejar una versión automática con su gemela)."""
    toks = [t for t in _tokens(description) if t and t not in AUTOMATIC_TOKENS and t not in MANUAL_TOKENS]
    return " ".join(toks)


def normalized_description(description: str | None) -> str:
    return " ".join(t for t in _tokens(description) if t)


def has_ambiguous_token(description: str | None) -> bool:
    return any(t in AMBIGUOUS_TOKENS for t in _tokens(description))


# ---------------------------------------------------------------------------
# Texto libre de transmisión (catálogo inicial, panel administrativo, CSV)
# ---------------------------------------------------------------------------
_UNCONFIRMED_PATTERNS = (
    "no especificad", "no documentad", "sin confirmar", "desconocid", "por confirmar",
    "pendiente", "sin dato", "n/d", "no disponible",
)
_GEARS_RE = re.compile(r"(\d{1,2})\s*(?:velocidades|vel\b|vel\.|marchas|cambios|speed|v\b)")


def classify_text(text: str | None) -> dict | None:
    """Traduce un texto de transmisión a categoría/subtipo/marchas/detalle.

    Devuelve None cuando el texto no permite afirmar una caja (vacío, «No especificada»,
    «No documentada…», «Sin confirmar» o texto contradictorio).
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    folded = _fold(raw)
    if any(p in folded for p in _UNCONFIRMED_PATTERNS):
        return None
    words = set(re.findall(r"[a-z0-9/\-\.]+", folded))
    gears = None
    m = _GEARS_RE.search(folded)
    if m:
        gears = int(m.group(1))
    m2 = re.search(r"\b(\d{1,2})\s*(?:at|mt|dct|dsg)\b", folded)
    if gears is None and m2:
        gears = int(m2.group(1))

    # Subtipos automáticos (de lo más específico a lo más general)
    subtype = None
    if re.search(r"\be-?cvt\b", folded) and ("e-cvt" in folded or "ecvt" in folded):
        subtype = "ecvt"
    elif re.search(r"\bcvt\b|variacion continua|multitronic|xtronic|x-tronic|\bivt\b", folded):
        subtype = "cvt"
    elif re.search(r"\bdht\b", folded):
        subtype = "dht"
    elif re.search(r"\bdct\b|\bdsg\b|doble embrague|s[\- ]?tronic|powershift|\bpdk\b|\bedc\b", folded):
        subtype = "dct"
    elif re.search(r"\bamt\b|automatizada|\basg\b|\bags\b|dualogic|easytronic|i-motion", folded):
        subtype = "amt"
    elif re.search(r"convertidor de par|torque converter", folded):
        subtype = "convencional"
    elif re.search(r"electric[oa]? .*reductor|reductor[a]?\b|una sola marcha|single[- ]speed", folded):
        subtype = "ev"

    automatic = bool(subtype) or bool(re.search(
        r"automatic|\bat\b|\ba/t\b|tiptronic|steptronic|geartronic|shiftronic|skyactiv-drive|\baut\.?\b", folded))
    # «modo manual» o «manual secuencial» dentro de una caja automática no la convierte en mecánica.
    manual_text = re.sub(r"modo manual|cambios manuales|secuencial manual|manual secuencial|paso manual", "", folded)
    manual = bool(re.search(r"mecanic|\bmanual\b|\bmt\b|\bm/t\b|sincronic", manual_text))

    detail_parts = []
    m3 = re.search(r"\((\d{1,2}) velocidades\)", folded)
    simulated = subtype == "cvt" and m3
    for label, pattern in (("SKYACTIV-DRIVE", r"skyactiv-drive"), ("Tiptronic", r"tiptronic"),
                           ("Steptronic", r"steptronic"), ("Geartronic", r"geartronic"),
                           ("con modo manual", r"modo manual"), ("con levas", r"levas|paddle")):
        if re.search(pattern, folded) and not (simulated and label == "con modo manual"):
            detail_parts.append(label)
    if simulated:
        # En una CVT las «velocidades» del modo manual son relaciones simuladas, no marchas físicas.
        detail_parts.append(f"modo manual con {m3.group(1)} relaciones simuladas")
        gears = None
    detail = ", ".join(detail_parts) or None

    if automatic and manual:
        return None  # texto contradictorio: no se adivina
    if manual:
        return {"category": CATEGORY_MANUAL, "subtype_code": "manual", "gears": gears, "detail": detail}
    if automatic:
        return {"category": CATEGORY_AUTOMATIC, "subtype_code": subtype, "gears": gears, "detail": detail}
    return None


def category_for_key(text: str | None) -> str:
    """Categoría usada en la clave lógica de la versión (equivalencias de escritura incluidas)."""
    value = str(text or "").strip()
    if value in CATEGORIES:
        return value
    parsed = classify_text(value)
    if parsed and parsed.get("category") in CONFIRMED_CATEGORIES:
        return parsed["category"]
    return CATEGORY_UNCONFIRMED


# ---------------------------------------------------------------------------
# Presentación
# ---------------------------------------------------------------------------
def subtype_code(subtype_name: str | None) -> str | None:
    if not subtype_name:
        return None
    return SUBTYPE_TO_CODE.get(subtype_name)


def transmission_label(category: str | None, subtype: str | None = None, gears: int | None = None,
                       detail: str | None = None) -> str:
    """Etiqueta legible: «Mecánica de 5 velocidades», «Automática · CVT», «Caja sin confirmar»."""
    if category not in CONFIRMED_CATEGORIES:
        return "Caja sin confirmar"
    code = subtype_code(subtype)
    parts = [category]
    if category == CATEGORY_AUTOMATIC and code and code != "manual":
        parts.append(SUBTYPE_SHORT.get(code) or subtype)
    text = " · ".join(p for p in parts if p)
    if gears:
        text += f" de {int(gears)} velocidades"
    if detail and category == CATEGORY_AUTOMATIC and code in (None, "convencional"):
        text += f" ({detail})"
    return text


def public_transmission(row) -> dict:
    """Campos de transmisión que se pueden publicar (sin fuentes ni evidencias internas)."""
    get = row.get if isinstance(row, dict) else (lambda k, d=None: row[k] if k in row.keys() else d)
    category = get("transmission")
    status = get("transmission_status") or STATUS_PENDING
    if category not in CATEGORIES:
        category = category_for_key(category)
    subtype = get("transmission_subtype")
    gears = get("transmission_gears")
    detail = get("transmission_detail")
    return {
        "transmission": category,
        "transmission_subtype": subtype,
        "transmission_gears": gears,
        "transmission_label": transmission_label(category, subtype, gears, detail),
        "transmission_status": status,
        "transmission_status_label": STATUS_PUBLIC.get(status, STATUS_PUBLIC[STATUS_PENDING]),
        "transmission_confirmed": category in CONFIRMED_CATEGORIES and status in CONFIRMED_STATUSES,
        "transmission_verified": status == STATUS_VERIFIED,
    }
