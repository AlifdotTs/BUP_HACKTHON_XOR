# GridWise Energy Optimizer

FastAPI service for the BUP CSE Fest 2026 GridWise challenge. A language model interprets operator notes into structured directives; deterministic code validates the directives and a SciPy linear program produces the 24-hour schedule.

## Local setup

Create and activate a virtual environment, then install the dependencies:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
```

If you do not already have a `.env` file, copy the environment template. Set `GROQ_API_KEY` for the primary provider and optionally set `OPENROUTER_API_KEY` for backup failover:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# Edit .env and set GROQ_API_KEY. Optionally set OPENROUTER_API_KEY.
```

Never commit the key or a `.env` file. Groq is the primary OpenAI-compatible provider. If `GROQ_API_KEY` is unavailable or Groq fails after its retries, the service uses OpenRouter as a secondary provider when `OPENROUTER_API_KEY` is configured. The default backup model is `openai/gpt-oss-20b` (low-cost, not free). The service sends only synthetic operator notes and battery capacity to the model. JSON mode requests valid JSON, and deterministic code validates the directives before optimization. Provider retries, invalid-output retries, and backoff share a 28-second total request budget so the API remains within the judge's 30-second limit.

Start the API from the repository root:

```powershell
python -m uvicorn app.main:app --reload --env-file .env
```

In Git Bash, use your activated virtual environment from the repository root:

```bash
source .venv/Scripts/activate
test -f .env || cp .env.example .env
# Edit .env and set GROQ_API_KEY. Optionally set OPENROUTER_API_KEY.
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

Docker Compose reads `.env` from the project directory and forwards the Groq primary and OpenRouter backup settings into the container.

Build and run the API with Docker Compose:

```powershell
docker compose up --build
```

The API is then available at `http://127.0.0.1:8000`. Stop it with:

```powershell
docker compose down
```

Compose reports the API as `healthy` after its `/health` check passes and stores the SQLite metrics in the named `metrics_data` volume. View the local metrics dashboard at `http://127.0.0.1:8000/test-dashboard`.

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
python -m scripts.smoke_groq
```

This checks the interpreted directives and optimal cost for `SAMPLE-01` without displaying the API key. It makes a real provider request.

Use `python -m scripts.smoke_groq --all` to run all 10 public cases through the live Groq model. This makes 10 provider requests and compares each interpretation and cost with the public reference. The runner waits one second between requests by default; set `GROQ_REQUEST_DELAY_SECONDS` or pass `--delay 2` to increase it. The API logs and retries transient provider errors, HTTP 429 rate-limit responses, timeouts, and invalid model output with exponential backoff; configure `GROQ_MAX_RETRIES`, `GROQ_RETRY_DELAY_SECONDS`, and `GROQ_TOTAL_TIMEOUT_SECONDS`.

### Local latency dashboard

Every real `POST /optimize-energy` request records only its UTC timestamp, route, status code, and latency in SQLite. Request bodies, headers, and secrets are never stored. Open this route after exercising the API:

```powershell
start http://127.0.0.1:8000/test-dashboard
```

The dashboard displays request count, successful requests, errors, recent requests, and the p95 latency calculated from the SQLite log at `METRICS_DB_PATH`.

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

Complete 24-hour request and response example (simplified flat tariff, no solar directive):

Request:

```json
{
  "scenario_id": "DASHBOARD-DEMO",
  "operator_notes": ["The cafeteria note has no effect on today's energy plan."],
  "hours": [
    {"hour": 0, "demand_kwh": 10, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
    {"hour": 1, "demand_kwh": 10, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
    {"hour": 2, "demand_kwh": 10, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
    {"hour": 3, "demand_kwh": 10, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
    {"hour": 4, "demand_kwh": 10, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
    {"hour": 5, "demand_kwh": 10, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
    {"hour": 6, "demand_kwh": 10, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
    {"hour": 7, "demand_kwh": 10, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
    {"hour": 8, "demand_kwh": 10, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
    {"hour": 9, "demand_kwh": 10, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
    {"hour": 10, "demand_kwh": 10, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
    {"hour": 11, "demand_kwh": 10, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
    {"hour": 12, "demand_kwh": 10, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
    {"hour": 13, "demand_kwh": 10, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
    {"hour": 14, "demand_kwh": 10, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
    {"hour": 15, "demand_kwh": 10, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
    {"hour": 16, "demand_kwh": 10, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
    {"hour": 17, "demand_kwh": 10, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
    {"hour": 18, "demand_kwh": 10, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
    {"hour": 19, "demand_kwh": 10, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
    {"hour": 20, "demand_kwh": 10, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
    {"hour": 21, "demand_kwh": 10, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
    {"hour": 22, "demand_kwh": 10, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
    {"hour": 23, "demand_kwh": 10, "solar_kwh": 0, "tariff_bdt_per_kwh": 5}
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

Response:

```json
{
  "scenario_id": "DASHBOARD-DEMO",
  "directive_interpretation": [{"note_index": 0, "applies": false, "directive_type": "no_op", "structured_adjustment": null, "explanation": "The note has no effect on today's energy plan."}],
  "hourly_plan": [
    {"hour": 0, "grid_kwh": 10, "solar_used_kwh": 0, "battery_action": "idle", "battery_kwh": 0, "battery_energy_after_kwh": 20},
    {"hour": 1, "grid_kwh": 10, "solar_used_kwh": 0, "battery_action": "idle", "battery_kwh": 0, "battery_energy_after_kwh": 20},
    {"hour": 2, "grid_kwh": 10, "solar_used_kwh": 0, "battery_action": "idle", "battery_kwh": 0, "battery_energy_after_kwh": 20},
    {"hour": 3, "grid_kwh": 10, "solar_used_kwh": 0, "battery_action": "idle", "battery_kwh": 0, "battery_energy_after_kwh": 20},
    {"hour": 4, "grid_kwh": 10, "solar_used_kwh": 0, "battery_action": "idle", "battery_kwh": 0, "battery_energy_after_kwh": 20},
    {"hour": 5, "grid_kwh": 10, "solar_used_kwh": 0, "battery_action": "idle", "battery_kwh": 0, "battery_energy_after_kwh": 20},
    {"hour": 6, "grid_kwh": 10, "solar_used_kwh": 0, "battery_action": "idle", "battery_kwh": 0, "battery_energy_after_kwh": 20},
    {"hour": 7, "grid_kwh": 10, "solar_used_kwh": 0, "battery_action": "idle", "battery_kwh": 0, "battery_energy_after_kwh": 20},
    {"hour": 8, "grid_kwh": 10, "solar_used_kwh": 0, "battery_action": "idle", "battery_kwh": 0, "battery_energy_after_kwh": 20},
    {"hour": 9, "grid_kwh": 10, "solar_used_kwh": 0, "battery_action": "idle", "battery_kwh": 0, "battery_energy_after_kwh": 20},
    {"hour": 10, "grid_kwh": 10, "solar_used_kwh": 0, "battery_action": "idle", "battery_kwh": 0, "battery_energy_after_kwh": 20},
    {"hour": 11, "grid_kwh": 10, "solar_used_kwh": 0, "battery_action": "idle", "battery_kwh": 0, "battery_energy_after_kwh": 20},
    {"hour": 12, "grid_kwh": 10, "solar_used_kwh": 0, "battery_action": "idle", "battery_kwh": 0, "battery_energy_after_kwh": 20},
    {"hour": 13, "grid_kwh": 10, "solar_used_kwh": 0, "battery_action": "idle", "battery_kwh": 0, "battery_energy_after_kwh": 20},
    {"hour": 14, "grid_kwh": 10, "solar_used_kwh": 0, "battery_action": "idle", "battery_kwh": 0, "battery_energy_after_kwh": 20},
    {"hour": 15, "grid_kwh": 10, "solar_used_kwh": 0, "battery_action": "idle", "battery_kwh": 0, "battery_energy_after_kwh": 20},
    {"hour": 16, "grid_kwh": 10, "solar_used_kwh": 0, "battery_action": "idle", "battery_kwh": 0, "battery_energy_after_kwh": 20},
    {"hour": 17, "grid_kwh": 10, "solar_used_kwh": 0, "battery_action": "idle", "battery_kwh": 0, "battery_energy_after_kwh": 20},
    {"hour": 18, "grid_kwh": 10, "solar_used_kwh": 0, "battery_action": "idle", "battery_kwh": 0, "battery_energy_after_kwh": 20},
    {"hour": 19, "grid_kwh": 10, "solar_used_kwh": 0, "battery_action": "idle", "battery_kwh": 0, "battery_energy_after_kwh": 20},
    {"hour": 20, "grid_kwh": 10, "solar_used_kwh": 0, "battery_action": "idle", "battery_kwh": 0, "battery_energy_after_kwh": 20},
    {"hour": 21, "grid_kwh": 10, "solar_used_kwh": 0, "battery_action": "idle", "battery_kwh": 0, "battery_energy_after_kwh": 20},
    {"hour": 22, "grid_kwh": 10, "solar_used_kwh": 0, "battery_action": "idle", "battery_kwh": 0, "battery_energy_after_kwh": 20},
    {"hour": 23, "grid_kwh": 10, "solar_used_kwh": 0, "battery_action": "idle", "battery_kwh": 0, "battery_energy_after_kwh": 20}
  ],
  "total_grid_kwh": 240,
  "total_cost_bdt": 1200,
  "peak_grid_kwh": 10,
  "plan_summary": "Minimizes grid electricity cost while satisfying the validated solar, battery, grid, and neutrality constraints."
}
```
