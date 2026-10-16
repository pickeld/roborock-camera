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
normalize_pattern = _util.normalize_pattern
offer_json_size = _util.offer_json_size
sdp_offer_payload = _util.sdp_offer_payload
shrink_offer = _util.shrink_offer


def test_pattern_uses_the_app_grid() -> None:
    """A drawn gesture is the digits of the dots, in order."""
    assert normalize_pattern("1-4-7-8-9") == "14789"
    assert normalize_pattern("14789") == "14789"
    assert normalize_pattern("") == ""
    assert normalize_pattern("123") is None
    assert normalize_pattern("1123") is None
    assert normalize_pattern("123a") is None


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


def test_oversized_offer_keeps_h264_and_drops_candidates() -> None:
    """The vacuum ignores offers once their JSON reaches 4096 bytes."""
    codecs = (
        "a=rtpmap:96 VP8/90000\r\n"
        "a=rtcp-fb:96 nack pli\r\n"
        "a=rtpmap:100 H264/90000\r\n"
        "a=fmtp:100 packetization-mode=1\r\n"
        "a=rtpmap:111 opus/48000/2"
    )
    candidates = "\r\n".join(
        f"a=candidate:1 1 UDP 2122252543 192.168.1.{index % 200} {40000 + index} typ host"
        for index in range(80)
    )
    offer = (
        "v=0\r\n"
        "o=- 1 1 IN IP4 127.0.0.1\r\n"
        "s=-\r\n"
        "t=0 0\r\n"
        "a=group:BUNDLE 0 1\r\n"
        "a=ice-ufrag:abcd\r\n"
        "m=video 9 UDP/TLS/RTP/SAVPF 96 100\r\n"
        "a=mid:0\r\n"
        "a=recvonly\r\n"
        f"{codecs}\r\n"
        "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
        "a=mid:1\r\n"
        "a=recvonly\r\n"
        f"{candidates}\r\n"
    )
    assert offer_json_size(offer) >= 4096

    reduced, extracted = shrink_offer(offer)

    assert offer_json_size(reduced) < 4096
    assert "a=ice-ufrag:abcd" in reduced
    assert "a=rtpmap:100 H264/90000" in reduced
    assert "a=rtpmap:96 VP8/90000" not in reduced
    assert "a=rtcp-fb:" not in reduced
    assert "a=candidate:" not in reduced
    assert extracted
    assert extracted[0]["candidate"].startswith("candidate:")
    assert extracted[0]["sdpMLineIndex"] == 1


def test_codec_list_is_reduced_when_candidates_are_not_the_bulk() -> None:
    """One video codec remains when the offer is still too large without candidates."""
    feedback = "\r\n".join(f"a=rtcp-fb:96 nack pli {index}" for index in range(200))
    offer = (
        "v=0\r\n"
        "a=ice-ufrag:abcd\r\n"
        "m=video 9 UDP/TLS/RTP/SAVPF 96 100\r\n"
        "a=mid:0\r\n"
        "a=recvonly\r\n"
        "a=rtpmap:96 VP8/90000\r\n"
        f"{feedback}\r\n"
        "a=rtpmap:100 H264/90000\r\n"
    )
    assert offer_json_size(offer) >= 4096

    reduced, extracted = shrink_offer(offer)

    assert extracted == []
    assert offer_json_size(reduced) < 4096
    assert "a=rtpmap:100 H264/90000" in reduced
    assert "a=rtpmap:96 VP8/90000" not in reduced


def test_browser_offer_keeps_identity_and_baseline_h264() -> None:
    """A small browser offer is rewritten to the shape the robot answers."""
    offer = (
        "v=0\r\n"
        "o=- 9 1 IN IP4 127.0.0.1\r\n"
        "s=-\r\n"
        "t=0 0\r\n"
        "a=group:BUNDLE 0\r\n"
        "a=extmap-allow-mixed\r\n"
        "m=video 9 UDP/TLS/RTP/SAVPF 102 104\r\n"
        "c=IN IP4 0.0.0.0\r\n"
        "a=rtcp:9 IN IP4 0.0.0.0\r\n"
        "a=ice-ufrag:frombrowser\r\n"
        "a=ice-pwd:browserpasswordvalue1\r\n"
        "a=fingerprint:sha-256 AA:BB:CC:DD\r\n"
        "a=setup:actpass\r\n"
        "a=mid:0\r\n"
        "a=recvonly\r\n"
        "a=rtcp-mux\r\n"
        "a=rtcp-rsize\r\n"
        "a=rtpmap:102 H264/90000\r\n"
        "a=fmtp:102 profile-level-id=42e01f\r\n"
        "a=rtcp-fb:102 nack pli\r\n"
        "a=rtpmap:104 H264/90000\r\n"
        "a=fmtp:104 profile-level-id=640c1f\r\n"
    )

    reduced, extracted = shrink_offer(offer)

    assert extracted == []
    assert "a=ice-ufrag:frombrowser" in reduced
    assert "a=fingerprint:sha-256 AA:BB:CC:DD" in reduced
    assert "a=rtpmap:102 H264/90000" in reduced
    assert "a=rtpmap:104 H264/90000" not in reduced
    assert "a=rtcp-fb:" not in reduced
    assert "a=rtcp:9" not in reduced
    assert "a=extmap" not in reduced
