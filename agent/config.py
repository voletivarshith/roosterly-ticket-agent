"""All configuration comes from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# NVIDIA_API_KEY is read by the model library itself; it is listed here so a missing key fails at start.
REQUIRED = ("HELPDESK_BASE_URL", "HELPDESK_API_KEY", "CANDIDATE_EMAIL", "MODEL", "NVIDIA_API_KEY")


@dataclass(frozen=True)
class Settings:
    helpdesk_base_url: str
    helpdesk_api_key: str
    candidate_email: str
    model: str
    reasoning_effort: str  # empty: the model's own default
    llm_temperature: float
    confidence_min: float
    max_body_chars: int
    workers: int  # tickets handled at the same time


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def load_settings() -> Settings:
    load_dotenv()
    missing = [name for name in REQUIRED if not _get(name)]
    if missing:
        raise SystemExit(
            f"Missing environment variables: {', '.join(missing)}. "
            "Copy .env.example to .env and fill them in."
        )
    return Settings(
        helpdesk_base_url=_get("HELPDESK_BASE_URL"),
        helpdesk_api_key=_get("HELPDESK_API_KEY"),
        candidate_email=_get("CANDIDATE_EMAIL"),
        model=_get("MODEL"),
        reasoning_effort=_get("REASONING_EFFORT").lower(),
        llm_temperature=float(_get("LLM_TEMPERATURE", "0")),
        confidence_min=float(_get("CONFIDENCE_MIN", "0.7")),
        max_body_chars=int(_get("MAX_BODY_CHARS", "6000")),
        workers=max(1, int(_get("WORKERS", "1"))),
    )
