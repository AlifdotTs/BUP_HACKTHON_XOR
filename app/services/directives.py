"""Deterministic conversion of structured directives into hourly constraints."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable

import numpy as np

from app.models import DirectiveInterpretation, OptimizeRequest


HOURS = 24
SUPPORTED_DIRECTIVES = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}


@dataclass(frozen=True)
class HourlyConstraints:
    effective_solar: np.ndarray
    minimum_reserve: np.ndarray
    charge_allowed: np.ndarray
    discharge_allowed: np.ndarray
    grid_limit: np.ndarray


def _finite_non_negative(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return number


def _hours(adjustment: dict[str, Any], directive_type: str) -> list[int]:
    raw_hours = adjustment.get("hours")
    if not isinstance(raw_hours, list) or not raw_hours:
        raise ValueError(f"{directive_type}.hours must be a non-empty list")
    if any(not isinstance(hour, int) or isinstance(hour, bool) for hour in raw_hours):
        raise ValueError(f"{directive_type}.hours must contain integers")
    if raw_hours != sorted(set(raw_hours)) or any(hour < 0 or hour > 23 for hour in raw_hours):
        raise ValueError(f"{directive_type}.hours must be unique, sorted, and in 0..23")
    return raw_hours


def normalize_directives(
    request: OptimizeRequest,
    interpretations: Iterable[DirectiveInterpretation],
) -> HourlyConstraints:
    """Compile validated directive interpretations into solver-ready arrays."""

    entries = list(interpretations)
    if len(entries) != len(request.operator_notes):
        raise ValueError("There must be exactly one directive interpretation per note")
    if [entry.note_index for entry in entries] != list(range(len(entries))):
        raise ValueError("Directive interpretations must be ordered by note_index")

    hours = sorted(request.hours, key=lambda item: item.hour)
    if [item.hour for item in hours] != list(range(HOURS)):
        raise ValueError("hours must contain exactly one entry for every hour 0..23")

    effective_solar = np.array([item.solar_kwh for item in hours], dtype=float)
    minimum_reserve = np.full(HOURS, request.battery.minimum_energy_kwh, dtype=float)
    charge_allowed = np.ones(HOURS, dtype=bool)
    discharge_allowed = np.ones(HOURS, dtype=bool)
    grid_limit = np.full(HOURS, np.inf, dtype=float)

    for entry in entries:
        if entry.directive_type not in SUPPORTED_DIRECTIVES:
            raise ValueError(f"Unsupported directive type: {entry.directive_type}")
        if entry.directive_type == "no_op":
            if entry.applies or entry.structured_adjustment is not None:
                raise ValueError("no_op requires applies=false and null adjustment")
            continue
        if not entry.applies or entry.structured_adjustment is None:
            raise ValueError("Applicable directives require applies=true and an adjustment")

        adjustment = entry.structured_adjustment
        listed_hours = _hours(adjustment, entry.directive_type)

        expected_keys = {
            "solar_reduction": {"hours", "factor"},
            "minimum_battery_reserve": {"hours", "minimum_energy_kwh"},
            "no_charge_window": {"hours"},
            "no_discharge_window": {"hours"},
            "max_grid_window": {"hours", "max_grid_kwh"},
        }[entry.directive_type]
        if set(adjustment) != expected_keys:
            raise ValueError(f"{entry.directive_type} adjustment has incorrect fields")

        if entry.directive_type == "solar_reduction":
            factor = _finite_non_negative(adjustment.get("factor"), "solar factor")
            if factor > 1:
                raise ValueError("solar factor must be between 0 and 1")
            effective_solar[listed_hours] *= factor
        elif entry.directive_type == "minimum_battery_reserve":
            reserve = _finite_non_negative(
                adjustment.get("minimum_energy_kwh"),
                "minimum battery reserve",
            )
            if reserve > request.battery.capacity_kwh:
                raise ValueError("minimum battery reserve exceeds battery capacity")
            minimum_reserve[listed_hours] = np.maximum(
                minimum_reserve[listed_hours], reserve
            )
        elif entry.directive_type == "no_charge_window":
            charge_allowed[listed_hours] = False
        elif entry.directive_type == "no_discharge_window":
            discharge_allowed[listed_hours] = False
        elif entry.directive_type == "max_grid_window":
            limit = _finite_non_negative(adjustment.get("max_grid_kwh"), "grid cap")
            grid_limit[listed_hours] = np.minimum(grid_limit[listed_hours], limit)

    return HourlyConstraints(
        effective_solar=effective_solar,
        minimum_reserve=minimum_reserve,
        charge_allowed=charge_allowed,
        discharge_allowed=discharge_allowed,
        grid_limit=grid_limit,
    )
