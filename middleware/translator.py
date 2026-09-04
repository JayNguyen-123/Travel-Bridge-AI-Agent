# middleware/translator.py
import re
import time
import requests

# --- CONFIGURATION & MULTI-CURRENCY CACHE ---
# STRING-VALUE translations only (e.g. a cabin-class code that shows up as a
# leaf value: "cabin": "ECONOMY" -> the value "ECONOMY" becomes "Hạng phổ
# thông"). This dictionary must NEVER be used to rename dict KEYS -- an
# earlier version of process_and_convert_all did exactly that (it used to
# also contain "price"/"currency"/"duration"/"cabin" as key-translation
# entries), which silently renamed the real Amadeus "price" and "currency"
# keys to "giá_tiền"/"tiền_tệ" recursively throughout every offer this
# module touches. Since search_flights/search_hotels's output is the ONLY
# copy of an offer the agent ever has, and the agent is instructed to pass
# that same offer JSON back into create_payment_checkout/confirm_flight_booking,
# that meant payments/stripe_client.py's price_from_flight_offer -- which
# reads offer["price"]["total"]/["currency"] -- received a dict with no
# "price" key at all and raised ValueError on every real booking attempt.
# See PRODUCTION_CHECKLIST.md and HOTEL_BOOKING_SCOPE.md section 4 for how
# this was found. Fixed by making key names immutable through this
# function; only leaf string values are ever translated.
AMADEUS_DICTIONARY = {
    "ECONOMY": "Hạng phổ thông",
    "PREMIUM_ECONOMY": "Hạng phổ thông đặc biệt",
    "BUSINESS": "Hạng thương gia",
    "FIRST": "Hạng nhất",
    "NON_REFUNDABLE": "Vé không hoàn huỷ",
    "REFUNDABLE": "Vé có thể hoàn huỷ",
}

CACHE_EXPIRY_SECONDS = 3600  # Sync currency indexes every hour
_rate_cache = {
    # validated fallback cross-rates in case the live api drops
    "to_vnd": {"USD": 26335.0, "EUR": 30581.0, "VND": 1.0},
    "to_usd": {"USD": 1.0, "EUR": 1.161, "VND": 0.000038},
    "last_fetched": 0.0,
}


def update_live_rates():
    """Fetches real-time base factors from ExchangeRate-API. Falls back to the
    cached rates above (and logs why) on any failure -- never raises."""
    current_time = time.time()
    if current_time - _rate_cache["last_fetched"] < CACHE_EXPIRY_SECONDS:
        return

    try:
        response = requests.get("https://er-api.com", timeout=3)
        if response.status_code == 200:
            data = response.json()
            rates_base_usd = data.get("rates", {})

            usd_to_vnd = rates_base_usd.get("VND", 26335.0)
            usd_to_eur = rates_base_usd.get("EUR", 0.861)

            _rate_cache["to_vnd"]["USD"] = usd_to_vnd
            _rate_cache["to_vnd"]["EUR"] = usd_to_vnd / usd_to_eur if usd_to_eur else 30581.0

            _rate_cache["to_usd"]["EUR"] = 1.0 / usd_to_eur if usd_to_eur else 1.161
            _rate_cache["to_usd"]["VND"] = 1.0 / usd_to_vnd

            _rate_cache["last_fetched"] = current_time
        else:
            print(f"[FX Warning]: rate provider returned status {response.status_code}, keeping cached rates.")
    except Exception as exc:
        print(f"[FX Warning]: live exchange rate refresh failed, keeping cached rates: {exc}")


def parse_iso_duration(duration_str: str) -> str:
    """Converts ISO 8601 strings (e.g. PT2H30M) to natural Vietnamese formatting."""
    if not isinstance(duration_str, str) or not duration_str.startswith("PT"):
        return duration_str
    hours = int(match.group(1)) if (match := re.search(r'(\d+)H', duration_str)) else 0
    minutes = int(match.group(1)) if (match := re.search(r'(\d+)M', duration_str)) else 0

    parts = []
    if hours > 0:
        parts.append(f"{hours} tiếng")
    if minutes > 0:
        parts.append(f"{minutes} phút")
    return " ".join(parts) if parts else "0 phút"


def process_and_convert_all(data):
    """Recursively maps through JSON structures, parsing time values and adding
    parallel USD and VND billing string pairs to price objects. This is purely
    a display/UX transform for what the agent speaks out loud: dict KEY NAMES
    are never altered (this used to rename "price"/"currency"/"duration"/
    "cabin" keys -- see AMADEUS_DICTIONARY's docstring on why that was a real
    bug and has been removed), so the offer's shape stays exactly what
    Amadeus sent. Only leaf STRING VALUES that match a known enum code (cabin
    class, refundability) get translated, and only converted_price_VND/USD
    are added alongside the untouched original price fields -- money handling
    (payments/stripe_client.py) always re-derives the charge amount from
    those original, unrenamed price fields, never from a translated copy."""
    update_live_rates()

    if isinstance(data, dict):
        if "currency" in data and ("total" in data or "price" in data or "amount" in data):
            src_currency = data.get("currency", "USD")
            target_key = "total" if "total" in data else ("price" if "price" in data else "amount")
            try:
                raw_amount = float(data[target_key])
                vnd_factor = _rate_cache["to_vnd"].get(src_currency, 1.0)
                usd_factor = _rate_cache["to_usd"].get(src_currency, 1.0)
                data["converted_price_VND"] = f"{raw_amount * vnd_factor:,.0f} VND".replace(",", ".")
                data["converted_price_USD"] = f"${raw_amount * usd_factor:,.2f}"
            except (ValueError, TypeError):
                pass

        new_dict = {}
        for key, value in data.items():
            if key == "duration" and isinstance(value, str):
                new_dict[key] = parse_iso_duration(value)
            else:
                new_dict[key] = process_and_convert_all(value)
        return new_dict

    elif isinstance(data, list):
        return [process_and_convert_all(item) for item in data]
    elif isinstance(data, str):
        return AMADEUS_DICTIONARY.get(data, data)

    return data
