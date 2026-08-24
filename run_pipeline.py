"""
End-to-end pipeline runner.

Usage:
    python run_pipeline.py --mode simulated              # full 86-event batch, no live API calls
    python run_pipeline.py --mode live --limit 8          # small REAL batch against Razorpay

Then:
    python reconcile_pipeline.py --mode simulated   # or --mode live, matching what you ran

WHY TWO MODES (found during real testing, see CHALLENGES.md):
Razorpay test mode enforces a hard cap of 30 payment links on the account -- not a
rate limit, an absolute ceiling that doesn't reset. Running the full 86-event batch
against the live API is therefore impossible past ~30 events, and burns real quota
on every dev/test run.

  --mode simulated: runs the FULL dataset through SimulatedRazorpayClient (deterministic,
                     reproducible, clearly labeled as simulated). This is what produces
                     your batch-level dashboard metrics for the README/submission.
  --mode live:       runs a SMALL subset (--limit, default 8) against the REAL Razorpay
                     test API. This is what you demo on video / screenshot to prove the
                     actual integration genuinely works end to end.

Both modes use the exact same classify -> decide -> execute pipeline code --
only the Razorpay client implementation differs.
"""

import argparse
import json
import os
import time
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from app.models import CheckoutEvent
from app.agent.classifier import classify
from app.agent.recovery_actions import decide_action, execute_action
from app.audit_log import AuditLogger
from app.razorpay_client import RazorpayRecoveryClient, SimulatedRazorpayClient
import anthropic

DELAY_BETWEEN_LIVE_CALLS_SEC = 1.5  # only used in --mode live, to avoid tripping rate limits


def load_events(path: str = "data/synthetic_events.json") -> list[CheckoutEvent]:
    with open(path) as f:
        raw_events = json.load(f)

    events = []
    parse_failures = 0
    for raw in raw_events:
        try:
            events.append(CheckoutEvent(**raw))
        except Exception as e:
            parse_failures += 1
            print(f"SKIPPING malformed event {raw.get('event_id', '?')}: {e}")

    if parse_failures:
        print(f"\n{parse_failures} event(s) skipped due to parse failures (see above).\n")

    return events


def run(mode: str, limit, dataset_path: str = "data/synthetic_events.json"):
    if mode == "live":
        razorpay_client = RazorpayRecoveryClient()
    else:
        razorpay_client = SimulatedRazorpayClient()

    llm_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    audit = AuditLogger(log_path=f"data/audit_log_{mode}.jsonl")
    audit.clear()

    run_id = datetime.now().strftime("%Y%m%d%H%M%S")
    print(f"Mode: {mode.upper()} | Run ID: {run_id}\n")

    events = load_events(dataset_path)
    if limit is not None:
        events = events[:limit]
        print(f"Limiting to first {limit} events (--limit).\n")

    for i, event in enumerate(events, start=1):
        classification = classify(event, llm_client=llm_client)
        action = decide_action(event, classification)

        unique_reference_id = f"{event.event_id}_{run_id}"
        outcome = execute_action(
            event, classification, action, razorpay_client, reference_id=unique_reference_id
        )
        audit.log(outcome)

        status_note = "" if outcome.action_success else f"  [FAILED: {outcome.error_message}]"
        print(f"[{i}/{len(events)}] {event.event_id}: {classification.predicted_reason.value} "
              f"({classification.method_used}, conf={classification.confidence:.2f}) "
              f"-> {action.value}{status_note}")

        if mode == "live" and action.value in (
            "send_payment_link", "offer_alternate_payment_method", "send_discount_nudge"
        ):
            time.sleep(DELAY_BETWEEN_LIVE_CALLS_SEC)

    print(f"\nDone. {len(events)} events processed -> data/audit_log_{mode}.jsonl")
    if mode == "live":
        print("\nNEXT STEP: open a few of the payment link short_urls above in your browser,")
        print("complete a test payment using Razorpay's test card numbers, then run:")
        print("    python reconcile_pipeline.py --mode live")
    else:
        print("\nNEXT STEP: python reconcile_pipeline.py --mode simulated")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["simulated", "live"], default="simulated")
    parser.add_argument("--limit", type=int, default=None,
                         help="Max number of events to process. Defaults to 8 in live mode, all in simulated mode.")
    args = parser.parse_args()

    limit = args.limit
    if args.mode == "live" and limit is None:
        limit = 8  # safe default to stay well within the 30-link test cap across repeated runs

    run(mode=args.mode, limit=limit)