"""Helpers for the Roborock Camera integration."""

import json
import re
from base64 import b64encode
from hashlib import md5
from typing import Any

_PATTERN_SEPARATORS = re.compile(r"[\s,.\-/>]+")


def normalize_pattern(value: str) -> str | None:
    """Return the 1-9 grid sequence for a drawn pattern.

    The Roborock app pattern is a 3x3 gesture. The vacuum expects the dots in
    draw order, numbered left to right from the top:

    1 2 3
    4 5 6
    7 8 9

    An empty value stays empty so options can keep the current pattern. Any
    other value must be 4 to 9 unique dots.
    """
    compact = _PATTERN_SEPARATORS.sub("", value.strip())
    if compact == "":
        return ""
    if not re.fullmatch(r"[1-9]{4,9}", compact):
        return None
    if len(set(compact)) != len(compact):
        return None
    return compact


def camera_pin_digest(pin: str) -> str:
    """Return the digest a Roborock vacuum expects for its video pattern.

    Live view identifies the pattern with this exact digest. The robot protocol
    requires it; this integration does not use it as a password hash.
    """
    return md5(pin.encode("utf-8"), usedforsecurity=False).hexdigest()


def json_b64(value: Any) -> str:
    """Encode a JSON object the way the robot's signaling channel expects."""
    return b64encode(json.dumps(value).encode("utf-8")).decode("ascii")


def sdp_offer_payload(offer_sdp: str) -> dict[str, str]:
    """Wrap a browser SDP offer for ``send_sdp_to_robot``."""
    return {"app_sdp": json_b64({"sdp": offer_sdp, "type": "offer"})}
