"""Live camera for Roborock vacuums that expose a video feed.

Signaling follows the V1 live-view protocol. Home Assistant only exchanges
the offer, answer, and ICE candidates. Media stays between the browser and
the vacuum.
"""

import asyncio
import json
import logging
from base64 import b64decode
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCClientConfiguration,
    WebRTCSendMessage,
)
from homeassistant.components.roborock.coordinator import RoborockDataUpdateCoordinator
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from roborock.exceptions import RoborockException
from roborock.roborock_typing import RoborockCommand
from webrtc_models import RTCConfiguration, RTCIceCandidateInit, RTCIceServer

from .const import CONF_CAMERA_PIN_HASH, CONF_ROBOROCK_ENTRY_ID, DOMAIN
from .util import json_b64, sdp_offer_payload

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1

_SDP_POLL_ATTEMPTS = 25
_SDP_POLL_DELAY = 0.25
_ICE_POLL_ATTEMPTS = 20
_ICE_POLL_DELAY = 0.25
_TURN_POLL_ATTEMPTS = 8
_VIDEO_QUALITY = "HD"


class _SessionSuperseded(Exception):
    """The browser left, or a newer viewer replaced this negotiation."""


def _first(response: Any) -> Any:
    """Return a single RPC result, unwrapping a one-item list."""
    if isinstance(response, list) and len(response) == 1:
        return response[0]
    return response


def _decode_json(payload: str) -> Any:
    """Decode a base64 JSON signaling payload from the robot."""
    return json.loads(b64decode(payload))


def _is_ice_url(url: str) -> bool:
    """Return whether this is a real STUN or TURN address.

    Before a preview starts, the vacuum answers ``retry`` instead of an address.
    Passing that to the browser prevents the player from opening.
    """
    return url.startswith(("turn:", "turns:", "stun:", "stuns:"))


def _supports_live_video(coordinator: RoborockDataUpdateCoordinator) -> bool:
    """Return whether this vacuum reports a remote live-view camera."""
    features = coordinator.properties_api.device_features
    return (
        getattr(features, "is_video_monitor_supported", None) is True
        or getattr(features, "is_two_key_real_time_video_supported", None) is True
    )


@dataclass
class _CameraSession:
    """One browser session watching the vacuum camera."""

    send_message: WebRTCSendMessage
    pending_ice: list[RTCIceCandidateInit] = field(default_factory=list)
    ice_task: asyncio.Task[None] | None = None
    closed: bool = False
    ready: bool = False
    preview_started: bool = False


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a camera for each vacuum that supports live video."""
    coordinators = config_entry.runtime_data
    added = False

    @callback
    def async_add_coordinator_entities(
        coordinator: RoborockDataUpdateCoordinator,
    ) -> None:
        """Add a camera when this vacuum reports live video support."""
        nonlocal added
        if not isinstance(coordinator, RoborockDataUpdateCoordinator):
            return
        if not _supports_live_video(coordinator):
            return
        added = True
        async_add_entities([RoborockLiveCamera(coordinator, config_entry)])

    for coordinator in getattr(coordinators, "v1", []):
        async_add_coordinator_entities(coordinator)

    if not added:
        _LOGGER.warning(
            "No vacuum on this Roborock account reported live video support"
        )

    roborock_entry_id = config_entry.data[CONF_ROBOROCK_ENTRY_ID]
    config_entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            f"roborock_coordinator_added_{roborock_entry_id}",
            async_add_coordinator_entities,
        )
    )


class RoborockLiveCamera(CoordinatorEntity[RoborockDataUpdateCoordinator], Camera):
    """WebRTC camera for a Roborock vacuum."""

    _attr_has_entity_name = True
    _attr_translation_key = "camera"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self,
        coordinator: RoborockDataUpdateCoordinator,
        camera_entry: ConfigEntry,
    ) -> None:
        """Initialize the camera."""
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self._attr_unique_id = f"{coordinator.duid}_roborock_camera"
        self._attr_device_info = coordinator.device_info
        self._camera_entry = camera_entry
        self._sessions: dict[str, _CameraSession] = {}
        self._early_closes: set[str] = set()
        self._ice_servers: list[RTCIceServer] = []
        self._negotiate_lock = asyncio.Lock()
        self._removed = False

    async def async_added_to_hass(self) -> None:
        """Fetch TURN credentials so the first viewer can relay media."""
        await super().async_added_to_hass()
        features = self.coordinator.properties_api.device_features
        if getattr(features, "is_two_key_real_time_video_supported", None) is True:
            _LOGGER.info(
                "%s may require a press of the power button on the robot to show video",
                self.entity_id,
            )
        await self._async_refresh_turn()

    async def async_will_remove_from_hass(self) -> None:
        """Stop the preview and cancel signaling when the entity is removed."""
        self._removed = True
        for session in self._sessions.values():
            session.closed = True
            if session.ice_task is not None:
                session.ice_task.cancel()
        self._sessions.clear()
        await self._stop_preview()
        self._attr_is_streaming = False
        await super().async_will_remove_from_hass()

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return no snapshot.

        The vacuum camera is a live WebRTC feed and does not provide a still.
        """
        return None

    @callback
    def _async_get_webrtc_client_configuration(self) -> WebRTCClientConfiguration:
        """Return cached TURN servers from the vacuum."""
        return WebRTCClientConfiguration(
            configuration=RTCConfiguration(list(self._ice_servers))
        )

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        """Forward the browser offer and return the vacuum's SDP answer."""
        pin_hash = self._camera_entry.options.get(CONF_CAMERA_PIN_HASH)
        if not isinstance(pin_hash, str) or not pin_hash:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="camera_pin_required",
            )

        async with self._negotiate_lock:
            if self._removed or session_id in self._early_closes:
                self._early_closes.discard(session_id)
                return
            await self._retire_other_sessions()
            session = _CameraSession(send_message)
            self._sessions[session_id] = session
            try:
                await self._negotiate(session_id, session, offer_sdp, pin_hash)
            except _SessionSuperseded:
                if session.preview_started:
                    self._sessions.pop(session_id, None)
                    await self._stop_preview()
                    self._set_streaming(False)
            except HomeAssistantError:
                if self._sessions.get(session_id) is session:
                    self._sessions.pop(session_id, None)
                    if session.preview_started:
                        await self._stop_preview()
                    self._set_streaming(False)
                raise

    async def async_on_webrtc_candidate(
        self, session_id: str, candidate: RTCIceCandidateInit
    ) -> None:
        """Forward a browser ICE candidate, or buffer it until the answer exists."""
        session = self._sessions.get(session_id)
        if session is None or session.closed:
            return
        if not session.ready:
            session.pending_ice.append(candidate)
            return
        await self._forward_local_ice(candidate)

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        """Stop the preview when the browser leaves, including mid-negotiation."""
        session = self._sessions.get(session_id)
        if session is None:
            self._early_closes.add(session_id)
            return
        session.closed = True
        self.hass.async_create_task(self._drop_session(session_id, session))

    async def _negotiate(
        self,
        session_id: str,
        session: _CameraSession,
        offer_sdp: str,
        pin_hash: str,
    ) -> None:
        """Start the preview, send the offer, and publish the answer."""
        await self._send(RoborockCommand.CHECK_HOMESEC_PASSWORD, {"password": pin_hash})
        self._raise_if_inactive(session_id, session)
        await self._send(
            RoborockCommand.SWITCH_VIDEO_QUALITY,
            {"quality": _VIDEO_QUALITY},
        )
        self._raise_if_inactive(session_id, session)
        await self._send(
            RoborockCommand.START_CAMERA_PREVIEW,
            {
                "client_id": session_id,
                "quality": _VIDEO_QUALITY,
                "password": pin_hash,
            },
        )
        session.preview_started = True
        self._raise_if_inactive(session_id, session)
        await self._async_refresh_turn(attempts=_TURN_POLL_ATTEMPTS)
        _LOGGER.info("Forwarding the browser video offer for %s", self.entity_id)
        await self._send(
            RoborockCommand.SEND_SDP_TO_ROBOT,
            sdp_offer_payload(offer_sdp),
        )
        self._raise_if_inactive(session_id, session)
        answer = await self._poll_sdp_answer(session_id, session)
        self._raise_if_inactive(session_id, session)

        session.ready = True
        _LOGGER.info("Received a camera answer from %s", self.entity_id)
        session.send_message(WebRTCAnswer(answer))
        pending = session.pending_ice
        session.pending_ice = []
        for candidate in pending:
            self._raise_if_inactive(session_id, session)
            await self._forward_local_ice(candidate)
        session.ice_task = self.hass.async_create_task(
            self._poll_remote_ice(session_id, session)
        )
        self._set_streaming(True)

    def _raise_if_inactive(self, session_id: str, session: _CameraSession) -> None:
        """Abort when this negotiation no longer owns the camera."""
        if (
            self._removed
            or session.closed
            or self._sessions.get(session_id) is not session
        ):
            raise _SessionSuperseded

    async def _retire_other_sessions(self) -> None:
        """Stop any preview already running before a replacement offer."""
        had_preview = False
        for session_id, session in list(self._sessions.items()):
            session.closed = True
            had_preview = had_preview or session.preview_started
            if session.ice_task is not None:
                session.ice_task.cancel()
            del self._sessions[session_id]
        if had_preview:
            await self._stop_preview()
            self._set_streaming(False)

    async def _drop_session(self, session_id: str, session: _CameraSession) -> None:
        """Forget a browser session and power the camera down."""
        async with self._negotiate_lock:
            if self._sessions.get(session_id) is not session:
                return
            self._sessions.pop(session_id, None)
            if session.ice_task is not None:
                session.ice_task.cancel()
            if not any(item.preview_started for item in self._sessions.values()):
                await self._stop_preview()
                self._set_streaming(False)

    async def _send(
        self,
        command: RoborockCommand | str,
        params: dict[str, Any] | list[Any] | int | None = None,
    ) -> Any:
        """Send a signaling command without refreshing the coordinator."""
        try:
            return await self.coordinator.properties_api.command.send(
                command, params=params
            )
        except RoborockException as err:
            command_name = (
                command.name if isinstance(command, RoborockCommand) else command
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"command": command_name},
            ) from err

    async def _async_refresh_turn(self, attempts: int = 1) -> None:
        """Cache ephemeral TURN credentials advertised by the vacuum."""
        for attempt in range(attempts):
            try:
                payload = _first(await self._send(RoborockCommand.GET_TURN_SERVER))
            except HomeAssistantError:
                _LOGGER.debug("Vacuum did not provide TURN credentials", exc_info=True)
                return
            url = payload.get("url") if isinstance(payload, dict) else None
            if isinstance(url, str) and _is_ice_url(url):
                username = payload.get("user")
                credential = payload.get("pwd")
                self._ice_servers = [
                    RTCIceServer(
                        url,
                        username if isinstance(username, str) else None,
                        credential if isinstance(credential, str) else None,
                    )
                ]
                return
            if attempt + 1 < attempts:
                await asyncio.sleep(_SDP_POLL_DELAY)
        _LOGGER.debug("Vacuum TURN server is not ready")

    async def _poll_sdp_answer(self, session_id: str, session: _CameraSession) -> str:
        """Poll until the vacuum returns its SDP answer."""
        for _ in range(_SDP_POLL_ATTEMPTS):
            self._raise_if_inactive(session_id, session)
            payload = _first(await self._send(RoborockCommand.GET_DEVICE_SDP))
            self._raise_if_inactive(session_id, session)
            dev_sdp = payload.get("dev_sdp") if isinstance(payload, dict) else None
            if isinstance(dev_sdp, str) and dev_sdp != "retry":
                decoded = _decode_json(dev_sdp)
                if isinstance(decoded, dict) and isinstance(decoded.get("sdp"), str):
                    return decoded["sdp"]
            await asyncio.sleep(_SDP_POLL_DELAY)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="camera_answer_timeout",
        )

    async def _poll_remote_ice(self, session_id: str, session: _CameraSession) -> None:
        """Forward ICE candidates produced by the vacuum."""
        seen: set[str] = set()
        try:
            for _ in range(_ICE_POLL_ATTEMPTS):
                if session.closed or self._sessions.get(session_id) is not session:
                    return
                payload = _first(await self._send(RoborockCommand.GET_DEVICE_ICE))
                encoded_candidates = (
                    payload.get("dev_ice") if isinstance(payload, dict) else None
                )
                if isinstance(encoded_candidates, list):
                    for encoded in encoded_candidates:
                        if not isinstance(encoded, str) or encoded in seen:
                            continue
                        seen.add(encoded)
                        self._forward_remote_candidate(session, encoded)
                await asyncio.sleep(_ICE_POLL_DELAY)
        except asyncio.CancelledError:
            raise
        except HomeAssistantError:
            _LOGGER.debug("Stopped reading vacuum ICE candidates", exc_info=True)

    def _forward_remote_candidate(self, session: _CameraSession, encoded: str) -> None:
        """Send one vacuum ICE candidate to the browser."""
        try:
            ice = _decode_json(encoded)
        except (ValueError, json.JSONDecodeError):
            return
        if not isinstance(ice, dict):
            return
        candidate = ice.get("candidate")
        if not isinstance(candidate, str):
            return
        sdp_m_line_index = ice.get("sdpMLineIndex")
        init_kwargs: dict[str, Any] = {
            "sdp_m_line_index": (
                sdp_m_line_index if isinstance(sdp_m_line_index, int) else 0
            )
        }
        if isinstance(sdp_mid := ice.get("sdpMid"), str):
            init_kwargs["sdp_mid"] = sdp_mid
        session.send_message(
            WebRTCCandidate(RTCIceCandidateInit(candidate, **init_kwargs))
        )

    async def _forward_local_ice(self, candidate: RTCIceCandidateInit) -> None:
        """Send one browser ICE candidate to the vacuum."""
        await self._send(
            RoborockCommand.SEND_ICE_TO_ROBOT,
            {
                "app_ice": json_b64(
                    {
                        "candidate": candidate.candidate,
                        "sdpMid": candidate.sdp_mid,
                        "sdpMLineIndex": candidate.sdp_m_line_index,
                    }
                )
            },
        )

    async def _stop_preview(self) -> None:
        """Release the vacuum's single camera session."""
        try:
            await self._send(RoborockCommand.STOP_CAMERA_PREVIEW, [])
        except HomeAssistantError:
            _LOGGER.debug("Unable to stop the vacuum camera preview", exc_info=True)

    def _set_streaming(self, streaming: bool) -> None:
        """Update the streaming flag once the entity is registered."""
        self._attr_is_streaming = streaming
        if self.hass is not None:
            self.async_write_ha_state()
