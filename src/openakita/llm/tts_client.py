"""Provider adapters and endpoint resolution for text-to-speech synthesis.

Currently only MiniMax's synchronous TTS HTTP API (``POST /v1/t2a_v2``) is
supported. Unlike chat / image endpoints this lives in its own
``tts_endpoints`` list inside ``llm_endpoints.json`` because the payload and
response shape (hex-encoded audio, ``extra_info``, ``base_resp``) share
nothing with the chat protocol.

Design mirrors :mod:`openakita.llm.image_generation` on purpose: same
load/select/url/build/parse split so the two media paths can be read
side by side.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .endpoint_manager import EndpointManager
from .types import EndpointConfig, normalize_base_url

logger = logging.getLogger(__name__)

SUPPORTED_TTS_API_TYPES = {"minimax"}
SUPPORTED_TTS_FORMATS = {"mp3", "wav", "flac", "pcm"}


class TTSError(RuntimeError):
    """A TTS provider returned an unusable response."""


@dataclass(frozen=True)
class TTSResult:
    endpoint_name: str
    model: str
    request_id: str | None = None
    audio_bytes: bytes | None = None
    audio_url: str | None = None
    audio_format: str = "mp3"
    extra_info: dict = field(default_factory=dict)


# ─── endpoint resolution ────────────────────────────────────────────


def load_tts_endpoints(workspace_dir: str | Path) -> list[EndpointConfig]:
    """Load enabled TTS endpoints in priority order."""
    manager = EndpointManager(Path(workspace_dir))
    endpoints: list[EndpointConfig] = []
    for raw in manager.list_endpoints("tts_endpoints"):
        if raw.get("enabled", True) is False:
            continue
        try:
            endpoint = EndpointConfig.from_dict(raw)
        except (KeyError, TypeError, ValueError):
            continue
        if endpoint.api_type.strip().lower() not in SUPPORTED_TTS_API_TYPES:
            continue
        endpoints.append(endpoint)
    endpoints.sort(key=lambda item: (item.priority, item.name))
    return endpoints


def select_tts_endpoints(
    endpoints: list[EndpointConfig], requested_name: str = ""
) -> list[EndpointConfig]:
    """Return the requested endpoint or the full priority-sorted fallback chain."""
    requested = requested_name.strip().lower()
    if not requested:
        return endpoints
    selected = [item for item in endpoints if item.name.strip().lower() == requested]
    if not selected:
        available = ", ".join(item.name for item in endpoints) or "none"
        raise TTSError(f"TTS endpoint {requested_name!r} was not found (available: {available})")
    return selected


def tts_endpoint_url(endpoint: EndpointConfig) -> str:
    api_type = endpoint.api_type.strip().lower()
    if api_type == "minimax":
        base_url = normalize_base_url(endpoint.base_url, extra_suffixes=("/t2a_v2",))
        return f"{base_url}/t2a_v2"
    raise TTSError(f"Unsupported TTS API type: {endpoint.api_type}")


def _request_overrides(endpoint: EndpointConfig) -> dict:
    extra = endpoint.extra_params or {}
    value = extra.get("request_params", {})
    return dict(value) if isinstance(value, dict) else {}


# ─── request / response ─────────────────────────────────────────────


def build_tts_request(
    endpoint: EndpointConfig,
    *,
    text: str,
    model: str = "",
    voice_id: str = "",
    speed: float | None = None,
    vol: float | None = None,
    pitch: int | None = None,
    emotion: str = "",
    audio_format: str = "",
    sample_rate: int | None = None,
    bitrate: int | None = None,
    language_boost: str = "",
) -> dict:
    """Build a provider-specific TTS request body."""
    api_type = endpoint.api_type.strip().lower()
    extra = endpoint.extra_params or {}
    effective_model = model.strip() or endpoint.model
    effective_format = (audio_format or str(extra.get("default_format") or "mp3")).strip().lower()

    if api_type == "minimax":
        voice_setting: dict = {
            "voice_id": (voice_id.strip() or str(extra.get("default_voice_id") or "male-qn-qingse")),
        }
        if speed is not None:
            voice_setting["speed"] = float(speed)
        if vol is not None:
            voice_setting["vol"] = float(vol)
        if pitch is not None:
            voice_setting["pitch"] = int(pitch)
        if emotion:
            voice_setting["emotion"] = emotion

        body: dict = {
            "model": effective_model,
            "text": text,
            "stream": False,
            "voice_setting": voice_setting,
            "audio_setting": {
                "sample_rate": int(sample_rate or extra.get("default_sample_rate") or 32000),
                "bitrate": int(bitrate or extra.get("default_bitrate") or 128000),
                "format": effective_format,
                "channel": int(extra.get("default_channel") or 1),
            },
            # "hex" avoids a second network hop and the 24h URL expiry.
            "output_format": str(extra.get("output_format") or "hex"),
        }
        if language_boost:
            body["language_boost"] = language_boost
        body.update(_request_overrides(endpoint))
        return body

    raise TTSError(f"Unsupported TTS API type: {endpoint.api_type}")


def parse_tts_response(endpoint: EndpointConfig, data: dict) -> TTSResult:
    """Normalize a TTS provider response into bytes or a downloadable URL."""
    api_type = endpoint.api_type.strip().lower()
    request_id = data.get("trace_id") or data.get("id")

    if api_type == "minimax":
        # MiniMax reports business failures inside a 200 response.
        base_resp = data.get("base_resp") or {}
        status_code = base_resp.get("status_code")
        if status_code not in (None, 0):
            raise TTSError(
                f"MiniMax TTS returned status_code={status_code}: {base_resp.get('status_msg')}"
            )

        payload = data.get("data") or {}
        audio = payload.get("audio")
        if not audio:
            raise TTSError("MiniMax TTS response contained no audio payload")

        extra_info = data.get("extra_info") or {}
        audio_format = str(extra_info.get("audio_format") or "mp3")
        common = {
            "endpoint_name": endpoint.name,
            "model": endpoint.model,
            "request_id": str(request_id) if request_id else None,
            "audio_format": audio_format,
            "extra_info": extra_info,
        }

        if str(audio).startswith(("http://", "https://")):
            return TTSResult(audio_url=str(audio), **common)

        try:
            raw = bytes.fromhex(str(audio))
        except ValueError as exc:
            raise TTSError("MiniMax TTS returned a malformed hex audio payload") from exc
        return TTSResult(audio_bytes=raw, **common)

    raise TTSError(f"Unsupported TTS API type: {endpoint.api_type}")


async def request_speech(
    client: httpx.AsyncClient,
    endpoint: EndpointConfig,
    **kwargs,
) -> TTSResult:
    """Call one configured TTS endpoint and normalize its response."""
    api_key = (endpoint.get_api_key() or "").strip()
    if not api_key:
        raise TTSError(
            f"API key is missing for TTS endpoint {endpoint.name!r} "
            f"({endpoint.api_key_env or 'no env var'})"
        )
    response = await client.post(
        tts_endpoint_url(endpoint),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        json=build_tts_request(endpoint, **kwargs),
        timeout=max(1, endpoint.timeout),
    )
    if response.status_code >= 400:
        raise TTSError(
            f"{endpoint.name}: HTTP {response.status_code}: {(response.text or '')[:800]}"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise TTSError(
            f"{endpoint.name}: provider returned non-JSON: {(response.text or '')[:800]}"
        ) from exc
    if not isinstance(data, dict):
        raise TTSError(f"{endpoint.name}: provider returned a non-object JSON response")
    return parse_tts_response(endpoint, data)
