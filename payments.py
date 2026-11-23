"""UddoktaPay gateway client (optional "Instant Pay" with bKash / Nagad / Rocket).

Enabled only when both `uddoktapay_base_url` and `uddoktapay_api_key` are set in
Admin -> Settings. Without them the site uses manual Send Money + TrxID instead.

Sandbox for testing:  base URL https://sandbox.uddoktapay.com
                      API key  982d381360a69d419689740d9f2e26ce36fb7a50
"""
import requests


class GatewayError(Exception):
    pass


def enabled(settings):
    return bool((settings.get("uddoktapay_base_url") or "").strip()
                and (settings.get("uddoktapay_api_key") or "").strip())


def _base(settings):
    return settings["uddoktapay_base_url"].strip().rstrip("/")


def _headers(settings):
    return {
        "RT-UDDOKTAPAY-API-KEY": settings["uddoktapay_api_key"].strip(),
        "accept": "application/json",
        "content-type": "application/json",
    }


def create_checkout(settings, *, full_name, email, amount, metadata, redirect_url, cancel_url, webhook_url):
    """Start a payment and return the URL to send the customer to."""
    payload = {
        "full_name": full_name,
        "email": email,
        "amount": str(amount),
        "metadata": metadata,
        "redirect_url": redirect_url,
        "return_type": "GET",
        "cancel_url": cancel_url,
        "webhook_url": webhook_url,
    }
    try:
        r = requests.post(_base(settings) + "/api/checkout-v2", json=payload,
                          headers=_headers(settings), timeout=20)
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        raise GatewayError("পেমেন্ট গেটওয়ের সাথে সংযোগ করা যাচ্ছে না। কিছুক্ষণ পর চেষ্টা করুন।") from exc
    url = data.get("payment_url") if isinstance(data, dict) else None
    if not url:
        msg = data.get("message") if isinstance(data, dict) else None
        raise GatewayError(msg or "পেমেন্ট শুরু করা যায়নি।")
    return url


def verify(settings, invoice_id):
    """Ask the gateway for the real status of an invoice (never trust the browser)."""
    try:
        r = requests.post(_base(settings) + "/api/verify-payment", json={"invoice_id": invoice_id},
                          headers=_headers(settings), timeout=20)
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        raise GatewayError("পেমেন্ট যাচাই করা যায়নি।") from exc
    if not isinstance(data, dict):
        raise GatewayError("পেমেন্ট যাচাই করা যায়নি।")
    return data
