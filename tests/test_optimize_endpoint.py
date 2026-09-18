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
    monkeypatch.setenv("GROQ_MAX_RETRIES", "0")
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
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    response = client.post("/optimize-energy", json=_request())
    assert response.status_code == 503
    assert response.json() == {"detail": "Operator-note model is not configured"}


def test_openrouter_is_used_after_groq_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "groq-test-key")
    monkeypatch.setenv("GROQ_MAX_RETRIES", "0")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-test-key")
    monkeypatch.setenv("OPENROUTER_MAX_RETRIES", "0")
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
    calls: list[tuple[str, str]] = []

    class FakeCompletions:
        def __init__(self, base_url: str) -> None:
            self.base_url = base_url

        async def create(self, **kwargs: object) -> dict:
            calls.append((self.base_url, str(kwargs["model"])))
            if "groq.com" in self.base_url:
                error = interpreter.OpenAIError("primary unavailable")
                error.status_code = 503
                raise error
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": output},
                    }
                ]
            }

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs: object) -> None:
            self.api_key = str(kwargs["api_key"])
            self.base_url = str(kwargs["base_url"])
            self.chat = type(
                "FakeChat",
                (),
                {"completions": FakeCompletions(self.base_url)},
            )()

        async def __aenter__(self) -> "FakeAsyncOpenAI":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(interpreter, "AsyncOpenAI", FakeAsyncOpenAI)

    with caplog.at_level("WARNING", logger=interpreter.__name__):
        response = client.post("/optimize-energy", json=_request())

    assert response.status_code == 200
    assert calls == [
        ("https://api.groq.com/openai/v1", "openai/gpt-oss-20b"),
        ("https://openrouter.ai/api/v1", "openai/gpt-oss-20b:free"),
    ]
    assert "Groq unavailable" in caplog.text


def test_openrouter_error_remains_controlled_and_secret_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-secret-key")
    monkeypatch.setenv("OPENROUTER_MAX_RETRIES", "0")

    class FakeCompletions:
        async def create(self, **kwargs: object) -> None:
            raise interpreter.OpenAIError("openrouter-secret-key rejected")

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs: object) -> None:
            self.chat = type(
                "FakeChat", (), {"completions": FakeCompletions()}
            )()

        async def __aenter__(self) -> "FakeAsyncOpenAI":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(interpreter, "AsyncOpenAI", FakeAsyncOpenAI)

    response = client.post("/optimize-energy", json=_request())

    assert response.status_code == 503
    assert response.json() == {"detail": "Operator-note model request failed"}
    assert "openrouter-secret-key" not in response.text


def test_test_dashboard_reports_logged_p95_latency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("METRICS_DB_PATH", str(tmp_path / "metrics.sqlite3"))
    _mock_model(monkeypatch, [
        {
            "note_index": 0,
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
            "explanation": "The cafeteria note has no effect on today's energy plan.",
        }
    ])

    optimize_response = client.post("/optimize-energy", json=_request())
    dashboard_response = client.get("/test-dashboard")

    assert optimize_response.status_code == 200
    assert dashboard_response.status_code == 200
    assert "GridWise Test Dashboard" in dashboard_response.text
    assert "p95 latency" in dashboard_response.text
    assert "POST" in dashboard_response.text
    assert "/optimize-energy" in dashboard_response.text
    assert "max-height: 420px" in dashboard_response.text
    assert "overflow: auto" in dashboard_response.text
    assert "position: sticky" in dashboard_response.text


def test_timeout_is_retried(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPEN_ROUTER_API", raising=False)
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
                raise TimeoutError("temporary timeout")
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": output},
                    }
                ]
            }

    completions = FakeCompletions()

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs: object) -> None:
            self.chat = type(
                "FakeChat", (), {"completions": completions}
            )()

        async def __aenter__(self) -> "FakeAsyncOpenAI":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(interpreter, "AsyncOpenAI", FakeAsyncOpenAI)

    with caplog.at_level("WARNING", logger=interpreter.__name__):
        response = client.post("/optimize-energy", json=_request())

    assert response.status_code == 200
    assert completions.attempts == 2
    assert "Groq timeout" in caplog.text


def test_retry_exhaustion_returns_controlled_503(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPEN_ROUTER_API", raising=False)
    monkeypatch.setenv("GROQ_MAX_RETRIES", "1")
    monkeypatch.setenv("GROQ_RETRY_DELAY_SECONDS", "0")

    class FakeCompletions:
        attempts = 0

        async def create(self, **kwargs: object) -> None:
            self.attempts += 1
            error = interpreter.OpenAIError("temporary upstream failure")
            error.status_code = 503
            raise error

    completions = FakeCompletions()

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs: object) -> None:
            self.chat = type(
                "FakeChat", (), {"completions": completions}
            )()

        async def __aenter__(self) -> "FakeAsyncOpenAI":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(interpreter, "AsyncOpenAI", FakeAsyncOpenAI)

    with caplog.at_level("WARNING", logger=interpreter.__name__):
        response = client.post("/optimize-energy", json=_request())

    assert response.status_code == 503
    assert response.json() == {"detail": "Operator-note model request failed"}
    assert completions.attempts == 2
    assert "Groq provider error" in caplog.text or "retries exhausted" in caplog.text


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
    monkeypatch.setenv("GROQ_MAX_RETRIES", "0")

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


def test_malformed_request_returns_controlled_400() -> None:
    response = client.post("/optimize-energy", content="{not-json")

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid request"}


def test_structurally_invalid_request_returns_controlled_400() -> None:
    payload = _request()
    payload["hours"] = payload["hours"][:-1]

    response = client.post("/optimize-energy", json=payload)

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid request"}


def test_transient_provider_error_is_retried(
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
                error = interpreter.OpenAIError("temporary upstream failure")
                error.status_code = 503
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

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs: object) -> None:
            self.chat = type("FakeChat", (), {"completions": completions})()

        async def __aenter__(self) -> "FakeAsyncOpenAI":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(interpreter, "AsyncOpenAI", FakeAsyncOpenAI)

    with caplog.at_level("WARNING", logger=interpreter.__name__):
        response = client.post("/optimize-energy", json=_request())

    assert response.status_code == 200
    assert completions.attempts == 2
    assert "Groq provider error" in caplog.text


def test_invalid_model_output_is_retried(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("GROQ_MAX_RETRIES", "1")
    monkeypatch.setenv("GROQ_RETRY_DELAY_SECONDS", "0")
    valid_output = json.dumps(
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
            content = '{"directive_interpretation": []}' if self.attempts == 1 else valid_output
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ]
            }

    completions = FakeCompletions()

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs: object) -> None:
            self.chat = type("FakeChat", (), {"completions": completions})()

        async def __aenter__(self) -> "FakeAsyncOpenAI":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(interpreter, "AsyncOpenAI", FakeAsyncOpenAI)

    with caplog.at_level("WARNING", logger=interpreter.__name__):
        response = client.post("/optimize-energy", json=_request())

    assert response.status_code == 200
    assert completions.attempts == 2
    assert "Groq invalid interpretation" in caplog.text


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
