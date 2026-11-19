from __future__ import annotations

from dataclasses import dataclass

from agent.schema import Proposal

# The client's "always go to a human" rules, as data. This one table feeds the
# prompt and the gate, so they cannot drift apart.
# A priority floor is the lowest urgency such a ticket may get.


@dataclass(frozen=True)
class Rule:
    id: str
    description: str
    priority_floor: str | None = None


RULES: tuple[Rule, ...] = (
    Rule("refund_or_disputed_charge", "refunds or disputed charges", "P2"),
    Rule("cancellation_or_contract_change", "cancellations and contract changes", "P2"),
    Rule("personal_data_request", "personal data requests (GDPR, called AVG in Dutch)", "P2"),
    Rule("security_incident", "possible security incidents", "P1"),
    Rule("legal_threat", "legal threats", "P1"),
    Rule("churn_threat", "customers threatening to leave", "P2"),  # the contract is involved
)

CLOSE_CONFIDENCE_MIN = 0.9
PRIORITY_ORDER = ("P1", "P2", "P3", "P4")


def rules_for_prompt() -> str:
    return "\n".join(f"- {rule.id}: {rule.description}" for rule in RULES)


@dataclass(frozen=True)
class GateResult:
    action: str
    priority: str
    reasons: list[str]  # empty when the proposal passes unchanged


def gate(proposal: Proposal, *, truncated: bool, confidence_min: float) -> GateResult:
    """Apply the client's rules to a proposal. Any reason turns it into a handoff, except an injected instruction."""
    reasons = [f"rule:{rule_id}" for rule_id in proposal.rule_hits]
    priority = _apply_floors(proposal.priority, proposal.rule_hits)

    if proposal.injection_suspected:  # closed without an answer, whatever else the ticket holds
        return GateResult("close", priority, ["injection_suspected", *reasons])
    if priority == "P1":
        reasons.append("p1_goes_to_human")
    if proposal.language == "other" and proposal.action != "close":  # spam is closed whatever its language
        reasons.append("unsupported_language")
    if truncated:
        reasons.append("body_truncated")

    if proposal.action == "reply":
        if not proposal.questions or any(not q.covered for q in proposal.questions):
            reasons.append("not_fully_covered")
        if not proposal.evidence:
            reasons.append("no_evidence")
        if proposal.confidence < confidence_min:
            reasons.append("low_confidence")
    elif proposal.action == "close":
        if not proposal.is_spam_or_empty:
            reasons.append("close_without_spam_or_empty")
        elif proposal.confidence < CLOSE_CONFIDENCE_MIN:
            reasons.append("close_not_confident")

    return GateResult("handoff" if reasons else proposal.action, priority, reasons)


def _apply_floors(priority: str, rule_hits: list[str]) -> str:
    floors = [rule.priority_floor for rule in RULES if rule.id in rule_hits and rule.priority_floor]
    return min([priority, *floors], key=PRIORITY_ORDER.index)
