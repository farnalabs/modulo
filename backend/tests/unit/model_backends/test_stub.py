import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from modulo.model_backends.stub import StubModelBackend, UnexpectedInputError, normalize_input
from modulo.model_backends.stub.backend import _content_as_text


def test_is_a_langchain_chat_model() -> None:
    assert isinstance(StubModelBackend(), BaseChatModel)


@pytest.mark.asyncio
async def test_ainvoke_returns_ai_message_from_normalized_fixture() -> None:
    backend = StubModelBackend({"Follow   the rules.\nSummarize this": "A short summary."})

    result = await backend.ainvoke(
        [
            SystemMessage(content="Follow the rules."),
            HumanMessage(content="  Summarize\nthis  "),
        ]
    )

    assert isinstance(result, AIMessage)
    assert result.content == "A short summary."


@pytest.mark.asyncio
async def test_ainvoke_raises_for_unmapped_input() -> None:
    backend = StubModelBackend({"known input": "known response"})

    with pytest.raises(UnexpectedInputError, match="unmapped input") as error:
        await backend.ainvoke([HumanMessage(content="unmapped   input")])

    assert error.value.normalized_input == "unmapped input"


def test_normalize_input_extracts_text_blocks() -> None:
    message = HumanMessage(content=[{"type": "text", "text": "hello"}, " world "])

    assert normalize_input([message]) == "hello world"


def test_conflicting_normalized_fixture_keys_are_rejected() -> None:
    with pytest.raises(ValueError, match="Conflicting fixtures"):
        StubModelBackend({"same input": "first", "same   input": "second"})


def test_backend_metadata_and_repr_are_stable() -> None:
    backend = StubModelBackend({"input": "response"})

    assert backend._llm_type == "modulo-stub"
    assert repr(backend) == "StubModelBackend(fixtures=1)"
    assert backend.fixture_map == {"input": "response"}


@pytest.mark.asyncio
async def test_agenerate_returns_fixture_and_zero_usage_metadata() -> None:
    backend = StubModelBackend({"hello": "world"})

    result = await backend.agenerate([[HumanMessage(content="hello")]])

    assert result.generations[0][0].message.content == "world"
    assert result.generations[0][0].message.usage_metadata == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }


@pytest.mark.asyncio
async def test_streaming_output_matches_non_streaming_fixture() -> None:
    backend = StubModelBackend({"stream this": "streamed response"})

    chunks = [chunk async for chunk in backend.astream([HumanMessage(content="stream this")])]

    assert [chunk.content for chunk in chunks] == ["streamed response"]


@pytest.mark.asyncio
async def test_health_check_reports_stub_as_healthy() -> None:
    result = await StubModelBackend().health_check()

    assert result.ok is True
    assert result.detail == "Stub backend always healthy"


def test_content_as_text_ignores_none_and_non_text_blocks() -> None:
    assert not _content_as_text(None)
    assert not _content_as_text([{"type": "image_url", "image_url": "ignored"}])
