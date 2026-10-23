"""Reading a QR payload as text. Nothing here touches the network.

The payload is a string a stranger chose, so it is parsed defensively and
never acted on: no DNS lookup, no request, no redirect following. The module
only needs to know what the text *says* it points at.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from ..core.config import get
from .models import ParsedURL, PayloadKind

# A payload is treated as a URL only with an explicit scheme. "verify.test/c/1"
# is suggestive but ambiguous, and guessing would mean reporting a domain
# finding against a host nobody wrote.
_HAS_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_SCRIPT_PREFIXES = (
    "LATIN",
    "CYRILLIC",
    "GREEK",
    "ARABIC",
    "HEBREW",
    "HAN",
    "HIRAGANA",
    "KATAKANA",
    "HANGUL",
    "DEVANAGARI",
    "BENGALI",
    "TAMIL",
    "THAI",
    "ARMENIAN",
    "GEORGIAN",
)


def classify(payload: str) -> PayloadKind:
    """Decide what kind of thing the payload is."""
    text = (payload or "").strip()
    if not text:
        return PayloadKind.OTHER
    if _HAS_SCHEME.match(text):
        scheme = text.split(":", 1)[0].lower()
        if scheme in ("http", "https"):
            return PayloadKind.URL
        # mailto:, tel:, javascript:, data: and friends are neither a
        # verification URL nor plain text, and are worth keeping distinct.
        return PayloadKind.OTHER
    return PayloadKind.PLAIN_TEXT


def parse_url(payload: str) -> ParsedURL:
    """Split a URL into the parts the checks look at."""
    text = (payload or "").strip()
    try:
        split = urlsplit(text)
    except ValueError as exc:
        return ParsedURL(parse_error=f"could not parse URL: {exc}")

    result = ParsedURL(scheme=(split.scheme or "").lower() or None)
    result.path = split.path or ""
    result.query = parse_qs(split.query, keep_blank_values=True)

    userinfo = None
    if split.username or split.password:
        # The user@host trick: everything before the @ is ignored by the
        # browser, so the text can read like an official domain while the
        # request goes to whatever follows.
        userinfo = split.username or ""
        if split.password:
            userinfo = f"{userinfo}:{split.password}"
    result.userinfo = userinfo

    try:
        raw_host = split.hostname
    except ValueError as exc:
        result.parse_error = f"could not read host: {exc}"
        return result
    if not raw_host:
        result.parse_error = "URL has no host"
        return result

    result.host_raw = raw_host
    result.host = _normalise_host(raw_host)
    result.punycode = any(
        label.startswith("xn--") for label in raw_host.lower().split(".")
    )
    result.mixed_script = _is_mixed_script(result.host)
    result.ip_host = _is_ip(result.host)

    try:
        result.port = split.port
    except ValueError as exc:
        result.parse_error = f"invalid port: {exc}"

    return result


def _normalise_host(host: str) -> str:
    """Lowercase, strip the root dot, and decode punycode where possible.

    Punycode is decoded so the host is compared as the characters it displays
    to a human: a lookalike written as xn--... should be judged on what it
    looks like, not on its ASCII encoding. Whether it *was* punycode is
    recorded separately, because that is itself a signal.
    """
    text = host.strip().lower().rstrip(".")
    if not text:
        return text
    labels = []
    for label in text.split("."):
        if label.startswith("xn--"):
            try:
                labels.append(label.encode("ascii").decode("idna"))
                continue
            except (UnicodeError, UnicodeDecodeError):
                pass
        labels.append(label)
    return ".".join(labels)


def _script_of(char: str) -> str | None:
    try:
        name = unicodedata.name(char)
    except ValueError:
        return None
    for prefix in _SCRIPT_PREFIXES:
        if name.startswith(prefix):
            return prefix
    return None


def _is_mixed_script(host: str) -> bool:
    """True when any single label draws on more than one writing system.

    Per label, not per host, and the distinction matters: nearly every
    internationalised domain pairs a non-Latin name with an ASCII TLD, so
    comparing scripts across the whole host would flag every legitimate IDN
    in the world. Mixing *within* one label is the homoglyph pattern — a
    Cyrillic 'а' sitting inside an otherwise Latin word.
    """
    for label in host.split("."):
        scripts = set()
        for char in label:
            if not char.isalpha():
                continue
            script = _script_of(char)
            if script:
                scripts.add(script)
        if len(scripts) > 1:
            return True
    return False


def _is_ip(host: str) -> bool:
    candidate = host.strip("[]")
    try:
        ipaddress.ip_address(candidate)
        return True
    except ValueError:
        return False


def host_labels(host: str) -> list[str]:
    return [label for label in (host or "").split(".") if label]


def extract_certificate_id(
    detection_text: str,
    parsed: ParsedURL | None,
    kind: PayloadKind,
    cfg: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Pull the certificate id out of a payload.

    Returns (id, where_it_came_from). The demo QR format percent-encodes the
    id into one path segment, because certificate ids contain slashes and an
    un-encoded NBTA/DEMO/2024/00417 would read as four path segments.
    """
    if kind is PayloadKind.PLAIN_TEXT:
        text = (detection_text or "").strip()
        return (text or None), ("plain_text" if text else None)

    if parsed is None or kind is not PayloadKind.URL:
        return None, None

    for pattern in get(cfg, "certificate_id.path_patterns", []) or []:
        try:
            match = re.match(str(pattern), parsed.path or "")
        except re.error:
            continue
        if match:
            try:
                raw = match.group("cert_id")
            except IndexError:
                continue
            # Percent-decode once: the id was encoded to survive the path, and
            # what the issuer printed is what we compare.
            value = unquote(raw or "").strip()
            if value:
                return value, "path"

    for name in get(cfg, "certificate_id.query_parameters", []) or []:
        values = (parsed.query or {}).get(str(name))
        if values:
            value = unquote(values[0] or "").strip()
            if value:
                return value, f"query:{name}"

    return None, None
