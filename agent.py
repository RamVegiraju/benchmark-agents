"""LangGraph ReAct agent with two mock tools, backed by the Databricks FM API."""

import asyncio
import random

from databricks_langchain import ChatDatabricks
from langchain.agents import create_agent
from langchain_core.tools import tool

LLM_ENDPOINT = "databricks-claude-opus-4-6"


# Async tools: on an async server, I/O-bound tools should be `async def` so they
# await (yielding the event loop) instead of relying on threadpool offload.
# The two tools simulate downstream APIs of different speeds so one is a clear
# bottleneck: the stock API (~0.8s) is ~5x slower than the weather API (~0.15s).
@tool
async def get_weather(city: str) -> str:
    """Return the current weather for a given city."""
    await asyncio.sleep(random.uniform(0.10, 0.20))  # fast downstream API
    return f"The weather in {city} is 72F and sunny."


@tool
async def get_stock_price(ticker: str) -> str:
    """Return the latest stock price for a given ticker symbol."""
    await asyncio.sleep(random.uniform(0.70, 0.90))  # slow downstream API (bottleneck)
    return f"{ticker.upper()} is trading at $187.34, up 1.2% today."


TOOLS = [get_weather, get_stock_price]

llm = ChatDatabricks(endpoint=LLM_ENDPOINT, temperature=0)
graph = create_agent(llm, TOOLS)


def build_messages(user_text: str) -> dict:
    return {"messages": [{"role": "user", "content": user_text}]}


if __name__ == "__main__":
    prompts = [
        "What's the weather in Boston?",  # single tool
        "What's the stock price of AAPL?",  # single tool
        "What's the weather in Boston and the stock price of AAPL?",  # both tools
    ]

    async def _main():
        for p in prompts:
            print(f"\n=== {p} ===")
            result = await graph.ainvoke(build_messages(p))
            print(result["messages"][-1].content)

    asyncio.run(_main())
