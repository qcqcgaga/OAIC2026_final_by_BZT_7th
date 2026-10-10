"""Post-implementation verification for the Codex route (ARC-Bench).

Runs after every ROOT module is implemented. Two layers plus a HARD GATE:

- Level 1: run the project's own committed test suites (`npm test` in
  frontend and backend) when the workspace has them installed. These tests are
  part of the agent's deliverable, so they are always available and app-general.
- Level 2: a generic, app-agnostic data-contract smoke:
    * extract named fixture entities from requirements.yaml (see WS-4: the
      extractor no longer depends on a single phrasing, so "extracted 0
      fixture entities" cannot silently empty the smoke; OPT-1 adds the
      product/item/article qualifier and allows hyphenated product names);
    * build the frontend if `dist` is missing, start the backend on a dedicated
      port, and wait for readiness;
    * if a Playwright chromium is available, run a MAIN-CHAIN browser smoke
      (WS-1): homepage + primary-nav pages, data-entry form accessible names,
      logo locatability, fixture-entity reachability across visited pages
      (OPT-1: walking pagination so "visible on page 1" is distinguished from
      "only reachable after pagination", which is gate-blocking),
      field-word-root name collisions (WS-3), label shadowing (WS-8),
      global-chrome button / page-action name shadowing (OPT-2), best-effort
      login (submit scoped to the email/password form) and state widget
      headings;
    * cross-check every extracted fixture title against the backend seed source;
    * (O2) extract "Seed data: ..." STATE qualifiers from the requirement text
      ("that is not pinned", "with the X label", "without the X label", "with a
      white background", "default label X", "active"/"archived") and verify the
      backend seed source honors them — a seed that pins a note the requirement
      says is "not pinned" is a blocking defect;
    * (O-A) resolve named fixtures by generic accessibility semantics —
      candidate roles (heading/button/link/...) probed with a short visibility
      window — instead of a page-text substring. Text present but no candidate
      role visible is a contract violation; a fixture that renders only after
      the ~500ms resolution window is an advisory;
    * (O-B) seed initial state is derived from the requirement's GIVEN/WHEN
      steps: a state-changing action (delete/archive/pin/favorite) requires
      its target to start in the pre-action state, so the seed must not
      pre-set an end state the scenario then acts on;
    * (K2) reconcile the SHIPPED database file the app actually serves
      (`backend/database.db`) against what the seed source declares: every
      entity value and every archived/pinned state the seed writes must be
      present in that file, because the platform never runs `db:seed` and an
      agent self-test can leave it half-seeded or renamed. Read-only, zero
      token, needs no browser — a divergence is blocking (main.py:
      FEATURE_FLAGS["db_reconcile"]).
- GATE: a hard verdict computed from the smoke. `gate.passed=False` lists
  blocking defects; main.py feeds them into exactly ONE repair round (see
  main._repair_against_gate), then re-verifies. Verification itself never
  crashes the run; the gate only decides whether the repair round is triggered.

Design constraints (see optimization reports O2 / P2-O1..O4 / O-A..C):
- Nothing here reads or RUNS the platform's hidden tests. L2 assertions are
  derived from the requirement text and from generic accessibility semantics
  (role + accessible-name resolution against the accessibility tree, first
  visible candidate). No delivered code probes, parses, or references any test
  file.
- Chromium is NOT downloaded by default. If the container has no browser binary,
  the browser smoke is reported as skipped and the gate degrades to
  "not-verifiable" (never blocking on checks that did not run); pass
  ``install_browser=True`` (main.py: FEATURE_FLAGS["verify_browser_install"])
  to allow one `playwright install chromium` attempt.
- A smoke that ERRORS (browser available but the run died/timed out) is retried
  once (ARCBENCH_VERIFY_SMOKE_RETRIES) and, if it still fails, BLOCKS: an app
  that could not be browser-verified must not silently ship unverified
  (a killed smoke used to produce zero results, the gate reported
  "not-verifiable", and no repair round ran).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

VERIFY_PORT = int(os.environ.get("ARCBENCH_VERIFY_PORT", "3199"))
TEST_TIMEOUT_S = int(os.environ.get("ARCBENCH_VERIFY_TEST_TIMEOUT", "420"))
BUILD_TIMEOUT_S = int(os.environ.get("ARCBENCH_VERIFY_BUILD_TIMEOUT", "300"))
SERVER_TIMEOUT_S = int(os.environ.get("ARCBENCH_VERIFY_SERVER_TIMEOUT", "60"))
SMOKE_TIMEOUT_S = int(os.environ.get("ARCBENCH_VERIFY_SMOKE_TIMEOUT", "120"))
SMOKE_RETRIES = int(os.environ.get("ARCBENCH_VERIFY_SMOKE_RETRIES", "1"))

# Feature switches are NOT read here: verify_application() takes them as explicit
# arguments and Codex/main.py's FEATURE_FLAGS table is the only place that decides.
# (Historically this file read ARCBENCH_VERIFY_BROWSER / ARCBENCH_CONTRACT_PROBES,
# which the platform container never sets.)

MAX_TITLES = 60
MAX_SMOKE_TITLES = 60
MAX_NAV_LINKS = 8

# S3': feed the route's contract index (Codex/contract_index.py) into this
# verifier for an EXACT-coverage advisory. It reads the requirement tree only -
# it never generates spec files, never writes into the artifact, and its
# findings are advisory (never blocking), because a name can legitimately come
# from seeded data rather than source text.
MAX_CONTRACT_MISSING = 40
# OPT-1: how many total pages the smoke may walk on a paginated listing to
# distinguish "fixture visible on page 1" (the contract) from "fixture only
# reachable after pagination" (a blocking seed/listing-order defect). Bounded
# so the smoke stays inside SMOKE_TIMEOUT_S.
PAGINATION_MAX_PAGES = int(os.environ.get("ARCBENCH_VERIFY_PAGINATION_MAX", "4"))

# O-A: candidate roles a named entity may answer under generic accessibility
# semantics (the roles users interact with by name). The smoke probes each
# fixture title against these roles with a short visibility window; a fixture
# whose text exists but matches NO candidate role is a contract violation.
# Parameterized per task via the smoke payload.
FIXTURE_ROLE_CANDIDATES = [
    "heading",
    "button",
    "link",
    "tab",
    "menuitem",
    "checkbox",
    "option",
]
# 单候选可见性探测窗口（通用冒烟参数）。
FIXTURE_WINDOW_MS = int(os.environ.get("ARCBENCH_VERIFY_FIXTURE_WINDOW_MS", "500"))

# Field word roots (WS-3): a non-form control (toolbar/group/aside/complementary)
# whose accessible name contains any of these roots hijacks getByLabel/getByRole
# field lookups (e.g. a "Settings menu" toolbar vs /settings/i, a toolbar
# "Description formatting" vs getByLabel(/Description/i)).
FIELD_WORD_ROOTS = (
    "description",
    "name",
    "tags",
    "title",
    "content",
    "email",
    "password",
    "search",
    "note",
    "book",
    "shelf",
    "page",
    "draft",
    "chapter",
    "label",
)

# Fixture-entity phrasing seen across ARC-Bench requirements files (WS-4). The
# qualifier pass collects "named/titled/..."/"Seed data: book \"X\"" entities;
# a quoted-string fallback then TOP-UP the list so "extracted 0" cannot happen
# and mixed phrasing cannot silently drop entities.
_QUALIFIER_PATTERNS = (
    re.compile(r"(?:named|titled|nickname|username)\s+[\"“\u2018']([^\"”\u2019']{2,80})[\"”\u2019']", re.IGNORECASE),
    re.compile(r"(?:with\s+(?:the\s+)?(?:name|nickname|title))\s+[\"“\u2018']([^\"”\u2019']{2,80})[\"”\u2019']", re.IGNORECASE),
    re.compile(r"(?:account|user|entity)\s+[\"“\u2018']([^\"”\u2019']{2,80})[\"”\u2019']", re.IGNORECASE),
    re.compile(r"\b(?:book|shelf|page|chapter|note|draft|dataset)\s+[\"“\u2018']([^\"”\u2019']{2,80})[\"”\u2019']", re.IGNORECASE),
    # OPT-1: product/catalog fixtures ("the product \"Hummingbird detail t-shirt\"")
    # are the entities users select by name; without this qualifier they fell
    # through to the fallback, whose strict shape then REJECTED hyphenated
    # product names (e.g. 'Hummingbird detail t-shirt' was never extracted, so
    # the smoke could not see it sat on Men page 2).
    re.compile(r"\b(?:product|item|article)\s+[\"“\u2018']([^\"”\u2019']{2,80})[\"”\u2019']", re.IGNORECASE),
)
_QUOTED_FALLBACK = re.compile(r"[\"“\u2018']([^\"”\u2019']{2,80})[\"”\u2019']")

# Strict shape for fallback-sourced titles: entity names in this benchmark are
# Title Case and short; sentence fragments / connector words / credentials
# ("Display the dashboard layout...", ", and", "Password123!") must not pollute.
# Hyphens are allowed (OPT-1): product names such as "Hummingbird detail t-shirt"
# and "Hummingbird Printed T-Shirt" are Title-Case quoted fixtures; rejecting the
# hyphen silently dropped them from the smoke.
_FALLBACK_NAME_SHAPE = re.compile(r"^[A-Z][A-Za-z0-9 \-]{2,29}$")

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PASSWORD_RE = re.compile(r"(?:password|passwd)\s*[:=]\s*['\"]([^'\"]{3,64})['\"]", re.IGNORECASE)

# K2: shipped-database reconcile. Identifier keys the seed source uses for the
# entity's own name (as opposed to foreign keys / descriptions), and the cap on
# how many findings reach the repair prompt (the gate line stays aggregated, so a
# wholesale-unseeded database must not flood the repair round).
_DB_ID_KEYS = ("username", "name", "title", "tag", "slug", "display_name", "email")
_DB_ID_KEY_RE = re.compile(
    r"\b(?:%s)\s*:\s*[\"']([^\"'\n]{2,80})[\"']" % "|".join(_DB_ID_KEYS), re.IGNORECASE
)
_DB_ID_COLUMNS = frozenset(key.lower() for key in _DB_ID_KEYS)
# Object literals WITHOUT nested braces - the flat shape a seed array of entity
# rows is written in.
_DB_OBJECT_RE = re.compile(r"\{[^{}]*\}")
_DB_STATE_CHECKS = (
    ("archived", re.compile(r"\b(?:archived|is_archived)\s*:\s*(true|false)\b", re.IGNORECASE),
     ("archived", "is_archived")),
    ("pinned", re.compile(r"\b(?:pinned|is_pinned)\s*:\s*(true|false)\b", re.IGNORECASE),
     ("pinned", "is_pinned")),
)
MAX_DB_MISSING_NAMES = 20
MAX_DB_STATE_LINES = 10
MAX_DB_SEED_BLOCKS = 4000
MAX_DB_SCAN_ROWS = 200000


def _log(message: str) -> None:
    print(f"[codex][verify] {message}", flush=True)


def _npm_cmd() -> list[str]:
    """npm executable that subprocess can spawn directly (npm.cmd on Windows)."""
    return ["npm.cmd"] if os.name == "nt" else ["npm"]


def _run(cmd: list[str], cwd: Path, timeout: int, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Run a command, capturing output, with a hard timeout."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return {
            "code": proc.returncode,
            "output_tail": ((proc.stdout or "") + (proc.stderr or "")).strip()[-1500:],
        }
    except subprocess.TimeoutExpired:
        return {"code": -1, "output_tail": f"timed out after {timeout}s"}
    except OSError as exc:
        return {"code": -2, "output_tail": str(exc)}


def _iter_source_files(root: Path) -> list[Path]:
    """Yield .js/.ts/.tsx files under root, skipping build/install artifacts."""
    skip_dirs = {"node_modules", "dist", "coverage", ".arc-verify", "build", ".git"}
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for name in filenames:
            if name.endswith((".js", ".ts", ".tsx")):
                out.append(Path(dirpath) / name)
    return out


def _looks_like_fixture_title(value: str) -> bool:
    if not (2 <= len(value) <= 80):
        return False
    if "@" in value or value.lower().startswith(("http://", "https://")):
        return False
    if value.strip().isdigit():
        return False
    return True


def _extract_fixture_titles(requirements_dir: Path) -> list[str]:
    """Extract named fixture entities from requirements.yaml (WS-4).

    Qualifier matches first ("a shelf named \"Shelf 4.1\""), then any quoted
    string as a fallback so the list is never silently empty.
    """
    req = requirements_dir / "requirements.yaml"
    if not req.is_file():
        _log("requirements.yaml not found; skipping fixture extraction")
        return []
    try:
        text = req.read_text(encoding="utf-8")
    except OSError as exc:
        _log(f"could not read requirements.yaml: {exc}")
        return []

    titles: list[str] = []
    for pattern in _QUALIFIER_PATTERNS:
        for match in pattern.finditer(text):
            title = match.group(1).strip()
            if title not in titles and _looks_like_fixture_title(title):
                titles.append(title)
        if len(titles) >= MAX_TITLES:
            break

    # Top up with any quoted string that looks like an entity name, so mixed
    # phrasing cannot drop entities. Fallback titles are held to a strict shape.
    for match in _QUOTED_FALLBACK.finditer(text):
        title = match.group(1).strip()
        if (
            title not in titles
            and _looks_like_fixture_title(title)
            and _FALLBACK_NAME_SHAPE.fullmatch(title)
        ):
            titles.append(title)
        if len(titles) >= MAX_TITLES:
            break

    _log(f"extracted {len(titles)} fixture entit(y/ies) from requirements.yaml")
    return titles


def _seed_evidence(output_dir: Path, titles: list[str]) -> dict[str, bool]:
    """Cross-check each fixture title against the backend seed source (advisory)."""
    hits: dict[str, bool] = {}
    if not titles:
        return hits
    backend = output_dir / "backend"
    files = _iter_source_files(backend) if backend.is_dir() else []
    for title in titles:
        needle = title.lower()
        found = False
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if needle in text.lower():
                found = True
                break
        hits[title] = found
    return hits


# --- Seed-state contract (O2) ---------------------------------------------
# Requirement "Seed data: ..." clauses carry STATE qualifiers that are as much a
# contract as the entity's existence: "regular note X that is not pinned", "note
# Y with the Work label", "note Z without the Work label", "note W with a white
# background", "default label Reminders", "active/archived note". Extract those
# expectations from the requirement text and cross-check the backend seed source;
# violations are blocking (a seed that pins a note the requirement says is
# "not pinned" is a concrete instance of this class).
_SEED_CLAUSE_RE = re.compile(r"Seed\s+data\s*:\s*(.+)", re.IGNORECASE)
_ENTITY_TITLE_IN_PHRASE = re.compile(r"[\"“\u2018]([^\"”\u2019]{2,80})[\"”\u2019]")
_LABEL_NAME_IN_QUOTES = re.compile(r"[\"“\u2018]([A-Za-z][A-Za-z0-9 \-']{1,40})[\"”\u2019]")


def _phrase_title(phrase: str) -> str | None:
    """First quoted string in a seed phrase that looks like an entity name."""
    for m in _ENTITY_TITLE_IN_PHRASE.finditer(phrase):
        title = m.group(1).strip()
        if not _looks_like_fixture_title(title):
            continue
        if any(ch in title for ch in ("@", "!", "#", "?", "=")):
            continue
        return title
    return None


def _quoted_label_names(fragment: str) -> list[str]:
    """Label names quoted inside a 'with/without the ... label' fragment."""
    names: list[str] = []
    for m in _LABEL_NAME_IN_QUOTES.finditer(fragment):
        name = m.group(1).strip()
        if name and name not in names:
            names.append(name)
    return names


def _extract_seed_state_clauses(requirements_dir: Path) -> list[dict[str, Any]]:
    """Extract per-entity seed STATE expectations from requirements.yaml (O2).

    Returns a list of dicts:
      {title, pinned?: bool, archived?: bool, labels_in?: [str],
       labels_out?: [str], color_white?: bool, clause: <raw text>}
    plus clause-level expectations merged under a pseudo-entity `__clause__`:
      {default_label?: str, label_exists?: str}
    Existence-only clauses ("deletable note X", "book Y") yield no dict — the
    fixture reachability check already covers them.
    """
    req = requirements_dir / "requirements.yaml"
    if not req.is_file():
        return []
    try:
        text = req.read_text(encoding="utf-8")
    except OSError:
        return []

    clauses: list[dict[str, Any]] = []
    for m in _SEED_CLAUSE_RE.finditer(text):
        clause = m.group(1).strip()
        # strip trailing YAML quote / period artifacts
        clause = clause.rstrip('"\'”’。 ')
        if not clause:
            continue

        # clause-level: default label / "; label X" definition
        clause_expect: dict[str, Any] = {}
        dm = re.search(r"default\s+label\s+[\"“\u2018]([^\"”\u2019]{1,40})[\"”\u2019]", clause, re.IGNORECASE)
        if dm:
            clause_expect["default_label"] = dm.group(1).strip()
        lm = re.search(r";\s*label\s+[\"“\u2018]([^\"”\u2019]{1,40})[\"”\u2019]", clause, re.IGNORECASE)
        if lm:
            clause_expect["label_exists"] = lm.group(1).strip()
        # "editable label X" reads as a label row that must exist
        em = re.search(r"editable\s+label\s+[\"“\u2018]([^\"”\u2019]{1,40})[\"”\u2019]", clause, re.IGNORECASE)
        if em and "label_exists" not in clause_expect:
            clause_expect["label_exists"] = em.group(1).strip()
        if clause_expect:
            clause_expect["title"] = "__clause__"
            clause_expect["clause"] = clause
            clauses.append(clause_expect)

        # per-entity phrases: split on ';' / ',' / ' and '
        phrases = [p.strip() for p in re.split(r"\s*;\s*|\s*,\s*|\s+and\s+", clause) if p.strip()]
        for phrase in phrases:
            title = _phrase_title(phrase)
            if not title:
                continue
            pl = phrase.lower()
            expect: dict[str, Any] = {"title": title, "clause": clause}
            if re.search(r"\bnot\s+pinned\b|\bunpinned\b", pl):
                expect["pinned"] = False
            elif re.search(r"\bpinned\b", pl):
                expect["pinned"] = True
            elif re.search(r"\bregular\s+(?:note|entry|item|card)\b", pl):
                expect["pinned"] = False  # "regular" implies not pinned
            if re.search(r"\barchived\b", pl):
                expect["archived"] = True
            elif re.search(r"\bactive\s+(?:note|entry|item|card)\b", pl):
                expect["archived"] = False
            wm = re.search(r"with\s+the\s+(?:default\s+)?(.+?)\s*label\b", phrase, re.IGNORECASE)
            if wm:
                names = _quoted_label_names(wm.group(1))
                if names:
                    expect["labels_in"] = names
            om = re.search(r"without\s+the\s+(.+?)\s*label\b", phrase, re.IGNORECASE)
            if om:
                names = _quoted_label_names(om.group(1))
                if names:
                    expect["labels_out"] = names
            if re.search(r"with\s+a\s+white\s+background", pl):
                expect["color_white"] = True
            # only keep phrases that carry at least one STATE expectation
            if any(k in expect for k in ("pinned", "archived", "labels_in", "labels_out", "color_white")):
                clauses.append(expect)

    if clauses:
        _log(f"seed-state: extracted {len(clauses)} state expectation(s) from requirements.yaml")
    return clauses


def _seed_block_for_title(file_text: str, title: str) -> str | None:
    """The JS object literal whose title/name field equals `title` (best-effort)."""
    key = r"\b(?:title|name)\s*[:=]\s*['\"]?" + re.escape(title) + r"['\"]"
    m = re.search(key, file_text, re.IGNORECASE)
    if not m:
        return None
    start = m.start()
    open_idx = file_text.rfind("{", 0, start)
    if open_idx == -1:
        return file_text[max(0, start - 200): start + 500]
    depth = 0
    for i in range(open_idx, len(file_text)):
        if file_text[i] == "{":
            depth += 1
        elif file_text[i] == "}":
            depth -= 1
            if depth == 0:
                return file_text[open_idx:i + 1]
    return file_text[open_idx:open_idx + 800]


def _seed_state_violations(output_dir: Path, clauses: list[dict[str, Any]]) -> list[str]:
    """Cross-check seed-state expectations against the backend seed source (O2)."""
    violations: list[str] = []
    if not clauses:
        return violations
    backend = output_dir / "backend"
    files = _iter_source_files(backend) if backend.is_dir() else []
    if not files:
        return violations

    texts = []
    for path in files:
        try:
            texts.append(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue

    for expect in clauses:
        title = expect["title"]
        clause = expect.get("clause") or title

        # clause-level expectations (default label / label definition row)
        if title == "__clause__":
            for label_key in ("default_label", "label_exists"):
                label = expect.get(label_key)
                if not label:
                    continue
                found = any(
                    re.search(r"\bname\s*[:=]\s*['\"]" + re.escape(label) + r"['\"]", t, re.IGNORECASE)
                    for t in texts
                )
                if not found:
                    severity = "blocking" if label_key == "default_label" else "advisory"
                    msg = (
                        f"seed {severity}: requirement seed clause expects {label_key} {label!r} "
                        f"(clause: {clause[:120]}) but no label row named it in backend seed source"
                    )
                    if severity == "blocking":
                        violations.append(msg)
            continue

        # entity-level: locate the entity's seed object
        block = None
        for t in texts:
            block = _seed_block_for_title(t, title)
            if block is not None:
                break
        if block is None:
            continue  # existence gap is covered by fixture reachability (advisory)
        bl = block.lower()

        if expect.get("pinned") is False and re.search(r"\bpinned\s*[:=]\s*true\b", bl):
            violations.append(
                f"seed-state blocking: requirement says note {title!r} is NOT pinned, but the "
                f"backend seed sets pinned=true (clause: {clause[:120]})"
            )
        elif expect.get("pinned") is True and not re.search(r"\bpinned\s*[:=]\s*true\b", bl):
            violations.append(
                f"seed-state blocking: requirement says note {title!r} IS pinned, but the "
                f"backend seed does not set pinned=true (clause: {clause[:120]})"
            )

        if expect.get("archived") is True and not re.search(
            r"\b(?:archived|is_archived|in_archive|inarchive)\s*[:=]\s*true\b", bl
        ):
            violations.append(
                f"seed-state blocking: requirement says {title!r} starts ARCHIVED, but the "
                f"backend seed does not mark it archived (clause: {clause[:120]})"
            )
        elif expect.get("archived") is False and re.search(
            r"\b(?:archived|is_archived|in_archive|inarchive)\s*[:=]\s*true\b", bl
        ):
            violations.append(
                f"seed-state blocking: requirement says {title!r} starts ACTIVE, but the "
                f"backend seed marks it archived (clause: {clause[:120]})"
            )

        if expect.get("color_white"):
            cm = re.search(r"\bcolor\s*[:=]\s*['\"]([^'\"]+)['\"]", bl) or re.search(
                r"\bbackground\s*[:=]\s*['\"]([^'\"]+)['\"]", bl
            )
            if cm and cm.group(1).strip().lower() not in ("default", "white", "none", ""):
                violations.append(
                    f"seed-state blocking: requirement says note {title!r} starts with a WHITE "
                    f"background, but the backend seed sets color={cm.group(1)!r} "
                    f"(clause: {clause[:120]})"
                )

        labels_in = expect.get("labels_in") or []
        labels_out = expect.get("labels_out") or []
        if labels_in or labels_out:
            lm = re.search(r"\blabels\s*[:=]\s*\[([^\]]*)\]", bl)
            present_names = {n.strip().lower() for n in re.findall(r"['\"]([^'\"]+)['\"]", lm.group(1))} if lm else set()
            for name in labels_in:
                if name.lower() not in present_names:
                    violations.append(
                        f"seed-state blocking: requirement says note {title!r} carries label "
                        f"{name!r}, but the backend seed labels=[] lack it (clause: {clause[:120]})"
                    )
            for name in labels_out:
                if name.lower() in present_names:
                    violations.append(
                        f"seed-state blocking: requirement says note {title!r} does NOT carry "
                        f"label {name!r}, but the backend seed includes it (clause: {clause[:120]})"
                    )

        # O-B: seed initial state — a state-changing action in the requirement
        # scenario requires the target to START in the pre-action state
        # (arrange-act). Deleting a note requires it on the main list (not
        # pre-trashed); clicking Favorite requires it not pre-favorited.
        if expect.get("trashed") is False and re.search(r"\btrashed\s*[:=]\s*true\b", bl):
            violations.append(
                f"seed-state blocking: the requirement scenario DELETES {title!r}, so "
                f"it must start NOT trashed, but the backend seed marks it trashed "
                f"(clause: {clause[:120]})"
            )
        if expect.get("favorited") is False and _favorited_in_seed(texts, title):
            violations.append(
                f"seed-state blocking: the requirement scenario clicks Favorite on "
                f"{title!r}, so it must start NOT favorited, but the backend seed "
                f"pre-favorites it "
                f"(clause: {clause[:120]})"
            )
    return violations


# --- Seed initial-state contract (O-B) -------------------------------------
# A state-changing action in the requirement scenario (delete/archive/pin/
# favorite) requires its target to START in the pre-action state (arrange-act):
# the seed must provide the state the WHEN step acts on, never a post-action
# end state. Deleting a note requires it on the main list (not pre-trashed);
# clicking Favorite requires it not pre-favorited.


def _favorited_in_seed(texts: list[str], title: str) -> bool:
    """True if the backend seed pre-favorites `title` (favorites registry)."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    for t in texts:
        block = _seed_block_for_title(t, title)
        if block and re.search(r"\bfavorit(?:ed|es?)\s*[:=]\s*true\b", block, re.IGNORECASE):
            return True
        for m in re.finditer(
            r"(?:SEED_FAVORITES|favorites\s*[:=]\s*\[|INSERT\s+INTO\s+favorites\b)",
            t,
            re.IGNORECASE,
        ):
            window = t[m.start() : m.start() + 3000]
            low = window.lower()
            if title.lower() in low:
                return True
            if slug and len(slug) > 3 and slug in low:
                return True
    return False


# --- Shipped-database reconcile (K2) --------------------------------------
# Every other seed check in this file reads the seed SOURCE. The app however
# serves a database FILE: `resolveDbPath('database.db')` resolves against the
# backend working directory, boot only runs `CREATE TABLE IF NOT EXISTS`, and
# `npm run db:seed` is a script the platform never executes. So the seed source
# and the shipped file can silently disagree - an agent self-test that ran
# against the default file, a snapshot restored from an earlier state, a seed
# that aborted mid-way and skipped its remaining fixtures. Requirement GIVEN
# fixtures that exist only in the source are then invisible to the app.
# Read-only (sqlite `mode=ro`), deterministic, zero-token.


def _shipped_database(output_dir: Path) -> Path | None:
    """The .db file `npm start` in backend/ would open (newest copy as fallback)."""
    backend = output_dir / "backend"
    exact = backend / "database.db"
    if exact.is_file():
        return exact
    candidates = [
        path
        for path in (list(backend.glob("*.db")) + list(output_dir.glob("*.db")))
        if path.is_file()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _seed_declaration_blocks(output_dir: Path) -> list[tuple[str, str]]:
    """Flat object literals of the backend seed source (file name, block text)."""
    backend = output_dir / "backend"
    if not backend.is_dir():
        return []
    blocks: list[tuple[str, str]] = []
    for path in _iter_source_files(backend):
        if "seed" not in path.name.lower():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in _DB_OBJECT_RE.finditer(text):
            blocks.append((path.name, match.group(0)))
            if len(blocks) >= MAX_DB_SEED_BLOCKS:
                return blocks
    return blocks


def _row_identity_values(row: dict[str, str]) -> set[str]:
    """Values of a database row that are entity names (not ids / timestamps)."""
    return {
        value
        for column, value in row.items()
        if column != "__table__" and column.lower() in _DB_ID_COLUMNS
    }


def _database_snapshot(db: Path) -> dict[str, Any]:
    """Every string value plus the name-keyed rows of every table (bounded)."""
    try:
        import sqlite3

        connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    except Exception as exc:  # noqa: BLE001 - cannot open / no sqlite module
        return {"status": "not-verifiable", "detail": f"database unreadable: {exc}"}
    values: set[str] = set()
    rows: list[dict[str, str]] = []
    try:
        scanned = 0
        for (table,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ):
            name = str(table)
            if not re.fullmatch(r"[A-Za-z0-9_]+", name):
                continue
            columns = [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{name}")')]
            for record in connection.execute(f'SELECT * FROM "{name}"'):
                entry = {"__table__": name}
                for column, value in zip(columns, record):
                    if value is None:
                        continue
                    text = str(value)
                    values.add(text)
                    entry[column] = text
                rows.append(entry)
                scanned += 1
                if scanned >= MAX_DB_SCAN_ROWS:
                    break
            if scanned >= MAX_DB_SCAN_ROWS:
                break
    except Exception as exc:  # noqa: BLE001 - a damaged file is not a code defect
        return {"status": "not-verifiable", "detail": f"database scan failed: {exc}"}
    finally:
        connection.close()
    return {"status": "ran", "values": values, "rows": rows}


def _database_reconcile(output_dir: Path) -> dict[str, Any]:
    """K2: does the shipped runtime database hold what the seed source declares?"""
    blocks = _seed_declaration_blocks(output_dir)
    db = _shipped_database(output_dir)
    if db is None:
        return {"status": "not-verifiable", "detail": "no shipped database file under backend/"}
    if not blocks:
        return {"status": "skipped", "detail": "no seed source entity blocks under backend/"}
    snapshot = _database_snapshot(db)
    if snapshot.get("status") != "ran":
        return snapshot
    values: set[str] = snapshot["values"]
    rows: list[dict[str, str]] = snapshot["rows"]

    missing: list[str] = []
    drift: list[str] = []
    declared = 0
    try:
        rel = db.relative_to(output_dir).as_posix()
    except ValueError:
        rel = db.as_posix()
    for source, block in blocks:
        entities: list[str] = []
        for value in _DB_ID_KEY_RE.findall(block):
            value = value.strip()
            if len(value) >= 2 and value not in entities:
                entities.append(value)
        if not entities:
            continue
        declared += len(entities)
        for value in entities:
            if value not in values and value not in missing:
                missing.append(value)
        for label, pattern, state_columns in _DB_STATE_CHECKS:
            match = pattern.search(block)
            if not match:
                continue
            expected = match.group(1).lower() == "true"
            for value in entities:
                for row in rows:
                    if value not in _row_identity_values(row):
                        continue
                    for column, cell in row.items():
                        if column == "__table__" or column.lower() not in state_columns:
                            continue
                        actual = cell.strip().lower() in ("1", "true")
                        if actual == expected:
                            continue
                        line = (
                            f"{value!r} is stored with {column}={cell} in {rel}, but "
                            f"{source} seeds {label}={'true' if expected else 'false'}"
                        )
                        if line not in drift:
                            drift.append(line)
    violations: list[str] = []
    if missing:
        shown = ", ".join(repr(value) for value in missing[:MAX_DB_MISSING_NAMES])
        more = ""
        if len(missing) > MAX_DB_MISSING_NAMES:
            more = f" (+{len(missing) - MAX_DB_MISSING_NAMES} more)"
        violations.append(
            f"db-inventory blocking: the shipped database the app serves ({rel}) holds NO row for "
            f"{len(missing)} entity value(s) the seed source declares: {shown}{more}. A requirement "
            "GIVEN fixture that is not in this file cannot be loaded by the app. Make "
            f"{rel} match the seed source (run the seed script in backend/ against this exact file, "
            "or insert the missing rows). NEVER resolve this by deleting entities from the seed "
            "source - the seed source is the contract, the file is what has to catch up"
        )
    for line in drift[:MAX_DB_STATE_LINES]:
        violations.append(
            f"db-state blocking: {line}. Restore that row's state in the shipped database "
            "(re-seed it); never flip the seed declaration to match the file"
        )
    return {
        "status": "ran",
        "database": str(rel),
        "declared": declared,
        "missing_count": len(missing),
        "missing": missing[:MAX_DB_MISSING_NAMES],
        "state_drift": drift[:MAX_DB_STATE_LINES],
        "violations": violations,
    }


def _extract_credentials(backend: Path) -> list[dict[str, str]]:
    """Best-effort email/password pairs from the backend seed/fixture sources."""
    if not backend.is_dir():
        return []
    # Prefer the fixtures/seed module (holds the requirement-named account) over
    # incidental email/password literals elsewhere (e.g. demo admins).
    files = sorted(_iter_source_files(backend), key=lambda p: 0 if "fixtures" in p.name else 1)
    pairs: list[dict[str, str]] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        emails = sorted({m.lower() for m in _EMAIL_RE.findall(text)})
        passwords = [m for m in _PASSWORD_RE.findall(text)]
        if not emails or not passwords:
            continue
        for i in range(max(len(emails), len(passwords))):
            pair = {
                "email": emails[min(i, len(emails) - 1)],
                "password": passwords[min(i, len(passwords) - 1)],
            }
            if pair not in pairs:
                pairs.append(pair)
    return pairs[:4]


def _level1_tests(project: Path, label: str) -> dict[str, Any]:
    pkg = project / "package.json"
    if not pkg.is_file():
        return {"label": label, "status": "skipped", "detail": "no package.json"}
    try:
        scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
    except (OSError, ValueError) as exc:
        return {"label": label, "status": "skipped", "detail": f"package.json unreadable: {exc}"}
    if "test" not in scripts:
        return {"label": label, "status": "skipped", "detail": "no 'test' script"}
    if not (project / "node_modules").is_dir():
        return {"label": label, "status": "skipped", "detail": "node_modules not installed"}
    _log(f"L1 {label}: running 'npm test' (timeout {TEST_TIMEOUT_S}s)")
    result = _run(_npm_cmd() + ["test"], project, TEST_TIMEOUT_S)
    status = "passed" if result["code"] == 0 else ("failed" if result["code"] > 0 else "errored")
    return {"label": label, "status": status, "code": result["code"], "output_tail": result["output_tail"]}


def _ensure_frontend_build(frontend: Path) -> dict[str, Any]:
    dist = frontend / "dist"
    if dist.is_dir() and any(dist.iterdir()):
        return {"status": "ok", "detail": "dist already present"}
    if not (frontend / "package.json").is_file():
        return {"status": "skipped", "detail": "no frontend package.json"}
    _log(f"L2 build: running 'npm run build' (timeout {BUILD_TIMEOUT_S}s)")
    result = _run(_npm_cmd() + ["run", "build"], frontend, BUILD_TIMEOUT_S)
    if result["code"] != 0:
        return {"status": "failed", "code": result["code"], "output_tail": result["output_tail"]}
    return {"status": "ok", "detail": "built"}


def _start_backend(backend: Path, port: int) -> tuple[subprocess.Popen | None, Path, str]:
    """Start `npm start` with a dedicated PORT. Returns (proc, logfile, base_url)."""
    logfile = backend / ".arc-verify-server.log"
    logfile.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PORT"] = str(port)
    try:
        handle = open(logfile, "w", encoding="utf-8")
        proc = subprocess.Popen(
            _npm_cmd() + ["start"],
            cwd=str(backend),
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        return proc, logfile, f"http://127.0.0.1:{port}"
    except OSError as exc:
        return None, logfile, f"http://127.0.0.1:{port} (start failed: {exc})"


def _wait_ready(base_url: str, timeout: int) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        for probe in ("/api/health", "/"):
            try:
                with urllib.request.urlopen(base_url + probe, timeout=3) as resp:
                    if resp.status == 200:
                        return True, f"GET {probe} -> {resp.status}"
            except Exception as exc:  # noqa: BLE001 - any probe failure is transient
                last_error = str(exc)[:200]
        time.sleep(1)
    return False, f"not ready in {timeout}s (last: {last_error})"


SMOKE_TEMPLATE = r"""
import { createRequire } from 'module';
import fs from 'fs';
const require = createRequire(import.meta.url);
const __payload = __PAYLOAD__;
let chromium;
try {
  ({ chromium } = require(__payload.playwrightPath));
} catch (e) {
  console.log(JSON.stringify({ fatal: 'playwright require failed: ' + String(e).slice(0, 200) }));
  process.exit(0);
}
const results = [];
(async () => {
  let browser;
  try {
    browser = await chromium.launch();
  } catch (e) {
    console.log(JSON.stringify({ skipped: 'chromium unavailable: ' + String(e).slice(0, 300) }));
    process.exit(0);
  }
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
    const texts = [];
    const visited = [];
    // Hard overall deadline: the runner kills this process at SMOKE_TIMEOUT_S, and a
    // killed smoke used to produce ZERO results -> gate "not-verifiable" -> shipped
    // unverified. Self-abort ~35s before the kill and still write the
    // results collected so far, marking them partial. Scales with
    // ARCBENCH_VERIFY_SMOKE_TIMEOUT so a custom timeout stays consistent.
    const deadlineAt = Date.now() + (Number(process.env.ARCBENCH_VERIFY_SMOKE_TIMEOUT || 120) * 1000) - 35000;
    const expired = () => Date.now() > deadlineAt;
    let pageLoadOk = false;
    let pageLoadError = '';
    try {
      await page.goto(__payload.baseUrl, { waitUntil: 'networkidle', timeout: 15000 });
      pageLoadOk = true;
    } catch (e) {
      pageLoadError = String(e).slice(0, 200);
    }

    async function scanPage() {
      const s = { forms: [], logo: { found: false, detail: '' }, collisions: [], shadowing: [], dialogs: [], semantic: [], hoverHidden: [] };
      try {
        s.forms = await page.$$eval('form', els => els.map(f => {
          const label = (f.getAttribute('aria-label') || '').trim();
          const labelledby = (f.getAttribute('aria-labelledby') || '').trim();
          const name = label || (labelledby ? labelledby + ' (labelledby)' : '');
          const first = f.querySelector('input, textarea, select, button');
          const sample = first
            ? (first.getAttribute('name') || first.getAttribute('placeholder') || (first.textContent || '')).trim().slice(0, 40)
            : '';
          const isSearch = !!f.querySelector('input[type="search"], [role="search"], input[placeholder*="earch" i]');
          return { named: Boolean(name), isSearch, sample };
        }));
      } catch (e) {}
      try {
        s.logo = await page.evaluate(() => {
          const home = document.querySelector('a[href="/"], a[href="' + location.origin + '/"]');
          if (home) {
            const name = (home.getAttribute('aria-label') || home.innerText || '').trim();
            return { found: /logo/i.test(name), detail: 'home-link name: ' + JSON.stringify(name.slice(0, 60)) };
          }
          const any = [...document.querySelectorAll('a,button')].find(e => /logo/i.test((e.getAttribute('aria-label') || e.innerText || '')));
          return {
            found: Boolean(any),
            detail: any ? 'element with "logo" in name: ' + JSON.stringify((any.getAttribute('aria-label') || any.innerText || '').slice(0, 60)) : 'no home link and no logo-named element',
          };
        });
      } catch (e) { s.logo = { found: false, detail: String(e).slice(0, 120) }; }
      try {
        s.collisions = await page.$$eval('[role="toolbar"], [role="group"], aside, [role="complementary"]', (els, roots) => {
          const out = [];
          for (const el of els) {
            const label = (el.getAttribute('aria-label') || '').trim();
            if (!label) continue;
            const low = label.toLowerCase();
            for (const root of roots) {
              if (new RegExp('\\b' + root + '\\b', 'i').test(low)) {
                out.push({ tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '', label: label.slice(0, 80), root });
                break;
              }
            }
          }
          return out;
        }, __payload.fieldWordRoots);
      } catch (e) { s.collisions = []; }
      try {
        // Label shadowing (WS-8, generalized): the standard label resolver tries
        // getByLabel(/<full label>/i) FIRST and takes the FIRST hit in document order,
        // so ANY element whose accessible name contains a field's label phrase
        // (substring match) and precedes the field in the DOM shadows it and
        // locator.fill() dies with "Element is not an <input>". The scan therefore
        // covers ALL named elements in global DOM order (including a[href] / button,
        // which the old container-only scan missed), matching by the FULL label
        // phrase (not single words, so "New password" vs "Confirm new password" does
        // not false-positive). Severity classes: non-editable container with a phrase
        // >= 4 chars (blocking), field-vs-field label collision (blocking); short
        // phrases (< 4 chars, e.g. "To") and interactive chrome links/buttons
        // (advisory: often requirement-mandated navigation, renaming is not always
        // possible).
        s.shadowing = await page.$$eval('*', els => {
          const accName = el => {
            const al = (el.getAttribute('aria-label') || '').trim();
            if (al) return al;
            const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
            if (ids.length) {
              return ids
                .map(id => (document.getElementById(id) || {}).textContent || '')
                .join(' ')
                .trim();
            }
            return '';
          };
          const fieldLabel = f => {
            const direct = accName(f);
            if (direct) return direct;
            if (f.id) {
              const lab = document.querySelector('label[for="' + CSS.escape(f.id) + '"]');
              const t = ((lab || {}).textContent || '').trim();
              if (t) return t;
            }
            const wrap = f.closest('label');
            if (wrap) return (wrap.textContent || '').trim();
            return (f.getAttribute('placeholder') || '').trim();
          };
          const norm = s => s.toLowerCase().replace(/[^a-z0-9 ]+/g, ' ').replace(/\s+/g, ' ').trim();
          const editable = t => t === 'input' || t === 'textarea' || t === 'select';
          // Every element with an accessible name, in document order.
          const named = [];
          for (let i = 0; i < els.length; i++) {
            const el = els[i];
            const tag = el.tagName.toLowerCase();
            let name = norm(accName(el));
            if (!name && editable(tag)) name = norm(fieldLabel(el));
            if (name.length < 2 || name.length > 90) continue;
            named.push({ el, name, tag });
          }
          // Editable fields with their label phrase.
          const fields = [];
          for (const el of els) {
            const tag = el.tagName.toLowerCase();
            if (!editable(tag)) continue;
            if (el.disabled || el.type === 'hidden') continue;
            const label = norm(fieldLabel(el));
            if (label.length < 2) continue;
            fields.push({ el, tag, label });
          }
          const out = [];
          const seen = new Set();
          for (const f of fields) {
            const phrase = f.label;
            if (seen.has(phrase)) continue;
            seen.add(phrase);
            let hit = null;
            for (const n of named) {
              if (n.name.includes(phrase)) { hit = n; break; }
            }
            if (!hit || hit.el === f.el) continue;
            const role = (hit.el.getAttribute('role') || '').toLowerCase();
            const interactive = hit.tag === 'a' || hit.tag === 'button' || role === 'button' || role === 'link';
            const kind = editable(hit.tag)
              ? 'field-collision'
              : (interactive ? 'chrome-link' : 'container');
            out.push({
              container: hit.tag,
              containerName: (accName(hit.el) || fieldLabel(hit.el)).slice(0, 80),
              field: f.tag,
              fieldName: (fieldLabel(f.el) || '').slice(0, 60),
              word: phrase,
              kind,
            });
          }
          return out.slice(0, 40);
        });
      } catch (e) { s.shadowing = []; }
      try {
        // Accessible-name shadowing of page actions by global chrome (generic
        // rule): a global getByRole('button', {name:/X/i}) query keeps the
        // first hit in document order, so a header/nav/footer BUTTON whose
        // accessible name equals a page action's name (a form submit, a
        // filter checkbox, a menu option) steals every unqualified click and
        // the page action never fires (e.g. a header <Link role="button">
        // aria-label="Sign in" preceding the login form's "SIGN IN" submit).
        // Only chrome buttons shadow (the button role is tried before link);
        // in-main elements are the victims. Exact name equality is blocking; a
        // >= 4 char containment overlap is advisory (substring regexes can also
        // collide, e.g. header "Sign in" vs a page "Sign in to your account").
        s.nameShadow = await page.$$eval(
          'button, [role="button"], input[type="submit"], a[href], [role="menuitem"], [role="tab"], [role="checkbox"], [role="radio"], [role="option"]',
          els => {
            const nameOf = el => {
              const al = (el.getAttribute('aria-label') || '').trim();
              if (al) return al;
              if (el.tagName === 'INPUT') return (el.getAttribute('value') || '').trim();
              return (el.textContent || '').replace(/\s+/g, ' ').trim();
            };
            const norm = s => s.toLowerCase().replace(/[^a-z0-9 ]+/g, ' ').replace(/\s+/g, ' ').trim();
            const isButton = el =>
              el.tagName === 'BUTTON' ||
              el.getAttribute('role') === 'button' ||
              (el.tagName === 'INPUT' && el.getAttribute('type') === 'submit');
            // "Chrome" used to be "anything outside <main>", so the verdict
            // depended on whether the agent happened to emit a <main> tag —
            // with none in the DOM every control read as chrome, the victim
            // set was structurally empty and the probe went silent. Now a
            // landmark region is the primary signal and `landmarksPresent`
            // tells the classifier when the chrome/action split is not
            // trustworthy.
            const LANDMARK_SEL =
              'header,[role="banner"],nav,[role="navigation"],aside,[role="complementary"],' +
              'footer,[role="contentinfo"]';
            const isLandmark = el => !!el.closest(LANDMARK_SEL);
            const landmarksPresent = !!document.querySelector(LANDMARK_SEL);
            // Landmark membership IS the chrome test; `<main>` only serves as the
            // fallback for landmark-free pages so the probe degrades instead of
            // silently treating every control as chrome.
            const chrome = el => landmarksPresent ? isLandmark(el) : !el.closest('main');
            const actionTag = el =>
              isButton(el) || ['menuitem', 'tab', 'checkbox', 'radio', 'option'].includes(el.getAttribute('role'));
            const buttons = [];
            const mains = [];
            let domIdx = 0;
            for (const el of els) {
              const myIdx = domIdx++;
              if (el.disabled || el.getAttribute('aria-hidden') === 'true') continue;
              const name = norm(nameOf(el));
              if (name.length < 3) continue;
              if (chrome(el)) {
                if (isButton(el)) buttons.push({
                  name, tag: el.tagName.toLowerCase(), idx: myIdx,
                  inLandmark: isLandmark(el),
                });
              } else if (actionTag(el)) {
                mains.push({ name, tag: el.tagName.toLowerCase(), idx: myIdx });
              }
            }
            const out = [];
            const seenExact = new Set();
            for (const b of buttons) {
              for (const m of mains) {
                if (b.name !== m.name) continue;
                const key = b.name;
                if (seenExact.has(key)) continue;
                seenExact.add(key);
                out.push({
                  kind: 'exact',
                  chromeTag: 'button',
                  chromeName: b.name,
                  mainTag: m.tag,
                  mainName: m.name,
                  chromeInLandmark: !!b.inLandmark,
                  landmarksPresent,
                  chromeFirst: b.idx < m.idx,
                });
              }
            }
            const seenContain = new Set();
            for (const b of buttons) {
              if (b.name.length < 4) continue;
              for (const m of mains) {
                if (b.name === m.name) continue;
                if (!(m.name.includes(b.name) || b.name.includes(m.name))) continue;
                const key = b.name + '|' + m.name;
                if (seenContain.has(key)) continue;
                seenContain.add(key);
                out.push({
                  kind: 'contains',
                  chromeTag: 'button',
                  chromeName: b.name,
                  mainTag: m.tag,
                  mainName: m.name,
                  chromeInLandmark: !!b.inLandmark,
                  landmarksPresent,
                  chromeFirst: b.idx < m.idx,
                });
              }
            }
            return out.slice(0, 30);
          }
        );
      } catch (e) { s.nameShadow = []; }
      try {
        // The MIRROR of `name-shadow` (generic rule): a navigation entry inside
        // a landmark (nav/aside/complementary) that carries a page action's
        // name MUST keep the button role — demoting it to a plain link to dodge
        // a same-name collision breaks landmark-scoped button queries. Only the
        // names supplied in the smoke payload are checked; with no evidence
        // list the probe stays inert.
        s.landmarkRoles = [];
        const wantedNames = (__payload.scopedButtonNames || [])
          .map(v => String(v || '').trim()).filter(Boolean);
        if (wantedNames.length) {
          s.landmarkRoles = await page.$$eval(
            'aside,nav,[role="complementary"],[role="navigation"]',
            (nodes, names) => {
              const nameOf = el => {
                const al = (el.getAttribute('aria-label') || '').trim();
                if (al) return al;
                if (el.tagName === 'INPUT') return (el.getAttribute('value') || '').trim();
                return (el.textContent || '').replace(/\s+/g, ' ').trim();
              };
              const norm = t => String(t).toLowerCase().replace(/[^a-z0-9 ]+/g, ' ')
                .replace(/\s+/g, ' ').trim();
              const isBtn = el =>
                el.tagName === 'BUTTON' || el.getAttribute('role') === 'button' ||
                (el.tagName === 'INPUT' && el.getAttribute('type') === 'submit');
              const isLink = el =>
                (el.tagName === 'A' && el.hasAttribute('href')) || el.getAttribute('role') === 'link';
              const out = [];
              for (const raw of names) {
                const want = norm(raw);
                if (!want) continue;
                let buttons = 0;
                let links = 0;
                for (const lm of nodes) {
                  for (const el of lm.querySelectorAll('a[href],button,input[type="submit"],[role="button"],[role="link"]')) {
                    if (el.getAttribute('aria-hidden') === 'true') continue;
                    if (norm(nameOf(el)) !== want) continue;
                    // Dual-role entries (<a role="button">) answer both queries and
                    // are counted as buttons, exactly like the resolver's role sweep.
                    if (isBtn(el)) buttons += 1;
                    else if (isLink(el)) links += 1;
                  }
                }
                if (links > 0 && buttons === 0) {
                  out.push({ name: raw, demoted: true, links, buttons });
                }
              }
              return out;
            },
            wantedNames,
          );
        }
      } catch (e) { s.landmarkRoles = []; }
      try {
        // Strict-locator semantic scan (rule B): interactive controls whose
        // accessible name is decorated (ellipsis / ": detail" / "— detail" /
        // trailing " menu") can never match an exact anchored getByRole name.
        s.semantic = await page.$$eval('button, a[href], input, [role="button"]', els => {
          const out = [];
          for (const el of els) {
            const al = (el.getAttribute('aria-label') || '').trim();
            const text = (el.textContent || '').replace(/\s+/g, ' ').trim();
            const name = al || text;
            if (!name) continue;
            if (al && al.endsWith('...')) {
              out.push({ tag: el.tagName.toLowerCase(), kind: 'ellipsis-name', name: name.slice(0, 80) });
            } else if (al && al.includes(': ')) {
              out.push({ tag: el.tagName.toLowerCase(), kind: 'colon-suffix', name: name.slice(0, 80) });
            } else if (al && al.includes(' — ')) {
              out.push({ tag: el.tagName.toLowerCase(), kind: 'dash-suffix', name: name.slice(0, 80) });
            } else if (al && /\smenu$/i.test(al)) {
              out.push({ tag: el.tagName.toLowerCase(), kind: 'menu-suffix', name: name.slice(0, 80) });
            } else if (!al && text.endsWith('...')) {
              out.push({ tag: el.tagName.toLowerCase(), kind: 'ellipsis-text', name: text.slice(0, 80) });
            }
          }
          return out;
        });
      } catch (e) { s.semantic = []; }
      try {
        // Rule A: hover-revealed controls hidden via visibility are removed from
        // the accessibility tree; card-scoped strict locators then only match
        // while the pointer is exactly on the card (fragile by luck).
        s.hoverHidden = await page.$$eval('div, section, li, article, ul, main', els => {
          const hits = [];
          for (const el of els) {
            const cls = (el.getAttribute('class') || '');
            const visHidden = /invisible/.test(cls) && /group-hover:visible|hover:visible/.test(cls);
            const dispHidden = /hidden/.test(cls) && /group-hover:flex|hover:flex/.test(cls);
            if ((visHidden || dispHidden) && el.querySelector('button')) {
              hits.push(cls.slice(0, 80));
            }
          }
          return hits;
        });
      } catch (e) { s.hoverHidden = []; }
      try {
        s.dialogs = await page.$$eval('[role="dialog"]', els => els.map(e => (e.getAttribute('aria-label') || '').slice(0, 80)).filter(Boolean));
      } catch (e) { s.dialogs = []; }
      return s;
    }

    async function capture() {
      try {
        const t = await page.evaluate(() => (document.body ? document.body.innerText : ''));
        texts.push(t);
        visited.push(page.url());
      } catch (e) {}
    }

    // Primary nav links + form-ish links (login/new/create/add) from header/nav.
    let navLinks = [];
    try {
      navLinks = await page.$$eval('header a[href^="/"], nav a[href^="/"]', els =>
        [...new Set(els.map(e => e.getAttribute('href')).filter(h => h && h.startsWith('/') && h.length > 1))]
      );
    } catch (e) { navLinks = []; }
    results.push({ check: 'nav-links', found: navLinks.length > 0, links: navLinks.slice(0, __payload.maxNavLinks) });

    // Accumulate per-page DOM scans across every visited page.
    const formSeen = [];
    const unnamedForms = [];
    const collisions = [];
    const shadowing = [];
    const nameShadow = [];
    const landmarkRoles = [];
    const dialogLabels = [];
    const semantic = [];
    const hoverHidden = [];
    let logoFound = false;
    let logoDetail = '';
    const formishLinks = (navLinks || []).filter(h => /(new|create|add|login|register)/i.test(h)).slice(0, 2);

    if (pageLoadOk) {
      await capture();
      const s = await scanPage();
      formSeen.push(...s.forms);
      unnamedForms.push(...s.forms.filter(f => !f.named && !f.isSearch));
      collisions.push(...s.collisions);
      shadowing.push(...s.shadowing);
      nameShadow.push(...(s.nameShadow || []));
      landmarkRoles.push(...(s.landmarkRoles || []));
      dialogLabels.push(...s.dialogs);
      if (s.logo.found) { logoFound = true; logoDetail = s.logo.detail; }
    }

    // OPT-1: named fixtures must be locatable on the FIRST page of a listing —
    // an entity users select by name must render on page 1 (a fixture seeded on
    // page 2 is unreachable from the initial listing even though the smoke
    // finds every other fixture). After each page-1 capture, walk the
    // listing's pagination (bounded) and record on which page any still-missing
    // fixture first shows up, so the gate can block "only reachable after
    // pagination" instead of reporting it as a plain missing advisory.
    const pagedTexts = [];
    const page1Lower = () => texts.join('\n').toLowerCase();
    const allFoundOnPage1 = () =>
      (__payload.titles || []).every(t => page1Lower().includes(String(t).toLowerCase()));
    async function capturePaged(pg) {
      try {
        const t = await page.evaluate(() => (document.body ? document.body.innerText : ''));
        pagedTexts.push({ url: page.url(), page: pg, text: t });
      } catch (e) {}
    }
    async function walkPagination() {
      if (!(__payload.titles || []).length || allFoundOnPage1()) return;
      let nav = null;
      try {
        nav = page.locator('nav[aria-label*="Pagination" i], [aria-label*="pagination" i], [role="navigation"][aria-label*="page" i]').first();
        if (!(await nav.count())) return;
      } catch (e) { return; }
      for (let pg = 2; pg <= __payload.paginationMaxPages && !allFoundOnPage1() && !expired(); pg++) {
        let clicked = false;
        try {
          const numBtn = nav.locator('button, a[href]').filter({ hasText: new RegExp('^\\s*' + pg + '\\s*$') }).first();
          if (await numBtn.count()) { await numBtn.click({ timeout: 3000 }); clicked = true; }
        } catch (e) {}
        if (!clicked) {
          try {
            const nextBtn = nav.locator('button, a[href]').filter({ hasText: /(next|›|»)/i }).first();
            if (await nextBtn.count()) { await nextBtn.click({ timeout: 3000 }); clicked = true; }
          } catch (e) {}
        }
        if (!clicked) break;
        await page.waitForTimeout(450);
        await capturePaged(pg);
      }
    }

    const toVisit = [...new Set([...navLinks, ...formishLinks])].slice(0, __payload.maxNavLinks);
    for (const href of toVisit) {
      if (expired()) break;
      try {
        // Navigate by clicking the nav link (client-side routing), never
        // full-page goto on a deep link (SPA fallback serving is not
        // guaranteed and clicking exercises the same path a user takes).
        const target = page.locator('header a[href="' + href + '"], nav a[href="' + href + '"]').first();
        if (await target.count()) {
          await target.click({ timeout: 5000 });
        } else {
          await page.goto(__payload.baseUrl + href, { waitUntil: 'domcontentloaded', timeout: 10000 });
        }
        await page.waitForTimeout(500);
        await capture();
        await walkPagination();
        const s = await scanPage();
        formSeen.push(...s.forms);
        unnamedForms.push(...s.forms.filter(f => !f.named && !f.isSearch));
        collisions.push(...s.collisions);
        shadowing.push(...s.shadowing);
        nameShadow.push(...(s.nameShadow || []));
        landmarkRoles.push(...(s.landmarkRoles || []));
        dialogLabels.push(...s.dialogs);
        semantic.push(...s.semantic);
        hoverHidden.push(...s.hoverHidden);
        if (s.logo.found) { logoFound = true; logoDetail = s.logo.detail; }
      } catch (e) {}
    }
    results.push({ check: 'pages-visited', count: visited.length, urls: visited.slice(0, 12) });

    // O-A: named fixtures must be reachable by generic accessibility semantics
    // — candidate roles probed with a short visibility window. The old
    // text-substring check only proved the TEXT exists somewhere, so a fixture
    // rendered as plain text / hidden / wrong-role still passed the smoke.
    // Candidates cover the common interactive role set (parameterized per task
    // via the smoke payload); "text present but no candidate role visible" is
    // a contract violation, not a pass.
    // Page-1 visibility is the fixture contract; pagination captures only
    // PROVE whether a still-missing fixture exists somewhere later in the list
    // (blocked by the gate as "only reachable after pagination").
    const FIXTURE_ROLES = __payload.fixtureRoleCandidates || ['heading', 'button', 'link', 'tab', 'menuitem', 'checkbox', 'option'];
    const FIXTURE_WINDOW_MS = __payload.fixtureWindowMs || 500;
    const page1Blob = page1Lower();
    const page1UrlOf = title => {
      const needle = String(title).toLowerCase();
      for (let i = 0; i < texts.length; i++) {
        if (texts[i].toLowerCase().includes(needle)) return visited[i] || '';
      }
      return '';
    };
    const escRe = s => String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    async function fixtureResolve(title) {
      const pattern = new RegExp(escRe(title), 'i'); // 与官方 toPattern 一致：宽松子串匹配
      const out = { title: String(title), ok: false, roles: [], visibleRoles: [], slow: null, textOnly: false };
      const needle = String(title).toLowerCase();
      for (const role of FIXTURE_ROLES) {
        if (expired()) break;
        let loc;
        try { loc = page.getByRole(role, { name: pattern }); } catch (e) { continue; }
        let count = 0;
        try { count = await loc.count(); } catch (e) { continue; }
        if (!count) continue;
        out.roles.push(role);
        const first = loc.first();
        const t0 = Date.now();
        // 单候选可见性探测窗口（通用冒烟参数）：等元素进入 DOM 最多 500ms，然后即时判定可见性
        try { await first.waitFor({ state: 'attached', timeout: FIXTURE_WINDOW_MS }); } catch (e) {}
        let visible = false;
        try { visible = await first.isVisible(); } catch (e) {}
        const elapsed = Date.now() - t0;
        if (visible) {
          out.visibleRoles.push(role);
          if (out.slow === null || elapsed > out.slow) out.slow = elapsed;
          if (!out.ok) out.ok = true;
        }
      }
      if (!out.ok && page1Blob.includes(needle)) out.textOnly = true;
      return out;
    }
    const fixtureFind = [];
    for (const title of (__payload.titles || [])) {
      if (expired()) break;
      const r = await fixtureResolve(title);
      const needle = String(title).toLowerCase();
      let later = null;
      if (!r.ok) {
        for (const p of pagedTexts) {
          if (p.text.toLowerCase().includes(needle)) { later = { page: p.page, url: p.url }; break; }
        }
      }
      r.firstPage = r.ok ? 1 : (later ? later.page : null);
      r.pageUrl = r.ok ? page1UrlOf(title) : (later ? later.url : null);
      fixtureFind.push(r);
    }
    results.push({
      check: 'fixtures',
      found: fixtureFind.filter(f => f.ok).length,
      total: fixtureFind.length,
      detail: fixtureFind,
    });

    results.push({ check: 'forms', count: formSeen.length, unnamed: unnamedForms });
    results.push({ check: 'logo', found: logoFound, detail: logoFound ? logoDetail : 'logo not found on any visited page' });
    results.push({ check: 'name-collisions', count: collisions.length, detail: collisions });
    const shadowSeen = new Set();
    const shadowUnique = shadowing.filter(x => {
      const k = x.container + '|' + x.word + '|' + x.fieldName;
      if (shadowSeen.has(k)) return false;
      shadowSeen.add(k);
      return true;
    });
    results.push({ check: 'label-shadowing', count: shadowUnique.length, detail: shadowUnique });
    const nsSeen = new Set();
    const nsUnique = nameShadow.filter(x => {
      const k = x.kind + '|' + x.chromeName + '|' + x.mainName;
      if (nsSeen.has(k)) return false;
      nsSeen.add(k);
      return true;
    });
    results.push({ check: 'name-shadow', count: nsUnique.length, detail: nsUnique });
    const lmSeen = new Set();
    const lmUnique = landmarkRoles.filter(x => {
      const k = String(x.name || '').toLowerCase();
      if (!k || lmSeen.has(k)) return false;
      lmSeen.add(k);
      return true;
    });
    results.push({ check: 'landmark-roles', count: lmUnique.length, detail: lmUnique });
    results.push({ check: 'semantic', count: semantic.length, detail: semantic });
    results.push({ check: 'hover-hidden', count: hoverHidden.length, detail: hoverHidden });
    results.push({ check: 'dialogs', labels: [...new Set(dialogLabels)] });

    // Best-effort login (advisory): go home, open the login page, fill each
    // credential pair until one succeeds.
    const creds = __payload.credentials || [];
    if (creds.length) {
      let login = { attempted: false, ok: false, detail: '' };
      for (const cred of creds.slice(0, 3)) {
        if (expired()) break;
        try {
          await page.goto(__payload.baseUrl, { waitUntil: 'domcontentloaded', timeout: 10000 });
          await page.waitForTimeout(300);
          const loginLink = page.locator('header a[href*="/login" i], nav a[href*="/login" i]').first();
          if (await loginLink.count()) {
            await loginLink.click();
            await page.waitForTimeout(800);
            // OPT-2: the auth page is exactly where a same-named chrome button
            // (header "Sign in" with role="button") hijacks the resolver and
            // where the login submit lives — scan it so `name-shadow` can prove
            // the collision even when the login page is beyond the nav-visit cap.
            try {
              const s = await scanPage();
              nameShadow.push(...(s.nameShadow || []));
              landmarkRoles.push(...(s.landmarkRoles || []));
            } catch (e) {}
          }
          const email = page.locator('input[type="email"], input[autocomplete="email"], input[name*="mail" i], input[placeholder*="mail" i]').first();
          const pass = page.locator('input[type="password"]').first();
          if ((await email.count()) && (await pass.count())) {
            login.attempted = true;
            await email.fill(cred.email);
            await pass.fill(cred.password);
            // Click the submit that belongs to the SAME form as the email/password
            // fields. A global chrome submit (header search "Search") sorts first
            // in DOM order and used to be clicked instead, bouncing the probe to
            // /search and reporting a false "login failed" (a global header
            // search submit sorts first and bounces the probe to /search).
            const clicked = await page.evaluate(() => {
              const em = document.querySelector(
                'input[type="email"], input[autocomplete="email"], input[name*="mail" i], input[placeholder*="mail" i]'
              );
              const form = em ? em.closest('form') : null;
              if (!form) return false;
              const btns = Array.from(form.querySelectorAll('button[type="submit"], input[type="submit"]'));
              const real = btns.find(b => (b.textContent || b.getAttribute('value') || '').trim());
              if (real) { real.click(); return true; }
              return false;
            });
            if (!clicked) await page.keyboard.press('Enter');
            await page.waitForTimeout(2500);
            // "The URL changed" is not a successful login: an app that bounces a failed
            // submit onto /search used to pass this probe. Require an observable
            // session on a non-auth/non-search landing page.
            let landed = page.url();
            let path = '';
            try { path = new URL(landed).pathname; } catch (e) { path = landed; }
            let session = false;
            try {
              session = await page.evaluate(() => {
                try {
                  const ls = Object.keys(localStorage).some(
                    k => /token|auth|session|user|jwt/i.test(k) && !!localStorage.getItem(k)
                  );
                  const ck = /token|auth|session|sid/i.test(document.cookie || '');
                  return ls || ck;
                } catch (e) { return false; }
              });
            } catch (e) { session = false; }
            let signOutSeen = 0;
            try {
              signOutSeen = await page.getByText(/sign out|log out|signout|logout/i).first().count();
            } catch (e) { signOutSeen = 0; }
            if (/\/(login|register|signin|sign-in)/i.test(path)) {
              login.detail = 'still on auth page, url: ' + landed;
            } else if (!(session || signOutSeen)) {
              login.detail = 'left /login but no session token or sign-out control, url: ' + landed;
            } else {
              login.ok = true;
              login.detail = 'after submit url: ' + landed + (session ? ' (session stored)' : ' (sign-out visible)');
              break;
            }
          } else {
            login.detail = 'no email/password form found';
          }
        } catch (e) { login.detail = 'attempt error: ' + String(e).slice(0, 150); }
      }
      results.push({ check: 'login', ...login });
      let headings = [];
      try {
        headings = await page.$$eval('h1,h2,h3', els => els.map(e => (e.textContent || '').trim()).filter(Boolean).slice(0, 25));
      } catch (e) {}
      results.push({ check: 'homepage-headings', headings });
    }

    if (expired()) results.push({ check: 'partial', reason: 'smoke deadline exceeded; some checks may be missing' });
    results.unshift({ check: 'page-load', ok: pageLoadOk, url: page.url(), error: pageLoadError });
  } catch (e) {
    results.push({ check: 'fatal', error: String(e).slice(0, 300) });
  } finally {
    await browser.close();
  }
  try {
    fs.writeFileSync(__payload.outputPath, JSON.stringify(results));
  } catch (e) {
    console.log(JSON.stringify({ fatal: 'could not write smoke output: ' + String(e).slice(0, 200) }));
    process.exit(0);
  }
  console.log('smoke-json-written ' + results.length);
})().catch((e) => {
  console.log(JSON.stringify({ fatal: String(e).slice(0, 500) }));
  process.exit(0);
});
"""


def _browser_smoke(
    backend: Path,
    base_url: str,
    titles: list[str],
    credentials: list[dict[str, str]],
    scoped_button_names: list[str] | None = None,
    *,
    install_browser: bool = False,
) -> dict[str, Any]:
    """Main-chain browser smoke via Playwright chromium (if available)."""
    backend = backend.resolve()
    playwright_pkg = backend / "node_modules" / "playwright"
    if not playwright_pkg.is_dir():
        return {"status": "skipped", "detail": "playwright not installed in backend/node_modules"}
    if install_browser:
        _log("L2 browser: installing chromium (FEATURE_FLAGS['verify_browser_install'])")
        _run(["npx", "playwright", "install", "chromium"], backend, 300)

    smoke_dir = backend / ".arc-verify"
    smoke_dir.mkdir(parents=True, exist_ok=True)
    script = smoke_dir / "smoke.mjs"
    output_path = smoke_dir / "smoke-output.json"
    payload = {
        "baseUrl": base_url,
        "titles": titles[:MAX_SMOKE_TITLES],
        "credentials": credentials,
        "playwrightPath": str(playwright_pkg),
        "fieldWordRoots": list(FIELD_WORD_ROOTS),
        "maxNavLinks": MAX_NAV_LINKS,
        "paginationMaxPages": PAGINATION_MAX_PAGES,
        "fixtureRoleCandidates": list(FIXTURE_ROLE_CANDIDATES),
        "fixtureWindowMs": FIXTURE_WINDOW_MS,
        "scopedButtonNames": list(scoped_button_names or []),
        "outputPath": str(output_path),
    }
    script.write_text(
        SMOKE_TEMPLATE.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False)),
        encoding="utf-8",
    )
    _log(
        f"L2 browser: main-chain smoke over {min(len(titles), MAX_SMOKE_TITLES)} fixture entit(y/ies) "
        f"(timeout {SMOKE_TIMEOUT_S}s)"
    )
    result = _run(["node", str(script)], backend, SMOKE_TIMEOUT_S)
    if result["code"] != 0 and SMOKE_RETRIES > 0:
        _log(
            f"L2 browser: smoke exited code {result['code']} "
            f"(tail: {str(result['output_tail'])[-120:]}); retrying once "
            f"({SMOKE_RETRIES} retr(y/ies) configured)"
        )
        result = _run(["node", str(script)], backend, SMOKE_TIMEOUT_S)
    if result["code"] != 0:
        return {"status": "errored", "code": result["code"], "output_tail": result["output_tail"]}
    try:
        parsed = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {
            "status": "errored",
            "detail": f"unparseable smoke output: {exc}",
            "output_tail": result["output_tail"],
        }
    checks: dict[str, Any] = {}
    for item in parsed:
        key = item.get("check") or "?"
        checks[key] = item
    return {"status": "ran", "checks": checks, "raw": parsed}


def _collect_level1_blockers(
    level1: list[dict[str, Any]] | None, blocking: list[str], advisory: list[str]
) -> None:
    """A red committed test suite is blocking: it is the only executable check of the
    contract the agent itself wrote (an app once shipped with level1 frontend status
    "failed" code 1 while the gate reported zero blocking defects).
    Any non-green completion is equally blocking — a suite that timed out, was killed
    or could not spawn never proved the assertions pass, and the gate must not ship an
    app whose own suite did not finish GREEN (exit 0) from a freshly-seeded state.
    "skipped"/"not-verifiable" (no test script, no node_modules) stay non-blocking."""
    for t in level1 or []:
        status = t.get("status")
        if status in (None, "", "skipped", "not-verifiable", "passed"):
            continue
        tail = re.sub(r"\x1b\[[0-9;]*m", "", str(t.get("output_tail") or ""))
        failed_files = sorted(set(re.findall(r"tests/[\w./@-]+\.(?:test|spec)\.[jt]sx?", tail)))
        label = str(t.get("label") or "?")
        files = f"; failing files: {', '.join(failed_files[:6])}" if failed_files else ""
        if status == "failed":
            blocking.append(
                f"own test suite {label!r} exited {t.get('code')} (status {status!r}){files} "
                "— fix the implementation to satisfy the committed assertions; do NOT delete, "
                "skip or weaken the tests to make them pass"
            )
        else:
            blocking.append(
                f"own test suite {label!r} did not complete (status {status!r}, "
                f"code {t.get('code')}){files} — the gate ships only an app whose committed "
                "suite finished GREEN (exit 0) from a freshly-seeded state; a timed-out, "
                "killed or unspawnable suite is NOT a pass, and the platform runs the same "
                "assertions"
            )


def _compute_gate(
    smoke: dict[str, Any],
    titles: list[str],
    output_dir: Path,
    seed_violations: list[str] | None = None,
    level1: list[dict[str, Any]] | None = None,
    contract_coverage: dict[str, Any] | None = None,
    db_violations: list[str] | None = None,
) -> dict[str, Any]:
    """Hard-gate verdict (WS-1). Blocking defects trigger one repair round."""
    advisory: list[str] = []
    # S3': exact contract names that never appear in the delivered source. Kept
    # advisory on purpose - a fixture name can legitimately live in seed data or
    # be produced by the backend, so this is a checklist, not a verdict.
    if contract_coverage and contract_coverage.get("status") == "ran":
        advisory.append(
            "contract-index (advisory): "
            f"{contract_coverage.get('found', 0)}/{contract_coverage.get('names', 0)} "
            "quoted requirement names appear verbatim in the delivered source"
        )
        for item in contract_coverage.get("missing") or []:
            advisory.append(
                f"contract-index: {item['name']!r} (from {item['source']}) not found in source"
            )
    if smoke.get("status") != "ran":
        # The self-test suites and the shipped-database reconcile need no browser,
        # so both must still block even when the smoke layer is unavailable.
        browser_free: list[str] = []
        _collect_level1_blockers(level1, browser_free, advisory)
        browser_free.extend(db_violations or [])
        if smoke.get("status") == "errored":
            # A smoke that errored (browser available, run died/timed out — already
            # retried once inside _browser_smoke) must NOT silently ship: the app could
            # not be browser-verified at all. A hang is often an app defect (infinite
            # loop, hanging fetch, overlay). One wasted repair round on an environment
            # flake is acceptable; an unverified silent ship is not (a killed
            # smoke with zero results used to produce a "not-verifiable" gate
            # and ship defects without any repair).
            reason = smoke.get("detail") or f"smoke exited code {smoke.get('code')}"
            browser_free.append(
                "browser smoke errored twice: the app could NOT be verified in a browser "
                f"({str(reason)[:160]}). Check that no page hangs (infinite loops, hanging "
                "fetches, overlays) — an app whose pages never settle is not shippable"
            )
            return {
                "status": "partial (smoke errored)",
                "passed": False,
                "reason": reason,
                "blocking": browser_free,
                "advisory": advisory,
            }
        if browser_free:
            return {
                "status": "partial (smoke unavailable)",
                "passed": False,
                "reason": smoke.get("detail") or smoke.get("status", "smoke unavailable"),
                "blocking": browser_free,
                "advisory": advisory,
            }
        return {
            "status": "not-verifiable",
            "passed": None,
            "reason": smoke.get("detail") or smoke.get("status", "smoke unavailable"),
            "blocking": [],
            "advisory": advisory,
        }

    checks = smoke.get("checks", {})
    blocking: list[str] = []

    if checks.get("partial"):
        advisory.append(f"smoke partial: {str(checks['partial'].get('reason', 'deadline exceeded'))}")

    # The agent's own committed tests are the only executable check of the contract it
    # wrote, so a red suite must never ship.
    _collect_level1_blockers(level1, blocking, advisory)

    page_load = checks.get("page-load", {})
    if not page_load.get("ok"):
        blocking.append(f"homepage failed to load: {str(page_load.get('error', ''))[:200]}")

    # O3: the smoke must navigate beyond the homepage. A smoke that only ever saw the
    # home page proves nothing about product surfaces (lists, editors, post-login
    # pages) — a run once shipped with pages-visited == 1 while 54/60 fixtures were
    # never visible and a full e2e suite was still red.
    pv = checks.get("pages-visited") or {}
    if isinstance(pv, dict) and isinstance(pv.get("count"), int) and pv["count"] <= 1:
        blocking.append(
            "browser smoke never left the homepage (pages-visited == 1) — no product "
            "surface (lists, editor, post-login pages) was browser-verified; the smoke "
            "must follow at least one navigation entry beyond the home page"
        )

    forms = checks.get("forms", {})
    for f in forms.get("unnamed") or []:
        sample = f.get("sample") or "<no sample>"
        blocking.append(
            f"data-entry <form> without an accessible name (sample field: {sample!r}); "
            'add aria-label="... form" (e.g. aria-label="Login form")'
        )

    logo = checks.get("logo", {})
    if not logo.get("found"):
        blocking.append(f"brand logo not locatable: {str(logo.get('detail', ''))[:200]}")

    smoke_titles = titles[:MAX_SMOKE_TITLES]
    fx = checks.get("fixtures", {})
    fx_detail = fx.get("detail") or []
    if (
        smoke_titles
        and fx.get("total", 0) > 0
        and fx.get("found", 0) == 0
        and not any(d.get("textOnly") for d in fx_detail)
    ):
        blocking.append(
            f"none of {fx.get('total')} fixture entit(y/ies) visible on any visited page "
            "(homepage + primary navigation); check backend seed data and list rendering"
        )
    if fx and smoke_titles:
        missing = [
            d.get("title")
            for d in fx_detail
            if not d.get("ok") and not d.get("textOnly") and not d.get("firstPage")
        ]
        if missing:
            advisory.append(f"fixture entit(y/ies) not visible: {missing}")

    # O-A: fixture text exists on a visited page but NO candidate role
    # (heading/button/link/...) is visible — under generic accessibility
    # semantics, a name users interact with must answer a role query with a
    # short window, so a name rendered as plain text / hidden / wrong role
    # fails even though the old text-substring smoke passed.
    if fx and smoke_titles:
        for d in fx_detail:
            if d.get("textOnly") and not d.get("ok"):
                roles = ", ".join(d.get("roles") or []) or "none"
                blocking.append(
                    f"fixture {d.get('title')!r} text exists on the page but NO candidate "
                    f"role ({roles}) is visible — named entities are resolved by role "
                    "(heading/button/link/...) within a short window, so a name rendered "
                    "as plain text / hidden / wrong role fails interaction. Render the name "
                    "as an element that answers a candidate role query"
                )

    # O-A: reachable but only AFTER the ~500ms resolution window — advisory:
    # cold-start render variance, product should render sooner.
    if fx and smoke_titles:
        slow = [
            d.get("title")
            for d in fx_detail
            if d.get("ok") and d.get("slow") and d.get("slow") > FIXTURE_WINDOW_MS
        ]
        if slow:
            advisory.append(
                f"fixture entit(y/ies) became visible only AFTER the ~"
                f"{FIXTURE_WINDOW_MS}ms resolution window (slow render): {slow} — "
                "cold-start environments render slowly; prefer parallel fetches / skeleton placeholders"
            )

    # OPT-1: a named fixture that is ONLY reachable after pagination is a
    # blocking seed/listing-order defect — list items must be locatable on the
    # FIRST page of a listing (an entity that only appears after pagination is
    # unreachable from the initial listing even when the smoke finds every
    # other fixture). "Visible without pagination" means on the first render of
    # at least one visited page.
    if fx and smoke_titles:
        paged = [
            d for d in (fx.get("detail") or [])
            if d.get("firstPage") and d.get("firstPage") > 1
        ]
        for d in paged:
            blocking.append(
                f"fixture {d.get('title')!r} is NOT visible without pagination "
                f"(first seen on page {d.get('firstPage')} of {d.get('pageUrl') or '?'}) — "
                "list items must be locatable on the first page of a listing; "
                "adjust the seed order / page size so the entity renders "
                "on page 1 of its listing"
            )

    # Word-root collisions on toolbar/group/aside wrappers stay advisory: an exact
    # anchored getByRole name lookup cannot be hijacked by them.
    coll = checks.get("name-collisions", {})
    for c in coll.get("detail") or []:
        advisory.append(
            f"name collision (advisory): <{c.get('tag')} role={c.get('role')}> aria-label "
            f"{c.get('label')!r} contains field word root '{c.get('root')}' — consider renaming "
            "(SKILL rule D2)"
        )

    # Label shadowing IS blocking for the classes the agent can fix. The standard
    # label resolver tries getByLabel(/<full label>/i) first and takes the first
    # hit in document order, so any element whose accessible name contains a
    # field's label phrase and precedes the field wins the lookup and fill()
    # targets a non-editable node. The scan covers ALL named elements in
    # global DOM order (a/button included) and reports per severity:
    #   - container (non-editable wrapper) with a phrase >= 4 chars -> blocking; rename
    #     so the name shares no phrase with any field it precedes (rule D2/E1);
    #   - field-collision (the phrase resolves to a DIFFERENT editable control) ->
    #     blocking; two fields share a label phrase, the resolver fills the wrong one;
    #   - chrome-link/button or short phrases (< 4 chars, e.g. "To") -> advisory: often
    #     requirement-mandated navigation/identity elements (header "My account",
    #     sidebar "Addresses", a logged-in full name) that cannot always be renamed.
    shadow = checks.get("label-shadowing", {})
    for x in shadow.get("detail") or []:
        kind = x.get("kind") or "container"
        word = x.get("word") or ""
        base = (
            f"label shadowed by <{x.get('container')} name={x.get('containerName')!r}>: "
            f"getByLabel(/{word}/i) resolves to it before the <{x.get('field')} "
            f"label={x.get('fieldName')!r}>"
        )
        if kind == "field-collision":
            blocking.append(
                base + " — two editable controls share the label phrase; rename one so the "
                "resolver targets the intended field (SKILL rule D2/E1)"
            )
        elif kind == "container" and len(word) >= 4:
            blocking.append(
                base + " — fill() fails with 'Element is not an <input>'. Rename the element "
                "so its accessible name shares NO label phrase with the field it precedes "
                "(SKILL rule D2/E1)"
            )
        else:
            advisory.append(
                base + " — renaming may not be possible for requirement-mandated navigation/"
                "identity elements; if the element is not required, rename it (SKILL rule D2/E1)"
            )

    # Accessible-name shadowing of page actions by global chrome (generic rule):
    # a global getByRole('button', {name:/X/i}) query keeps the first hit in
    # document order, so a chrome BUTTON whose accessible name equals a page
    # action's name steals the click and the page action never fires. Exact
    # collisions are blocking under the generic naming rule when a semantic
    # landmark anchors the chrome/action split; the fix is to make the page
    # action win document order or give it its own requirement qualifier, never
    # to demote a nav/aside/complementary entry (which keeps its button role).
    ns = checks.get("name-shadow", {})
    for x in ns.get("detail") or []:
        cname = str(x.get("chromeName") or "")
        base = (
            f"global chrome button name={cname!r} shadows the page's "
            f"<{x.get('mainTag')} name={x.get('mainName')!r}> — a global "
            "getByRole('button') query would hit the chrome control first (DOM order) "
            "and the page action would never fire"
        )
        if x.get("kind") != "exact":
            advisory.append(base + " — substring overlap only, roles left unchanged")
        elif not x.get("landmarksPresent"):
            # No semantic landmark to anchor the chrome/action split: the probe
            # cannot tell chrome from content, so it must not order a role change.
            advisory.append(
                base + " — the page exposes no landmark, so chrome vs page action is not "
                "reliably distinguishable; treated as advisory (roles left unchanged)"
            )
        else:
            blocking.append(
                base + " — generic accessible-name conflict rule: make the page action "
                "win document order (render it before the nav/header entry) or give the "
                "page action its own requirement qualifier; never demote a "
                "nav/aside/complementary entry (SKILL E.5)"
            )

    # The mirror probe (see s.landmarkRoles in SMOKE_TEMPLATE): a demotion that
    # already happened is a blocking defect, so one repair round can no longer
    # ship a landmark nav entry users cannot click as a button.
    for x in (checks.get("landmark-roles", {}) or {}).get("detail") or []:
        nm = str(x.get("name") or "")
        blocking.append(
            f"landmark nav entry name={nm!r} answers only as a link "
            f"({x.get('links')} <a>, {x.get('buttons')} button) inside nav/aside/"
            "complementary — navigation entries keep the button role (CONTRACT rule 7); "
            "the name-shadow E.5 exception never applies to a landmark entry"
        )

    # Strict-locator semantic scan (P6): a decorated accessible name can never
    # match an exact anchored getByRole name (blocking); a hover-revealed control
    # hidden with visibility/display leaves the accessibility tree (blocking); a
    # trailing " menu" name is usually a decoration but can be legitimate (advisory).
    sem = checks.get("semantic", {})
    for it in sem.get("detail") or []:
        kind = it.get("kind")
        name = it.get("name") or ""
        if kind in ("ellipsis-name", "colon-suffix", "dash-suffix", "ellipsis-text"):
            blocking.append(
                f"interactive control with decorated accessible name <{it.get('tag')} {kind}>: "
                f"{name!r} — accessible name must be the exact scenario-quoted name, no "
                "ellipsis / ': detail' / '— detail' suffix (see arcbench-frontend-spec SKILL rule B)"
            )
        elif kind == "menu-suffix":
            advisory.append(
                f"control whose aria-label ends with ' menu' (may miss an exact 'X' anchor): {name!r}"
            )
    hh = checks.get("hover-hidden", {})
    for cls in hh.get("detail") or []:
        blocking.append(
            f"hover-revealed control hidden via visibility/display (out of the accessibility tree): "
            f"class {cls!r} — reveal with opacity only (see arcbench-frontend-spec SKILL rule A)"
        )

    dlg = checks.get("dialogs", {})
    suffixed = [label for label in (dlg.get("labels") or []) if ":" in label]
    if suffixed:
        advisory.append(f"dialogs with suffixed names (may break exact anchors): {suffixed}")

    login = checks.get("login", {})
    if login.get("attempted"):
        state = "ok" if login.get("ok") else "failed"
        advisory.append(f"login attempt {state}: {str(login.get('detail', ''))[:120]}")
    hd = checks.get("homepage-headings", {})
    if hd.get("headings"):
        advisory.append(f"homepage headings after login: {hd['headings'][:12]}")

    miss_seed = [title for title in titles if not _seed_evidence(output_dir, titles).get(title)]
    if miss_seed:
        advisory.append(f"fixture entit(y/ies) missing backend seed evidence: {miss_seed}")

    for violation in seed_violations or []:
        blocking.append(violation)

    # K2: the seed SOURCE can be perfect while the shipped database FILE is not -
    # a GIVEN fixture that never reached the file the app serves is invisible to
    # every other check here, so this is the only line that catches it.
    for violation in db_violations or []:
        blocking.append(violation)

    return {
        "status": "checked",
        "passed": len(blocking) == 0,
        "blocking": blocking,
        "advisory": advisory,
    }


def _load_contract_index_module():
    """Import the route's contract_index module (Codex/contract_index.py)."""
    route_root = Path(__file__).resolve().parents[1]
    if str(route_root) not in sys.path:
        sys.path.insert(0, str(route_root))
    import contract_index  # noqa: PLC0415 - optional, route-local module

    return contract_index


def _contract_coverage(requirements_dir: Path, output_dir: Path) -> dict[str, Any]:
    """S3': exact contract names from the requirement tree vs the delivered source.

    Static read only: no spec generation, no artifact writes, never blocking.
    """
    try:
        contract_index = _load_contract_index_module()
        index = contract_index.build_index(requirements_dir)
    except Exception as exc:  # noqa: BLE001 - advisory only, must never be fatal
        return {"status": "skipped", "detail": f"{type(exc).__name__}: {exc}"}

    names: list[dict[str, str]] = []
    for leaf in index.get("leaves") or []:
        origin = f"{leaf.get('id', '')} {leaf.get('name', '')}".strip()
        for control in leaf.get("controls") or []:
            names.append({"name": control.get("name", ""), "role": control.get("role", ""), "source": origin})
        for extra in leaf.get("names") or []:
            names.append({"name": extra, "role": "", "source": origin})
    names = [item for item in names if item["name"]]
    if not names:
        return {"status": "skipped", "detail": "the requirement tree yielded no quoted names"}

    haystack = "\n".join(
        path.read_text(encoding="utf-8", errors="replace") for path in _iter_source_files(output_dir)
    ).lower()

    missing = [item for item in names if item["name"].lower() not in haystack]
    return {
        "status": "ran",
        "names": len(names),
        "found": len(names) - len(missing),
        "missing_count": len(missing),
        "missing": missing[:MAX_CONTRACT_MISSING],
    }


def verify_application(
    requirements_dir: Path,
    output_dir: Path,
    *,
    contract_probes: bool = True,
    install_browser: bool = False,
    reconcile_database: bool = True,
) -> dict[str, Any]:
    """Run the two verification layers plus the hard gate. Always returns a report dict.

    The switches are passed in by Codex/main.py from its FEATURE_FLAGS table;
    the defaults here only apply when someone calls the verifier standalone.
    """
    report: dict[str, Any] = {"level1": [], "level2": {}}
    started = time.monotonic()

    frontend = output_dir / "frontend"
    backend = output_dir / "backend"

    # --- Level 1: the agent's own committed tests -------------------------
    for project, label in ((frontend, "frontend"), (backend, "backend")):
        report["level1"].append(_level1_tests(project, label))

    # --- Level 2: data-contract smoke ------------------------------------
    titles = _extract_fixture_titles(requirements_dir)
    report["level2"]["fixture_titles"] = titles
    report["level2"]["seed_evidence"] = _seed_evidence(output_dir, titles)
    report["level2"]["credentials_source"] = _extract_credentials(backend)
    seed_state_clauses = _extract_seed_state_clauses(requirements_dir)
    report["level2"]["seed_state_clauses"] = seed_state_clauses
    # O-B: seed initial-state expectations derived from the requirement's
    # GIVEN/WHEN steps (no test files are read or probed). Merged into the same
    # violation check so a pre-trashed/pre-favorited seed blocks. The fixed
    # empty values below are the compliance baseline for this report.
    report["level2"]["test_flow_source"] = None
    report["level2"]["test_flow_clauses"] = []
    # Accessible-name conflict evidence layer (generic rule): global naming must
    # not duplicate page-action names. No test files are read; the empty shape
    # is the compliance baseline.
    query_shapes = {"source": None, "landmark_button": [], "global": []}
    report["level2"]["query_shapes"] = {
        "source": query_shapes.get("source"),
        "landmark_button_count": len(query_shapes.get("landmark_button") or []),
        "global_count": len(query_shapes.get("global") or []),
        "landmark_button": (query_shapes.get("landmark_button") or [])[:40],
        "global": (query_shapes.get("global") or [])[:40],
    }
    report["level2"]["seed_state_violations"] = _seed_state_violations(
        output_dir, seed_state_clauses
    )
    db_reconcile = _database_reconcile(output_dir) if reconcile_database else {"status": "disabled"}
    report["level2"]["database_reconcile"] = db_reconcile
    if db_reconcile.get("status") == "ran":
        _log(
            f"db-reconcile: {db_reconcile.get('missing_count', 0)} declared value(s) missing "
            f"from {db_reconcile.get('database')}, "
            f"{len(db_reconcile.get('state_drift') or [])} state drift(s)"
        )
    elif db_reconcile.get("status") not in ("disabled", "skipped"):
        _log(f"db-reconcile {db_reconcile.get('status')}: {db_reconcile.get('detail', '')}")
    if contract_probes:
        report["level2"]["contract_coverage"] = _contract_coverage(requirements_dir, output_dir)

    report["level2"]["build"] = _ensure_frontend_build(frontend)

    proc: subprocess.Popen | None = None
    logfile: Path | None = None
    base_url = ""
    smoke: dict[str, Any] = {"status": "skipped", "detail": "backend not started"}
    try:
        if not (backend / "package.json").is_file():
            report["level2"]["server"] = {"status": "skipped", "detail": "no backend package.json"}
        else:
            proc, logfile, base_url = _start_backend(backend, VERIFY_PORT)
            ready, detail = _wait_ready(base_url, SERVER_TIMEOUT_S)
            report["level2"]["server"] = {
                "status": "ready" if ready else "not-ready",
                "port": VERIFY_PORT,
                "detail": detail,
            }
            if ready:
                credentials = _extract_credentials(backend)
                smoke = _browser_smoke(
                    backend,
                    base_url,
                    titles,
                    credentials,
                    scoped_button_names=[],
                    install_browser=install_browser,
                )
                report["level2"]["browser_smoke"] = smoke
            else:
                report["level2"]["browser_smoke"] = {
                    "status": "skipped",
                    "detail": "backend not ready",
                }
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if logfile is not None and logfile.is_file() and logfile.stat().st_size:
            report["level2"]["server_log_tail"] = logfile.read_text(encoding="utf-8", errors="replace").strip()[-800:]

    # --- Hard gate ---------------------------------------------------------
    report["gate"] = _compute_gate(
        smoke,
        titles,
        output_dir,
        seed_violations=report["level2"].get("seed_state_violations") or [],
        level1=report.get("level1") or [],
        contract_coverage=report["level2"].get("contract_coverage"),
        db_violations=db_reconcile.get("violations") or [],
    )
    gate = report["gate"]
    if gate.get("passed") is True:
        _log(f"gate PASSED ({len(gate.get('blocking') or [])} blocking defects)")
    elif gate.get("passed") is False:
        _log(f"gate FAILED with {len(gate.get('blocking') or [])} blocking defect(s):")
        for line in gate.get("blocking") or []:
            _log(f"  - {line}")
    else:
        _log(f"gate not-verifiable: {gate.get('reason', '')}")

    report["elapsed_s"] = round(time.monotonic() - started, 1)
    _log(f"done in {report['elapsed_s']}s")
    return report
