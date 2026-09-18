"""Run all public cases against a deployed GridWise API and save full JSON responses."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import time
from time import perf_counter

import httpx


ROOT = Path(__file__).resolve().parents[1]
FIELDS = ("note_index", "applies", "directive_type", "structured_adjustment")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="https://bup-fest-api-xor.onrender.com",
        help="Deployed API base URL",
    )
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument(
        "--log-path",
        default="logs/remote_10_post_test.log",
        help="Path for the full response log",
    )
    args = parser.parse_args()
    if args.delay < 0:
        parser.error("--delay must be non-negative")

    cases = json.loads(
        (ROOT / "docs" / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json").read_text(
            encoding="utf-8"
        )
    )["cases"]
    lines = [
        "GridWise remote 10-case POST test with full responses",
        f"Endpoint: {args.base_url.rstrip('/')}/optimize-energy",
        f"Request count: {len(cases)}",
        f"Delay between requests: {args.delay:g} seconds",
        "",
    ]
    passed: list[bool] = []
    latencies: list[float] = []

    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=90.0) as client:
        for index, case in enumerate(cases):
            if index and args.delay:
                time.sleep(args.delay)
            started = perf_counter()
            try:
                response = client.post("/optimize-energy", json=case["input"])
                latency_ms = (perf_counter() - started) * 1000
                latencies.append(latency_ms)
                body = response.json()
                expected = case["expected_output"]
                interpretation = response.status_code == 200 and len(
                    body.get("directive_interpretation", [])
                ) == len(expected["directive_interpretation"]) and all(
                    all(got[field] == want[field] for field in FIELDS)
                    for got, want in zip(
                        body.get("directive_interpretation", []),
                        expected["directive_interpretation"],
                        strict=True,
                    )
                )
                cost = response.status_code == 200 and abs(
                    body.get("total_cost_bdt", 0) - expected["total_cost_bdt"]
                ) <= 0.01
                plan_shape = response.status_code == 200 and len(
                    body.get("hourly_plan", [])
                ) == 24
                case_passed = interpretation and cost and plan_shape
                passed.append(case_passed)
                lines.extend(
                    [
                        f"=== {case['id']} ===",
                        f"HTTP status: {response.status_code}",
                        f"Latency ms: {latency_ms:.1f}",
                        f"Interpretation match: {interpretation}",
                        f"Cost match: {cost}",
                        f"24-hour plan: {plan_shape}",
                        "Raw response:",
                        json.dumps(body, indent=2, ensure_ascii=False),
                        "",
                    ]
                )
                print(
                    f"{case['id']}: HTTP {response.status_code}; "
                    f"pass={case_passed}; latency_ms={latency_ms:.1f}",
                    flush=True,
                )
            except Exception as exc:
                latency_ms = (perf_counter() - started) * 1000
                latencies.append(latency_ms)
                passed.append(False)
                lines.extend(
                    [
                        f"=== {case['id']} ===",
                        f"ERROR: {type(exc).__name__}",
                        f"Latency ms: {latency_ms:.1f}",
                        "Raw response: unavailable",
                        "",
                    ]
                )
                print(f"{case['id']}: ERROR {type(exc).__name__}", flush=True)

    ordered = sorted(latencies)
    p95 = ordered[max(0, min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1))]
    lines.extend(
        [
            "=== Aggregate ===",
            f"Passed: {sum(passed)}/{len(cases)}",
            f"Minimum latency ms: {min(ordered):.1f}",
            f"Maximum latency ms: {max(ordered):.1f}",
            f"P95 latency ms: {p95:.1f}",
        ]
    )
    log_path = ROOT / args.log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Full response log: {log_path}")
    return int(sum(passed) != len(cases))


if __name__ == "__main__":
    raise SystemExit(main())
