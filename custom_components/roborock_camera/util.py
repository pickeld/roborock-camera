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


_VIDEO_CODECS = ("H264", "VP8", "VP9", "AV1")
_AUDIO_CODECS = ("OPUS", "PCMU", "PCMA", "G722")


def offer_json_size(offer_sdp: str) -> int:
    """Return the JSON size the vacuum sees for this offer."""
    return len(json.dumps({"sdp": offer_sdp, "type": "offer"}).encode("utf-8"))


def shrink_offer(offer_sdp: str) -> tuple[str, list[dict[str, Any]]]:
    """Return the small offer this vacuum answers, plus candidates to send later.

    The Roborock app offers one H264 video codec and one audio codec, and keeps
    the viewer's ICE and DTLS identity. A browser offer lists every codec and
    every gathered address. This vacuum acknowledges that offer and then leaves
    ``get_device_sdp`` on ``retry``, including after the addresses alone are
    removed.
    """
    stripped, candidates = _without_candidates(offer_sdp)
    return _phone_offer(stripped), candidates


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


def _phone_offer(offer_sdp: str) -> str:
    """Rebuild an offer as one codec per media section."""
    lines = offer_sdp.splitlines()
    session: list[str] = []
    sections: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        if line.startswith("m="):
            current = [line]
            sections.append(current)
            continue
        if current is None:
            session.append(line)
            continue
        current.append(line)

    rebuilt: list[str] = []
    for line in session:
        if line.startswith(("v=", "o=", "s=", "t=")):
            rebuilt.append(line)
    mids: list[str] = []
    for section in sections:
        reduced = _reduce_section(section, session)
        mid = _first_prefixed(reduced, "a=mid:")
        if mid is not None:
            mids.append(mid.removeprefix("a=mid:"))
        rebuilt.extend(reduced)
    if mids:
        rebuilt.insert(
            min(4, len(rebuilt)),
            "a=group:BUNDLE " + " ".join(mids),
        )
    return _sdp_text(rebuilt)


_SECTION_PREFIXES = (
    "c=",
    "a=ice-ufrag:",
    "a=ice-pwd:",
    "a=fingerprint:",
    "a=setup:",
    "a=mid:",
    "a=rtcp-mux",
    "a=recvonly",
    "a=sendrecv",
    "a=sendonly",
    "a=inactive",
)


def _reduce_section(lines: list[str], session: list[str]) -> list[str]:
    """Rewrite one media section so it advertises one codec."""
    kind = lines[0].split()[0].removeprefix("m=")
    codecs = _rtpmap_codecs(lines)
    chosen = _preferred_payload(kind, codecs, lines)
    parts = lines[0].split()
    head = parts[:3]
    payload = chosen if chosen is not None else (parts[3] if len(parts) > 3 else None)
    reduced = [" ".join(head if payload is None else [*head, payload])]
    for prefix in _SECTION_PREFIXES:
        line = _first_prefixed(lines, prefix) or _first_prefixed(session, prefix)
        if line is not None and line not in reduced:
            reduced.append(line)
    if chosen is not None:
        rtpmap = _first_prefixed(lines, f"a=rtpmap:{chosen} ")
        if rtpmap is not None:
            reduced.append(rtpmap)
        fmtp = _first_prefixed(lines, f"a=fmtp:{chosen}")
        if fmtp is not None:
            reduced.append(fmtp)
    return reduced


def _first_prefixed(lines: list[str], prefix: str) -> str | None:
    """Return the first SDP line with this prefix."""
    for line in lines:
        if line.startswith(prefix):
            return line
    return None


def _rtpmap_codecs(lines: list[str]) -> list[tuple[str, str]]:
    """Return payload type and codec name from rtpmap lines."""
    found: list[tuple[str, str]] = []
    for line in lines:
        if not line.startswith("a=rtpmap:"):
            continue
        payload_type, rest = line.removeprefix("a=rtpmap:").split(" ", 1)
        found.append((payload_type, rest.split("/", 1)[0].upper()))
    return found


def _preferred_payload(
    kind: str, codecs: list[tuple[str, str]], lines: list[str]
) -> str | None:
    """Pick the payload type a Roborock camera is most likely to answer."""
    if not codecs:
        return None
    if kind == "video":
        h264 = [payload for payload, name in codecs if name == "H264"]
        for payload in h264:
            fmtp = _first_prefixed(lines, f"a=fmtp:{payload}") or ""
            if "42e01f" in fmtp.lower():
                return payload
        if h264:
            return h264[0]
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
