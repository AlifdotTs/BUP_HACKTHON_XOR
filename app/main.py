"""FastAPI entry point for the GridWise service."""

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
import html
import time

from fastapi.responses import HTMLResponse, JSONResponse

from app.models import OptimizeRequest, OptimizeResponse
from app.services.directives import normalize_directives
from app.services.interpreter import (
    InterpreterUnavailable,
    InvalidInterpretation,
    interpret_operator_notes,
)
from app.services.optimizer import optimize_schedule
from app.services.metrics import get_metrics, record_request


app = FastAPI(
    title="GridWise Energy Optimizer",
    version="0.1.0",
    description="LLM-assisted smart-campus energy scheduling API.",
)


@app.middleware("http")
async def request_metrics_middleware(request: Request, call_next):
    """Log optimize request status and latency without storing request data."""

    if request.url.path != "/optimize-energy":
        return await call_next(request)

    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        record_request(
            request.method,
            request.url.path,
            500,
            (time.perf_counter() - started) * 1000,
        )
        raise
    record_request(
        request.method,
        request.url.path,
        response.status_code,
        (time.perf_counter() - started) * 1000,
    )
    return response


@app.exception_handler(RequestValidationError)
async def request_validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return a controlled 400 without exposing raw validation details."""

    return JSONResponse(status_code=400, content={"detail": "Invalid request"})


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    """Return readiness status for the judging harness."""

    return {"status": "ok"}


@app.get("/test-dashboard", response_class=HTMLResponse, include_in_schema=False)
def test_dashboard() -> HTMLResponse:
    """Show local request metrics collected by the SQLite test store."""

    metrics = get_metrics()
    p95 = (
        f"{metrics['p95_latency_ms']:.3f} ms"
        if metrics["p95_latency_ms"] is not None
        else "No requests yet"
    )
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(item['created_at']))}</td>"
        f"<td>{html.escape(str(item['method']))}</td>"
        f"<td>{html.escape(str(item['path']))}</td>"
        f"<td>{html.escape(str(item['status_code']))}</td>"
        f"<td>{float(item['latency_ms']):.3f}</td>"
        "</tr>"
        for item in metrics["recent_requests"]
    )
    if not rows:
        rows = '<tr><td colspan="5">No optimize requests logged yet.</td></tr>'
    content = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta http-equiv="refresh" content="10">
<title>GridWise Test Dashboard</title>
<style>
body {{ font-family: Arial, sans-serif; margin: 2rem; color: #17202a; }}
.cards {{ display: flex; gap: 1rem; flex-wrap: wrap; }}
.card {{ border: 1px solid #ccd6dd; border-radius: 8px; padding: 1rem 1.25rem; min-width: 150px; }}
.value {{ display: block; font-size: 1.5rem; font-weight: bold; margin-top: .4rem; }}
.table-wrap {{ max-height: 420px; overflow: auto; margin-top: 1.5rem; border: 1px solid #ccd6dd; }}
table {{ border-collapse: collapse; width: 100%; min-width: 760px; margin: 0; }}
th, td {{ border: 1px solid #ccd6dd; padding: .55rem; text-align: left; }}
th {{ background: #eef3f6; position: sticky; top: 0; z-index: 1; }}
code {{ background: #eef3f6; padding: .15rem .3rem; }}
</style></head>
<body>
<h1>GridWise Test Dashboard</h1>
<p>Metrics are collected from real <code>POST /optimize-energy</code> requests. Request bodies, headers, and secrets are not stored.</p>
<div class="cards">
  <div class="card">Requests<span class="value">{metrics['request_count']}</span></div>
  <div class="card">Successful<span class="value">{metrics['success_count']}</span></div>
  <div class="card">Errors<span class="value">{metrics['error_count']}</span></div>
  <div class="card">p95 latency<span class="value">{html.escape(p95)}</span></div>
</div>
<h2>Recent requests</h2>
<div class="table-wrap"><table><thead><tr><th>UTC time</th><th>Method</th><th>Path</th><th>Status</th><th>Latency (ms)</th></tr></thead>
<tbody>{rows}</tbody></table>
</div>
</body></html>"""
    return HTMLResponse(content=content)


@app.post("/optimize-energy", response_model=OptimizeResponse, tags=["optimization"])
async def optimize_energy(request: OptimizeRequest) -> OptimizeResponse:
    """Return a validated cost-minimizing schedule for a scenario."""

    try:
        interpretations = await interpret_operator_notes(request)
        constraints = normalize_directives(request, interpretations)
        result = optimize_schedule(request, constraints)
        return OptimizeResponse(
            scenario_id=request.scenario_id,
            directive_interpretation=interpretations,
            **result,
        )
    except InterpreterUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except InvalidInterpretation as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
