"""Tests for Roborock camera signaling helpers."""

import importlib.util
import json
from base64 import b64decode
from pathlib import Path

_UTIL = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "roborock_camera"
    / "util.py"
)
_spec = importlib.util.spec_from_file_location("roborock_camera_util", _UTIL)
assert _spec is not None and _spec.loader is not None
_util = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_util)
camera_pin_digest = _util.camera_pin_digest
sdp_offer_payload = _util.sdp_offer_payload


def test_pin_digest_matches_the_robot_protocol() -> None:
    """The vacuum identifies a PIN by this digest."""
    digest = camera_pin_digest("1234")

    assert digest == "81dc9bdb52d04dc20036dbd8313ed055"
    assert "1234" not in digest


def test_sdp_offer_is_forwarded_as_base64_json() -> None:
    """The robot expects the browser offer, not an empty preview command."""
    payload = sdp_offer_payload("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\n")

    decoded = json.loads(b64decode(payload["app_sdp"]))
    assert decoded["type"] == "offer"
    assert decoded["sdp"].startswith("v=0")
