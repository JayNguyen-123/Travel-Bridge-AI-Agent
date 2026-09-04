# notifications/email.py
"""
Shared SendGrid email helper for the itinerary/e-ticket-style confirmation
sent after a real booking succeeds. Mirrors notifications/sms.py's pattern
(lazy client init from env vars, defensive try/except, never raises) so
booking_tools.py can send email without a circular import, and so a failed
email -- SendGrid outage, bad address, missing credentials -- never breaks a
booking that already succeeded. Every function here is purely a
notification/display concern: nothing in this module ever derives or
touches a charge amount; total_price/currency shown below are for display
only, already charged via Stripe (or, for GUARANTEE-only hotel bookings,
never charged at all) before this email is ever sent.
"""
import os

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

BRAND_NAME = "TravelBridge AI"
BRAND_PRIMARY = "#065A82"
BRAND_JADE = "#1FA37A"
BRAND_DARK = "#0E1A3A"
BRAND_MUTED = "#5C6B7A"


def send_email(to_email: str, subject: str, html_body: str, plain_body: str = "") -> dict:
    """Sends an HTML email via SendGrid. Returns {'sent': bool, ...}. Never
    raises -- callers should treat a failed email as a warning, not an error,
    since the booking itself already happened by the time this is called."""
    api_key = os.environ.get("SENDGRID_API_KEY")
    from_email = os.environ.get("SENDGRID_FROM_EMAIL")

    if not all([api_key, from_email]):
        print("[Email Warning]: SendGrid credentials missing. Skipping email dispatch.")
        return {"sent": False, "reason": "sendgrid_credentials_missing"}
    if not to_email:
        print("[Email Warning]: No recipient email address on file. Skipping email dispatch.")
        return {"sent": False, "reason": "no_recipient_email"}

    try:
        message = Mail(
            from_email=from_email,
            to_emails=to_email,
            subject=subject,
            html_content=html_body,
            plain_text_content=plain_body or subject,
        )
        client = SendGridAPIClient(api_key)
        response = client.send(message)
        print(f"[Email Success]: Message sent via SendGrid to {to_email}. Status: {response.status_code}")
        return {"sent": True, "status_code": response.status_code}
    except Exception as e:
        print(f"[Email Error]: Failed to send notification via SendGrid: {e}")
        return {"sent": False, "reason": str(e)}


def _itinerary_rows_html(itinerary_legs: list) -> str:
    if not itinerary_legs:
        return (
            f'<tr><td style="padding:12px 16px;color:{BRAND_MUTED};font-size:14px;">'
            "Itinerary details were unavailable at booking time -- your confirmation code above "
            "is still valid; contact support if you need the flight schedule reissued."
            "</td></tr>"
        )
    rows = []
    for leg in itinerary_legs:
        flight_code = f"{leg.get('carrier_code', '')}{leg.get('flight_number', '')}".strip() or "--"
        rows.append(
            '<tr style="border-bottom:1px solid #E4DFD4;">'
            f'<td style="padding:10px 16px;font-size:14px;color:{BRAND_DARK};font-weight:600;">{flight_code}</td>'
            f'<td style="padding:10px 16px;font-size:14px;color:{BRAND_DARK};">{leg.get("origin", "?")} &rarr; {leg.get("destination", "?")}</td>'
            f'<td style="padding:10px 16px;font-size:13px;color:{BRAND_MUTED};">{leg.get("departure_display", leg.get("departure_at", "?"))}</td>'
            f'<td style="padding:10px 16px;font-size:13px;color:{BRAND_MUTED};">{leg.get("arrival_display", leg.get("arrival_at", "?"))}</td>'
            "</tr>"
        )
    return "".join(rows)


def _traveler_list_html(travelers: list) -> str:
    if not travelers:
        return ""
    items = "".join(
        f'<li style="margin-bottom:4px;">{t.get("first_name", "")} {t.get("last_name", "")}</li>'
        for t in travelers
    )
    return f'<ul style="margin:8px 0 0;padding-left:20px;color:{BRAND_DARK};font-size:14px;">{items}</ul>'


def send_flight_itinerary_email(
    to_email: str,
    traveler_name: str,
    reference_code: str,
    itinerary_legs: list,
    travelers: list,
    total_price: str,
    currency: str,
) -> dict:
    """Builds and sends a simple HTML e-ticket-style itinerary email after a
    real Amadeus flight booking succeeds. `itinerary_legs` is the flattened
    per-segment list from tools/booking_tools.py's _flight_itinerary_legs
    (already display-formatted -- this function does no Amadeus-shape
    parsing of its own, matching how notifications/sms.py never parses an
    offer either)."""
    subject = f"Xác nhận vé máy bay / Flight Confirmation -- {reference_code}"

    itinerary_html = _itinerary_rows_html(itinerary_legs)
    traveler_html = _traveler_list_html(travelers)

    html_body = f"""
    <div style="font-family:Arial,Helvetica,sans-serif;max-width:600px;margin:0 auto;background:#FFFFFF;">
      <div style="background:{BRAND_DARK};padding:24px 28px;">
        <span style="color:#FFFFFF;font-size:18px;font-weight:700;">{BRAND_NAME}</span>
      </div>
      <div style="padding:28px;">
        <p style="font-size:15px;color:{BRAND_DARK};margin:0 0 4px;">Chào {traveler_name} / Hi {traveler_name},</p>
        <p style="font-size:15px;color:{BRAND_DARK};margin:0 0 20px;">
          Vé máy bay của bạn đã được xác nhận. / Your flight is confirmed.
        </p>
        <div style="background:#F6F1E9;border-radius:10px;padding:16px 20px;margin-bottom:24px;">
          <span style="font-size:12px;letter-spacing:0.08em;text-transform:uppercase;color:{BRAND_MUTED};">
            Confirmation code / Mã xác nhận
          </span><br/>
          <span style="font-size:22px;font-weight:700;color:{BRAND_PRIMARY};">{reference_code}</span>
        </div>

        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-bottom:24px;">
          <thead>
            <tr style="border-bottom:2px solid {BRAND_DARK};">
              <th style="text-align:left;padding:8px 16px;font-size:12px;color:{BRAND_MUTED};text-transform:uppercase;">Flight</th>
              <th style="text-align:left;padding:8px 16px;font-size:12px;color:{BRAND_MUTED};text-transform:uppercase;">Route</th>
              <th style="text-align:left;padding:8px 16px;font-size:12px;color:{BRAND_MUTED};text-transform:uppercase;">Departs</th>
              <th style="text-align:left;padding:8px 16px;font-size:12px;color:{BRAND_MUTED};text-transform:uppercase;">Arrives</th>
            </tr>
          </thead>
          <tbody>{itinerary_html}</tbody>
        </table>

        <p style="font-size:13px;color:{BRAND_MUTED};margin:0 0 4px;text-transform:uppercase;letter-spacing:0.06em;">
          Travelers / Hành khách
        </p>
        {traveler_html}

        <p style="font-size:14px;color:{BRAND_DARK};margin:20px 0 0;">
          Total paid / Tổng đã thanh toán: <strong>{total_price} {currency}</strong>
        </p>

        <p style="font-size:12px;color:{BRAND_MUTED};margin-top:28px;border-top:1px solid #E4DFD4;padding-top:16px;">
          This confirms your flight booking with {BRAND_NAME}. Times shown are local to each airport.
          Keep your confirmation code handy for check-in or if you need to contact support.<br/>
          Đây là xác nhận đặt vé máy bay của bạn với {BRAND_NAME}. Vui lòng lưu mã xác nhận để làm thủ tục
          hoặc liên hệ hỗ trợ khi cần.
        </p>
      </div>
    </div>
    """

    plain_lines = [
        f"{BRAND_NAME} -- Flight Confirmation",
        f"Traveler: {traveler_name}",
        f"Confirmation code: {reference_code}",
        "",
        "Itinerary:",
    ]
    for leg in itinerary_legs:
        flight_code = f"{leg.get('carrier_code', '')}{leg.get('flight_number', '')}".strip() or "--"
        plain_lines.append(
            f"  {flight_code} {leg.get('origin', '?')} -> {leg.get('destination', '?')} "
            f"({leg.get('departure_display', leg.get('departure_at', '?'))} -> "
            f"{leg.get('arrival_display', leg.get('arrival_at', '?'))})"
        )
    plain_lines.append("")
    plain_lines.append(f"Total paid: {total_price} {currency}")
    plain_body = "\n".join(plain_lines)

    return send_email(to_email=to_email, subject=subject, html_body=html_body, plain_body=plain_body)
