"""URL normalization, deduplication, and small shared helpers."""

import hashlib
import urllib.parse
from typing import Any

from gmas.config.logging import logger

# ============================================================
# Blank result dict
# ============================================================


def _empty_result(url: str = "", **extra: Any) -> dict[str, Any]:
    """Return a blank fetch-result dict."""
    result: dict[str, Any] = {
        "success": False,
        "url": url,
        "title": "",
        "content": "",
        "error": "",
    }
    result.update(extra)
    return result


# ============================================================
# URL normalization & deduplication
# ============================================================

_TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        # Google / general analytics
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "utm_cid",
        # Facebook / Meta
        "fbclid",
        "fb_action_ids",
        "fb_action_types",
        "fb_ref",
        "fb_source",
        # Google Ads / click IDs
        "gclid",
        "gclsrc",
        "dclid",
        "gbraid",
        "wbraid",
        # Microsoft / Bing
        "msclkid",
        # Twitter / X
        "twclid",
        # HubSpot
        "hsa_cam",
        "hsa_grp",
        "hsa_mt",
        "hsa_src",
        "hsa_ad",
        "hsa_acc",
        "hsa_net",
        "hsa_ver",
        "hsa_la",
        "hsa_ol",
        "hsa_kw",
        "hsa_tgt",
        # Mailchimp
        "mc_cid",
        "mc_eid",
        # Various
        "ref",
        "ref_src",
        "ref_url",
        "_ga",
        "_gl",
        "yclid",
        "spm",
        "scm",
    }
)


def normalize_url(url: str) -> str:
    """
    Normalize a URL for deduplication.

    The normalization pipeline:

    1. Lowercase the scheme and hostname.
    2. Remove default ports (80 for HTTP, 443 for HTTPS).
    3. Collapse ``//`` sequences and strip trailing ``/`` from the path.
    4. Remove common tracking query parameters.
    5. Sort remaining query parameters alphabetically.
    6. Remove the fragment (``#…``).
    """
    if not url:
        return ""

    url = url.strip()

    if not url.startswith(("http://", "https://", "//")):
        url = "https://" + url

    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return url.lower()

    # Lowercase scheme and host
    scheme = (parsed.scheme or "https").lower()
    netloc = (parsed.netloc or "").lower()

    # Remove default ports
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    # Normalize path
    path = parsed.path or "/"
    while "//" in path:
        path = path.replace("//", "/")
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    if not path:
        path = "/"

    # Filter and sort query parameters
    query = ""
    if parsed.query:
        params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        filtered = [(k, v) for k, v in params if k.lower() not in _TRACKING_PARAMS]
        if filtered:
            filtered.sort(key=lambda x: x[0])
            query = urllib.parse.urlencode(filtered)

    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def _url_signature(url: str) -> str:
    """Return a short hash key for a normalized URL."""
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()[:16]


def deduplicate_results(
    results: list[dict[str, str]],
) -> list[dict[str, str]]:
    """
    Remove duplicate search results based on normalized URL.

    Keeps the first occurrence (highest ranked by the provider).
    """
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for result in results:
        raw_url = result.get("url", "")
        if not raw_url:
            unique.append(result)
            continue
        norm = normalize_url(raw_url)
        if norm in seen:
            logger.debug("Dedup: dropping duplicate URL {} (normalized: {})", raw_url, norm)
            continue
        seen.add(norm)
        unique.append(result)
    return unique
