"""Interpretación de descripciones publicadas por Revista Motor.

Reglas puras (sin base de datos). Principios:
  * Nunca se inventan datos: si la guía no publica motor, combustible o
    transmisión, el campo queda "No especificado/a" (o ``None``).
  * La descripción original se conserva siempre (``raw_description``).
  * Todo alias o ambigüedad se registra en ``flags`` para el informe de pendientes.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Marcas: nombre publicado -> nombre de presentación
# --------------------------------------------------------------------------
BRAND_DISPLAY = {
    "MERCEDES BENZ": "Mercedes-Benz", "MERCEDES-BENZ": "Mercedes-Benz",
    "LAND ROVER": "Land Rover", "LANDROVER": "Land Rover",
    "SSANGYONG": "SsangYong", "CITROEN": "Citroën", "CITROËN": "Citroën",
    "VOLKSWAGEN": "Volkswagen", "BMW": "BMW", "BYD": "BYD", "MG": "MG",
    "GAC": "GAC", "GWM": "GWM", "JAC": "JAC", "JMC": "JMC", "KGM": "KGM",
    "KYC": "KYC", "DFSK": "DFSK", "RAM": "RAM", "SEAT": "SEAT", "MINI": "MINI",
    "CUPRA": "Cupra", "DEEPAL": "Deepal", "CAN-AM": "Can-Am",
    "HARLEY-DAVIDSON": "Harley-Davidson", "MOTO GUZZI": "Moto Guzzi",
    "ROYAL ENFIELD": "Royal Enfield", "AKT": "AKT", "KTM": "KTM", "TVS": "TVS",
}

# Marcas / palabras cuyo prefijo se elimina de la descripción si aparece repetido
BRAND_PREFIXES = ("MERCEDES-AMG", "MERCEDES-MAYBACH", "MERCEDES-BENZ", "MERCEDES", "LEXUS",
                  "MAZDA", "TOYOTA", "FORD", "CHEVROLET", "RENAULT")

# --------------------------------------------------------------------------
# Modelos de varias palabras (más largos primero). Solo nombres de modelo
# publicados; no se agrupan modelos distintos.
# --------------------------------------------------------------------------
MULTI_WORD_MODELS = sorted([
    "GRAND VITARA", "GRAND CHEROKEE", "RANGE ROVER EVOQUE", "RANGE ROVER SPORT",
    "RANGE ROVER VELAR", "RANGE ROVER", "DISCOVERY SPORT", "LAND CRUISER", "CROSS FOX",
    "ECLIPSE CROSS", "MONTERO SPORT", "MONTERO HARD TOP", "BRONCO SPORT", "COROLLA CROSS",
    "YARIS CROSS", "GR YARIS", "C4 CACTUS", "C5 AIRCROSS", "C-ELYSEE", "TIGUAN ALL SPACE",
    "NP300 FRONTIER", "I25 ACCENT", "SANTA FE", "SWIFT DZIRE", "JOHN COOPER WORKS",
    "XC POLESTAR", "ALL NEW MAZDA 3", "GRAND EKOTAXI", "CAPTIVA EV", "SPARK GT",
    "SPARK ACTIVE EUV", "SPARK EUV", "MACAN ELECTRIC", "CAYENNE ELECTRIC", "MERCEDES-MAYBACH GLS",
    "CROSS TREK", "OUTLANDER SPORT", "FJ CRUISER", "GRAN VITARA", "BIG VAN", "GRAND AVENUE",
], key=lambda s: -len(s))

# Alias documentados (grafías distintas del mismo modelo dentro de la propia publicación)
MODEL_ALIASES = {
    "SANTAFE": "SANTA FE", "CX9": "CX-9", "SCROSS": "S-CROSS", "CROSS TREK": "CROSSTREK",
    "RAV": "RAV4", "GRAN VITARA": "GRAND VITARA",
}
# Siglas de modelo que se conservan en mayúscula aunque sean alfabéticas y cortas
MODEL_ACRONYMS = {"XV", "ASX", "RX", "TX", "NX", "GX", "LX", "UX", "CLE", "FJ", "XM", "ZS", "RAM",
                  "BMW", "DS", "GT", "BT", "SUV", "EV", "CX", "IX", "WRX", "XC", "GR", "JMEV", "EUV", "ICAR",
                  "MX", "CR", "HR", "WR", "ZR", "BZ4X", "GLC", "GLE", "GLA", "GLB", "GLS", "CLA", "SLC", "EQA"}

PREFIX_RE = re.compile(r"^(ALL NEW|NEW|NUEVO|NUEVA)\s+", re.I)

AUTO_TOKENS = {
    "AUT", "AUT.", "AT", "A/T", "CVT", "DSG", "TIP", "TIP.", "TIPTRONIC", "S-TRONIC", "TRONIC",
    "TRONIC-S", "BVA", "ASG", "POWERSHIFT", "ECVT", "E-CVT", "DHT",
}
# Tokens que se consideran solo marcadores de caja y se retiran del nombre de la versión
PLAIN_AUTO = {"AUT", "AUT.", "AT", "A/T"}
TURBO_TOKENS = {"T", "T.", "TSI", "TFSI", "THP", "TURBO", "BITURBO", "BI-T", "BI-T.", "ECOBOOST", "ETSI"}
DIESEL_TOKENS = {"DIÉSEL", "DIESEL", "TDI", "BLUEHDI"}
ENGINE_RE = re.compile(r"^(\d)[.,](\d{1,2})(L|T|D|TSI|TFSI)?\.?$", re.I)
ENGINE_COMPOUND_RE = re.compile(r"^(\d[.,]\d)/(\d[.,]\d)$")
YEAR_LIKE = re.compile(r"^(19|20)\d\d$")

SMALL_WORDS_KEEP_UPPER = 3  # palabras alfabéticas de hasta 3 letras se conservan en mayúscula


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def display_brand(raw: str) -> str:
    key = " ".join(raw.upper().split())
    if key in BRAND_DISPLAY:
        return BRAND_DISPLAY[key]
    return "-".join(w.capitalize() for w in key.split("-")) if " " not in key else " ".join(
        w.capitalize() for w in key.split())


def _pretty_word(word: str) -> str:
    """Capitaliza palabras alfabéticas largas; conserva siglas y códigos (LTZ, CX-5, 4X4)."""
    if "/" in word:
        return "/".join(_pretty_word(p) for p in word.split("/"))
    if "-" in word and len(word) > 1:
        return "-".join(_pretty_word(p) for p in word.split("-"))
    letters = "".join(ch for ch in word if ch.isalpha())
    if word.isalpha() and len(word) > SMALL_WORDS_KEEP_UPPER:
        return word[0].upper() + word[1:].lower()
    if letters and any(ch.isdigit() for ch in word):
        return word.upper()
    if word.isalpha() and len(word) <= SMALL_WORDS_KEEP_UPPER:
        return word.upper()
    return word


def _pretty_model_word(word: str) -> str:
    if "-" in word:
        return "-".join(p if (p.isalpha() and len(p) <= 2) else _pretty_model_word(p) for p in word.split("-"))
    if word in MODEL_ACRONYMS:
        return word
    if word.isalpha() and len(word) >= 2:
        return word[0].upper() + word[1:].lower()
    return word.upper() if any(c.isdigit() for c in word) else word


def pretty_model(text: str) -> str:
    return " ".join(_pretty_model_word(w) for w in text.split())


def pretty(text: str) -> str:
    return " ".join(_pretty_word(w) for w in text.split())


@dataclass
class ParsedVehicle:
    brand: str
    model: str
    generation: str | None
    version_name: str
    engine_label: str
    fuel: str | None
    transmission: str
    displacement_text: str | None
    turbo: bool
    raw_description: str
    category: str
    flags: list[str] = field(default_factory=list)

    @property
    def trim_tokens(self) -> set[str]:
        return {t for t in re.split(r"[^A-Z0-9]+", strip_accents(self.version_name).upper()) if t}


def _pop_model(tokens: list[str], brand_key: str, flags: list[str]) -> tuple[str, list[str]]:
    joined = " ".join(tokens)
    upper = joined.upper()

    # Mercedes-Benz: clase + número (A 200, GLC 300, G 63). AMG queda como parte de la versión.
    if brand_key == "MERCEDES BENZ":
        m = re.match(r"^(?:(AMG|MERCEDES-AMG|MERCEDES-MAYBACH)\s+)?([A-Z]{1,3})\s?(\d{2,3}[A-Z]?\+?)\b\s*(.*)$", upper)
        if m:
            prefix, letters, digits, rest = m.groups()
            model = f"{letters} {digits}"
            rest_tokens = ((["AMG"] if prefix and "AMG" in prefix else []) +
                           (["MAYBACH"] if prefix and "MAYBACH" in prefix else []) + rest.split())
            return model, rest_tokens
    if brand_key == "MINI":
        for special in ("COUNTRYMAN", "CLUBMAN", "ACEMAN"):
            if special in tokens:
                rest = [t for t in tokens if t != special]
                return special, rest
        if upper.startswith("JOHN COOPER WORKS"):
            return "JOHN COOPER WORKS", tokens[3:]
    # Modelos multi-palabra conocidos
    for multi in MULTI_WORD_MODELS:
        if upper == multi or upper.startswith(multi + " "):
            n = len(multi.split())
            return multi, tokens[n:]
    first = tokens[0].upper()
    return first, tokens[1:]


def parse_description(brand_raw: str, description: str, category: str = "") -> ParsedVehicle:
    flags: list[str] = []
    brand = display_brand(brand_raw)
    brand_key = " ".join(brand_raw.upper().split()).replace("-", " ") if brand_raw.upper() in ("MERCEDES-BENZ",) else \
        " ".join(brand_raw.upper().split())
    raw = " ".join(description.split())
    text = raw.upper()
    # Prefijo de generación publicado (NEW / NUEVO / NUEVA / ALL NEW)
    generation = None
    m = PREFIX_RE.match(text)
    if m:
        generation = {"ALL NEW": "All New", "NEW": "New", "NUEVO": "Nuevo", "NUEVA": "Nueva"}[m.group(1).upper()]
        text = text[m.end():]
    # Marca repetida al comienzo (p. ej. "NUEVO MAZDA 6", "LEXUS NX350H")
    for pref in BRAND_PREFIXES:
        if text.startswith(pref + " ") and not text.startswith("MERCEDES-AMG") and not text.startswith("MERCEDES-MAYBACH"):
            if pref.replace("-", " ") in brand_key or pref == brand_key.split()[0]:
                text = text[len(pref) + 1:]
                break
    if text.startswith("MERCEDES-AMG ") or text.startswith("MERCEDES-MAYBACH "):
        pass  # lo resuelve _pop_model
    # Separar paréntesis y ruido de puntuación pegado
    text = text.replace("(", " ").replace(")", " ")
    text = re.sub(r"\s*,\s*(?=\D)", " ", text)  # comas no decimales
    tokens = text.split()
    if not tokens:
        raise ValueError("descripción vacía")

    model_raw, rest = _pop_model(tokens, brand_key if brand_key != "MERCEDES-BENZ" else "MERCEDES BENZ", flags)
    model_key = model_raw
    if model_key in MODEL_ALIASES:
        flags.append(f"alias de modelo aplicado: {model_raw} → {MODEL_ALIASES[model_key]}")
        model_key = MODEL_ALIASES[model_key]
    is_benz_class = brand_key == "MERCEDES BENZ" and re.match(r"^[A-Z]{1,3} \d", model_key) is not None
    model = model_key if is_benz_class else pretty_model(model_key)

    displacement = None
    turbo = False
    diesel = False
    auto = False
    kept: list[str] = []
    for tok in rest:
        t = tok.strip()
        if not t:
            continue
        up = t.upper()
        em = ENGINE_RE.match(up)
        cm = ENGINE_COMPOUND_RE.match(up)
        if em and displacement is None:
            displacement = f"{em.group(1)}.{em.group(2)}"
            suffix = (em.group(3) or "").upper()
            if suffix in ("T", "TSI", "TFSI"):
                turbo = True
            if suffix == "D":
                diesel = True
            continue
        if cm and displacement is None:
            displacement = f"{cm.group(1).replace(',', '.')}/{cm.group(2).replace(',', '.')}"
            flags.append("cilindrada publicada como alternativa (dos valores)")
            continue
        if up in PLAIN_AUTO:
            auto = True
            continue
        if up in AUTO_TOKENS:
            auto = True
            kept.append(up)
            continue
        if up in TURBO_TOKENS:
            turbo = True
            if up in ("TSI", "TFSI", "THP", "ECOBOOST", "ETSI", "BITURBO", "BI-T", "BI-T."):
                kept.append(up)
            continue
        if up in DIESEL_TOKENS:
            diesel = True
            if up in ("TDI", "BLUEHDI"):
                kept.append(up)
            continue
        kept.append(t)

    # Limpieza de residuos de puntuación
    kept = [k for k in kept if k not in {"/", "-", "+", "."}] if len(kept) > 1 else kept
    version = pretty(" ".join(kept)).strip(" /-,")
    if "/" in re.sub(r"\d[.,]\d/\d[.,]\d", "", raw):
        flags.append("descripción compuesta: varias versiones/variantes agrupadas en una sola fila de la guía")
    if not version:
        version = "Estándar"

    if displacement:
        engine_label = f"{displacement} L" + (" turbo" if turbo else "")
    elif turbo:
        engine_label = "Turbo (cilindrada no especificada)"
    else:
        engine_label = "No especificado"

    cat = category.upper()
    fuel = None
    if diesel:
        fuel = "Diésel"
    elif "ELÉCTRICO" in cat or "ELECTRICO" in cat:
        fuel = "Eléctrico"
    elif "HÍBRIDO" in cat or "HIBRIDO" in cat:
        fuel = "Híbrido (según categoría de la guía)"

    # Sin marca de caja la transmisión NO se supone: queda «Sin confirmar» hasta que la auditoría
    # (transmission_audit.py) la confirme con una ficha técnica o una regla documentada.
    transmission = "Automática" if auto else "Sin confirmar"
    return ParsedVehicle(
        brand=brand, model=model, generation=generation, version_name=version,
        engine_label=engine_label, fuel=fuel, transmission=transmission,
        displacement_text=displacement, turbo=turbo, raw_description=raw,
        category=category, flags=flags,
    )


# --------------------------------------------------------------------------
# Alcance: qué filas del listado de nuevos pertenecen a automóviles / camperos /
# camionetas / pick-ups (el alcance de la aplicación) y cuáles no.
# --------------------------------------------------------------------------
OUT_OF_SCOPE_CATEGORIES = {
    "MOTOCICLETAS": "motocicleta / cuatriciclo / moto acuática (fuera del alcance de la aplicación)",
    "UTILITARIOS": "vehículo utilitario o de carga (fuera del alcance de la aplicación)",
    "TAXIS": "taxi (fuera del alcance de la aplicación)",
}
COMMERCIAL_BRANDS = {"FREIGHTLINER", "FUSO", "MACK", "FARIZON"}
COMMERCIAL_WORDS = re.compile(
    r"\b(CARGA|CARGO|BUS|MINITRUCK|MILER|PANEL|TRACTO|VOLCO|MIXER|CUMMINS|ATEGO|AROCS|ACTROS|"
    r"CANTER|DELIVERY|E-TRANSIT)\b|\b\d(?:[.,]\d)?\s?TON\b|\bMINIVAN\b")
PASSENGER_VAN = re.compile(r"\bPAS(?:AJEROS)?\b")
PICKUP_CARGO_MODELS = re.compile(r"\b(ALASKAN|OROCH)\b")
COMMERCIAL_CHASSIS = re.compile(r"\bCHAS[ÍI]S\b")


def _commercial_description(d: str, category: str) -> bool:
    d = d.upper()
    cat = category.upper()
    if PASSENGER_VAN.search(d) and not re.search(r"\b(CARGA|CARGO|BUS|CHAS[ÍI]S)\b", d):
        return False  # van de pasajeros: dentro del alcance
    if PICKUP_CARGO_MODELS.search(d) and "PICK" in cat:
        return False  # Alaskan/Oroch son pick-ups; «Cargo» es nombre de versión
    return bool(COMMERCIAL_WORDS.search(d))


def new_scope_decision(category: str, brand: str, description: str) -> tuple[bool, str]:
    cat = category.upper()
    if cat in OUT_OF_SCOPE_CATEGORIES:
        return False, OUT_OF_SCOPE_CATEGORIES[cat]
    b = brand.upper().replace("Ë", "E")
    d = description.upper()
    if b in COMMERCIAL_BRANDS:
        return False, "marca de vehículos comerciales/pesados (fuera del alcance)"
    if _commercial_description(d, cat):
        return False, "descripción de vehículo de carga, bus o utilitario (fuera del alcance)"
    if COMMERCIAL_CHASSIS.search(d) and "PICK" not in cat and cat != "4X4":
        return False, "chasís comercial (fuera del alcance)"
    return True, ""


def used_scope_decision(category: str, brand: str, description: str) -> tuple[bool, str]:
    if _commercial_description(description, category):
        return False, "descripción de vehículo de carga o utilitario (fuera del alcance)"
    return True, ""
