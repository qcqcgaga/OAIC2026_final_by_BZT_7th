"""S5: requirement tree -> assertable contract index (deterministic, 0 token).

Why this exists
---------------
Standard accessibility semantics locate elements by role + accessible name, and a
measurement across past requirement sets shows 88-100% of
those names (97-100% on the ``-t`` tasks) appear **verbatim in requirements.yaml**.
So the contract surface can be transcribed from the requirement tree instead of being
guessed by the model. This module performs that transcription once, deterministically,
before any module is implemented, and writes it into the artifact:

    <output-dir>/.codex/assertable-contracts.md    (human/agent readable)
    <output-dir>/.codex/assertable-contracts.json  (machine readable; S2/S3 input)

Design constraints (lossless degradation)
-----------------------------------------
* Gated by the caller: ``main.py`` only builds/writes the index when
  ``FEATURE_FLAGS["contract_index"]`` is on (currently on). This module reads no
  environment variable, so there is exactly one place that decides.
* This module never touches the model context by itself - it only writes files, so
  enabling it costs 0 tokens. (Feeding the index back into a *prompt* is S2's job and
  will be measured separately.)
* Nothing here replaces or weakens ``verify/verify.py``: the index is an additional
  input for the gate (S2/S3), never a substitute.
* Everything is capped and exception-safe: a malformed requirement tree degrades to a
  regex-based extraction pass instead of failing the run.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterator

try:  # pyyaml ships in the runner image; the fallback keeps the CLI usable anyway.
    import yaml
except Exception:  # noqa: BLE001
    yaml = None  # type: ignore[assignment]

INDEX_DIR = ".codex"
INDEX_MD = "assertable-contracts.md"
INDEX_JSON = "assertable-contracts.json"

MAX_NAMES_PER_LEAF = 40
MAX_FLOW_STEPS = 40

# --------------------------------------------------------------------------- #
# Extraction patterns
# --------------------------------------------------------------------------- #

CONTROL_ROLES = (
    "region|button|dialog|textbox|searchbox|article|form|heading|tab|menuitem|"
    "checkbox|radio|link|option|table|row|list|listitem|cell|combobox|switch|"
    "group|meter|alert|status|navigation|banner|complementary|main|toolbar|tablist"
)

# "region named `Notes workspace`" / "button named `Take a note`" - the highest
# confidence class: the requirement states both the role and the accessible name.
ROLE_PHRASE_RE = re.compile(
    rf"\b(?P<role>{CONTROL_ROLES})\b[^.\n`]{{0,40}}?\b(?:named|labelled|labeled)\s+"
    r"[`'\"\u201c\u2018](?P<name>[^`'\"\u201d\u2019\n]{1,80})[`'\"\u201d\u2019]",
    re.IGNORECASE,
)

# Anything the requirement quotes verbatim: `Take a note`, "Weekend plan", ...
BACKTICK_RE = re.compile(r"`([^`\n]{1,80})`")
QUOTED_RE = re.compile(r"[\"\u201c\u2018']([^\"\u201d\u2019'\n]{1,80})[\"\u201d\u2019']")

ARIA_RE = re.compile(r"\baria-[a-z]+(?:=[A-Za-z0-9_-]+)?", re.IGNORECASE)
EXCLUSIVE_RE = re.compile(r"\b(exactly one|only one|unique|uniquely)\b", re.IGNORECASE)
SEED_CLAUSE_RE = re.compile(r"Seed\s+data\s*:\s*(.+)", re.IGNORECASE)

# State qualifiers that turn a "seed data" clause into a state contract (O2 / O-B).
SEED_QUALIFIERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("not_pinned", re.compile(r"\bnot\s+pinned\b|\bunpinned\b|\bthat is not pinned\b", re.I)),
    ("pinned", re.compile(r"\bpinned\b(?!\s+(?:false|:)\s*)", re.I)),
    ("archived", re.compile(r"\barchived\b", re.I)),
    ("active", re.compile(r"\bactive\b|\bnot archived\b", re.I)),
    ("trashed", re.compile(r"\btrashed\b|\bin the trash\b", re.I)),
    ("favorited", re.compile(r"\bfavorited?\b|\bfavourite\b|\bfavorite\b", re.I)),
    ("white_background", re.compile(r"\bwhite background\b", re.I)),
)

ARCHETYPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("auth", re.compile(r"\b(log[ -]?in|sign[ -]?in|log[ -]?out|sign[ -]?out|regist\w*|sign[ -]?up|password|credential|verification)\b", re.I)),
    ("delete", re.compile(r"\b(delet\w*|remov\w*|discard\w*|trash\w*|clear)\b", re.I)),
    ("validation", re.compile(r"\b(valid\w*|invalid\w*|required|error\w*|empty|weak|mismatch\w*|format\w*|exception\w*|insufficient|block\w*)\b", re.I)),
    ("toggle-state", re.compile(r"\b(archiv\w*|pin\w*|pinned|favorit\w*|star\w*|follow\w*|toggle\w*|vot\w*|like|enable\w*|disable\w*|switch\w*|preference\w*|setting\w*|status|mark\w*)\b", re.I)),
    ("edit", re.compile(r"\b(edit\w*|updat\w*|modif\w*|renam\w*|chang\w*|manag\w*)\b", re.I)),
    ("search-filter-sort", re.compile(r"\b(search\w*|filter\w*|sort\w*|quer\w*)\b", re.I)),
    ("create", re.compile(r"\b(creat\w*|add\w*|new|writ\w*|post\w*|submit\w*|upload\w*|compos\w*|sav\w*|book\w*|pay\w*|checkout|order\w*|fill\w*)\b", re.I)),
    ("list-detail-nav", re.compile(r"\b(view\w*|list\w*|brows\w*|navigat\w*|open\w*|enter\w*|homepage|home|detail\w*|display\w*|show\w*|pag\w*|grid|item\w*|layout|director\w*|histor\w*)\b", re.I)),
)

_ID_RE = re.compile(r"^\s*-\s*id:\s*(\S+)", re.M)
_TYPE_RE = re.compile(r"^\s*type:\s*(\w+)", re.M)
_NAME_RE = re.compile(r"^\s{0,40}name:\s*(.+?)\s*$", re.M)
_DESC_RE = re.compile(r"^\s{0,40}description:\s*(.+?)\s*$", re.M)
_CONTENT_RE = re.compile(r"^\s*content:\s*(.+?)\s*$", re.M)


# --------------------------------------------------------------------------- #
# Requirement loading (tolerant: B1-style broken yaml must not kill the run)
# --------------------------------------------------------------------------- #


def _regex_chunks(raw: str) -> list[dict[str, Any]]:
    """Recover node chunks from a requirement file that yaml.safe_load rejects."""
    lines = raw.splitlines()
    starts = [i for i, line in enumerate(lines) if _ID_RE.match(line)]
    chunks: list[dict[str, Any]] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        body = "\n".join(lines[start:end])
        node_id = _ID_RE.search(body).group(1)
        name = _NAME_RE.search(body)
        node_type = _TYPE_RE.search(body)
        contents = _CONTENT_RE.findall(body)
        chunks.append(
            {
                "id": node_id,
                "name": (name.group(1).strip("'\"") if name else node_id),
                "type": (node_type.group(1) if node_type else ""),
                # Keep the whole chunk: folded/literal scalars lose their layout in a
                # regex pass, so quoting the full text is what preserves the names.
                "description": body.strip()[:4000],
                "scenarios": [{"steps": [{"content": c} for c in contents]}],
            }
        )
    return chunks


def load_requirement_tree(requirements_dir: Path) -> tuple[dict[str, Any], bool]:
    """Return ``(tree, yaml_ok)``. Never raises for a malformed requirement file."""
    path = Path(requirements_dir) / "requirements" / "requirements.yaml"
    if not path.is_file():
        path = Path(requirements_dir) / "requirements.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"requirements.yaml not found under {requirements_dir}")
    raw = path.read_text(encoding="utf-8", errors="replace")
    if yaml is not None:
        try:
            loaded = yaml.safe_load(raw)
            if isinstance(loaded, dict) and (loaded.get("children") or loaded.get("id")):
                return loaded, True
        except Exception:  # noqa: BLE001 - degraded mode below
            pass
    return {"id": "ROOT", "name": "ROOT", "type": "FOLDER", "children": _regex_chunks(raw)}, False


def iter_atomic_leaves(tree: dict[str, Any]) -> Iterator[dict[str, Any]]:
    stack = [tree]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        children = [c for c in (node.get("children") or []) if isinstance(c, dict)]
        node_type = str(node.get("type", "")).upper()
        if node_type == "ATOMIC":
            if node.get("id"):
                yield node
            continue
        if not children and node_type not in ("FOLDER",) and node.get("id") and node.get("description"):
            # Tolerate trees that omit `type` on leaves; never treat a named FOLDER
            # as a leaf (the B1 fallback pass has no child links at all).
            yield node
            continue
        stack.extend(reversed(children))


# --------------------------------------------------------------------------- #
# Contract extraction
# --------------------------------------------------------------------------- #


def _looks_like_name(value: str) -> bool:
    text = value.strip()
    if not (2 <= len(text) <= 60):
        return False
    if not re.search(r"[A-Za-z\u4e00-\u9fff]", text):
        return False
    if text.lower().startswith(("http", "./", "data:", "public/", "/", "\\")):
        return False
    if re.fullmatch(r"[a-z_]+", text):  # yaml keys / snake_case identifiers
        return False
    return True


def leaf_texts(node: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    if node.get("name"):
        parts.append(str(node["name"]))
    if node.get("description"):
        parts.append(str(node["description"]))
    for scenario in node.get("scenarios") or []:
        if not isinstance(scenario, dict):
            continue
        if scenario.get("name"):
            parts.append(str(scenario["name"]))
        for step in scenario.get("steps") or []:
            if isinstance(step, dict) and step.get("content"):
                parts.append(str(step["content"]))
    return parts


def classify_archetype(name: str, body: str) -> str:
    scores = {
        label: 3 * len(rx.findall(name or "")) + len(rx.findall(body or ""))
        for label, rx in ARCHETYPES
    }
    best = max(scores.values())
    if best == 0:
        return "unclassified"
    return next(label for label, rx in ARCHETYPES if scores[label] == best)


def seed_contracts(text: str) -> list[dict[str, Any]]:
    """Turn every "Seed data: ..." clause into an entity + state-qualifier contract."""
    clauses: list[dict[str, Any]] = []
    for match in SEED_CLAUSE_RE.finditer(text):
        clause = match.group(1)
        entities: list[str] = []
        for quoted in list(BACKTICK_RE.findall(clause)) + list(QUOTED_RE.findall(clause)):
            if _looks_like_name(quoted) and quoted not in entities:
                entities.append(quoted)
        qualifiers = [label for label, rx in SEED_QUALIFIERS if rx.search(clause)]
        clauses.append(
            {
                "clause": clause.strip()[:400],
                "entities": entities[:MAX_NAMES_PER_LEAF],
                "qualifiers": qualifiers,
            }
        )
    return clauses


def extract_leaf_contract(node: dict[str, Any], *, advisory: bool = False) -> dict[str, Any]:
    texts = leaf_texts(node)
    body = "\n".join(texts)

    controls: list[dict[str, Any]] = []
    seen_controls: set[tuple[str, str]] = set()
    for match in ROLE_PHRASE_RE.finditer(body):
        role = match.group("role").lower()
        name = match.group("name").strip()
        if not _looks_like_name(name):
            continue
        key = (role, name.lower())
        if key in seen_controls:
            continue
        seen_controls.add(key)
        controls.append({"role": role, "name": name, "source": "role-phrase"})

    exact_names: list[str] = []
    for quoted in list(BACKTICK_RE.findall(body)) + list(QUOTED_RE.findall(body)):
        name = quoted.strip()
        if _looks_like_name(name) and name.lower() not in {n.lower() for n in exact_names}:
            exact_names.append(name)

    role_names = {c["name"].lower() for c in controls}
    unbound_names = [n for n in exact_names if n.lower() not in role_names]

    advisory_names: list[str] = []
    if advisory:
        for clause in seed_contracts(body):
            for entity in clause["entities"]:
                if entity.lower() not in role_names and entity.lower() not in {n.lower() for n in exact_names}:
                    if entity not in advisory_names:
                        advisory_names.append(entity)

    flow: list[dict[str, Any]] = []
    for scenario in node.get("scenarios") or []:
        if not isinstance(scenario, dict):
            continue
        for step in scenario.get("steps") or []:
            if not isinstance(step, dict):
                continue
            content = str(step.get("content") or "").strip()
            if not content:
                continue
            names = [
                n
                for n in list(BACKTICK_RE.findall(content)) + list(QUOTED_RE.findall(content))
                if _looks_like_name(n)
            ]
            flow.append(
                {
                    "keyword": str(step.get("keyword") or "").upper(),
                    "text": content[:300],
                    "names": names[:MAX_NAMES_PER_LEAF],
                }
            )
            if len(flow) >= MAX_FLOW_STEPS:
                break

    exclusive = sorted({c["name"] for c in controls if EXCLUSIVE_RE.search(body)}) if controls else []

    return {
        "id": str(node.get("id") or ""),
        "name": str(node.get("name") or ""),
        "archetype": classify_archetype(str(node.get("name") or ""), body),
        "controls": controls[:MAX_NAMES_PER_LEAF],
        "names": unbound_names[:MAX_NAMES_PER_LEAF],
        "advisory_names": advisory_names[:MAX_NAMES_PER_LEAF],
        "aria": sorted(set(ARIA_RE.findall(body))),
        "exclusive": exclusive,
        "seed": seed_contracts(body),
        "flow": flow,
    }


def build_index(requirements_dir: Path, *, advisory: bool = False) -> dict[str, Any]:
    tree, yaml_ok = load_requirement_tree(Path(requirements_dir))
    leaves = [extract_leaf_contract(node, advisory=advisory) for node in iter_atomic_leaves(tree)]
    index: dict[str, Any] = {
        "source": str(Path(requirements_dir).resolve()),
        "yaml_ok": yaml_ok,
        "advisory": advisory,
        "summary": {
            "leaves": len(leaves),
            "controls": sum(len(l["controls"]) for l in leaves),
            "names": sum(len(l["names"]) + len(l["controls"]) for l in leaves),
            "aria": sum(len(l["aria"]) for l in leaves),
            "exclusive": sum(len(l["exclusive"]) for l in leaves),
            "seed_entities": sum(len(s["entities"]) for l in leaves for s in l["seed"]),
            "flow_steps": sum(len(l["flow"]) for l in leaves),
            "archetypes": {},
        },
        "leaves": leaves,
    }
    counts: dict[str, int] = {}
    for leaf in leaves:
        counts[leaf["archetype"]] = counts.get(leaf["archetype"], 0) + 1
    index["summary"]["archetypes"] = dict(sorted(counts.items(), key=lambda kv: -kv[1]))
    return index


# --------------------------------------------------------------------------- #
# Rendering / writing
# --------------------------------------------------------------------------- #


def render_markdown(index: dict[str, Any]) -> str:
    s = index["summary"]
    lines = [
        "# Assertable contracts (generated - do not hand-edit)",
        "",
        f"Source: `{index['source']}`  |  yaml_ok={index['yaml_ok']}  |  leaves={s['leaves']}",
        f"controls={s['controls']}  names={s['names']}  aria={s['aria']}  "
        f"exclusive={s['exclusive']}  seed_entities={s['seed_entities']}",
        "",
        "Every entry below is transcribed verbatim from requirements.yaml — these are",
        "the names that",
        "must exist exactly as written (no ellipsis, no ': detail' suffix, no decoration).",
        "",
        "Archetypes: " + ", ".join(f"{k}={v}" for k, v in s["archetypes"].items()),
        "",
    ]
    for leaf in index["leaves"]:
        lines.append(f"## {leaf['id']}  {leaf['name']}    [archetype: {leaf['archetype']}]")
        if leaf["controls"]:
            rendered = " | ".join(
                f"{c['role']} \"{c['name']}\"" + (" (exclusive)" if c["name"] in leaf["exclusive"] else "")
                for c in leaf["controls"]
            )
            lines.append(f"- controls: {rendered}")
        if leaf["names"]:
            lines.append("- named (role unbound): " + " | ".join(f'"{n}"' for n in leaf["names"]))
        if leaf["aria"]:
            lines.append("- state: " + ", ".join(leaf["aria"]))
        for clause in leaf["seed"]:
            entities = ", ".join(clause["entities"]) or "(no quoted entity)"
            qualifiers = ",".join(clause["qualifiers"]) or "none"
            lines.append(f"- seed: {entities}  [qualifiers: {qualifiers}]")
        for step in leaf["flow"][:12]:
            names = (" -> " + ", ".join(f'"{n}"' for n in step["names"])) if step["names"] else ""
            lines.append(f"- {step['keyword'] or 'STEP'}: {step['text']}{names}")
        if leaf["advisory_names"]:
            lines.append("- advisory (unverified): " + ", ".join(leaf["advisory_names"]))
        lines.append("")
    return "\n".join(lines)


def write_index(index: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    target = Path(output_dir) / INDEX_DIR
    target.mkdir(parents=True, exist_ok=True)
    md_path = target / INDEX_MD
    json_path = target / INDEX_JSON
    md_path.write_text(render_markdown(index), encoding="utf-8")
    json_path.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    return md_path, json_path

