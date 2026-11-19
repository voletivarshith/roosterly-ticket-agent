from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from agent.config import Settings, load_settings
from agent.crosscheck import CrossCheck
from agent.helpdesk import AlreadyHandled, HelpdeskClient, RequestRejected, Unavailable
from agent.knowledge import load_help_centre
from agent.llm import ModelUnavailable, build_chat
from agent.pipeline import Pipeline
from agent.runlock import run_lock
from agent.runlog import RunLog
from agent.schema import Decision, Ticket
from agent.triage import Triage

DELIVERED = ("sent", "sent_unconfirmed", "already_handled")


def show(text: str, end: str = "\n") -> None:
    print(text, end=end, flush=True)


def build_client(settings: Settings) -> HelpdeskClient:
    return HelpdeskClient(settings.helpdesk_base_url, settings.helpdesk_api_key, settings.candidate_email)


def build_pipeline(settings: Settings) -> Pipeline:
    help_centre = load_help_centre()
    chat = build_chat(settings)  # one model for triage and for the cross-check, which starts from a clean context
    return Pipeline(
        Triage(chat, help_centre),
        CrossCheck(chat, help_centre),
        help_centre,
        confidence_min=settings.confidence_min,
        max_body_chars=settings.max_body_chars,
    )


def run_tickets(
    client: HelpdeskClient,
    decide: Callable[[Ticket], Decision],
    log: RunLog,
    *,
    workers: int = 1,
    echo: Callable[..., None] = show,
) -> Counter:
    """Decide and send one answer per open ticket, `workers` tickets at a time.

    A ticket that gets no answer stays open for the next run.
    """
    tickets = [Ticket.model_validate(raw) for raw in client.open_tickets()]
    echo(f"{len(tickets)} open tickets, {workers} at a time")

    def handle(ticket: Ticket) -> tuple[Decision, str]:
        """One ticket from start to finish, on one thread: decide, send, log."""
        decision = decide(ticket)
        attempts = 0
        before = Counter(client.stats)
        try:
            delivery = client.post_decision(ticket.id, decision.action, decision.body())
            outcome, attempts = ("sent" if delivery.confirmed else "sent_unconfirmed"), delivery.attempts
        except AlreadyHandled:
            outcome = "already_handled"
        except RequestRejected as error:
            outcome = f"rejected: {error}"
        except Unavailable:
            outcome = "helpdesk_unavailable"
        log.write(decision, outcome, attempts=attempts, http=dict(Counter(client.stats) - before))
        return decision, outcome

    summary: Counter = Counter()
    tokens_in = tokens_out = 0
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(handle, ticket) for ticket in tickets]
        try:
            for done, future in enumerate(as_completed(futures), start=1):
                decision, outcome = future.result()
                summary[decision.action if outcome in DELIVERED else "not_delivered"] += 1
                tokens_in += decision.tokens_in
                tokens_out += decision.tokens_out
                echo(f"[{done:>2}/{len(tickets)}] {decision.ticket_id}  {decision.action:<8}{decision.category:<16}"
                     f"{decision.priority}  {decision.language}  {decision.seconds:>5}s  "
                     f"{decision.tokens_in:>6} in {decision.tokens_out:>5} out  "
                     f"confidence {decision.confidence:.2f}  {outcome}")
                if decision.reasons:  # what the code checks found; this is why a proposal became a handoff
                    echo(f"          model proposed {decision.proposed_action} · {'; '.join(decision.reasons)[:160]}")
        except BaseException:  # a failed ticket or Ctrl-C: tickets not started yet stay open
            pool.shutdown(cancel_futures=True)
            raise
    echo(f"Tokens: {tokens_in:,} in, {tokens_out:,} out · {(time.monotonic() - started) / 60:.1f} minutes")
    return summary


def command_run(settings: Settings) -> int:
    client = build_client(settings)
    with run_lock():
        pipeline = build_pipeline(settings)
        log = RunLog()
        show(f"Model {settings.model}. Log: {log.path}")
        try:
            summary = run_tickets(client, pipeline.process, log, workers=settings.workers)
        except ModelUnavailable as error:
            print(f"\nStopped: the model call kept failing ({error}). Tickets not yet handled stay open; run again.")
            return 1
    print(f"\n{dict(summary)}  log: {log.path}")
    status = client.status()
    print(json.dumps(status))
    return 1 if summary["not_delivered"] else 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="agent", description="Roosterly support-ticket agent")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("run", help="Fetch the tickets and process the tickets")
    actions.add_parser("status", help="show open and handled ticket counts")
    reset = actions.add_parser("reset", help="re-open All the tickets")
    reset.add_argument("--yes", action="store_true", help="confirm: this wipes the previous run")
    args = parser.parse_args()

    settings = load_settings()
    if args.action == "run":
        return command_run(settings)
    if args.action == "status":
        print(json.dumps(build_client(settings).status(), indent=2))
        return 0
    if args.action == "reset":
        if not args.yes:
            print("Reset re-opens all tickets and wipes the previous run, including a final run.")
            print("Run again with --yes to confirm.")
            return 2
        print(json.dumps(build_client(settings).reset(), indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
