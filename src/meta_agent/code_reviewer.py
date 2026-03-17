"""
Weekly read-only code review agent.
Reads source files, sends to Claude for structured analysis,
writes findings to logs/code_review_YYYY-MM-DD.json.
Never modifies code — suggestions only.
"""
from __future__ import annotations

import glob
import json
import os
import time
from datetime import date

from src.utils.logger import logger

# File patterns to include (relative to project root)
_INCLUDE_PATTERNS = [
    "main.py",
    "config.py",
    "src/strategies/*.py",
    "src/exchange/*.py",
    "src/portfolio/*.py",
    "src/risk/*.py",
    "src/meta_agent/*.py",
    "src/utils/*.py",
    # Dashboard backend only (not the HTML template)
    "src/dashboard/app.py",
]

# Substrings that mark a file as not worth reviewing
_SKIP_IF_CONTAINS = ["__init__"]

# Per-file char limit; larger files get truncated
_MAX_FILE_CHARS = 10_000

# Total input char cap (controls total token cost)
_MAX_TOTAL_CHARS = 120_000

# Priority files — included in full even if total cap is approaching
_PRIORITY_FILES = {"main.py", "config.py", "src/risk/risk_manager.py"}


def _collect_files() -> dict[str, str]:
    """Return {relative_path: content} for all reviewable source files."""
    files: dict[str, str] = {}
    for pattern in _INCLUDE_PATTERNS:
        for raw_path in glob.glob(pattern):
            rel = raw_path.replace("\\", "/")
            if any(skip in rel for skip in _SKIP_IF_CONTAINS):
                continue
            try:
                with open(raw_path, encoding="utf-8") as f:
                    content = f.read()
            except OSError:
                continue

            # For dashboard app.py, strip the large embedded HTML template
            if "dashboard/app.py" in rel:
                cutoff = content.find("DASHBOARD_HTML")
                if cutoff > 0:
                    content = content[:cutoff] + "\n# [DASHBOARD_HTML omitted — HTML template]\n"

            if len(content) > _MAX_FILE_CHARS:
                content = content[:_MAX_FILE_CHARS] + f"\n# ... [truncated at {_MAX_FILE_CHARS} chars]\n"

            files[rel] = content

    # Enforce total char cap: keep priority files, then fill by file size ascending
    total = sum(len(v) for v in files.values())
    if total > _MAX_TOTAL_CHARS:
        priority = {k: v for k, v in files.items() if any(p in k for p in _PRIORITY_FILES)}
        others = sorted(
            [(k, v) for k, v in files.items() if k not in priority],
            key=lambda kv: len(kv[1]),
        )
        kept = dict(priority)
        remaining = _MAX_TOTAL_CHARS - sum(len(v) for v in kept.values())
        for k, v in others:
            if len(v) <= remaining:
                kept[k] = v
                remaining -= len(v)
        files = kept

    return files


def _build_prompt(files: dict[str, str]) -> str:
    lines = [
        "Review the following Python source files from a live Polymarket/Kalshi "
        "prediction-market arbitrage bot running on Railway.\n"
    ]
    for path in sorted(files):
        lines.append(f"\n### {path}\n```python\n{files[path]}\n```")
    return "\n".join(lines)


async def run_code_review() -> dict:
    """
    Execute a full code review via Claude. Returns the structured findings dict.
    Writes result to logs/code_review_<date>.json.
    Read-only: never modifies any source file.
    """
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.info("Code review: skipped (no ANTHROPIC_API_KEY)")
        return {}

    files = _collect_files()
    if not files:
        logger.warning("Code review: no source files found")
        return {}

    total_k = sum(len(v) for v in files.values()) // 1000
    logger.info(f"Code review: reviewing {len(files)} files ({total_k}k chars)...")

    system = (
        "You are a senior Python engineer performing a READ-ONLY code review of a "
        "Polymarket/Kalshi prediction-market arbitrage bot. "
        "You never modify code — only report findings.\n\n"
        "Review criteria (in priority order):\n"
        "1. Bugs / logic errors that could cause incorrect trades or financial loss\n"
        "2. Async safety issues (shared state, race conditions, event-loop misuse)\n"
        "3. Unhandled exceptions / missing retries on external API calls\n"
        "4. Performance bottlenecks in the hot scanning loop\n"
        "5. Security concerns (credential handling, injection risks)\n"
        "6. Architecture / coupling problems between modules\n"
        "7. Dead code or misleading documentation\n\n"
        "OUTPUT FORMAT — respond with ONLY valid JSON (no markdown fences):\n"
        "{\n"
        '  "summary": "<1–2 sentence overall assessment>",\n'
        '  "health_score": <integer 0–100, higher is better>,\n'
        '  "grade": <"A"|"B"|"C"|"D"|"F">,\n'
        '  "strengths": ["<what the codebase does well>", ...],\n'
        '  "findings": [\n'
        '    {\n'
        '      "severity": <"high"|"medium"|"low"|"info">,\n'
        '      "category": <"bug"|"performance"|"security"|"architecture"|"style">,\n'
        '      "file": "<path>",\n'
        '      "title": "<concise title>",\n'
        '      "description": "<what is wrong and why it matters>",\n'
        '      "suggestion": "<concrete fix>"\n'
        '    }\n'
        '  ]\n'
        "}\n\n"
        "Limit findings to the 12 most impactful. Skip minor style nits. "
        "Focus on correctness, robustness, and trading safety."
    )

    client = anthropic.AsyncAnthropic(api_key=api_key)
    response = await client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": _build_prompt(files)}],
    )
    raw = next((b.text for b in response.content if hasattr(b, "text")), "")

    # Parse JSON — strip accidental code fences if present
    findings_data: dict = {}
    try:
        clean = raw.strip()
        if clean.startswith("```"):
            clean = clean.split("```", 2)[1]
            if clean.startswith("json"):
                clean = clean[4:]
            clean = clean.rsplit("```", 1)[0]
        findings_data = json.loads(clean.strip())
    except json.JSONDecodeError as exc:
        logger.warning(f"Code review: could not parse Claude response as JSON: {exc}")
        findings_data = {
            "summary": "Review completed but JSON parsing failed.",
            "health_score": None,
            "grade": "?",
            "strengths": [],
            "findings": [],
            "raw_response": raw[:3000],
        }

    # Enrich with metadata
    findings = findings_data.get("findings", [])
    findings_data.update({
        "timestamp": time.time(),
        "date": str(date.today()),
        "files_reviewed": sorted(files.keys()),
        "files_count": len(files),
        "total_findings": len(findings),
        "high_findings": sum(1 for f in findings if f.get("severity") == "high"),
        "medium_findings": sum(1 for f in findings if f.get("severity") == "medium"),
        "low_findings": sum(1 for f in findings if f.get("severity") == "low"),
    })

    # Write report
    os.makedirs("logs", exist_ok=True)
    out_path = f"logs/code_review_{date.today()}.json"
    with open(out_path, "w") as f:
        json.dump(findings_data, f, indent=2)

    logger.info(
        f"Code review: grade={findings_data.get('grade','?')} "
        f"score={findings_data.get('health_score','?')} "
        f"| {findings_data['total_findings']} findings "
        f"({findings_data['high_findings']} high, "
        f"{findings_data['medium_findings']} medium, "
        f"{findings_data['low_findings']} low)"
    )
    return findings_data
