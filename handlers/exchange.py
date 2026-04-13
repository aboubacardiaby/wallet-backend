"""
Exchange rate service
- Fetches rates from api.exchangerate-api.com (free tier, no API key)
- In-memory cache with 1-hour TTL
- XOF is pegged to EUR at 655.957 — derived automatically when not in upstream data
"""
import time
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

router = APIRouter(tags=["exchange"])

# ── Cache ────────────────────────────────────────────────────────────────────

_CACHE_TTL = 3600          # 1 hour
_cache: dict[str, dict] = {}   # {base: {"rates": {...}, "fetched_at": float}}

XOF_EUR_RATE = 655.957      # fixed peg (CFA franc / Euro)

POPULAR_CURRENCIES = ["USD", "EUR", "GBP", "XOF", "MAD", "DZD", "EGP",
                      "NGN", "GHS", "KES", "XAF", "CAD", "CHF", "JPY", "CNY",
                      "AED", "SAR", "INR", "BRL", "ZAR"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _inject_xof(rates: dict, base: str) -> dict:
    """Add XOF to a rates dict when the upstream API omits it."""
    if "XOF" in rates:
        return rates
    if base == "EUR":
        rates["XOF"] = XOF_EUR_RATE
    elif "EUR" in rates:
        rates["XOF"] = round(rates["EUR"] * XOF_EUR_RATE, 4)
    return rates


async def _fetch_rates(base: str) -> dict:
    """Return rates for *base*, using cache when fresh."""
    now = time.time()
    cached = _cache.get(base)
    if cached and now - cached["fetched_at"] < _CACHE_TTL:
        return cached

    # XOF is not a base currency in the free API — fetch via EUR and invert
    fetch_base = "EUR" if base == "XOF" else base
    url = f"https://api.exchangerate-api.com/v4/latest/{fetch_base}"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Exchange rate service unavailable: {exc}")

    raw_rates = data.get("rates", {})
    _inject_xof(raw_rates, fetch_base)

    if base == "XOF":
        # invert: 1 XOF = 1/655.957 EUR, then scale each rate
        eur_to_base = XOF_EUR_RATE          # 1 EUR = 655.957 XOF
        rates = {
            currency: round(value / eur_to_base, 8)
            for currency, value in raw_rates.items()
        }
        rates["XOF"] = 1.0
    else:
        rates = raw_rates
        rates[base] = 1.0   # self

    result = {
        "base": base,
        "rates": rates,
        "fetched_at": now,
        "source": "exchangerate-api.com",
    }
    _cache[base] = result
    return result


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/exchange/rates")
async def get_rates(
    base: str = Query(default="XOF", description="Base currency code"),
    popular_only: bool = Query(default=False, description="Return only popular currencies"),
):
    base = base.upper()
    data = await _fetch_rates(base)
    rates = data["rates"]

    if popular_only:
        rates = {k: v for k, v in rates.items() if k in POPULAR_CURRENCIES}

    return {
        "base": base,
        "rates": rates,
        "fetched_at": data["fetched_at"],
        "cache_expires_in": max(0, int(_CACHE_TTL - (time.time() - data["fetched_at"]))),
        "source": data["source"],
    }


@router.get("/exchange/convert")
async def convert(
    from_currency: str = Query(alias="from"),
    to_currency: str   = Query(alias="to"),
    amount: float      = Query(default=1.0, gt=0),
):
    from_currency = from_currency.upper()
    to_currency   = to_currency.upper()

    if from_currency == to_currency:
        return {"from": from_currency, "to": to_currency,
                "amount": amount, "result": amount, "rate": 1.0}

    data = await _fetch_rates(from_currency)
    rate = data["rates"].get(to_currency)
    if rate is None:
        raise HTTPException(status_code=400, detail=f"Unsupported currency: {to_currency}")

    return {
        "from": from_currency,
        "to": to_currency,
        "amount": amount,
        "result": round(amount * rate, 2),
        "rate": rate,
        "fetched_at": data["fetched_at"],
    }


@router.get("/exchange/popular")
async def popular_rates(base: str = Query(default="XOF")):
    """Rates for the most common currencies — used by dashboard ticker."""
    base = base.upper()
    data = await _fetch_rates(base)
    rates = {k: v for k, v in data["rates"].items() if k in POPULAR_CURRENCIES}
    return {
        "base": base,
        "rates": rates,
        "fetched_at": data["fetched_at"],
    }
