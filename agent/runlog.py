from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from agent.redact import redact
from agent.schema import Decision

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


class RunLog:
    """One JSON line per ticket. Ticket text and the model's reasoning are never written."""

    def __init__(self, directory: Path | str = LOG_DIR):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.path = directory / f"run-{stamp}.jsonl"
        self._lock = threading.Lock()  # several tickets may finish at the same moment

    def write(self, decision: Decision, outcome: str, *, attempts: int = 0, http: dict | None = None) -> None:
        line = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ticket_id": decision.ticket_id,
            "outcome": outcome,
            "action": decision.action,
            "proposed_action": decision.proposed_action,
            "language": decision.language,
            "category": decision.category,
            "priority": decision.priority,
            "confidence": round(decision.confidence, 2),
            "reason": decision.reason,
            "changed_because": decision.reasons,
            "faq_ids": decision.faq_ids,
            "ticket_summary_en": decision.english_summary,
            "reply": decision.draft_reply,
            "reply_in_english": decision.back_translation,
            "rejected_draft": decision.rejected_draft,
            "handoff_note": decision.handoff_note,
            "redactions": decision.redactions,
            "post_attempts": attempts,
            "http": http or {},
            "tokens_in": decision.tokens_in,
            "tokens_out": decision.tokens_out,
            "seconds": decision.seconds,
        }
        # Last line of defence: nothing sensitive reaches the log, whatever produced it.
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_scrub(line), ensure_ascii=False) + "\n")


def _scrub(value):
    if isinstance(value, str):
        return redact(value)[0]
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    return value
