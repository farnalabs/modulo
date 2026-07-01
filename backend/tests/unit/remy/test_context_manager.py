"""Unit tests for ContextManager — token counting, pruning, and summarization."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.remy.context_manager import ContextManager, ConversationContext, PruneResult
from modulo.db.models.remy_message import ChatMessage


class TestCountTokens:
    """Tests for ContextManager.count_tokens."""

    def test_empty_text_returns_zero(self) -> None:
        assert ContextManager.count_tokens("") == 0

    def test_heuristic_fallback_when_no_tiktoken(self) -> None:
        with patch("modulo.core.remy.context_manager.HAS_TIKTOKEN", False):
            count = ContextManager.count_tokens("hello world")
            assert count == max(1, len("hello world") // 4)

    def test_heuristic_returns_at_least_one(self) -> None:
        with patch("modulo.core.remy.context_manager.HAS_TIKTOKEN", False):
            count = ContextManager.count_tokens("a")
            assert count == 1

    def test_heuristic_short_string(self) -> None:
        with patch("modulo.core.remy.context_manager.HAS_TIKTOKEN", False):
            count = ContextManager.count_tokens("abc")  # len=3, 3//4=0 -> max(1,0)=1
            assert count == 1

    def test_uses_tiktoken_when_available(self) -> None:
        mock_encoding = MagicMock()
        mock_encoding.encode = MagicMock(return_value=[1, 2, 3, 4, 5])
        with (
            patch("modulo.core.remy.context_manager.HAS_TIKTOKEN", True),
            patch("modulo.core.remy.context_manager.tiktoken.get_encoding", return_value=mock_encoding),
        ):
            count = ContextManager.count_tokens("Hello, world!")
            assert count == 5

    def test_tiktoken_fallback_on_exception(self) -> None:
        mock_encoding = MagicMock()
        mock_encoding.encode = MagicMock(side_effect=Exception("encoding error"))
        with (
            patch("modulo.core.remy.context_manager.HAS_TIKTOKEN", True),
            patch("modulo.core.remy.context_manager.tiktoken.get_encoding", return_value=mock_encoding),
        ):
            count = ContextManager.count_tokens("Hello, world!")
            assert count == max(1, len("Hello, world!") // 4)


class TestMessageToDict:
    """Tests for ContextManager._message_to_dict."""

    def test_basic_message(self) -> None:
        msg = MagicMock(spec=ChatMessage)
        msg.role = "user"
        msg.content = "Hello"
        msg.tool_calls_json = None
        msg.tool_results_json = None

        result = ContextManager._message_to_dict(msg)
        assert result == {"role": "user", "content": "Hello"}

    def test_message_with_tool_calls(self) -> None:
        msg = MagicMock(spec=ChatMessage)
        msg.role = "assistant"
        msg.content = "Calling tool..."
        msg.tool_calls_json = {"tool_calls": [{"id": "call_1", "name": "code_interpreter"}]}
        msg.tool_results_json = None

        result = ContextManager._message_to_dict(msg)
        assert result["role"] == "assistant"
        assert "tool_calls" in result
        assert result["tool_calls"] == [{"id": "call_1", "name": "code_interpreter"}]

    def test_message_with_tool_result(self) -> None:
        msg = MagicMock(spec=ChatMessage)
        msg.role = "tool_result"
        msg.content = '{"status": "ok"}'
        msg.tool_calls_json = None
        msg.tool_results_json = {"result": "ok"}

        result = ContextManager._message_to_dict(msg)
        assert result["role"] == "tool_result"
        assert result["tool_result"] == {"result": "ok"}


class TestBuildSummaryPrompt:
    """Tests for ContextManager._build_summary_prompt."""

    def test_builds_prompt_with_pruned_messages(self) -> None:
        msg1 = MagicMock(spec=ChatMessage)
        msg1.role = "user"
        msg1.content = "What is the weather?"

        msg2 = MagicMock(spec=ChatMessage)
        msg2.role = "assistant"
        msg2.content = "It is sunny."

        prompt = ContextManager._build_summary_prompt([msg1, msg2])
        assert "Summarise the following conversation" in prompt
        assert "[User]: What is the weather?" in prompt
        assert "[Assistant]: It is sunny." in prompt
        assert "Summary:" in prompt


class TestPruneMessages:
    """Tests for ContextManager.prune_messages."""

    def test_empty_messages_returns_empty(self) -> None:
        result = ContextManager.prune_messages([], 1000)
        assert result.kept_messages == []
        assert result.pruned_count == 0

    def test_under_budget_no_pruning(self) -> None:
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
        ]
        with patch.object(ContextManager, "count_tokens", return_value=10):
            result = ContextManager.prune_messages(msgs, budget=100)
        assert len(result.kept_messages) == 3
        assert result.pruned_count == 0

    def test_over_budget_prunes_oldest(self) -> None:
        msgs = [
            {"role": "system", "content": "System prompt here"},
            {"role": "user", "content": "Message 1"},
            {"role": "assistant", "content": "Response 1"},
            {"role": "user", "content": "Message 2"},
            {"role": "assistant", "content": "Response 2"},
            {"role": "user", "content": "Message 3"},
        ]
        with patch.object(ContextManager, "count_tokens", return_value=100):
            result = ContextManager.prune_messages(msgs, budget=100)
        assert result.pruned_count > 0
        assert len(result.kept_messages) < len(msgs)

    def test_first_and_last_preserved(self) -> None:
        msgs = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "A"},
            {"role": "assistant", "content": "B"},
            {"role": "user", "content": "C"},
            {"role": "assistant", "content": "D"},
            {"role": "user", "content": "E"},
        ]
        with patch.object(ContextManager, "count_tokens", return_value=100):
            result = ContextManager.prune_messages(msgs, budget=50)
        assert result.kept_messages[0] == msgs[0], "First message (system) should be preserved"
        assert result.kept_messages[-1] == msgs[-1], "Last message should be preserved"

    def test_only_two_messages_no_pruning_possible(self) -> None:
        msgs = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Hello"},
        ]
        with patch.object(ContextManager, "count_tokens", return_value=1000):
            result = ContextManager.prune_messages(msgs, budget=10)
        # Both preserved since we need at least first and last
        assert len(result.kept_messages) == 2


class TestReconstruct:
    """Tests for ContextManager.reconstruct."""

    @pytest.fixture
    def manager(self) -> ContextManager:
        return ContextManager()

    async def test_under_budget_no_pruning(self, manager: ContextManager) -> None:
        session_id = uuid.uuid4()
        mock_session = AsyncMock()

        # No existing messages
        mock_scalars = MagicMock()
        mock_scalars.all = MagicMock(return_value=[])
        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=mock_scalars)
        mock_session.execute = AsyncMock(return_value=mock_result)

        with (
            patch.object(manager, "count_tokens", return_value=500),
        ):
            ctx = await manager.reconstruct(
                session_id=session_id,
                new_message="Hello!",
                system_prompt="You are Remy.",
                page_context=None,
                context_window_tokens=200000,
                session=mock_session,
            )

        assert isinstance(ctx, ConversationContext)
        assert ctx.pruned_count == 0
        assert ctx.has_summary is False
        assert len(ctx.messages) == 2  # system + user

    async def test_over_budget_triggers_pruning(self, manager: ContextManager) -> None:
        session_id = uuid.uuid4()
        mock_session = AsyncMock()

        # Create 5 existing messages
        db_msgs = []
        for i in range(5):
            msg = MagicMock(spec=ChatMessage)
            msg.role = "user" if i % 2 == 0 else "assistant"
            msg.content = f"Message {i}"
            msg.tool_calls_json = None
            msg.tool_results_json = None
            db_msgs.append(msg)

        mock_scalars = MagicMock()
        mock_scalars.all = MagicMock(return_value=db_msgs)
        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=mock_scalars)
        mock_session.execute = AsyncMock(return_value=mock_result)

        # Each message is "expensive" — forces pruning
        with (
            patch.object(manager, "count_tokens", return_value=2000),
            patch.object(manager, "generate_summary", new_callable=AsyncMock, return_value=None),
        ):
            ctx = await manager.reconstruct(
                session_id=session_id,
                new_message="New user message",
                system_prompt="System prompt here",
                page_context=None,
                context_window_tokens=1000,  # budget = 800
                session=mock_session,
            )

        assert ctx.pruned_count > 0
        assert len(ctx.messages) < 7  # at least some were pruned

    async def test_heavy_pruning_generates_summary(self, manager: ContextManager) -> None:
        session_id = uuid.uuid4()
        mock_session = AsyncMock()

        db_msgs = []
        for i in range(20):
            msg = MagicMock(spec=ChatMessage)
            msg.role = "user" if i % 2 == 0 else "assistant"
            msg.content = f"Message {i} is quite long and uses tokens "
            msg.tool_calls_json = None
            msg.tool_results_json = None
            db_msgs.append(msg)

        mock_scalars = MagicMock()
        mock_scalars.all = MagicMock(return_value=db_msgs)
        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=mock_scalars)
        mock_session.execute = AsyncMock(return_value=mock_result)

        # count_tokens = 500 for system/new, 500 for each conv message
        # total = 500 + 500 + 20*500 = 11000, budget = 4000
        # need to prune ~14 messages (ratio 14/20=0.7 > 0.5)
        with (
            patch.object(manager, "count_tokens", return_value=500),
            patch.object(manager, "generate_summary", new_callable=AsyncMock, return_value="Summary text"),
        ):
            ctx = await manager.reconstruct(
                session_id=session_id,
                new_message="Final message",
                system_prompt="System",
                page_context=None,
                context_window_tokens=5000,  # budget = 4000
                session=mock_session,
                provider="anthropic",
                model="claude-sonnet-4-20250514",
                api_key="test-key",
            )

        assert ctx.has_summary is True
        assert ctx.pruned_count > 0

    async def test_safety_margin_respected(self, manager: ContextManager) -> None:
        """Budget should be 80% of context_window_tokens."""
        session_id = uuid.uuid4()
        mock_session = AsyncMock()

        mock_scalars = MagicMock()
        mock_scalars.all = MagicMock(return_value=[])
        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=mock_scalars)
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch.object(manager, "count_tokens", return_value=10):
            ctx = await manager.reconstruct(
                session_id=session_id,
                new_message="Hi",
                system_prompt="Sys",
                page_context=None,
                context_window_tokens=100000,
                session=mock_session,
            )

        assert len(ctx.messages) == 2
        assert ctx.pruned_count == 0

    async def test_system_prompt_always_included(self, manager: ContextManager) -> None:
        session_id = uuid.uuid4()
        mock_session = AsyncMock()

        mock_scalars = MagicMock()
        mock_scalars.all = MagicMock(return_value=[])
        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=mock_scalars)
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch.object(manager, "count_tokens", return_value=10):
            ctx = await manager.reconstruct(
                session_id=session_id,
                new_message="Hi",
                system_prompt="You are Remy, the assistant.",
                page_context=None,
                context_window_tokens=100000,
                session=mock_session,
            )

        assert ctx.messages[0]["role"] == "system"
        assert ctx.messages[0]["content"] == "You are Remy, the assistant."

    async def test_empty_conversation_only_system_and_user(self, manager: ContextManager) -> None:
        session_id = uuid.uuid4()
        mock_session = AsyncMock()

        mock_scalars = MagicMock()
        mock_scalars.all = MagicMock(return_value=[])
        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=mock_scalars)
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch.object(manager, "count_tokens", return_value=10):
            ctx = await manager.reconstruct(
                session_id=session_id,
                new_message="First message",
                system_prompt="System",
                page_context=None,
                context_window_tokens=100000,
                session=mock_session,
            )

        assert len(ctx.messages) == 2
        assert ctx.messages[0]["role"] == "system"
        assert ctx.messages[1]["role"] == "user"
        assert ctx.messages[1]["content"] == "First message"


class TestGenerateSummary:
    """Tests for ContextManager.generate_summary."""

    @pytest.fixture
    def manager(self) -> ContextManager:
        return ContextManager()

    async def test_unsupported_provider_returns_none(self, manager: ContextManager) -> None:
        msg = MagicMock(spec=ChatMessage)
        msg.role = "user"
        msg.content = "Hello"

        result = await manager.generate_summary(
            pruned_messages=[msg],
            provider="unsupported_provider",
            model="some-model",
            api_key="key",
        )
        assert result is None

    async def test_anthropic_calls_api(self, manager: ContextManager) -> None:
        msg = MagicMock(spec=ChatMessage)
        msg.role = "user"
        msg.content = "Hello"

        with patch.object(manager, "_call_anthropic", new_callable=AsyncMock, return_value="Summary") as mock_call:
            result = await manager.generate_summary(
                pruned_messages=[msg],
                provider="anthropic",
                model="claude-sonnet-4-20250514",
                api_key="test-key",
            )
            assert result == "Summary"
            mock_call.assert_awaited_once()

    async def test_openai_compat_calls_api(self, manager: ContextManager) -> None:
        msg = MagicMock(spec=ChatMessage)
        msg.role = "user"
        msg.content = "Hello"

        with patch.object(manager, "_call_openai_compat", new_callable=AsyncMock, return_value="Summary") as mock_call:
            result = await manager.generate_summary(
                pruned_messages=[msg],
                provider="openai",
                model="gpt-4o",
                api_key="test-key",
            )
            assert result == "Summary"
            mock_call.assert_awaited_once()

    async def test_exception_returns_none(self, manager: ContextManager) -> None:
        msg = MagicMock(spec=ChatMessage)
        msg.role = "user"
        msg.content = "Hello"

        with patch.object(manager, "_call_anthropic", new_callable=AsyncMock, side_effect=Exception("API down")):
            result = await manager.generate_summary(
                pruned_messages=[msg],
                provider="anthropic",
                model="claude-sonnet-4-20250514",
                api_key="test-key",
            )
            assert result is None


class TestCallAnthropic:
    """Tests for ContextManager._call_anthropic."""

    @pytest.fixture
    def manager(self) -> ContextManager:
        return ContextManager()

    async def test_successful_call(self, manager: ContextManager) -> None:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"content": [{"text": "Summary text"}]})

        with (
            patch("modulo.core.remy.context_manager.httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await manager._call_anthropic(
                prompt="Summarise this",
                model="claude-sonnet-4-20250514",
                api_key="test-key",
            )
            assert result == "Summary text"


class TestCallOpenAICompat:
    """Tests for ContextManager._call_openai_compat."""

    @pytest.fixture
    def manager(self) -> ContextManager:
        return ContextManager()

    async def test_successful_call(self, manager: ContextManager) -> None:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(
            return_value={"choices": [{"message": {"content": "Summary text"}}]},
        )

        with (
            patch("modulo.core.remy.context_manager.httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await manager._call_openai_compat(
                prompt="Summarise this",
                model="gpt-4o",
                api_key="test-key",
                provider="openai",
            )
            assert result == "Summary text"

    async def test_uses_correct_base_url(self, manager: ContextManager) -> None:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(
            return_value={"choices": [{"message": {"content": "Summary"}}]},
        )

        with (
            patch("modulo.core.remy.context_manager.httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            await manager._call_openai_compat(
                prompt="Summarise",
                model="deepseek-chat",
                api_key="key",
                provider="deepseek",
            )

            _args, kwargs = mock_client.post.call_args
            assert "api.deepseek.com" in kwargs.get("url", str(kwargs.get("base_url", ""))) or \
                "deepseek.com" in str(kwargs.get("url", mock_client.post.call_args[0]))


class TestPruneResult:
    """Tests for PruneResult data model."""

    def test_prune_result_fields(self) -> None:
        result = PruneResult(
            kept_messages=[{"role": "user", "content": "Hi"}],
            pruned_count=2,
            summary="Summary text",
        )
        assert len(result.kept_messages) == 1
        assert result.pruned_count == 2
        assert result.summary == "Summary text"

    def test_prune_result_default_summary_none(self) -> None:
        result = PruneResult(kept_messages=[], pruned_count=0)
        assert result.summary is None
