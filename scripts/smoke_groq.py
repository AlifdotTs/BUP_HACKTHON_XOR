"""Run public GridWise cases through the real Groq interpreter."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi.testclient import TestClient

from app.main import app


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="Run all 10 public cases")
    parser.add_argument(
        "--delay",
        type=float,
        default=None,
        help="Seconds to wait between requests; defaults to GROQ_REQUEST_DELAY_SECONDS or 1.0",
    )
    args = parser.parse_args()

    if not os.getenv("GROQ_API_KEY"):
        print("GROQ_API_KEY is unavailable; set it in .env or your shell")
        return 1

    try:
        delay = args.delay if args.delay is not None else float(
            os.getenv("GROQ_REQUEST_DELAY_SECONDS", "1.0")
        )
    except ValueError:
        print("GROQ_REQUEST_DELAY_SECONDS must be a non-negative number")
        return 1
    if delay < 0:
        print("--delay must be non-negative")
        return 1

    cases = json.loads(
        (ROOT / "docs" / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json").read_text(
            encoding="utf-8"
        )
    )["cases"]
    fields = ("note_index", "applies", "directive_type", "structured_adjustment")
    selected = cases if args.all else [next(item for item in cases if item["id"] == "SAMPLE-01")]
    client = TestClient(app)
    passed = 0

    for index, case in enumerate(selected):
        if index and delay:
            time.sleep(delay)
        response = client.post("/optimize-energy", json=case["input"])
        if response.status_code != 200:
            print(f"{case['id']}: HTTP {response.status_code}", flush=True)
            continue

        actual = response.json()
        expected = case["expected_output"]
        interpretation_matches = len(actual["directive_interpretation"]) == len(
            expected["directive_interpretation"]
        ) and all(
            all(got[field] == want[field] for field in fields)
            for got, want in zip(
                actual["directive_interpretation"],
                expected["directive_interpretation"],
                strict=True,
            )
        )
        cost_matches = abs(actual["total_cost_bdt"] - expected["total_cost_bdt"]) <= 0.01
        passed += interpretation_matches and cost_matches
        print(
            f"{case['id']}: interpretation={interpretation_matches}, "
            f"optimal_cost={cost_matches}, returned_bdt={actual['total_cost_bdt']:.2f}",
            flush=True,
        )

    print(f"Passed: {passed}/{len(selected)}")
    return int(passed != len(selected))


if __name__ == "__main__":
    raise SystemExit(main())
