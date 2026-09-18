"""Exercise the live API routes and print a small human-readable report."""

from __future__ import annotations

import json
import re
from pathlib import Path
import os

import httpx


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    base_url = os.getenv("GRIDWISE_URL", "http://127.0.0.1:8000").rstrip("/")
    cases = json.loads(
        (ROOT / "docs" / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json").read_text(
            encoding="utf-8"
        )
    )["cases"]

    with httpx.Client(base_url=base_url, timeout=40.0) as client:
        health = client.get("/health")
        print(f"GET  /health:          HTTP {health.status_code}")
        if health.status_code != 200:
            return 1

        optimize = client.post("/optimize-energy", json=cases[0]["input"])
        print(f"POST /optimize-energy: HTTP {optimize.status_code}")
        if optimize.status_code != 200:
            print(f"  safe error: {optimize.json().get('detail', 'unknown error')}")
            return 1
        result = optimize.json()
        print(f"  scenario: {result['scenario_id']}")
        print(f"  hourly plan: {len(result['hourly_plan'])} hours")
        print(f"  total cost: {result['total_cost_bdt']:.2f} BDT")

        dashboard = client.get("/test-dashboard")
        print(f"GET  /test-dashboard:  HTTP {dashboard.status_code}")
        if dashboard.status_code != 200:
            return 1
        p95_match = re.search(
            r"p95 latency<span class=\"value\">([^<]+)", dashboard.text
        )
        print(f"  logged p95: {p95_match.group(1) if p95_match else 'not available'}")

    print("CUSTOM TEST: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
