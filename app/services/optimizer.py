"""Linear-programming energy optimizer used by the FastAPI endpoint."""

from __future__ import annotations

import math

import numpy as np
from scipy.optimize import linprog

from app.models import OptimizeRequest
from app.services.directives import HourlyConstraints


HOURS = 24
NUM_VARS = 5 * HOURS
TOLERANCE = 1e-6


def _idx(kind: str, hour: int) -> int:
    offsets = {"grid": 0, "solar": 24, "charge": 48, "discharge": 72, "battery": 96}
    return offsets[kind] + hour


def _finite_non_negative(value: float, name: str) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")


def _validate_request(request: OptimizeRequest) -> None:
    hours = sorted(request.hours, key=lambda item: item.hour)
    if [item.hour for item in hours] != list(range(HOURS)):
        raise ValueError("hours must contain exactly one entry for every hour 0..23")
    battery = request.battery
    if battery.minimum_energy_kwh > battery.capacity_kwh:
        raise ValueError("minimum battery energy cannot exceed capacity")
    if not battery.minimum_energy_kwh <= battery.initial_energy_kwh <= battery.capacity_kwh:
        raise ValueError("initial battery energy must be within battery bounds")
    for item in hours:
        _finite_non_negative(item.demand_kwh, "demand")
        _finite_non_negative(item.solar_kwh, "solar")
        _finite_non_negative(item.tariff_bdt_per_kwh, "tariff")


def _build_lp(request: OptimizeRequest, constraints: HourlyConstraints):
    hours = sorted(request.hours, key=lambda item: item.hour)
    battery = request.battery
    objective = np.zeros(NUM_VARS)
    objective[0:24] = [item.tariff_bdt_per_kwh for item in hours]

    equality_count = HOURS + HOURS + 1
    A_eq = np.zeros((equality_count, NUM_VARS))
    b_eq = np.zeros(equality_count)

    for hour, item in enumerate(hours):
        # grid + solar + discharge - charge = demand
        A_eq[hour, _idx("grid", hour)] = 1
        A_eq[hour, _idx("solar", hour)] = 1
        A_eq[hour, _idx("discharge", hour)] = 1
        A_eq[hour, _idx("charge", hour)] = -1
        b_eq[hour] = item.demand_kwh

        # battery[h] - battery[h-1] - charge + discharge = 0
        row = HOURS + hour
        A_eq[row, _idx("battery", hour)] = 1
        A_eq[row, _idx("charge", hour)] = -1
        A_eq[row, _idx("discharge", hour)] = 1
        if hour == 0:
            b_eq[row] = battery.initial_energy_kwh
        else:
            A_eq[row, _idx("battery", hour - 1)] = -1

    # End-of-day neutrality.
    A_eq[-1, _idx("battery", HOURS - 1)] = 1
    b_eq[-1] = battery.initial_energy_kwh

    bounds: list[tuple[float, float | None]] = []
    for hour in range(HOURS):
        bounds.append((0.0, float(constraints.grid_limit[hour])))
    for hour in range(HOURS):
        bounds.append((0.0, float(constraints.effective_solar[hour])))
    for hour in range(HOURS):
        upper = battery.max_charge_kwh_per_hour if constraints.charge_allowed[hour] else 0.0
        bounds.append((0.0, upper))
    for hour in range(HOURS):
        upper = battery.max_discharge_kwh_per_hour if constraints.discharge_allowed[hour] else 0.0
        bounds.append((0.0, upper))
    for hour in range(HOURS):
        bounds.append((float(constraints.minimum_reserve[hour]), battery.capacity_kwh))

    return objective, A_eq, b_eq, bounds


def _cancel_simultaneous_actions(solution: np.ndarray) -> np.ndarray:
    """Remove simultaneous charge/discharge without changing any balance."""

    cleaned = solution.copy()
    charge = cleaned[48:72]
    discharge = cleaned[72:96]
    cancellation = np.minimum(charge, discharge)
    charge -= cancellation
    discharge -= cancellation
    return cleaned


def _replay_validate(
    request: OptimizeRequest,
    constraints: HourlyConstraints,
    solution: np.ndarray,
) -> None:
    hours = sorted(request.hours, key=lambda item: item.hour)
    battery = request.battery
    previous_energy = battery.initial_energy_kwh

    for hour, item in enumerate(hours):
        grid = solution[_idx("grid", hour)]
        solar = solution[_idx("solar", hour)]
        charge = solution[_idx("charge", hour)]
        discharge = solution[_idx("discharge", hour)]
        energy_after = solution[_idx("battery", hour)]

        if abs(grid + solar + discharge - item.demand_kwh - charge) > TOLERANCE:
            raise RuntimeError(f"energy balance failed at hour {hour}")
        if min(grid, solar, charge, discharge, energy_after) < -TOLERANCE:
            raise RuntimeError(f"negative energy value at hour {hour}")
        if solar < -TOLERANCE or solar > constraints.effective_solar[hour] + TOLERANCE:
            raise RuntimeError(f"solar limit failed at hour {hour}")
        if abs(energy_after - (previous_energy + charge - discharge)) > TOLERANCE:
            raise RuntimeError(f"battery transition failed at hour {hour}")
        if energy_after < constraints.minimum_reserve[hour] - TOLERANCE:
            raise RuntimeError(f"battery reserve failed at hour {hour}")
        if energy_after > battery.capacity_kwh + TOLERANCE:
            raise RuntimeError(f"battery capacity failed at hour {hour}")
        if charge > battery.max_charge_kwh_per_hour + TOLERANCE:
            raise RuntimeError(f"charge rate failed at hour {hour}")
        if discharge > battery.max_discharge_kwh_per_hour + TOLERANCE:
            raise RuntimeError(f"discharge rate failed at hour {hour}")
        if not constraints.charge_allowed[hour] and charge > TOLERANCE:
            raise RuntimeError(f"charging is forbidden at hour {hour}")
        if not constraints.discharge_allowed[hour] and discharge > TOLERANCE:
            raise RuntimeError(f"discharging is forbidden at hour {hour}")
        if grid > constraints.grid_limit[hour] + TOLERANCE:
            raise RuntimeError(f"grid cap failed at hour {hour}")
        previous_energy = energy_after

    if abs(previous_energy - battery.initial_energy_kwh) > TOLERANCE:
        raise RuntimeError("end-of-day battery neutrality failed")


def optimize_schedule(request: OptimizeRequest, constraints: HourlyConstraints) -> dict:
    """Solve and validate one 24-hour scenario."""

    _validate_request(request)
    objective, A_eq, b_eq, bounds = _build_lp(request, constraints)
    result = linprog(
        c=objective,
        A_eq=A_eq,
        b_eq=b_eq,
        bounds=bounds,
        method="highs",
    )
    if not result.success or result.x is None:
        raise RuntimeError(f"optimizer failed: {result.message}")

    solution = _cancel_simultaneous_actions(np.asarray(result.x, dtype=float))
    _replay_validate(request, constraints, solution)

    hours = sorted(request.hours, key=lambda item: item.hour)
    hourly_plan = []
    for hour in range(HOURS):
        charge = solution[_idx("charge", hour)]
        discharge = solution[_idx("discharge", hour)]
        if charge > TOLERANCE:
            action = "charge"
            battery_kwh = charge
        elif discharge > TOLERANCE:
            action = "discharge"
            battery_kwh = discharge
        else:
            action = "idle"
            battery_kwh = 0.0

        hourly_plan.append(
            {
                "hour": hour,
                "grid_kwh": float(solution[_idx("grid", hour)]),
                "solar_used_kwh": float(solution[_idx("solar", hour)]),
                "battery_action": action,
                "battery_kwh": float(battery_kwh),
                "battery_energy_after_kwh": float(solution[_idx("battery", hour)]),
            }
        )

    grid_values = np.array([item["grid_kwh"] for item in hourly_plan])
    tariffs = np.array([item.tariff_bdt_per_kwh for item in hours])
    return {
        "hourly_plan": hourly_plan,
        "total_grid_kwh": float(grid_values.sum()),
        "total_cost_bdt": float(np.dot(grid_values, tariffs)),
        "peak_grid_kwh": float(grid_values.max()),
        "plan_summary": "Minimizes grid electricity cost while satisfying the validated solar, battery, grid, and neutrality constraints.",
    }
