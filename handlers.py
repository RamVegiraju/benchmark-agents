"""Async agent handlers + MLflow tracing config for the AgentServer.

Follows the Databricks app-templates pattern (agent-langgraph/agent_server):
  * Both handlers are `async def` so the AgentServer awaits them and a single
    uvicorn worker can serve many concurrent (I/O-bound) LLM requests instead of
    blocking the event loop on each one.
  * `stream_handler` is the single source of truth; `invoke_handler` just drains
    it and collects the finished output items.

Kept in its own module (imported once) so the @invoke/@stream decorators register
exactly once regardless of how uvicorn imports the app.
"""

import json
import logging
import os
from typing import Any, AsyncGenerator, AsyncIterator

import mlflow
from langchain_core.messages import AIMessageChunk, ToolMessage
from mlflow.genai.agent_server import invoke, stream
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentResponse,
    ResponsesAgentStreamEvent,
    create_text_delta,
    output_to_responses_items_stream,
    to_chat_completions_input,
)

from agent import graph

logger = logging.getLogger(__name__)

EXPERIMENT_PATH = os.environ.get(
    "MLFLOW_EXPERIMENT_PATH", "/Shared/load-test-agents"
)

# Tracing on/off toggle (MLFLOW_TRACING_ENABLED=0 disables). Export runs on a
# background thread, so tracing stays off the request path either way.
TRACING_ENABLED = os.environ.get("MLFLOW_TRACING_ENABLED", "1").lower() not in ("0", "false", "no")

if TRACING_ENABLED:
    mlflow.set_tracking_uri("databricks")
    mlflow.set_experiment(EXPERIMENT_PATH)
    mlflow.langchain.autolog()
else:
    mlflow.langchain.autolog(disable=True)
    mlflow.tracing.disable()


async def _process_astream(async_stream: AsyncIterator[Any]) -> AsyncGenerator[ResponsesAgentStreamEvent, None]:
    """Convert LangGraph astream events into Responses stream events.

    `updates` mode -> finished output items (assistant/tool messages).
    `messages` mode -> token-level text deltas of the answer.
    """
    async for event in async_stream:
        mode, payload = event[0], event[1]
        if mode == "updates":
            for node_data in payload.values():
                messages = node_data.get("messages", [])
                for msg in messages:
                    if isinstance(msg, ToolMessage) and not isinstance(msg.content, str):
                        msg.content = json.dumps(msg.content)
                for item in output_to_responses_items_stream(messages):
                    yield item
        elif mode == "messages":
            chunk = payload[0]
            if isinstance(chunk, AIMessageChunk) and (content := chunk.content):
                yield ResponsesAgentStreamEvent(**create_text_delta(delta=content, item_id=chunk.id))


@stream()
async def stream_handler(request: ResponsesAgentRequest) -> AsyncGenerator[ResponsesAgentStreamEvent, None]:
    messages = {"messages": to_chat_completions_input([i.model_dump() for i in request.input])}
    async for event in _process_astream(graph.astream(input=messages, stream_mode=["updates", "messages"])):
        yield event


@invoke()
async def invoke_handler(request: ResponsesAgentRequest) -> ResponsesAgentResponse:
    outputs = [
        event.item
        async for event in stream_handler(request)
        if event.type == "response.output_item.done"
    ]
    return ResponsesAgentResponse(output=outputs)
