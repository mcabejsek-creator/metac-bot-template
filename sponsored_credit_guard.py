"""Fail-closed GET-only check for Metaculus-funded OpenRouter credits.

The key is sponsor-provided, not associated with the operator's own OpenRouter
dashboard. No LLM call, forecast, journal operation, payment or API mutation.
This check is DORMANT until a separately approved automatic publisher exists.
"""

from decimal import Decimal, InvalidOperation
import json
import re
import urllib.error
import urllib.request


METACULUS_FUNDED_KEY_ENDPOINT = "https://openrouter.ai/api/v1/key"
MINIMUM_SPONSORED_RESERVE_USD = Decimal("5.00")
MAX_METADATA_BYTES = 16000


class SponsoredCreditUnavailable(RuntimeError):
    """Never start model research when sponsored funding cannot be verified."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise SponsoredCreditUnavailable("OpenRouter redirected; no funded-key check")


def _amount(value, label):
    if type(value) not in (int, float):
        raise SponsoredCreditUnavailable("Unverifiable sponsored " + label)
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise SponsoredCreditUnavailable("Invalid sponsored " + label) from None
    if not amount.is_finite() or amount < 0 or amount > Decimal("1000000"):
        raise SponsoredCreditUnavailable("Invalid sponsored " + label)
    return amount


def verify_sponsored_openrouter_credit(api_key, *, expected_suffix, opener=None):
    """Make exactly one read-only GET, with no retries and no secret logging.

    expected_suffix is a short, operator-verified fingerprint of the donated
    key and MUST be explicitly configured outside the repository. The
    identifier is a precaution against accidental personal-key substitution,
    not authentication or cryptographic proof of ownership.
    """
    if not isinstance(api_key, str):
        raise SponsoredCreditUnavailable("Missing funded OpenRouter credential")
    key = api_key.strip()
    if not key.startswith("sk-or-v1-") or len(key) < 25 or any(c.isspace() for c in key):
        raise SponsoredCreditUnavailable("Invalid funded OpenRouter credential")
    if not isinstance(expected_suffix, str) or not re.fullmatch(r"[A-Za-z0-9]{3}", expected_suffix):
        raise SponsoredCreditUnavailable("Donated-key identity not approved")
    if not key.endswith(expected_suffix):
        raise SponsoredCreditUnavailable("Unexpected OpenRouter key; no personal-key fallback")

    request = urllib.request.Request(
        METACULUS_FUNDED_KEY_ENDPOINT,
        headers={
            "Authorization": "Bearer " + key,
            "Accept": "application/json",
            "User-Agent": "vrchcab-sponsored-credit-guard",
        },
        method="GET",
    )
    safe_opener = opener if opener is not None else urllib.request.build_opener(_NoRedirects())
    try:
        with safe_opener.open(request, timeout=15) as response:
            if response.status != 200:
                raise SponsoredCreditUnavailable("Funded-key GET returned unexpected status")
            raw = response.read(MAX_METADATA_BYTES + 1)
            if len(raw) > MAX_METADATA_BYTES:
                raise SponsoredCreditUnavailable("Funded-key metadata too large")
    except SponsoredCreditUnavailable:
        raise
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError):
        raise SponsoredCreditUnavailable("Funded-key GET failed; no retry") from None

    try:
        data = json.loads(raw)["data"]
        if not isinstance(data, dict):
            raise ValueError("Non-object data")
    except (KeyError, ValueError, TypeError, UnicodeDecodeError):
        raise SponsoredCreditUnavailable("Invalid funded-key metadata") from None

    masked_label = data.get("label")
    if (not isinstance(masked_label, str)
            or not re.search(r"\.\.\.[A-Za-z0-9]{" + str(len(expected_suffix)) + r"}$", masked_label)
            or not masked_label.endswith(expected_suffix)):
        raise SponsoredCreditUnavailable("Current-key masked identity mismatch")
    if data.get("include_byok_in_limit") is not True:
        raise SponsoredCreditUnavailable("Funded-key BYOK limit not confirmed")
    if data.get("is_management_key") is not False:
        raise SponsoredCreditUnavailable("Unexpected OpenRouter management key")

    ceiling = _amount(data.get("limit"), "credit limit")
    remaining = _amount(data.get("limit_remaining"), "remaining credits")
    used_byok = _amount(data.get("byok_usage"), "BYOK usage")
    if remaining > ceiling:
        raise SponsoredCreditUnavailable("Sponsored remaining balance exceeds credit ceiling")
    if remaining < MINIMUM_SPONSORED_RESERVE_USD:
        raise SponsoredCreditUnavailable("Sponsored reserve reached; no models")

    # Only non-sensitive accounting amounts, never the bearer token or label.
    return {
        "ceiling_usd": str(ceiling),
        "remaining_usd": str(remaining),
        "sponsored_byok_usage_usd": str(used_byok),
    }
