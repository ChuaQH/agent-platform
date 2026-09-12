"""Unit conversion tool — offline, static conversion tables, no API needed."""

from __future__ import annotations

from typing import Any

from core.tool_framework import Tool, ToolCapability

# ── Length (base: meter) ────────────────────────────────────────────────
_LENGTH: dict[str, float] = {
    "m": 1.0, "meter": 1.0, "meters": 1.0,
    "km": 1000.0, "kilometer": 1000.0, "kilometers": 1000.0,
    "cm": 0.01, "centimeter": 0.01, "centimeters": 0.01,
    "mm": 0.001, "millimeter": 0.001, "millimeters": 0.001,
    "mi": 1609.344, "mile": 1609.344, "miles": 1609.344,
    "yd": 0.9144, "yard": 0.9144, "yards": 0.9144,
    "ft": 0.3048, "foot": 0.3048, "feet": 0.3048,
    "in": 0.0254, "inch": 0.0254, "inches": 0.0254,
}

# ── Mass (base: kilogram) ───────────────────────────────────────────────
_MASS: dict[str, float] = {
    "kg": 1.0, "kilogram": 1.0, "kilograms": 1.0,
    "g": 0.001, "gram": 0.001, "grams": 0.001,
    "mg": 1e-6, "milligram": 1e-6, "milligrams": 1e-6,
    "lb": 0.45359237, "lbs": 0.45359237, "pound": 0.45359237, "pounds": 0.45359237,
    "oz": 0.028349523125, "ounce": 0.028349523125, "ounces": 0.028349523125,
    "st": 6.35029318, "stone": 6.35029318,
    "t": 1000.0, "tonne": 1000.0, "ton": 907.18474, "tons": 907.18474,
}

# ── Volume (base: liter) ────────────────────────────────────────────────
_VOLUME: dict[str, float] = {
    "l": 1.0, "liter": 1.0, "liters": 1.0, "litre": 1.0, "litres": 1.0,
    "ml": 0.001, "milliliter": 0.001, "milliliters": 0.001,
    "gal": 3.785411784, "gallon": 3.785411784, "gallons": 3.785411784,
    "uk_gal": 4.54609, "imperial_gallon": 4.54609,
    "qt": 0.946352946, "quart": 0.946352946, "quarts": 0.946352946,
    "pt": 0.473176473, "pint": 0.473176473, "pints": 0.473176473,
    "cup": 0.2365882365, "cups": 0.2365882365,
    "fl_oz": 0.0295735295625, "fluid_ounce": 0.0295735295625,
    "tbsp": 0.01478676478125, "tablespoon": 0.01478676478125, "tablespoons": 0.01478676478125,
    "tsp": 0.00492892159375, "teaspoon": 0.00492892159375, "teaspoons": 0.00492892159375,
}

# ── Temperature (special — not linear) ──────────────────────────────────
_TEMP_FUNCTIONS: dict[str, dict[str, Any]] = {
    "c": {"celsius": "same", "f": "c_to_f", "fahrenheit": "c_to_f", "k": "c_to_k", "kelvin": "c_to_k"},
    "f": {"fahrenheit": "same", "c": "f_to_c", "celsius": "f_to_c", "k": "f_to_k", "kelvin": "f_to_k"},
    "k": {"kelvin": "same", "c": "k_to_c", "celsius": "k_to_c", "f": "k_to_f", "fahrenheit": "k_to_f"},
}

# ── Area (base: square meter) ───────────────────────────────────────────
_AREA: dict[str, float] = {
    "m2": 1.0, "sq_m": 1.0, "square_meter": 1.0, "square_meters": 1.0,
    "km2": 1e6, "sq_km": 1e6, "square_kilometer": 1e6,
    "ha": 10000.0, "hectare": 10000.0, "hectares": 10000.0,
    "ac": 4046.8564224, "acre": 4046.8564224, "acres": 4046.8564224,
    "ft2": 0.09290304, "sq_ft": 0.09290304, "square_foot": 0.09290304, "square_feet": 0.09290304,
    "in2": 0.00064516, "sq_in": 0.00064516, "square_inch": 0.00064516, "square_inches": 0.00064516,
}

# ── Speed (base: m/s) ───────────────────────────────────────────────────
_SPEED: dict[str, float] = {
    "m/s": 1.0, "meter_per_second": 1.0, "meters_per_second": 1.0,
    "km/h": 0.2777777778, "kph": 0.2777777778, "kilometer_per_hour": 0.2777777778,
    "mph": 0.44704, "mile_per_hour": 0.44704, "miles_per_hour": 0.44704,
    "knot": 0.5144444444, "kn": 0.5144444444, "knots": 0.5144444444,
}

# ── Data storage (base: byte) ───────────────────────────────────────────
_DATA: dict[str, float] = {
    "b": 1.0, "byte": 1.0, "bytes": 1.0,
    "kb": 1000.0, "kilobyte": 1000.0, "kilobytes": 1000.0,
    "mb": 1e6, "megabyte": 1e6, "megabytes": 1e6,
    "gb": 1e9, "gigabyte": 1e9, "gigabytes": 1e9,
    "tb": 1e12, "terabyte": 1e12, "terabytes": 1e12,
    "kib": 1024.0, "kibibyte": 1024.0,
    "mib": 1048576.0, "mebibyte": 1048576.0,
    "gib": 1073741824.0, "gibibyte": 1073741824.0,
}

# ── Time (base: second) ─────────────────────────────────────────────────
_TIME: dict[str, float] = {
    "s": 1.0, "sec": 1.0, "second": 1.0, "seconds": 1.0,
    "ms": 0.001, "millisecond": 0.001, "milliseconds": 0.001,
    "min": 60.0, "minute": 60.0, "minutes": 60.0,
    "h": 3600.0, "hr": 3600.0, "hour": 3600.0, "hours": 3600.0,
    "d": 86400.0, "day": 86400.0, "days": 86400.0,
    "wk": 604800.0, "week": 604800.0, "weeks": 604800.0,
    "mo": 2629800.0, "month": 2629800.0, "months": 2629800.0,
    "yr": 31557600.0, "year": 31557600.0, "years": 31557600.0,
}

_CATEGORIES: dict[str, dict[str, float]] = {
    "length": _LENGTH, "mass": _MASS, "weight": _MASS,
    "volume": _VOLUME, "area": _AREA, "speed": _SPEED,
    "data": _DATA, "time": _TIME, "temperature": {},
}


def _find_unit(unit: str) -> tuple[str, dict[str, float]]:
    key = unit.lower().strip().replace(" ", "_")
    for name, table in _CATEGORIES.items():
        if name == "temperature":
            continue
        if key in table:
            return name, table
    return "", {}


class UnitConvertTool(Tool):
    name = "unit_convert"
    capabilities = frozenset({ToolCapability.COMPUTE})
    description = (
        "Convert between units of measurement. "
        "Supports length, mass/weight, volume, area, speed, data storage, time, and temperature. "
        'Provide the amount and source/target units (e.g. amount=5, from="miles", to="km"). '
        "For temperature, use 'C', 'F', or 'K'."
    )
    when_to_use = "To convert between units of measurement (length, weight, temperature, volume, speed, data, etc.)."
    how_to_use = "Provide amount, source unit, and target unit (e.g., amount=5, from='miles', to='km')."
    parameters = {
        "type": "object",
        "properties": {
            "amount": {
                "type": "number",
                "description": "The numeric value to convert.",
            },
            "from": {
                "type": "string",
                "description": "Source unit (e.g. 'miles', 'kg', 'C', 'gallons').",
            },
            "to": {
                "type": "string",
                "description": "Target unit (e.g. 'km', 'lbs', 'F', 'liters').",
            },
        },
        "required": ["amount", "from", "to"],
    }

    async def execute(self, amount: float, **kwargs: Any) -> str:
        from_unit = str(kwargs.get("from", "")).strip()
        to_unit = str(kwargs.get("to", "")).strip()

        if not from_unit or not to_unit:
            return "Error: both 'from' and 'to' units are required."

        # Temperature uses special functions
        temp_result = _convert_temperature(amount, from_unit, to_unit)
        if temp_result is not None:
            return temp_result

        # Standard linear conversion
        from_cat, from_table = _find_unit(from_unit)
        if not from_table:
            return (
                f"Unknown unit: '{from_unit}'. "
                f"Supported categories: {', '.join(sorted(_CATEGORIES.keys()))}."
            )

        _, to_table = _find_unit(to_unit)
        if from_table is not to_table:
            return f"Cannot convert between different categories: '{from_unit}' and '{to_unit}'."

        from_key = from_unit.lower().strip().replace(" ", "_")
        to_key = to_unit.lower().strip().replace(" ", "_")

        from_factor = from_table.get(from_key)
        to_factor = from_table.get(to_key)

        if from_factor is None:
            return f"Unknown source unit: '{from_unit}'."
        if to_factor is None:
            return f"Unknown target unit: '{to_unit}'."

        base = amount * from_factor
        result = base / to_factor

        return f"{amount} {from_unit} = {result:g} {to_unit}"


def _convert_temperature(amount: float, from_u: str, to_u: str) -> str | None:
    from_key = from_u.lower().strip()
    to_key = to_u.lower().strip()

    if from_key not in _TEMP_FUNCTIONS or to_key not in _TEMP_FUNCTIONS.get(from_key, {}):
        return None  # Not a temperature conversion

    mapping = _TEMP_FUNCTIONS[from_key]
    fn = mapping.get(to_key)
    if fn is None:
        return f"Unknown temperature unit: '{to_u}'."

    if fn == "same":
        result = amount
    elif fn == "c_to_f":
        result = amount * 9 / 5 + 32
    elif fn == "f_to_c":
        result = (amount - 32) * 5 / 9
    elif fn == "c_to_k":
        result = amount + 273.15
    elif fn == "k_to_c":
        result = amount - 273.15
    elif fn == "f_to_k":
        result = (amount - 32) * 5 / 9 + 273.15
    elif fn == "k_to_f":
        result = (amount - 273.15) * 9 / 5 + 32
    else:
        return f"Temperature conversion '{from_u}' → '{to_u}' not supported."

    return f"{amount}°{from_u.upper()} = {result:.2f}°{to_u.upper()}"
