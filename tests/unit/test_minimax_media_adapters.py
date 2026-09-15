"""Regression cover for the MiniMax media adapters (image / STT / TTS).

These pin the three vendor-specific wire formats so a refactor of the
shared load/select/build/parse pipeline cannot silently break them:

* image  → POST /v1/image_generation, ``data.image_urls``, ``aspect_ratio`` enum
* STT    → POST /v1/speech_to_text, multipart, ``language`` as a *header*
* TTS    → POST /v1/t2a_v2, hex-encoded audio inside ``data.audio``
"""

from __future__ import annotations

import json

import httpx
import pytest

from openakita.llm.image_generation import (
    ImageGenerationError,
    _to_minimax_aspect_ratio,
    build_image_request,
    image_endpoint_url,
    parse_image_response,
    request_image,
)
from openakita.llm.stt_client import STTClient, _is_minimax_asr
from openakita.llm.tts_client import (
    TTSError,
    build_tts_request,
    load_tts_endpoints,
    parse_tts_response,
    request_speech,
    tts_endpoint_url,
)
from openakita.llm.types import EndpointConfig
from openakita.tools.definitions.system import SYSTEM_TOOLS
from openakita.tools.handlers.system import SystemHandler

MINIMAX_BASE = "https://api.minimax.cn/v1"


def _endpoint(**overrides) -> EndpointConfig:
    values = {
        "name": "minimax-cn-image",
        "provider": "minimax-cn",
        "api_type": "minimax",
        "base_url": MINIMAX_BASE,
        "api_key": "sk-test",
        "model": "image-01",
        "priority": 1,
        "capabilities": ["image_generation"],
    }
    values.update(overrides)
    return EndpointConfig(**values)


def _tts_endpoint(**overrides) -> EndpointConfig:
    values = {
        "name": "minimax-cn-tts",
        "provider": "minimax-cn",
        "api_type": "minimax",
        "base_url": MINIMAX_BASE,
        "api_key": "sk-test",
        "model": "speech-2.8-hd",
        "priority": 1,
        "capabilities": ["tts"],
    }
    values.update(overrides)
    return EndpointConfig(**values)


# ─── image ──────────────────────────────────────────────────────────


def test_minimax_image_url_handles_base_and_full_url() -> None:
    assert image_endpoint_url(_endpoint()) == f"{MINIMAX_BASE}/image_generation"
    assert (
        image_endpoint_url(_endpoint(base_url=f"{MINIMAX_BASE}/image_generation"))
        == f"{MINIMAX_BASE}/image_generation"
    )


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ("1024x1024", "1:1"),
        ("1280*720", "16:9"),
        ("1152x864", "4:3"),
        ("1248x832", "3:2"),
        ("832x1248", "2:3"),
        ("864x1152", "3:4"),
        ("720x1280", "9:16"),
        ("1344x576", "21:9"),
        ("1:1", "1:1"),
        # Unknown sizes snap to the nearest allowed ratio…
        ("1000x1000", "1:1"),
        ("1920x817", "21:9"),
        # …and garbage falls back to square instead of raising.
        ("", "1:1"),
        ("not-a-size", "1:1"),
        ("1024x0", "1:1"),
    ],
)
def test_minimax_aspect_ratio_snapping(size: str, expected: str) -> None:
    assert _to_minimax_aspect_ratio(size) == expected


def test_build_minimax_image_request_uses_aspect_ratio() -> None:
    body = build_image_request(
        _endpoint(),
        prompt="a red lantern",
        negative_prompt="text, watermark",
        size="1280x720",
        seed=42,
        prompt_extend=False,
        watermark=True,
    )

    assert body["model"] == "image-01"
    assert body["prompt"] == "a red lantern\nAvoid: text, watermark"
    assert body["aspect_ratio"] == "16:9"
    assert body["response_format"] == "url"
    assert body["n"] == 1
    assert body["prompt_optimizer"] is False
    assert body["aigc_watermark"] is True
    assert body["seed"] == 42


def test_build_minimax_image_request_truncates_overlong_prompt() -> None:
    body = build_image_request(_endpoint(), prompt="x" * 2000)
    assert len(body["prompt"]) == 1500


def test_parse_minimax_image_response_reads_image_urls() -> None:
    result = parse_image_response(
        _endpoint(),
        {
            "id": "06f84475ebd6",
            "data": {"image_urls": ["https://cdn.example/a.jpeg"]},
            "base_resp": {"status_code": 0, "status_msg": "success"},
        },
    )

    assert result.request_id == "06f84475ebd6"
    assert result.image_url == "https://cdn.example/a.jpeg"
    assert result.image_bytes is None


def test_parse_minimax_image_response_decodes_base64() -> None:
    import base64

    payload = b"\xff\xd8jpeg-bytes"
    result = parse_image_response(
        _endpoint(),
        {"data": {"image_base64": [base64.b64encode(payload).decode()]}},
    )

    assert result.image_bytes == payload


def test_parse_minimax_image_response_surfaces_business_error() -> None:
    """MiniMax hides failures inside HTTP 200 — they must still fail loudly."""

    with pytest.raises(ImageGenerationError) as excinfo:
        parse_image_response(
            _endpoint(),
            {"base_resp": {"status_code": 1026, "status_msg": "content blocked"}},
        )

    assert "1026" in str(excinfo.value)
    assert "content blocked" in str(excinfo.value)


@pytest.mark.asyncio
async def test_request_image_calls_minimax_protocol() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == f"{MINIMAX_BASE}/image_generation"
        assert request.headers["authorization"] == "Bearer sk-test"
        body = json.loads(request.content)
        assert body["model"] == "image-01"
        assert body["aspect_ratio"] == "1:1"
        return httpx.Response(
            200,
            json={
                "id": "req-1",
                "data": {"image_urls": ["https://cdn.example/x.jpeg"]},
                "base_resp": {"status_code": 0},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await request_image(client, _endpoint(), prompt="a lantern")

    assert result.endpoint_name == "minimax-cn-image"
    assert result.image_url == "https://cdn.example/x.jpeg"


# ─── STT ────────────────────────────────────────────────────────────


def _stt_endpoint(**overrides) -> EndpointConfig:
    values = {
        "name": "minimax-cn-asr",
        "provider": "minimax-cn",
        "api_type": "openai",
        "base_url": MINIMAX_BASE,
        "api_key": "sk-test",
        "model": "asr-1.0",
        "priority": 1,
    }
    values.update(overrides)
    return EndpointConfig(**values)


def test_stt_routes_minimax_endpoints_to_the_speech_to_text_protocol() -> None:
    assert _is_minimax_asr(_stt_endpoint()) is True
    # provider alone is enough…
    assert _is_minimax_asr(_stt_endpoint(api_type="openai")) is True
    # …as is api_type alone, or even just the host.
    assert _is_minimax_asr(_stt_endpoint(provider="", api_type="minimax")) is True
    assert _is_minimax_asr(_stt_endpoint(provider="", api_type="openai")) is True
    # A non-MiniMax endpoint must stay on the OpenAI Whisper path.
    assert (
        _is_minimax_asr(
            _stt_endpoint(provider="openai", api_type="openai", base_url="https://api.openai.com/v1")
        )
        is False
    )


@pytest.mark.asyncio
async def test_minimax_stt_uploads_multipart_and_reads_text(tmp_path, monkeypatch) -> None:
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"ID3fake-audio")
    captured: dict = {}

    class _FakeResponse:
        status_code = 200
        text = '{"text": "你好世界"}'

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"text": "你好世界", "duration": 1.5}

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers=None, files=None, data=None, json=None):
            captured.update(url=url, headers=headers, files=files, data=data)
            return _FakeResponse()

    monkeypatch.setattr("httpx.Client", _FakeClient)

    client = STTClient([_stt_endpoint()])
    result = await client.transcribe(str(audio), language="zh")

    assert result == "你好世界"
    assert captured["url"] == f"{MINIMAX_BASE}/speech_to_text"
    # language travels as a request HEADER for MiniMax, not a form field.
    assert captured["headers"]["language"] == "zh"
    assert "language" not in captured["data"]
    assert captured["data"]["model"] == "asr-1.0"
    assert "file" in captured["files"]


# ─── TTS ────────────────────────────────────────────────────────────


def test_tts_url_builds_and_normalizes() -> None:
    assert tts_endpoint_url(_tts_endpoint()) == f"{MINIMAX_BASE}/t2a_v2"
    assert tts_endpoint_url(_tts_endpoint(base_url=f"{MINIMAX_BASE}/t2a_v2")) == (
        f"{MINIMAX_BASE}/t2a_v2"
    )


def test_build_minimax_tts_request_uses_extra_param_defaults() -> None:
    endpoint = _tts_endpoint(
        extra_params={
            "default_voice_id": "female-shaonv",
            "default_sample_rate": 16000,
            "output_format": "url",
        }
    )
    body = build_tts_request(endpoint, text="你好", speed=1.2, pitch=-2)

    assert body["model"] == "speech-2.8-hd"
    assert body["text"] == "你好"
    assert body["stream"] is False
    assert body["voice_setting"]["voice_id"] == "female-shaonv"
    assert body["voice_setting"]["speed"] == 1.2
    assert body["voice_setting"]["pitch"] == -2
    assert body["audio_setting"]["sample_rate"] == 16000
    assert body["audio_setting"]["format"] == "mp3"
    assert body["output_format"] == "url"


def test_build_minimax_tts_request_lets_arguments_win_over_defaults() -> None:
    endpoint = _tts_endpoint(extra_params={"default_voice_id": "female-shaonv"})
    body = build_tts_request(endpoint, text="hi", voice_id="male-qn-qingse", audio_format="wav")

    assert body["voice_setting"]["voice_id"] == "male-qn-qingse"
    assert body["audio_setting"]["format"] == "wav"


def test_parse_minimax_tts_response_decodes_hex_audio() -> None:
    result = parse_tts_response(
        _tts_endpoint(),
        {
            "data": {"audio": "494433", "status": 2},
            "extra_info": {"audio_format": "mp3", "audio_length": 4536},
            "trace_id": "trace-1",
            "base_resp": {"status_code": 0, "status_msg": "success"},
        },
    )

    assert result.audio_bytes == b"ID3"
    assert result.audio_url is None
    assert result.audio_format == "mp3"
    assert result.request_id == "trace-1"


def test_parse_minimax_tts_response_accepts_url_output() -> None:
    result = parse_tts_response(
        _tts_endpoint(),
        {"data": {"audio": "https://cdn.example/a.mp3"}, "extra_info": {"audio_format": "mp3"}},
    )

    assert result.audio_url == "https://cdn.example/a.mp3"
    assert result.audio_bytes is None


def test_parse_minimax_tts_response_surfaces_business_error() -> None:
    with pytest.raises(TTSError) as excinfo:
        parse_tts_response(
            _tts_endpoint(),
            {"data": None, "base_resp": {"status_code": 1004, "status_msg": "auth failed"}},
        )

    assert "1004" in str(excinfo.value)

    with pytest.raises(TTSError):
        parse_tts_response(_tts_endpoint(), {"data": {}, "base_resp": {"status_code": 0}})


@pytest.mark.asyncio
async def test_request_speech_calls_minimax_protocol() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == f"{MINIMAX_BASE}/t2a_v2"
        body = json.loads(request.content)
        assert body["text"] == "hello"
        assert body["stream"] is False
        return httpx.Response(
            200,
            json={
                "data": {"audio": "49443304", "status": 2},
                "extra_info": {"audio_format": "mp3"},
                "base_resp": {"status_code": 0},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await request_speech(client, _tts_endpoint(), text="hello")

    assert result.audio_bytes == b"ID3\x04"


def test_tts_endpoints_are_loaded_by_priority(tmp_path) -> None:
    from openakita.llm.endpoint_manager import EndpointManager

    manager = EndpointManager(tmp_path)
    for name, priority in (("second", 20), ("first", 10)):
        manager.save_endpoint(
            {
                "name": name,
                "provider": "minimax-cn",
                "api_type": "minimax",
                "base_url": MINIMAX_BASE,
                "model": "speech-2.8-hd",
                "priority": priority,
                "capabilities": ["tts"],
            },
            api_key=f"{name}-key",
            endpoint_type="tts_endpoints",
        )

    endpoints = load_tts_endpoints(tmp_path)

    assert [endpoint.name for endpoint in endpoints] == ["first", "second"]


# ─── tool registration ──────────────────────────────────────────────


def test_generate_speech_is_fully_registered() -> None:
    """A tool definition without a handler (or vice versa) fails at runtime."""
    definition_names = {tool["name"] for tool in SYSTEM_TOOLS}

    assert "generate_speech" in definition_names
    assert "generate_speech" in SystemHandler.TOOLS
    assert "generate_speech" in SystemHandler.TOOL_CLASSES
    assert hasattr(SystemHandler, "_generate_speech")

    from openakita.tools.definitions.base import CATEGORY_PREFIXES

    assert "generate_speech" in CATEGORY_PREFIXES["System"]


def test_generate_speech_schema_requires_text() -> None:
    schema = next(tool for tool in SYSTEM_TOOLS if tool["name"] == "generate_speech")

    assert schema["input_schema"]["required"] == ["text"]
    assert "text" in schema["input_schema"]["properties"]
    # Voice shaping options the MiniMax adapter actually forwards.
    for key in ("voice_id", "speed", "vol", "pitch", "emotion", "audio_format"):
        assert key in schema["input_schema"]["properties"]
