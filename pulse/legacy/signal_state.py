"""ADR-0032: auxiliary transition state with explicit storage dependencies."""
import math

from pulse.config import SIGNAL_STATE_KEY_PREFIX, DISPLAYABLE_SIGNAL_STATES, TRACKED_TRADE_SIGNALS
from pulse.legacy.generation import _epoch_seconds


def _signal_state_key(resolution, horizon):
    return f"{SIGNAL_STATE_KEY_PREFIX}:{resolution}:{horizon}"


def _positive_finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _matching_signal_state_fields(state, expected_signal):
    if (
        expected_signal not in DISPLAYABLE_SIGNAL_STATES
        or not isinstance(state, dict)
        or state.get("signal") != expected_signal
    ):
        return None
    since_price = _positive_finite_number(state.get("since_price"))
    since_at = _epoch_seconds(state.get("since_at"))
    if since_price is None or since_at is None or since_at <= 0:
        return None
    return {
        "signal_since_price": since_price,
        "signal_since_at": since_at,
    }


def _signal_observations(rows):
    observations = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        instrument = str(row.get("instrument") or "")
        signal = str(row.get("trade_signal") or "")
        price = _positive_finite_number(row.get("price_last"))
        if (
            not instrument
            or signal not in TRACKED_TRADE_SIGNALS
            or price is None
        ):
            continue
        observations.append({
            "instrument": instrument,
            "signal": signal,
            "price": price,
        })
    return observations


def _reconcile_report_signal_states(report, resolution, horizon, source_at, generation_id, prune=False, *, storage):
    VercelStorage = storage
    observations = _signal_observations(
        report.get("summary", []) if isinstance(report, dict) else []
    )
    if not observations:
        return {"accepted": False, "states": {}, "reason": "no_observations"}
    return VercelStorage.reconcile_signal_states(
        _signal_state_key(resolution, horizon),
        observations,
        source_at,
        generation_id=generation_id,
        prune=prune,
    )


def _best_effort_reconcile_generation_signal_states(generation_id, generated_reports, published_at, *, reconcile_report):
    """Update auxiliary transition state without affecting report publication."""
    _reconcile_report_signal_states = reconcile_report
    for tf, hz, _, report in generated_reports:
        source_at = (
            _epoch_seconds(report.get("meta", {}).get("last_hydrated"))
            or published_at
        )
        try:
            reconciliation = _reconcile_report_signal_states(
                report,
                tf,
                hz,
                source_at=source_at,
                generation_id=generation_id,
                prune=True,
            )
        except Exception as exc:
            print(
                "[SIGNAL STATE] Generation reconciliation raised for "
                f"{tf}/{hz}: {type(exc).__name__}: {exc}. "
                "Primary report publication remains successful."
            )
            break
        if reconciliation.get("reason") in {"redis_error", "redis_unavailable"}:
            print(
                "[SIGNAL STATE] Generation reconciliation failed for "
                f"{tf}/{hz}; primary report publication remains successful "
                "and the transition line will retry on live refresh."
            )
            break


def _attach_signal_states(report, resolution, horizon, *, storage):
    """
    Add transition metadata to response rows without mutating immutable reports.

    A mismatched or malformed state is omitted instead of being guessed. This
    matters during the brief handoff from a newly published model generation to
    the first live-price reconciliation.
    """
    VercelStorage = storage
    if not isinstance(report, dict):
        return report
    rows = report.get("summary")
    if not isinstance(rows, list) or not rows:
        return report

    instruments = [
        row.get("instrument")
        for row in rows
        if isinstance(row, dict) and row.get("instrument")
    ]
    states = VercelStorage.get_signal_states(
        _signal_state_key(resolution, horizon),
        instruments,
    )
    if not states:
        return report

    response = dict(report)
    attached_rows = []
    for row in rows:
        if not isinstance(row, dict):
            attached_rows.append(row)
            continue
        attached = dict(row)
        fields = _matching_signal_state_fields(
            states.get(row.get("instrument")),
            row.get("trade_signal"),
        )
        if fields:
            attached.update(fields)
        attached_rows.append(attached)
    response["summary"] = attached_rows
    return response

