"""Token accounting for the ARC-Bench Codex route (S0).

Three sources feed one append-only ledger (``<output-dir>/.arc/token-usage.jsonl``):

* ``model_call``  - one record per model response inside a turn, taken from the
  turn stream's ``token_usage`` notifications (the ``last`` breakdown). This is
  the only per-call source: input / cached / cache-write / output / reasoning.
* ``turn_total``  - the turn's cumulative breakdown, from ``TurnResult.usage``.
  Used as the fallback when streaming is unavailable.
* ``vision_call`` - one record per reference-image analysis, from the vision
  model's ``response.usage``.

Recording is strictly best-effort: a failure to write the ledger degrades to a
warning and never interrupts a run. Nothing here changes what the agent does or
what it is told.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

LEDGER_NAME = "token-usage.jsonl"

# Protocol (camelCase) -> Python attribute name, in wire order.
_BREAKDOWN_FIELDS = {
    "input_tokens": "inputTokens",
    "cached_input_tokens": "cachedInputTokens",
    "cache_write_input_tokens": "cacheWriteInputTokens",
    "output_tokens": "outputTokens",
    "reasoning_output_tokens": "reasoningOutputTokens",
    "total_tokens": "totalTokens",
}

def ledger_path(output_dir: Path | str) -> Path:
    return Path(output_dir) / ".arc" / LEDGER_NAME


def _now_iso() -> str:
    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    return stamp.replace("+00:00", "Z")


def _field(obj: Any, name: str) -> Any:
    """Read one breakdown field from a pydantic model or a plain dict."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        for key in (name, _BREAKDOWN_FIELDS[name]):
            if key in obj:
                return obj[key]
        return None
    value = getattr(obj, name, None)
    if value is None:
        value = getattr(obj, _BREAKDOWN_FIELDS[name], None)
    return value


def breakdown_to_dict(breakdown: Any) -> dict[str, int] | None:
    """Normalize a TokenUsageBreakdown into snake_case ints."""
    if breakdown is None:
        return None
    out: dict[str, int] = {}
    for name in _BREAKDOWN_FIELDS:
        raw = _field(breakdown, name)
        try:
            out[name] = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            out[name] = 0
    return out


def append(output_dir: Path | str, record: dict[str, Any]) -> None:
    """Append one ledger record; never raises."""
    try:
        path = ledger_path(output_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": _now_iso(), **record}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 - accounting must never break a run
        print(f"[codex] WARNING: token ledger write failed: {exc}", flush=True)


def read_all(output_dir: Path | str) -> list[dict[str, Any]]:
    """Read the ledger; a missing file yields an empty list."""
    records: list[dict[str, Any]] = []
    try:
        text = ledger_path(output_dir).read_text(encoding="utf-8")
    except FileNotFoundError:
        return records
    except OSError as exc:
        print(f"[codex] WARNING: token ledger unreadable: {exc}", flush=True)
        return records
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _zero() -> dict[str, int]:
    return {name: 0 for name in _BREAKDOWN_FIELDS}


def summarize(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate ledger records into one row.

    ``model_call`` records are incremental and preferred; when a scope has none
    (the non-streaming fallback) its ``turn_total`` records are used instead.
    ``vision_call`` records are always incremental and are added on top, so a
    vision record can never mask the coding model's cost.
    """
    records = list(records)
    calls = [r for r in records if r.get("kind") == "model_call"]
    turn_totals = [r for r in records if r.get("kind") == "turn_total"]
    vision = [r for r in records if r.get("kind") == "vision_call"]
    chosen = calls or turn_totals
    totals = _zero()
    for record in [*chosen, *vision]:
        for name in _BREAKDOWN_FIELDS:
            value = record.get(name)
            if isinstance(value, (int, float)):
                totals[name] += int(value)
    model_calls = len(calls)
    vision_calls = len(vision)
    cached = totals["cached_input_tokens"]
    inp = totals["input_tokens"]
    totals["cached_pct"] = round(cached / inp * 100, 1) if inp else 0.0
    totals["model_calls"] = model_calls
    totals["vision_calls"] = vision_calls
    totals["used_turn_totals"] = not calls and bool(turn_totals)
    return totals


def scope(output_dir: Path | str, **filters: Any) -> dict[str, Any]:
    """Summarize the ledger subset whose fields equal the given filters."""
    records = read_all(output_dir)
    for key, expected in filters.items():
        records = [r for r in records if r.get(key) == expected]
    return summarize(records)


def format_row(label: str, totals: dict[str, Any]) -> str:
    """One-line human summary (thousands separators, cache hit rate)."""
    vision = totals.get("vision_calls") or 0
    suffix = f"+vision {vision}" if vision else ""
    return (
        f"{label}: calls={totals.get('model_calls', 0)}{suffix} "
        f"in={totals.get('input_tokens', 0):,} "
        f"cached={totals.get('cached_input_tokens', 0):,} "
        f"({totals.get('cached_pct', 0.0)}%) "
        f"out={totals.get('output_tokens', 0):,} "
        f"reasoning={totals.get('reasoning_output_tokens', 0):,} "
        f"total={totals.get('total_tokens', 0):,}"
    )
