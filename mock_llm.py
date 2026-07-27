"""Mock chat model for capacity load tests (opt-in via MOCK_LLM=1).

Purpose: isolate the *serving infrastructure* throughput (uvicorn workers, event
loop, SSE framing, network) from Foundation-Model-API latency and token cost. When
enabled, the agent answers directly with a canned, streamed response instead of
calling the FM endpoint — so a load test measures the app/server ceiling, not the
model.

Trade-off: mock mode skips the tool-calling round trip (it streams one text answer),
so it does NOT exercise tool spans. Use it for capacity planning; run the real model
(MOCK_LLM unset) for end-to-end numbers. Default is OFF — Parts 1 & 2 are unaffected.

Timing knobs (match the app-templates load-testing skill):
  MOCK_CHUNK_COUNT     number of text chunks streamed  (default 80)
  MOCK_CHUNK_DELAY_MS  delay between chunks in ms       (default 10)
"""

import asyncio
import os
from typing import Any, AsyncIterator, List, Optional

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

_ANSWER = (
    "Here is a synthesized market and weather summary. The requested figures are "
    "steady today with no material moves. Conditions are clear and within normal "
    "ranges, and there are no anomalies to report across the tracked indicators. "
    "This canned response streams token-by-token to mimic a real model so the load "
    "test can measure serving-infrastructure throughput without any Foundation Model "
    "API latency, token cost, or response variability entering the measurement."
)


class MockChatModel(BaseChatModel):
    """Streams a fixed answer with configurable chunking; ignores any bound tools."""

    chunk_count: int = 80
    chunk_delay_s: float = 0.01
    answer: str = _ANSWER

    @property
    def _llm_type(self) -> str:
        return "mock-chat"

    # ReAct agents call .bind_tools(); we intentionally ignore tools and answer
    # directly (see module docstring) so mock mode is a single, deterministic call.
    def bind_tools(self, tools: Any, **kwargs: Any) -> "MockChatModel":
        return self

    def _chunks(self) -> List[str]:
        words = self.answer.split()
        n = max(1, min(self.chunk_count, len(words)))
        base, rem = divmod(len(words), n)
        out, idx = [], 0
        for i in range(n):
            size = base + (1 if i < rem else 0)
            if size == 0:
                continue
            out.append(" ".join(words[idx:idx + size]) + " ")
            idx += size
        return out

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.answer))])

    async def _astream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        for piece in self._chunks():
            if self.chunk_delay_s > 0:
                await asyncio.sleep(self.chunk_delay_s)
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=piece))
            if run_manager:
                await run_manager.on_llm_new_token(piece, chunk=chunk)
            yield chunk


def build_mock_llm() -> MockChatModel:
    return MockChatModel(
        chunk_count=int(os.environ.get("MOCK_CHUNK_COUNT", "80")),
        chunk_delay_s=int(os.environ.get("MOCK_CHUNK_DELAY_MS", "10")) / 1000,
    )
