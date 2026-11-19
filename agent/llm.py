from __future__ import annotations

import warnings
from typing import Any, Iterable

from langchain.agents.middleware import ModelCallLimitMiddleware, ModelRetryMiddleware
from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage

from agent.config import Settings

# The NVIDIA library's own model table does not list every model NVIDIA serves, and warns for the others.
warnings.filterwarnings("ignore", message=".*(type is unknown|not known to support tools).*")

MAX_OUTPUT_TOKENS = 4096  # the provider's default of 1,024 cuts a proposal off
TIMEOUT_SECONDS = 180  # for one model call; a call that times out starts again from nothing
MAX_MODEL_CALLS = 6  # per ticket step: draft checks, the answer, and room for a correction
RETRIES = 8
RETRY_DELAY = 2.0  # seconds before the first retry; doubles up to a minute, about four minutes in total,
#                    long enough to sit out the model API's rate limit (429)


class ModelUnavailable(Exception):
    """The model cannot be reached. Tickets stay open; nothing is guessed."""


def build_chat(settings: Settings) -> Any:
    """The chat model named in MODEL, written as provider:model, for example nvidia:openai/gpt-oss-20b."""
    effort = {"reasoning_effort": settings.reasoning_effort} if settings.reasoning_effort else {}
    return init_chat_model(
        settings.model,
        temperature=settings.llm_temperature,
        max_completion_tokens=MAX_OUTPUT_TOKENS,
        timeout=TIMEOUT_SECONDS,
        model_kwargs=effort,  # sent only when set: not every model accepts it
    )


def middleware() -> list:
    """For every agent: retry a failed model call, and end a loop that does not finish."""
    return [
        ModelRetryMiddleware(max_retries=RETRIES, initial_delay=RETRY_DELAY, on_failure="error"),
        ModelCallLimitMiddleware(run_limit=MAX_MODEL_CALLS, exit_behavior="end"),
    ]


def ask(agent: Any, content: str, **for_tools: Any) -> tuple[Any, list]:
    """Run an agent on one message. Returns its typed answer, or None if it gave none, and every message.

    `for_tools` travels with this one call to the agent's tools, so nothing is shared between threads.
    """
    try:
        result = agent.invoke({"messages": [HumanMessage(content)]}, config={"configurable": for_tools})
    except Exception as error:  # still failing after the retries: the API is down, rate limited or timing out
        raise ModelUnavailable(f"{type(error).__name__}: {error}"[:300]) from error
    return result.get("structured_response"), result["messages"]


def tokens_used(messages: Iterable) -> tuple[int, int]:
    """Input and output tokens summed over the model's messages."""
    tokens_in = tokens_out = 0
    for message in messages:
        usage = getattr(message, "usage_metadata", None) or {}
        tokens_in += usage.get("input_tokens", 0)
        tokens_out += usage.get("output_tokens", 0)
    return tokens_in, tokens_out
