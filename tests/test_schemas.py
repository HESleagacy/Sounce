from __future__ import annotations

import pytest
from app.assistant.schemas import AudioDecision
from pydantic import ValidationError


def test_audio_decision_requires_a_transcript() -> None:
    with pytest.raises(ValidationError):
        AudioDecision(intent="answer_question", response="Okay")


def test_audio_decision_accepts_a_non_empty_transcript() -> None:
    decision = AudioDecision(
        intent="answer_question",
        response="Okay",
        transcript="Yes",
        detected_languages=["en"],
    )

    assert decision.transcript == "Yes"
    assert decision.detected_languages == ["en"]
