# tools/flight_tools.py
import asyncio
import json
import os

import requests

from middleware.translator import process_and_convert_all

# Defaults to the Amadeus sandbox host. Set AMADEUS_BASE_URL=https://api.amadeus.com
# in production once you've completed Amadeus's move-to-production process.
AMADEUS_BASE_URL = os.environ.get("AMADEUS_BASE_URL", "https://test.api.amadeus.com")

# Enforced by this app independently of whatever Amadeus's own per-request
# passenger limit is (that varies by contract/tier and isn't documented
# anywhere this app can check at runtime) -- so an oversized group gets one
# clear message from us at search time, before the customer collects an
# offer they won't be able to book. Larger parties are meant to go through
# request_human_support instead (see tools/booking_tools.py's matching
# MAX_PARTY_SIZE, enforced again at booking time).
MAX_PARTY_SIZE = int(os.environ.get("MAX_PARTY_SIZE", "9"))

# Amadeus's own documented cap on originDestinations entries in one Flight
# Offers Search (POST) request -- see search_multi_city_flights below.
MAX_MULTI_CITY_LEGS = int(os.environ.get("MAX_MULTI_CITY_LEGS", "6"))


def _search_flights_sync(originLocationCode: str, destinationLocationCode: str, departureDate: str,
                          adults: int = 1, children: int = 0, infants: int = 0,
                          returnDate: str = None) -> dict:
    token = os.environ.get("AMADEUS_ACCESS_TOKEN")
    if not token:
        return {"error": "AMADEUS_ACCESS_TOKEN environment variable is missing."}

    party_size = adults + children + infants
    if party_size > MAX_PARTY_SIZE:
        return {
            "error": f"This system supports searching/booking up to {MAX_PARTY_SIZE} travelers at "
                     f"once ({party_size} requested). For a larger group, please use "
                     f"request_human_support instead."
        }

    if infants > adults:
        # Amadeus (and every airline) requires each infant to be accompanied
        # by an adult -- catch this before spending a request on it.
        return {"error": "Each infant traveler must be accompanied by an adult -- infants cannot exceed adults."}

    if returnDate and returnDate < departureDate:
        # Cheap to catch here -- Amadeus would otherwise just return zero
        # offers for a nonsensical date range, which reads to the agent (and
        # then the traveler) as "no flights available" rather than "you
        # mixed up your dates."
        return {"error": "returnDate cannot be before departureDate."}

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    params = {
        "originLocationCode": originLocationCode.upper(),
        "destinationLocationCode": destinationLocationCode.upper(),
        "departureDate": departureDate,
        "adults": adults,
    }
    if children:
        params["children"] = children
    if infants:
        params["infants"] = infants
    if returnDate:
        # Amadeus's own param name -- supplying it turns each result into a
        # round-trip offer (itineraries[0]=outbound, itineraries[1]=return),
        # priced and booked as a single combined fare (see the "who sends
        # the itinerary" notification code in tools/booking_tools.py, which
        # already flattens every itinerary/segment, not just the first).
        params["returnDate"] = returnDate

    try:
        response = requests.get(f"{AMADEUS_BASE_URL}/v2/shopping/flight-offers", headers=headers, params=params, timeout=10)
        if response.status_code != 200:
            return {"error": f"Amadeus API returned status code {response.status_code}", "details": response.text}
        raw_json = response.json()
        return process_and_convert_all(raw_json)
    except requests.exceptions.RequestException as e:
        return {"error": "Failed to connect to Amadeus Flight service", "details": str(e)}


async def search_flights(originLocationCode: str, destinationLocationCode: str, departureDate: str,
                          adults: int = 1, children: int = 0, infants: int = 0,
                          returnDate: str = None) -> dict:
    """
    Tra cứu vé máy bay trực tiếp từ API Amadeus và xử lý đồng thời qua middleware.
    Hỗ trợ đặt vé nhóm/gia đình: chỉ định đủ số người lớn, trẻ em, và em bé --
    giá trả về (`price.total`) là TỔNG giá cho cả nhóm, không phải giá mỗi người.
    Hỗ trợ vé khứ hồi (round-trip): truyền thêm `returnDate` để nhận về một vé
    duy nhất gồm cả hai chặng (đi và về), với MỘT giá tổng duy nhất -- không
    phải tìm hai vé một chiều riêng biệt. Bỏ trống `returnDate` cho vé một chiều.

    Args:
        originLocationCode (str): Mã IATA 3 ký tự nơi đi (VD: SGN, DFW).
        destinationLocationCode (str): Mã IATA 3 ký tự nơi đến (VD: CDG, NRT).
        departureDate (str): Ngày khởi hành định dạng YYYY-MM-DD.
        adults (int): Số lượng hành khách người lớn (Từ 12 tuổi trở lên).
        children (int): Số lượng trẻ em (2-11 tuổi). Mặc định 0.
        infants (int): Số lượng em bé (dưới 2 tuổi, ngồi cùng người lớn).
            Không được vượt quá số người lớn. Mặc định 0.
            Tổng số hành khách (adults + children + infants) tối đa là
            MAX_PARTY_SIZE (mặc định 9); nhóm lớn hơn cần dùng
            request_human_support.
        returnDate (str, optional): Ngày khởi hành CHIỀU VỀ, định dạng
            YYYY-MM-DD. Nếu có, kết quả trả về sẽ là vé khứ hồi (round-trip):
            mỗi offer chứa itineraries[0] = chặng đi, itineraries[1] = chặng
            về, và price.total là giá gộp cho CẢ HAI chặng. Bỏ trống (None)
            để tìm vé một chiều như trước. Phải >= departureDate.

    Returns:
        dict: Cấu trúc dữ liệu đã được dịch thuật và quy đổi tiền tệ song song
        sang VND/USD. `price.total` là tổng giá cho toàn bộ nhóm đã yêu cầu
        (adults + children + infants) VÀ cho toàn bộ hành trình (một chiều,
        hoặc cả đi lẫn về nếu có returnDate), sẵn sàng để tính vào
        create_payment_checkout như một lần thanh toán duy nhất.
    """
    # Wrapped in asyncio.to_thread so a slow Amadeus response never blocks the
    # live audio event loop for other concurrent voice sessions.
    return await asyncio.to_thread(
        _search_flights_sync, originLocationCode, destinationLocationCode, departureDate,
        adults, children, infants, returnDate,
    )


# ---------------------------------------------------------------------------
# Multi-city search
#
# A genuine multi-city trip (3+ distinct destinations, or an "open-jaw" trip
# that doesn't return to its starting city) can't be expressed with the
# simple GET /v2/shopping/flight-offers search above -- that endpoint only
# takes one origin/destination pair plus an optional single returnDate back
# to the origin. Amadeus's documented way to search multiple legs in one
# request is the POST variant of the same Flight Offers Search resource,
# with a JSON body listing one `originDestinations` entry per leg instead of
# query params. This was built and manually verified against Amadeus's
# documented request/response shape, but -- like the traveler-id contract in
# tools/booking_tools.py -- could not be freshly re-verified against a live
# Amadeus account in this network-restricted sandbox; see
# PRODUCTION_CHECKLIST.md before trusting it with a real booking.
# ---------------------------------------------------------------------------

def _build_multi_city_search_body(legs: list, adults: int, children: int = 0, infants: int = 0) -> dict:
    """Builds the POST /v2/shopping/flight-offers request body Amadeus's
    multi-city search expects: one `originDestinations` entry per leg (in
    travel order), and a `travelers` array with one {"id", "travelerType"}
    slot per passenger, built in the same adults-then-children-then-infants
    convention `_traveler_pricing_slots` (tools/booking_tools.py) relies on
    when booking. Pure/no I/O -- shared between this file's multi-city
    search tool and Tier 2's multi-city rebook re-search in
    tools/booking_tools.py, so the request shape is defined in exactly one
    place. `legs` is a list of {"origin", "destination", "date"} dicts."""
    origin_destinations = [
        {
            "id": str(i + 1),
            "originLocationCode": leg["origin"].upper(),
            "destinationLocationCode": leg["destination"].upper(),
            "departureDateTimeRange": {"date": leg["date"]},
        }
        for i, leg in enumerate(legs)
    ]
    travelers = []
    next_id = 1
    for traveler_type, count in (("ADULT", adults), ("CHILD", children), ("HELD_INFANT", infants)):
        for _ in range(count):
            travelers.append({"id": str(next_id), "travelerType": traveler_type})
            next_id += 1
    return {
        "originDestinations": origin_destinations,
        "travelers": travelers,
        "sources": ["GDS"],
    }


def _post_multi_city_search(legs: list, adults: int, children: int, infants: int, token: str):
    """One raw POST call to Amadeus's multi-city Flight Offers Search.
    Returns (raw_json, None) on success, or (None, error_dict) on failure --
    deliberately no currency conversion/translation here, so this can be
    reused as-is by Tier 2's rebook re-search (tools/booking_tools.py), which
    only needs raw offers to compare prices, same as its existing one-way/
    round-trip re-search path never calls process_and_convert_all either."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        # Amadeus's documentation for the POST form of this endpoint calls
        # for this override header (the endpoint is semantically a search/
        # read, just with a body too large for query params) -- flagged as
        # unverified against a live account above.
        "X-HTTP-Method-Override": "GET",
    }
    body = _build_multi_city_search_body(legs, adults, children, infants)
    try:
        response = requests.post(f"{AMADEUS_BASE_URL}/v2/shopping/flight-offers", headers=headers, json=body, timeout=15)
    except requests.exceptions.RequestException as e:
        return None, {"error": "Failed to connect to Amadeus Flight service", "details": str(e)}
    if response.status_code != 200:
        return None, {"error": f"Amadeus API returned status code {response.status_code}", "details": response.text}
    return response.json(), None


def _search_multi_city_flights_sync(legs: list, adults: int = 1, children: int = 0, infants: int = 0) -> dict:
    token = os.environ.get("AMADEUS_ACCESS_TOKEN")
    if not token:
        return {"error": "AMADEUS_ACCESS_TOKEN environment variable is missing."}

    if not isinstance(legs, list) or len(legs) < 2:
        return {
            "error": "Multi-city search needs at least 2 legs. For a single route -- with or "
                     "without a return to the same city -- use search_flights instead."
        }
    if len(legs) > MAX_MULTI_CITY_LEGS:
        return {"error": f"This system supports up to {MAX_MULTI_CITY_LEGS} legs in one multi-city search ({len(legs)} requested)."}

    for i, leg in enumerate(legs):
        if not isinstance(leg, dict) or not all(leg.get(k) for k in ("origin", "destination", "date")):
            return {"error": f"Leg {i + 1} is missing an origin, destination, or date."}

    for i in range(1, len(legs)):
        if legs[i]["date"] < legs[i - 1]["date"]:
            return {
                "error": f"Leg {i + 1}'s date ({legs[i]['date']}) is before leg {i}'s date "
                         f"({legs[i - 1]['date']}) -- legs must be given in chronological order."
            }

    party_size = adults + children + infants
    if party_size > MAX_PARTY_SIZE:
        return {
            "error": f"This system supports searching/booking up to {MAX_PARTY_SIZE} travelers at "
                     f"once ({party_size} requested). For a larger group, please use "
                     f"request_human_support instead."
        }
    if infants > adults:
        return {"error": "Each infant traveler must be accompanied by an adult -- infants cannot exceed adults."}

    raw_json, error = _post_multi_city_search(legs, adults, children, infants, token)
    if error:
        return error
    return process_and_convert_all(raw_json)


async def search_multi_city_flights(legs_json_str: str, adults: int = 1, children: int = 0, infants: int = 0) -> dict:
    """
    Tra cứu vé máy bay NHIỀU CHẶNG (multi-city) -- ví dụ SGN→BKK ngày 1/7, BKK→NRT ngày 5/7,
    NRT→SGN ngày 10/7: ba điểm đến khác nhau trong một hành trình, KHÔNG phải vé khứ hồi đơn
    giản (dùng search_flights với returnDate cho khứ hồi hai chặng đi/về cùng một điểm đến).
    Toàn bộ hành trình nhiều chặng được tìm, đặt, và thanh toán như MỘT giao dịch duy nhất với
    MỘT giá tổng duy nhất -- không phải tìm/đặt từng chặng riêng lẻ.

    Args:
        legs_json_str (str): Chuỗi JSON là một MẢNG các chặng bay, mỗi chặng có dạng
            {"origin": "SGN", "destination": "BKK", "date": "2026-07-01"}, liệt kê ĐÚNG theo
            thứ tự thời gian di chuyển (chặng sau không được có ngày sớm hơn chặng trước). Tối
            thiểu 2 chặng -- một chặng thì dùng search_flights thay vì hàm này. Tối đa
            MAX_MULTI_CITY_LEGS chặng (mặc định 6, đúng giới hạn của chính Amadeus).
        adults, children, infants: giống search_flights -- áp dụng cho TOÀN BỘ hành trình nhiều
            chặng, không phải riêng từng chặng (không hỗ trợ đổi số hành khách giữa các chặng,
            và không hỗ trợ hạng ghế khác nhau cho từng chặng).

    Returns:
        dict: cùng cấu trúc dữ liệu như search_flights (đã dịch thuật + quy đổi tiền tệ song
        song sang VND/USD). Mỗi offer's `itineraries` có đúng số phần tử bằng số chặng đã tìm,
        theo đúng thứ tự đã cung cấp trong legs_json_str. `price.total` là giá gộp cho TOÀN BỘ
        hành trình nhiều chặng, sẵn sàng để tính vào create_payment_checkout như một lần thanh
        toán duy nhất, giống hệt vé một chiều/khứ hồi.
    """
    try:
        legs = json.loads(legs_json_str)
    except (json.JSONDecodeError, TypeError) as e:
        return {"error": f"legs_json_str must be a valid JSON array of legs: {e}"}
    # Wrapped in asyncio.to_thread so a slow Amadeus response never blocks the
    # live audio event loop for other concurrent voice sessions.
    return await asyncio.to_thread(_search_multi_city_flights_sync, legs, adults, children, infants)
