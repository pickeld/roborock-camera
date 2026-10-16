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


# A Saros 20 acknowledges a larger offer, then leaves get_device_sdp on "retry".
# Offers whose JSON encoding is under 4096 bytes get an answer.
_MAX_OFFER_JSON = 4096
_VIDEO_CODECS = ("H264", "VP8", "VP9", "AV1")
_AUDIO_CODECS = ("OPUS", "PCMU", "PCMA", "G722")
_MEDIA_PREFIXES = (
    "c=",
    "a=ice-ufrag:",
    "a=ice-pwd:",
    "a=fingerprint:",
    "a=setup:",
    "a=mid:",
    "a=rtcp-mux",
    "a=rtcp:",
    "a=recvonly",
    "a=sendrecv",
    "a=sendonly",
    "a=inactive",
)


def offer_json_size(offer_sdp: str) -> int:
    """Return the JSON size the vacuum sees for this offer."""
    return len(json.dumps({"sdp": offer_sdp, "type": "offer"}).encode("utf-8"))


def shrink_offer(offer_sdp: str) -> tuple[str, list[dict[str, Any]]]:
    """Return an offer the vacuum will answer, plus candidates pulled out of it.

    Browser offers list every codec and every gathered network address. This
    vacuum ignores an offer once that payload reaches 4096 bytes. Candidates
    are sent separately, and only one codec is kept in each media section when
    the offer is still too large.
    """
    if offer_json_size(offer_sdp) < _MAX_OFFER_JSON:
        return offer_sdp, []

    reduced, candidates = _without_candidates(offer_sdp)
    if offer_json_size(reduced) < _MAX_OFFER_JSON:
        return reduced, candidates

    reduced = _one_codec_per_section(reduced, keep_details=True)
    if offer_json_size(reduced) < _MAX_OFFER_JSON:
        return reduced, candidates

    return _one_codec_per_section(reduced, keep_details=False), candidates


def _without_candidates(offer_sdp: str) -> tuple[str, list[dict[str, Any]]]:
    """Drop candidate lines and remember which media section they belonged to."""
    kept: list[str] = []
    candidates: list[dict[str, Any]] = []
    section = -1
    mid: str | None = None
    for line in offer_sdp.splitlines():
        if line.startswith("m="):
            section += 1
            mid = None
            kept.append(line)
            continue
        if line.startswith("a=mid:"):
            mid = line.split(":", 1)[1]
            kept.append(line)
            continue
        if line.startswith(("a=candidate:", "a=a=candidate:")):
            raw = line.split("candidate:", 1)[1]
            candidates.append(
                {
                    "candidate": f"candidate:{raw}",
                    "sdpMid": mid,
                    "sdpMLineIndex": max(section, 0),
                }
            )
            continue
        kept.append(line)
    return _sdp_text(kept), candidates


def _one_codec_per_section(offer_sdp: str, *, keep_details: bool) -> str:
    """Keep a single payload type in each media section."""
    session: list[str] = []
    sections: list[list[str]] = []
    current: list[str] | None = None
    for line in offer_sdp.splitlines():
        if line.startswith("m="):
            current = [line]
            sections.append(current)
            continue
        if current is None:
            if not keep_details and line.startswith("a=extmap"):
                continue
            session.append(line)
            continue
        current.append(line)

    reduced = list(session)
    for section in sections:
        reduced.extend(_reduce_section(section, keep_details=keep_details))
    return _sdp_text(reduced)


def _reduce_section(lines: list[str], *, keep_details: bool) -> list[str]:
    """Rewrite one media section so it advertises one codec."""
    kind = lines[0].split()[0].removeprefix("m=")
    codecs = _rtpmap_codecs(lines)
    chosen = _preferred_payload(kind, codecs)
    if chosen is None:
        return lines

    head = lines[0].split()[:3]
    reduced = [" ".join([*head, chosen])]
    for line in lines[1:]:
        if line.startswith("a=rtpmap:"):
            if line.startswith(f"a=rtpmap:{chosen} "):
                reduced.append(line)
            continue
        if line.startswith(("a=fmtp:", "a=rtcp-fb:", "a=extmap")):
            if not keep_details:
                continue
            if line.startswith("a=extmap"):
                continue
            payload_type = line.split(":", 1)[1].split(" ", 1)[0]
            if payload_type == chosen:
                reduced.append(line)
            continue
        if line.startswith(_MEDIA_PREFIXES):
            reduced.append(line)
    return reduced


def _rtpmap_codecs(lines: list[str]) -> list[tuple[str, str]]:
    """Return payload type and codec name from rtpmap lines."""
    found: list[tuple[str, str]] = []
    for line in lines:
        if not line.startswith("a=rtpmap:"):
            continue
        payload_type, rest = line.removeprefix("a=rtpmap:").split(" ", 1)
        found.append((payload_type, rest.split("/", 1)[0].upper()))
    return found


def _preferred_payload(kind: str, codecs: list[tuple[str, str]]) -> str | None:
    """Pick the payload type a Roborock camera is most likely to answer."""
    if not codecs:
        return None
    preference = _VIDEO_CODECS if kind == "video" else _AUDIO_CODECS
    by_name = {name: payload_type for payload_type, name in codecs}
    for name in preference:
        if name in by_name:
            return by_name[name]
    return codecs[0][0]


def _sdp_text(lines: list[str]) -> str:
    """Join SDP lines the way WebRTC writes them."""
    if not lines:
        return ""
    return "\r\n".join(lines) + "\r\n"
