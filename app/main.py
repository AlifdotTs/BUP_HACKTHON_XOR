"""FastAPI entry point for the GridWise service."""

from fastapi import FastAPI, HTTPException

from app.models import OptimizeRequest, OptimizeResponse
from app.services.directives import normalize_directives
from app.services.interpreter import (
    InterpreterUnavailable,
    InvalidInterpretation,
    interpret_operator_notes,
)
from app.services.optimizer import optimize_schedule


app = FastAPI(
    title="GridWise Energy Optimizer",
    version="0.1.0",
    description="LLM-assisted smart-campus energy scheduling API.",
)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    """Return readiness status for the judging harness."""

    return {"status": "ok"}


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
