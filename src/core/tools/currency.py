"""Currency conversion tool — live exchange rates via open.er-api.com (free, no key)."""

from __future__ import annotations

from typing import Any

import httpx

from core.tool_framework import Tool

_API_URL = "https://open.er-api.com/v6/latest"


class CurrencyTool(Tool):
    name = "currency_convert"
    description = (
        "Convert an amount between currencies using live exchange rates. "
        "Provide the amount, source currency, and target currency. "
        "Currency codes are 3-letter ISO codes like USD, EUR, GBP, JPY, SGD. "
        'Example: amount=100, from="USD", to="EUR"'
    )
    parameters = {
        "type": "object",
        "properties": {
            "amount": {
                "type": "number",
                "description": "The amount to convert.",
            },
            "from": {
                "type": "string",
                "description": "Source currency code (3-letter ISO, e.g. USD).",
            },
            "to": {
                "type": "string",
                "description": "Target currency code (3-letter ISO, e.g. EUR).",
            },
        },
        "required": ["amount", "from", "to"],
    }

    async def execute(self, amount: float, **kwargs: Any) -> str:
        from_cur = str(kwargs.get("from", "")).upper().strip()
        to_cur = str(kwargs.get("to", "")).upper().strip()

        if not from_cur or not to_cur:
            return "Error: both 'from' and 'to' currency codes are required."

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{_API_URL}/{from_cur}")
                resp.raise_for_status()
                data = resp.json()

                if data.get("result") != "success":
                    return f"Error fetching rates: {data.get('error-type', 'unknown error')}"

                rates = data.get("rates", {})
                target_rate = rates.get(to_cur)
                if target_rate is None:
                    return f"Unknown target currency: {to_cur}"

                converted = amount * target_rate
                base_rate = rates.get(from_cur, 1.0)

                lines = [
                    f"{amount:.2f} {from_cur} = {converted:.4f} {to_cur}",
                    f"Rate: 1 {from_cur} = {target_rate:.6f} {to_cur}",
                    f"Updated: {data.get('time_last_update_utc', 'unknown')}",
                ]
                return "\n".join(lines)

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return f"Unknown source currency: {from_cur}"
            return f"Currency API error (HTTP {e.response.status_code})"
        except Exception as e:
            return f"Currency conversion failed: {e}"
