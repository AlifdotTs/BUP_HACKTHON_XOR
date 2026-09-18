import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import interpreter


client = TestClient(app)


def _request() -> dict:
    return {
        "scenario_id": "TEST-001",
        "operator_notes": ["The cafeteria menu changes tomorrow."],
        "hours": [
            {
                "hour": hour,
                "demand_kwh": 10,
                "solar_kwh": 0,
                "tariff_bdt_per_kwh": 5 if hour < 12 else 10,
            }
            for hour in range(24)
        ],
        "battery": {
            "capacity_kwh": 50,
            "initial_energy_kwh": 20,
            "minimum_energy_kwh": 5,
            "max_charge_kwh_per_hour": 10,
            "max_discharge_kwh_per_hour": 10,
        },
    }


def _mock_model(monkeypatch: pytest.MonkeyPatch, interpretation: list[dict]) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    output = json.dumps({"directive_interpretation": interpretation})

    class FakeCompletions:
        async def create(self, **body: object) -> dict:
            assert body["model"] == "openai/gpt-oss-20b"
            assert body["response_format"] == {"type": "json_object"}
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": output},
                    }
                ]
            }

    class FakeChat:
        completions = FakeCompletions()

    class FakeAsyncOpenAI:
        chat = FakeChat()

        def __init__(self, **kwargs: object) -> None:
            assert kwargs["api_key"] == "test-key"
            assert kwargs["base_url"] == "https://api.groq.com/openai/v1"

        async def __aenter__(self) -> "FakeAsyncOpenAI":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(interpreter, "AsyncOpenAI", FakeAsyncOpenAI)


def test_optimize_energy_returns_valid_plan_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_model(monkeypatch, [
        {
            "note_index": 0,
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
            "explanation": "The cafeteria note has no effect on today's energy plan.",
        }
    ])
    response = client.post("/optimize-energy", json=_request())

    assert response.status_code == 200
    body = response.json()
    assert body["scenario_id"] == "TEST-001"
    assert body["directive_interpretation"][0]["directive_type"] == "no_op"
    assert len(body["hourly_plan"]) == 24
    assert {entry["hour"] for entry in body["hourly_plan"]} == set(range(24))


CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "docs" / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json")
    .read_text(encoding="utf-8")
)["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_public_case_pipeline_with_model_output(
    case: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = case["expected_output"]
    _mock_model(monkeypatch, expected["directive_interpretation"])

    response = client.post("/optimize-energy", json=case["input"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scenario_id"] == case["id"]
    for actual, reference in zip(
        body["directive_interpretation"], expected["directive_interpretation"], strict=True
    ):
        assert {k: v for k, v in actual.items() if k != "explanation"} == {
            k: v for k, v in reference.items() if k != "explanation"
        }
    assert len(body["hourly_plan"]) == 24
    assert body["total_cost_bdt"] == pytest.approx(expected["total_cost_bdt"], abs=0.01)


def test_missing_model_configuration_is_controlled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OPEN_ROUTER_API", raising=False)
    response = client.post("/optimize-energy", json=_request())
    assert response.status_code == 503
    assert response.json() == {"detail": "Operator-note model is not configured"}


def test_invalid_model_directive_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_model(monkeypatch, [
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "solar_reduction",
            "structured_adjustment": {"hours": [24], "factor": 0.2},
            "explanation": "Invalid hour",
        }
    ])
    response = client.post("/optimize-energy", json=_request())
    assert response.status_code == 502
    assert response.json() == {
        "detail": "Model interpretation failed deterministic validation"
    }


def test_provider_error_does_not_leak_key_or_provider_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "secret-test-key")

    class FakeCompletions:
        async def create(self, **kwargs: object) -> None:
            raise interpreter.OpenAIError("secret-test-key rejected")

    class FakeChat:
        completions = FakeCompletions()

    class FakeAsyncOpenAI:
        chat = FakeChat()

        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncOpenAI":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(interpreter, "AsyncOpenAI", FakeAsyncOpenAI)

    response = client.post("/optimize-energy", json=_request())
    assert response.status_code == 503
    assert response.json() == {"detail": "Operator-note model request failed"}


def test_rate_limit_is_logged_and_retried(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("GROQ_MAX_RETRIES", "1")
    monkeypatch.setenv("GROQ_RETRY_DELAY_SECONDS", "0")
    output = json.dumps(
        {
            "directive_interpretation": [
                {
                    "note_index": 0,
                    "applies": False,
                    "directive_type": "no_op",
                    "structured_adjustment": None,
                    "explanation": "The cafeteria note has no effect on today's energy plan.",
                }
            ]
        }
    )

    class FakeCompletions:
        attempts = 0

        async def create(self, **kwargs: object) -> dict:
            self.attempts += 1
            if self.attempts == 1:
                error = interpreter.OpenAIError("rate limited")
                error.status_code = 429
                raise error
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": output},
                    }
                ]
            }

    completions = FakeCompletions()

    class FakeChat:
        pass

    chat = FakeChat()
    chat.completions = completions

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs: object) -> None:
            self.chat = chat

        async def __aenter__(self) -> "FakeAsyncOpenAI":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(interpreter, "AsyncOpenAI", FakeAsyncOpenAI)

    with caplog.at_level("WARNING", logger=interpreter.__name__):
        response = client.post("/optimize-energy", json=_request())

    assert response.status_code == 200
    assert completions.attempts == 2
    assert "Groq rate limit triggered" in caplog.text
