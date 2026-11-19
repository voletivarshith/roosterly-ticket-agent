from __future__ import annotations

import re
import time
from typing import Any

from agent.guardrails import check_reply, detect_language
from agent.knowledge import HelpCentre
from agent.llm import ModelUnavailable
from agent.policy import RULES, gate
from agent.redact import redact
from agent.schema import Decision, Proposal, Ticket
from agent.triage import TriageFailed

# The model proposes; this module decides. For every ticket:
#   0. redact, and deal with empty or oversized tickets without a model
#   1. triage proposes a decision
#   2. the gate applies the client's rules
#   3. guardrails compare a drafted reply with the help centre
#   4. the cross-check reads the reply back in English
# A reply is sent only if steps 2, 3 and 4 raise no objection. Anything else is a handoff,
# except a ticket that tries to instruct the agent: the gate closes it.

REASON_TEXT = {
    "injection_suspected": "The ticket contains text that tries to instruct the agent.",
    "p1_goes_to_human": "P1 tickets always go to a human.",
    "unsupported_language": "The ticket is not in Dutch, English or German.",
    "body_truncated": "The ticket was too long to read in full; only the first part was analysed.",
    "not_fully_covered": "The help centre does not fully answer this ticket.",
    "no_evidence": "The drafted reply had no supporting help-centre quote.",
    "low_confidence": "The agent was not confident enough to reply.",
    "close_without_spam_or_empty": "It did not clearly look like spam or an empty ticket.",
    "close_not_confident": "The agent was not sure enough to close it.",
    "crosscheck_failed": "The independent check of the reply could not be completed.",
    "crosscheck:does_not_answer": "The independent check found the reply does not answer the question.",
    "crosscheck:makes_promise": "The independent check found a promise in the reply.",
    "crosscheck:no_claims_found": "The independent check found nothing to verify in the reply.",
    "triage_failed": "Automated triage failed.",
}
RULE_TEXT = {rule.id: rule.description for rule in RULES}


class Pipeline:
    def __init__(self, triage: Any, crosscheck: Any, help_centre: HelpCentre, *, confidence_min: float, max_body_chars: int):
        self._triage, self._crosscheck, self._help_centre = triage, crosscheck, help_centre
        self._confidence_min, self._max_body_chars = confidence_min, max_body_chars

    def process(self, ticket: Ticket) -> Decision:
        started = time.monotonic()
        subject, subject_counts = redact(ticket.subject)
        body, body_counts = redact(ticket.body)
        redactions = dict(subject_counts + body_counts)

        if not re.search(r"\w{2,}", f"{subject} {body}"):
            return Decision(
                ticket_id=ticket.id, action="close", language="nl", category="other", priority="P4",
                reason="Empty ticket: there is no subject or body to act on.", confidence=1.0,
                proposed_action="close", reasons=["empty_ticket"],
            )

        truncated = len(body) > self._max_body_chars
        safe = ticket.model_copy(update={"subject": subject, "body": body[: self._max_body_chars]})

        try:
            proposal, tokens = self._triage.propose(safe)
        except ModelUnavailable:
            raise  # nothing can be decided without the model; the ticket stays open
        except TriageFailed as error:
            return self._triage_failed(safe, redactions, time.monotonic() - started, str(error))

        result = gate(proposal, truncated=truncated, confidence_min=self._confidence_min)
        reasons = list(result.reasons)
        back_translation = ""
        if result.action == "reply":
            reasons += [f"guardrail: {problem}" for problem in
                        check_reply(proposal.draft_reply, proposal.evidence, proposal.language, self._help_centre,
                                    f"{safe.subject}\n{safe.body}")]
        if result.action == "reply" and not reasons:
            try:
                problems, back_translation, check_tokens = self._crosscheck.review(safe, proposal)
                reasons += problems
                tokens["tokens_in"] += check_tokens["tokens_in"]
                tokens["tokens_out"] += check_tokens["tokens_out"]
            except ModelUnavailable:
                raise
            except Exception as error:  # no verdict means no reply; keep the cause for the log
                reasons.append(f"crosscheck_failed: {type(error).__name__}: {error}"[:200])

        action = "handoff" if reasons and result.action == "reply" else result.action
        language = proposal.language if proposal.language != "other" else "en"
        decision = Decision(
            ticket_id=ticket.id,
            action=action,
            language=language,
            category=proposal.category,
            priority=result.priority,
            reason=_clean(_reason(proposal, action, reasons)),
            confidence=proposal.confidence,
            draft_reply=proposal.draft_reply if action == "reply" else "",
            handoff_note=_clean(_handoff_note(safe, proposal, reasons)) if action == "handoff" else "",
            proposed_action=proposal.action,
            reasons=[_clean(reason) for reason in reasons],
            faq_ids=sorted({item.faq_id for item in proposal.evidence} | {i for q in proposal.questions for i in q.faq_ids}),
            english_summary=_clean(proposal.english_summary),
            back_translation=_clean(back_translation),
            rejected_draft=_clean(proposal.draft_reply) if proposal.action == "reply" and action != "reply" else "",
            redactions=redactions,
            tokens_in=tokens["tokens_in"],
            tokens_out=tokens["tokens_out"],
            seconds=round(time.monotonic() - started, 1),
        )
        return decision

    def _triage_failed(self, ticket: Ticket, redactions: dict, seconds: float, cause: str) -> Decision:
        language = detect_language(ticket.body) or "nl"
        return Decision(
            ticket_id=ticket.id, action="handoff", language=language, category="other", priority="P3",
            reason="Automated triage failed, so a human should read this ticket.", confidence=0.0,
            handoff_note="WANTS: unknown; automated triage failed for this ticket.\n"
                         "WHY HUMAN: The agent could not produce a reliable decision.\n"
                         f"FACTS: plan {ticket.customer_plan or 'unknown'} · subject: {ticket.subject[:80]}",
            proposed_action="none", reasons=[_clean(f"triage_failed: {cause}"[:200])], redactions=redactions,
            seconds=round(seconds, 1),
        )


def explain(reason: str) -> str:
    """Turn a reason code into a sentence a support colleague can read."""
    if reason.startswith("rule:"):
        return f"Client rule: {RULE_TEXT.get(reason[5:], reason[5:])} always go to a human."
    if reason.startswith("crosscheck:rule:"):
        return f"The independent check saw a client-rule topic: {RULE_TEXT.get(reason[16:], reason[16:])}."
    if reason.startswith("guardrail: "):
        return f"The drafted reply failed a check: {reason[11:]}."
    if reason.startswith("crosscheck:unsupported_claim: "):
        return f"The drafted reply made a claim the help centre does not support: \"{reason[30:]}\"."
    if reason.startswith("crosscheck:reply_in_"):
        return "The drafted reply was not in the customer's language."
    if reason.startswith("crosscheck_failed"):
        return REASON_TEXT["crosscheck_failed"]
    if reason.startswith("triage_failed"):
        return REASON_TEXT["triage_failed"]
    return REASON_TEXT.get(reason, reason)


def _reason(proposal: Proposal, action: str, reasons: list[str]) -> str:
    if action == proposal.action or not reasons:
        return proposal.reason
    changed = "Closed instead" if action == "close" else "Handed to a human instead"
    return f"{proposal.reason} {changed}: {explain(reasons[0])}"


def _handoff_note(ticket: Ticket, proposal: Proposal, reasons: list[str]) -> str:
    if reasons:
        why = " ".join(explain(reason) for reason in reasons[:3])
    else:
        why = proposal.why_human or "The help centre does not cover this."
    facts = [f"plan {ticket.customer_plan or 'unknown'}", f"via {ticket.channel or 'unknown'}",
             f"wrote in {proposal.language}"]
    if proposal.facts:
        facts.append(proposal.facts)
    covered = sorted({i for q in proposal.questions if q.covered for i in q.faq_ids})
    if covered:
        facts.append(f"help centre covers part of it: {', '.join(covered)}")
    return (
        f"WANTS: {proposal.wants or proposal.english_summary}\n"
        f"WHY HUMAN: {why}\n"
        f"FACTS: {' · '.join(facts)}"
    )


def _clean(text: str) -> str:
    """A second redaction pass over everything the agent writes itself."""
    return redact(text)[0]
