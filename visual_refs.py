"""Reference-image analyses for the Codex route.

Requirement descriptions embed reference screenshots as markdown links
(``![image](./reference/xxx.png)``). The Codex main flow runs on a coding model
that may not be multimodal, so this module turns those screenshots into text
analyses through the platform's *visual model* and injects them into each
module prompt as a ``## Reference image analyses`` block. The Codex model then
implements against the analysis without ever needing to see the image itself.

Model / credential contract (same fallback chain as
``TDD-ARC/core/visual_analysis.py``):

- ``VISUAL_MODEL``    -> ``MODEL``
- ``VISUAL_API_KEY``  -> ``OPENAI_API_KEY``
- ``VISUAL_BASE_URL`` -> ``OPENAI_BASE_URL`` -> ``OPENAI_API_BASE``

Failures degrade gracefully: a missing model/key/base-url or a failed API call
only warns and skips that image — the Codex main flow always proceeds
text-only. Analyses are cached under ``<output-dir>/.arc/visual_refs_cache.json``
keyed by file mtime + size + prompt version, so an image shared by several
modules is analyzed only once per run.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any

from openai import OpenAI

import token_usage

VISUAL_ANALYSIS_PROMPT_VERSION = "codex-ui-impl-v1"

# Transient platform-side timeouts can wipe out reference analyses mid-window,
# and the affected runs regress exactly on homepage/product-page points. Retry
# once before degrading to "no visual grounding".
VISUAL_RETRIES = int(os.environ.get("ARCBENCH_VISUAL_RETRIES", "1"))

_IMAGE_LINK_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

# Printed once per process so a run with many image-bearing modules is not
# spammed by the same "not configured" warning.
_config_warning_shown = False


def build_visual_analysis_prompt() -> str:
    """System prompt for the vision model: image -> implementation guidance."""
    return """
**ROLE:** You are the "eyes" for a coding agent that will implement a web UI.
A reference screenshot of a target page is attached. Convert it into a precise,
implementation-oriented description the agent can code against.

**WHAT TO CAPTURE (in order of importance):**
1. Layout hierarchy: top-to-bottom page skeleton; major sections (header/nav,
   sidebar, content areas, footer); how sections compose (rows/columns, widths,
   alignment, sticky/fixed elements).
2. Components and controls: every visible control — buttons, links, tabs,
   inputs, dropdowns, checkboxes, cards, tables, dialogs — with its EXACT
   visible label text (verbatim, including case and punctuation).
3. Interaction surfaces: forms and their fields (exact field labels,
   placeholders, input types, required/disabled states), navigation structure
   (menu items, tabs), and any visible state (selected, hover, empty, error).
4. Data display patterns: lists/tables/cards/grids — describe columns, field
   types, badges, density, and how data should be presented. Describe the
   SHAPE, never the concrete values.
5. Style: colors (approximate hex), typography scale, spacing rhythm, borders,
   shadows, corner radii, icon usage.

**STRICT RULES:**
- Do NOT transcribe or reproduce screenshot-specific business data (names,
  phone numbers, emails, ids, dates, prices, counts, table rows, chart values)
  as content to seed; that comes from the requirement text and seed data.
- Keep visible text ONLY when it is structural page chrome: navigation labels,
  section titles, field labels, button labels, tab names, status categories.
- Visible text is for VISUAL fidelity only; it is NOT the accessible-name
  contract. When the requirement text quotes a control's name (e.g. "Take a
  note"), the accessible name must be that quoted name even if the screenshot
  shows a decorated variant ("Take a note..."). Explicitly note when a control's
  visible text differs from its requirement-quoted name, so the implementer does
  not copy screenshot decorations (ellipsis "...", ":" or "—" suffixes, extra
  words) into aria-label.
- Write as implementation guidance, not as an image caption and not as OCR.
"""


def collect_image_links(subtree: Any) -> list[str]:
    """Collect deduped image references from one requirement subtree.

    Sources (mirroring TDD-ARC): markdown image links ``![alt](path)`` in any
    string field (e.g. ``description``), plus ``visual_reference`` list
    entries' ``image_path`` keys. Order of first appearance is preserved.
    """
    seen: set[str] = set()
    links: list[str] = []

    def _add(link: Any) -> None:
        text = str(link or "").strip()
        if text and text not in seen:
            seen.add(text)
            links.append(text)

    def _walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "visual_reference" and isinstance(item, list):
                    for entry in item:
                        if isinstance(entry, dict):
                            _add(entry.get("image_path"))
                    continue
                _walk(item)
        elif isinstance(value, list):
            for item in value:
                _walk(item)
        elif isinstance(value, str):
            for match in _IMAGE_LINK_RE.findall(value):
                _add(match)

    _walk(subtree)
    return links


def _resolve_image_source(link: str, requirements_dir: Path) -> tuple[str, Path | None, str | None]:
    """Return ``(display, local_path, remote_url)``; exactly one of the paths set.

    Relative links resolve against ``requirements_dir`` (mirroring TDD-ARC);
    http(s) links pass through untouched as remote image URLs.
    """
    if _URL_RE.match(link):
        return link, None, link
    normalized = os.path.normpath(link).lstrip(os.sep)
    full_path = (requirements_dir / normalized).resolve()
    return link, full_path, None


def _local_cache_key(full_path: Path) -> str:
    stat = full_path.stat()
    raw = f"{full_path}::{stat.st_mtime_ns}::{stat.st_size}::{VISUAL_ANALYSIS_PROMPT_VERSION}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _remote_cache_key(url: str) -> str:
    raw = f"url::{url}::{VISUAL_ANALYSIS_PROMPT_VERSION}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_path(output_dir: Path) -> Path:
    return Path(output_dir) / ".arc" / "visual_refs_cache.json"


def _load_cache(output_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads(_cache_path(output_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(output_dir: Path, cache: dict[str, Any]) -> None:
    cache_path = _cache_path(output_dir)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _visual_config() -> tuple[str, str, str]:
    """Resolve ``(model, api_key, base_url)`` with main-model fallbacks."""
    model = os.environ.get("VISUAL_MODEL") or os.environ.get("MODEL", "")
    api_key = os.environ.get("VISUAL_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    base_url = (
        os.environ.get("VISUAL_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("OPENAI_API_BASE", "")
    ).strip()
    return _normalize_model_name(model), api_key.strip(), base_url


def _normalize_model_name(model_name: str) -> str:
    normalized = str(model_name or "").strip()
    if normalized.startswith("openai:"):
        return normalized.split(":", 1)[1].strip()
    return normalized


def _extract_text(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if choices:
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()
    raise RuntimeError(f"visual API response had no chat completion text: {_short(response)}")


def _short(response: Any, limit: int = 800) -> str:
    text = str(response)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...<truncated>"


def _analyze_image(
    local_path: Path | None,
    remote_url: str | None,
    model: str,
    api_key: str,
    base_url: str,
    *,
    output_dir: Path | None = None,
    label: str = "",
) -> str:
    if local_path is not None:
        mime_type, _ = mimetypes.guess_type(str(local_path))
        data_url = (
            f"data:{mime_type or 'image/png'};base64,"
            f"{base64.b64encode(local_path.read_bytes()).decode('utf-8')}"
        )
        image_url: dict[str, str] = {"url": data_url}
    else:
        image_url = {"url": str(remote_url)}
    last_error: Exception | None = None
    for attempt in range(1, VISUAL_RETRIES + 2):
        try:
            client = OpenAI(api_key=api_key, base_url=base_url)
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": build_visual_analysis_prompt()},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Analyze this UI reference image for implementation."},
                            {"type": "image_url", "image_url": image_url},
                        ],
                    },
                ],
                timeout=120.0,
            )
            _record_vision_usage(
                output_dir, response, model=model, label=label or str(local_path or remote_url), attempt=attempt
            )
            return _extract_text(response)
        except Exception as exc:  # noqa: BLE001 - transient API/network failure, retry once
            last_error = exc
            if attempt <= VISUAL_RETRIES:
                print(
                    f"[codex] WARNING: visual analysis attempt {attempt} failed "
                    f"({type(exc).__name__}: {exc}); retrying",
                    flush=True,
                )
                time.sleep(3.0)
    assert last_error is not None
    raise last_error


def _record_vision_usage(
    output_dir: Path | None,
    response: Any,
    *,
    model: str,
    label: str,
    attempt: int,
) -> None:
    """Record one vision call in the shared token ledger (S0).

    The vision channel is the second-place token consumer after the coding
    model (16-46 images per task) and used to be invisible: only the platform's
    run-level meter saw it. Recording it here makes the per-task picture
    complete. Best effort by construction - token_usage.append never raises.
    """
    if output_dir is None:
        return
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return

    def field(name: str) -> int:
        raw = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        try:
            return int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            return 0

    prompt_tokens = field("prompt_tokens")
    completion_tokens = field("completion_tokens")
    total_tokens = field("total_tokens") or prompt_tokens + completion_tokens
    details = usage.get("prompt_tokens_details") if isinstance(usage, dict) else getattr(
        usage, "prompt_tokens_details", None
    )
    if isinstance(details, dict):
        cached = int(details.get("cached_tokens") or 0)
    else:
        cached = int(getattr(details, "cached_tokens", 0) or 0)
    token_usage.append(
        output_dir,
        {
            "phase": "vision",
            "kind": "vision_call",
            "model": model,
            "image": label,
            "attempt": attempt,
            "input_tokens": prompt_tokens,
            "cached_input_tokens": cached,
            "cache_write_input_tokens": 0,
            "output_tokens": completion_tokens,
            "reasoning_output_tokens": 0,
            "total_tokens": total_tokens,
        },
    )


def _vlog(verbose: bool, *args: Any) -> None:
    if verbose:
        print("[codex][verbose]", *args, flush=True)


def build_visual_block(
    subtree: dict[str, Any],
    requirements_dir: Path | str,
    output_dir: Path | str,
    *,
    verbose: bool = False,
) -> str:
    """Return the ``## Reference image analyses`` block for one module, or "".

    Analyzes each image referenced by the subtree through the visual model,
    caching results under ``<output-dir>/.arc/visual_refs_cache.json``. Any
    configuration or API failure degrades to a warning and skips that image;
    no images (or no usable config) yields "" so the caller keeps its
    text-only prompt.
    """
    global _config_warning_shown

    requirements_dir = Path(requirements_dir).expanduser().resolve()
    output_dir = Path(output_dir).expanduser().resolve()
    links = collect_image_links(subtree)
    if not links:
        return ""

    model, api_key, base_url = _visual_config()
    if not model or not api_key or not base_url:
        if not _config_warning_shown:
            _config_warning_shown = True
            print(
                "[codex] WARNING: visual analysis skipped — set VISUAL_MODEL with "
                "VISUAL_API_KEY/VISUAL_BASE_URL (or the main MODEL / OPENAI_API_KEY / "
                "OPENAI_BASE_URL equivalents)",
                flush=True,
            )
        return ""

    cache = _load_cache(output_dir)
    cache_updated = False
    analyzed: list[tuple[str, str]] = []

    for link in links:
        display, local_path, remote_url = _resolve_image_source(link, requirements_dir)
        if local_path is not None:
            if not local_path.is_file():
                print(f"[codex] WARNING: reference image not found: {local_path}", flush=True)
                continue
            cache_key = _local_cache_key(local_path)
        else:
            cache_key = _remote_cache_key(str(remote_url))

        entry = cache.get(cache_key)
        if isinstance(entry, dict) and entry.get("analysis"):
            analysis = str(entry["analysis"])
            _vlog(verbose, f"reusing cached visual analysis: {display}")
        else:
            _vlog(verbose, f"analyzing visual element: {display}")
            try:
                analysis = _analyze_image(
                    local_path,
                    remote_url,
                    model,
                    api_key,
                    base_url,
                    output_dir=output_dir,
                    label=display,
                )
            except Exception as exc:  # noqa: BLE001 - degrade, never block the main flow
                print(f"[codex] WARNING: visual analysis failed for {display}: {exc}", flush=True)
                continue
            cache[cache_key] = {
                "image_path": display,
                "prompt_version": VISUAL_ANALYSIS_PROMPT_VERSION,
                "analysis": analysis,
            }
            cache_updated = True
        analyzed.append((display, analysis))

    if cache_updated:
        _save_cache(output_dir, cache)

    print(
        f"[codex] visual refs: {len(links)} image(s) -> {len(analyzed)} analysis(ies)",
        flush=True,
    )
    if not analyzed:
        return ""

    lines = [
        "",
        "## Reference image analyses",
        "The reference screenshots below were described by a separate vision model.",
        "Use them to match layout, components, controls and visible labels.",
        "They are implementation guidance only: never seed business data that merely",
        "appears in a screenshot.",
    ]
    for display, analysis in analyzed:
        lines.append("")
        lines.append(f"### {display}")
        lines.append(analysis)
    return "\n".join(lines)
