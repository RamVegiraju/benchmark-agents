"""MLflow AgentServer exposing the LangGraph agent over /invocations.

Serves both non-streaming (@invoke) and streaming (@stream) responses. Each
request is auto-traced to a Databricks workspace MLflow experiment; trace export
runs on a background thread so it does not add latency to the served response.

Run: python start_server.py --port 8000 [--workers N] [--reload]
"""

import handlers  # noqa: F401  (registers @invoke/@stream handlers + tracing config)
from mlflow.genai.agent_server import AgentServer

agent_server = AgentServer("ResponsesAgent")
app = agent_server.app


def main():
    agent_server.run(app_import_string="start_server:app")


if __name__ == "__main__":
    main()
