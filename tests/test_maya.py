from __future__ import annotations

import json

import app.providers.maya as maya_module
import httpx
from app.providers.maya import MayaProvider


def test_maya_sends_hindi_and_converts_raw_pcm(monkeypatch) -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["authorization"]
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "audio/L16; rate=24000; channels=1"},
            content=b"\x00\x00" * 100,
        )

    monkeypatch.setattr(
        maya_module,
        "pcm_to_ogg_opus",
        lambda pcm, timeout_seconds: b"OggS" + pcm[:4],
    )
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer maya_test"},
    )
    provider = MayaProvider(
        "https://tts.mayaresearch.ai/v1/tts",
        "maya_test",
        voice="Ananya",
        client=client,
    )

    audio = provider.synthesize("नमस्ते", "Hindi")

    assert audio.startswith(b"OggS")
    assert captured["authorization"] == "Bearer maya_test"
    assert captured["payload"] == {
        "text": "नमस्ते",
        "voice": "Ananya",
        "model": "Maya 2 Native",
        "language": "hi",
    }


def test_maya_omits_language_for_code_mixed_text(monkeypatch) -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "audio/L16; rate=24000; channels=1"},
            content=b"\x00\x00",
        )

    monkeypatch.setattr(maya_module, "pcm_to_ogg_opus", lambda *_args, **_kwargs: b"OggS")
    provider = MayaProvider(
        "https://tts.mayaresearch.ai/v1/tts",
        "maya_test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert provider.synthesize("Bhai kal चार बजे", None) == b"OggS"
    assert "language" not in captured


def test_maya_error_falls_back_to_text() -> None:
    provider = MayaProvider(
        "https://tts.mayaresearch.ai/v1/tts",
        "bad-key",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(401, json={"error": "invalid key"}))
        ),
    )

    assert provider.synthesize("hello", "en") is None
