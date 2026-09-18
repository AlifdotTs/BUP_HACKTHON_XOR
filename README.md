# GridWise Energy Optimizer

FastAPI service for the BUP CSE Fest 2026 GridWise challenge. A language model interprets operator notes into structured directives; deterministic code validates the directives and a SciPy linear program produces the 24-hour schedule.

## Local setup

Create and activate a virtual environment, then install the dependencies:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
```

If you do not already have a `.env` file, copy the environment template. Then set `GROQ_API_KEY` in `.env` or in your shell environment:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# Edit .env and set GROQ_API_KEY to your key.
```

Never commit the key or a `.env` file. The integration calls Groq's OpenAI-compatible Chat Completions API. `GROQ_MODEL` defaults to `openai/gpt-oss-20b`. The service sends only synthetic operator notes and battery capacity to the model. JSON mode requests valid JSON, and deterministic code validates the directives before optimization. The model call has a 25-second overall deadline.

Start the API from the repository root:

```powershell
python -m uvicorn app.main:app --reload --env-file .env
```

In Git Bash, use your activated virtual environment from the repository root:

```bash
source .venv/Scripts/activate
test -f .env || cp .env.example .env
# Edit .env and set GROQ_API_KEY to your key.
python -m uvicorn app.main:app --reload --env-file .env
```

Check readiness:

```powershell
curl.exe http://127.0.0.1:8000/health
```

Expected response:

```json
{"status":"ok"}
```

## Docker

Docker Compose reads `.env` from the project directory and forwards `GROQ_API_KEY` and optional `GROQ_MODEL` into the container.

Build and run the API with Docker Compose:

```powershell
docker compose up --build
```

The API is then available at `http://127.0.0.1:8000`. Stop it with:

```powershell
docker compose down
```

To build and run the image directly:

```powershell
docker build -t gridwise-api .
docker run --rm -p 8000:8000 --env-file .env gridwise-api
```

The image includes a health check for `GET /health`.

Run the tests:

```powershell
pytest
```

Run one public case against the real Groq model after setting `GROQ_API_KEY`:

```powershell
python -m scripts.smoke_openai
```

This checks the interpreted directives and optimal cost for `SAMPLE-01` without displaying the API key. It makes a real provider request.

Use `python -m scripts.smoke_openai --all` to run all 10 public cases through the live Groq model. This makes 10 provider requests and compares each interpretation and cost with the public reference. The runner waits one second between requests by default; set `GROQ_REQUEST_DELAY_SECONDS` or pass `--delay 2` to increase it. The API logs and retries HTTP 429 rate-limit responses with exponential backoff; configure `GROQ_MAX_RETRIES` and `GROQ_RETRY_DELAY_SECONDS`.

## Optimization endpoint

`POST /optimize-energy` validates the scenario, requests a structured interpretation from the configured language model, checks every directive against deterministic guardrails, solves the linear program, and independently replays the schedule. A missing key returns 503; an invalid model interpretation returns a controlled 502 response. Do not interpret either as a successful plan.

Example request:

```json
{
  "scenario_id": "TEST-001",
  "operator_notes": ["The cafeteria menu changes tomorrow."],
  "hours": [
    {"hour": 0, "demand_kwh": 10, "solar_kwh": 0, "tariff_bdt_per_kwh": 5}
  ],
  "battery": {
    "capacity_kwh": 50,
    "initial_energy_kwh": 20,
    "minimum_energy_kwh": 5,
    "max_charge_kwh_per_hour": 10,
    "max_discharge_kwh_per_hour": 10
  }
}
```

The real request must contain all 24 hourly entries. The endpoint returns the required 24-hour `hourly_plan`, totals, and directive interpretation. The tests run the public sample cases with mocked model responses; they verify the solver and API integration without making paid API calls. Run a live model smoke test separately before deployment.
