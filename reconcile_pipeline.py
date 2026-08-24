"""
Reconciliation script -- run this SEPARATELY and LATER than run_pipeline.py.

Usage:
    python reconcile_pipeline.py --mode simulated   # matches a simulated run
    python reconcile_pipeline.py --mode live        # matches a live run, after you've
                                                      # manually paid a few test links

Reads outcomes from data/audit_log_<mode>.jsonl, checks each payment link's status,
and computes the final dashboard summary: total abandoned, total offered, and total
CONFIRMED recovered (only counting links where status == "paid").
"""

import argparse
import json
import os
from dotenv import load_dotenv

load_dotenv()

from app.audit_log import AuditLogger
from app.reconciliation import reconcile_batch
from app.dashboard import compute_summary
from app.razorpay_client import RazorpayRecoveryClient, SimulatedRazorpayClient


def run(mode: str, dataset_path: str = "data/synthetic_events.json"):
    audit = AuditLogger(log_path=f"data/audit_log_{mode}.jsonl")
    outcomes = audit.load_all()
    if not outcomes:
        print(f"No outcomes found in data/audit_log_{mode}.jsonl -- run run_pipeline.py --mode {mode} first.")
        return

    if mode == "live":
        razorpay_client = RazorpayRecoveryClient()
    else:
        # Fresh SimulatedRazorpayClient instance -- fetch_payment_link_status re-derives
        # each link's status deterministically from its link_id, so this correctly
        # matches what run_pipeline.py --mode simulated decided at creation time,
        # even though this is a separate script/process with no shared memory.
        razorpay_client = SimulatedRazorpayClient()

    with open(dataset_path) as f:
        raw_events = json.load(f)
    original_cart_values = {e["event_id"]: e["cart_value"] for e in raw_events}

    print(f"Loaded {len(outcomes)} outcomes from data/audit_log_{mode}.jsonl. Checking payment status...\n")
    reconciled = reconcile_batch(outcomes, razorpay_client)

    for o in reconciled:
        if o.payment_link_id:
            status_note = "PAID" if o.confirmed_recovered_amount else "not yet paid"
            print(f"{o.event_id}: {o.payment_link_id} -> {status_note}")

    summary = compute_summary(reconciled, original_cart_values)
    summary["mode"] = mode

    print("\n" + "=" * 60)
    print(f"RECOVERY PIPELINE SUMMARY ({mode.upper()})")
    print("=" * 60)
    print(json.dumps(summary, indent=2))

    out_path = f"data/dashboard_summary_{mode}.json"
    with open(out_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["simulated", "live"], default="simulated")
    args = parser.parse_args()
    run(mode=args.mode)