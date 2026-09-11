"""Lexical retrieval and the context budget."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from app.assistant.context import CONTEXT_CHAR_BUDGET, MEMORY_LIMIT, build_context
from app.domain.messages import InboundMessage, MessageType
from app.domain.retrieval import rank, score, tokenize
from app.persistence.database import Database
from app.persistence.models import Base
from app.persistence.repositories import Repository

OWNER = "919876543210@s.whatsapp.net"


@dataclass
class Item:
    content: str


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Mujhe penicillin se allergy hai", {"penicillin", "allergy"}),
        ("Main aksar Sunday ko medicines order karta hoon", {"aksar", "sunday", "medicines", "order"}),
        ("kya hai", set()),
        ("", set()),
        (None, set()),
    ],
)
def test_tokenize_drops_filler(text: str | None, expected: set[str]) -> None:
    assert tokenize(text) == expected


@pytest.mark.parametrize(
    ("query", "candidate", "matches"),
    [
        ("penicillin allergy", "User reports a penicillin allergy", True),
        ("penicillin allergy", "User orders medicines on Sunday", False),
        ("doctor Sharma", "Mera doctor Dr Sharma hai", True),
        ("bijli ka bill", "Electricity bill due on the 18th", True),
        ("bijli ka bill", "Dentist appointment on Tuesday", False),
        ("", "anything at all", False),
    ],
)
def test_score_only_rewards_real_overlap(query: str, candidate: str, matches: bool) -> None:
    assert (score(tokenize(query), tokenize(candidate)) > 0) is matches


def test_rank_surfaces_the_relevant_item_buried_under_recency() -> None:
    candidates = [Item(f"unrelated note number {index}") for index in range(40)]
    candidates.append(Item("User reports a penicillin allergy"))

    ranked = rank("penicillin allergy kya thi", candidates, lambda item: item.content, limit=5)

    assert any("penicillin" in item.content for item in ranked)
    assert len(ranked) == 5


def test_rank_keeps_recent_items_so_follow_ups_still_work() -> None:
    candidates = [Item("just discussed this"), Item("older"), Item("penicillin allergy")]

    ranked = rank("penicillin", candidates, lambda item: item.content, limit=2, keep_recent=1)

    assert ranked[0].content == "just discussed this"
    assert any("penicillin" in item.content for item in ranked)


def test_rank_falls_back_to_recency_without_a_usable_query() -> None:
    candidates = [Item("first"), Item("second"), Item("third")]
    assert [item.content for item in rank("kya hai", candidates, lambda i: i.content, limit=2)] == [
        "first",
        "second",
    ]


def _seed(tmp_path, memory_count: int, content: str = "note"):
    database = Database(f"sqlite:///{tmp_path / 'ctx.db'}")
    Base.metadata.create_all(database.engine)
    with database.session() as session:
        repository = Repository(session)
        user = repository.get_or_create_user(OWNER, "Asia/Kolkata")
        source = repository.add_inbound(
            user,
            InboundMessage(
                whatsapp_message_id="src",
                chat_jid=OWNER,
                sender_jid=OWNER,
                message_type=MessageType.TEXT,
                text="seed",
                occurred_at=datetime.now(timezone.utc),
                is_from_me=True,
                is_self_chat=True,
            ),
        )
        for index in range(memory_count):
            repository.add_memory(user.id, source.id, "personal_fact", "general", f"{content} {index}", 1.0)
        repository.add_memory(
            user.id, source.id, "personal_fact", "health", "User reports a penicillin allergy", 1.0
        )
    return database


def test_context_selects_relevant_memories_not_merely_recent(tmp_path) -> None:
    database = _seed(tmp_path, memory_count=80, content="shopping list item")
    with database.session() as session:
        repository = Repository(session)
        user = repository.get_or_create_user(OWNER, "Asia/Kolkata")
        context = build_context(repository, user, None, query="Meri penicillin allergy kya hai?")

    assert len(context.memories) <= MEMORY_LIMIT
    assert any("penicillin" in str(item["content"]) for item in context.memories)


def test_context_stays_inside_its_budget(tmp_path) -> None:
    database = _seed(tmp_path, memory_count=400, content="x" * 400)
    with database.session() as session:
        repository = Repository(session)
        user = repository.get_or_create_user(OWNER, "Asia/Kolkata")
        context = build_context(repository, user, None, query="penicillin allergy")

    assert len(context.as_prompt()) <= CONTEXT_CHAR_BUDGET
