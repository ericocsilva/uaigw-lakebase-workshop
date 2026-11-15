"""A minimal LangGraph chat agent whose state lives in Lakebase.

    START -> assistant -> END

* The graph state is LangGraph's built-in `MessagesState` (a list of messages).
* The checkpointer is `PostgresSaver` on the Lakebase connection pool, so after
  every step LangGraph writes a checkpoint row keyed by `thread_id`.
  Restart or redeploy the app and the conversation is still there.
* The LLM is called through Unity Gateway (OpenAI-compatible chat completions on
  /ai-gateway/mlflow/v1) using the *model service* chosen in the UI -- so traffic
  splits, rate limits and guardrails you configure apply to the chat too.
"""
from __future__ import annotations

from functools import lru_cache

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, MessagesState, StateGraph

import db
from gateway import auth_header, host

SYSTEM_PROMPT = (
    "You are a concise, friendly assistant in a Databricks workshop about Unity Gateway "
    "and Lakebase. Keep answers short (under 120 words) unless asked for detail."
)


def _llm(service: str) -> ChatOpenAI:
    token = auth_header()["Authorization"].split(" ", 1)[1]
    return ChatOpenAI(
        model=service,                       # three-part UC model service name
        base_url=f"{host()}/ai-gateway/mlflow/v1",
        api_key=token,                       # the app service principal's OAuth token
        # Unity Gateway's chat-completions API expects `max_tokens`; newer
        # langchain-openai would send `max_completion_tokens`, so pass it raw.
        extra_body={"max_tokens": 600},
        temperature=0.3,
        max_retries=1,
        timeout=90,
    )


def assistant(state: MessagesState, config) -> dict:
    service = config["configurable"]["service"]
    reply = _llm(service).invoke([SystemMessage(SYSTEM_PROMPT), *state["messages"]])
    return {"messages": [reply]}


@lru_cache(maxsize=1)
def graph():
    builder = StateGraph(MessagesState)
    builder.add_node("assistant", assistant)
    builder.add_edge(START, "assistant")
    builder.add_edge("assistant", END)
    return builder.compile(checkpointer=PostgresSaver(db.pool()))


def _as_dict(m) -> dict:
    role = "user" if isinstance(m, HumanMessage) else "assistant" if isinstance(m, AIMessage) else m.type
    content = m.content
    if isinstance(content, list):  # some models return content blocks
        content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    meta = getattr(m, "response_metadata", {}) or {}
    return {"role": role, "content": content, "model": meta.get("model_name") or meta.get("model", "")}


def chat(thread_id: str, message: str, service: str) -> dict:
    cfg = {"configurable": {"thread_id": thread_id, "service": service}}
    out = graph().invoke({"messages": [HumanMessage(message)]}, cfg)
    return {"reply": _as_dict(out["messages"][-1]), "checkpoints": checkpoint_count(thread_id)}


def history(thread_id: str) -> list[dict]:
    snap = graph().get_state({"configurable": {"thread_id": thread_id}})
    return [_as_dict(m) for m in (snap.values or {}).get("messages", [])]


def checkpoint_count(thread_id: str) -> int:
    return sum(1 for _ in graph().get_state_history({"configurable": {"thread_id": thread_id}}))


def delete(thread_id: str) -> None:
    graph().checkpointer.delete_thread(thread_id)
