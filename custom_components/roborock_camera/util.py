"""Helpers for the Roborock Camera integration."""

import json
from base64 import b64encode
from hashlib import md5
from typing import Any


def camera_pin_digest(pin: str) -> str:
    """Return the digest a Roborock vacuum expects for its video PIN.

    Live view identifies the PIN with this exact digest. The robot protocol
    requires it; this integration does not use it as a password hash.
    """
    return md5(pin.encode("utf-8"), usedforsecurity=False).hexdigest()


def json_b64(value: Any) -> str:
    """Encode a JSON object the way the robot's signaling channel expects."""
    return b64encode(json.dumps(value).encode("utf-8")).decode("ascii")


def sdp_offer_payload(offer_sdp: str) -> dict[str, str]:
    """Wrap a browser SDP offer for ``send_sdp_to_robot``."""
    return {"app_sdp": json_b64({"sdp": offer_sdp, "type": "offer"})}
