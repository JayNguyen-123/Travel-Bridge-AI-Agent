# tools/hotel_tools.py
import asyncio
import os

import requests

from middleware.translator import process_and_convert_all

AMADEUS_BASE_URL = os.environ.get("AMADEUS_BASE_URL", "https://test.api.amadeus.com")


def _search_hotels_sync(hotelIds: str, checkInDate: str, adults: int = 1, roomQuantity: int = 1) -> dict:
    token = os.environ.get("AMADEUS_ACCESS_TOKEN")
    if not token:
        return {"error": "AMADEUS_ACCESS_TOKEN environment variable is missing."}

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    params = {"hotelIds": hotelIds, "checkInDate": checkInDate, "adults": adults, "roomQuantity": roomQuantity}

    try:
        response = requests.get(f"{AMADEUS_BASE_URL}/v3/shopping/hotel-offers", headers=headers, params=params, timeout=10)
        if response.status_code != 200:
            return {"error": f"Amadeus API returned status code {response.status_code}", "details": response.text}
        raw_json = response.json()
        return process_and_convert_all(raw_json)
    except requests.exceptions.RequestException as e:
        return {"error": "Failed to connect to Amadeus Hotel service", "details": str(e)}


async def search_hotels(hotelIds: str, checkInDate: str, adults: int = 1, roomQuantity: int = 1) -> dict:
    """
    Tra cứu phòng khách sạn trực tiếp từ API Amadeus và xử lý đồng thời qua middleware.

    Args:
        hotelIds (str): Chuỗi ID khách sạn phân tách bằng dấu phẩy (VD: MCLONGPAR,CYPARIS4).
        checkInDate (str): Ngày nhận phòng định dạng YYYY-MM-DD.
        adults (int): Số lượng khách người lớn trên mỗi phòng.
        roomQuantity (int): Tổng số lượng phòng cần đặt.

    Returns:
        dict: Cấu trúc dữ liệu phòng đã được dịch thuật và quy đổi song song sang VND/USD.
        Mỗi offer có trường policies.paymentType (GUARANTEE/DEPOSIT/PREPAY) --
        chỉ offer GUARANTEE mới có thể đặt qua confirm_hotel_booking (xem
        tools/hotel_booking_tools.py); DEPOSIT/PREPAY cần request_human_support.
    """
    return await asyncio.to_thread(_search_hotels_sync, hotelIds, checkInDate, adults, roomQuantity)
