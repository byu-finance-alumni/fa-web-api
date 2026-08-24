"""Fold every scanner's output into one report and one Slack line.

The weekly audit runs four tools that disagree about everything — severity
names, JSON shape, what counts as a finding. This normalises them so the Slack
message can be a single sentence and the artifact can be read top to bottom.

⚠️ A MISSING FILE IS NOT ZERO FINDINGS. Each scanner step is
``continue-on-error`` so one flaky download cannot cost us the other three
reports, which means "no JSON on disk" means "this tool did not run", not "this
tool found nothing". Those are reported as UNKNOWN and they degrade the verdict,
because a scan that silently checked less than you think is the failure mode
that makes a weekly audit worthless.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

SEV_ORDER = ["critical", "high", "medium", "low", "info"]


def _load(name: str):
    p = ROOT / name
    if not p.exists() or not p.stat().st_size:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def tripwires() -> tuple[str, list[str], dict[str, int]]:
    data = _load("tripwires.json")
    if data is None:
        return "UNKNOWN", ["Project tripwires did not produce a report."], {}
    counts: dict[str, int] = {}
    lines = []
    for f in data.get("findings", []):
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
        lines.append(
            f"- **{f['severity'].upper()}** `{f['check']}` — {f['where']}\n  - {f['detail']}"
        )
    routes = data.get("routes", "?")
    head = f"{routes} routes inspected."
    actionable = sum(n for sev, n in counts.items() if sev != "info")
    return ("OK" if not actionable else "FINDINGS"), [head, *lines], counts


def pip_audit() -> tuple[str, list[str], int]:
    data = _load("pip-audit.json")
    if data is None:
        return "UNKNOWN", ["Dependency audit did not produce a report."], 0
    deps = data.get("dependencies", data if isinstance(data, list) else [])
    lines, n = [], 0
    for dep in deps:
        for v in dep.get("vulns", []) or []:
            n += 1
            fix = ", ".join(v.get("fix_versions") or []) or "no fix published"
            lines.append(
                f"- `{dep.get('name')}` {dep.get('version')} — {v.get('id')} (fix: {fix})"
            )
    return ("OK" if not n else "FINDINGS"), (lines or ["No known vulnerabilities."]), n


def semgrep() -> tuple[str, list[str], dict[str, int]]:
    data = _load("semgrep.json")
    if data is None:
        return "UNKNOWN", ["Semgrep did not produce a report."], {}
    counts: dict[str, int] = {}
    lines = []
    for r in data.get("results", []):
        sev = (r.get("extra", {}).get("severity") or "INFO").lower()
        sev = {"error": "high", "warning": "medium", "info": "low"}.get(sev, sev)
        counts[sev] = counts.get(sev, 0) + 1
        where = f"{r.get('path')}:{r.get('start', {}).get('line')}"
        msg = (r.get("extra", {}).get("message") or "").strip().splitlines()[0][:200]
        rule = r.get("check_id", "").split(".")[-1]
        lines.append(f"- **{sev.upper()}** `{rule}` — {where}\n  - {msg}")
    actionable = sum(n for sev, n in counts.items() if sev not in ("info", "low"))
    return ("OK" if not actionable else "FINDINGS"), (lines or ["No findings."]), counts


def gitleaks() -> tuple[str, list[str], int]:
    data = _load("gitleaks.json")
    if data is None:
        # gitleaks writes no file when it finds nothing in some versions, but it
        # also writes none when the download failed. The step's own exit code is
        # what tells them apart, and we cannot see it here — so say so.
        return "UNKNOWN", ["Secret scan produced no report (clean, or it did not run)."], 0
    n = len(data)
    lines = [
        f"- `{d.get('RuleID')}` in {d.get('File')} @ {str(d.get('Commit'))[:8]}" for d in data
    ]
    return ("OK" if not n else "FINDINGS"), (lines or ["No secrets found."]), n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="fa-web-api")
    ap.add_argument("--run-url", default="")
    args = ap.parse_args()

    tw_state, tw_lines, tw_counts = tripwires()
    dep_state, dep_lines, dep_n = pip_audit()
    sg_state, sg_lines, sg_counts = semgrep()
    gl_state, gl_lines, gl_n = gitleaks()

    critical = tw_counts.get("critical", 0) + sg_counts.get("critical", 0) + gl_n
    high = tw_counts.get("high", 0) + sg_counts.get("high", 0)
    medium = tw_counts.get("medium", 0) + sg_counts.get("medium", 0)
    unknown = [
        name
        for name, state in (
            ("tripwires", tw_state),
            ("dependencies", dep_state),
            ("semgrep", sg_state),
            ("secrets", gl_state),
        )
        if state == "UNKNOWN"
    ]

    bits = []
    if critical:
        bits.append(f"{critical} critical")
    if high:
        bits.append(f"{high} high")
    if medium:
        bits.append(f"{medium} medium")
    if dep_n:
        bits.append(f"{dep_n} vulnerable dependencies")
    summary = ", ".join(bits) if bits else "nothing new"
    if unknown:
        summary += f" (!) {', '.join(unknown)} did not report"

    verdict = ":white_check_mark:"
    if unknown:
        verdict = ":warning:"
    if medium or dep_n:
        verdict = ":large_yellow_circle:"
    if critical or high:
        verdict = ":rotating_light:"

    report = [
        f"# {args.repo} — weekly security audit",
        "",
        f"**{summary}**",
        "",
        f"[Workflow run]({args.run_url})" if args.run_url else "",
        "",
        f"## Project tripwires ({tw_state})",
        "",
        "The checks that know what this app is: which routes are reachable "
        "without a session, whether every cron route still verifies CRON_SECRET, "
        "and whether any SQL is built by interpolation.",
        "",
        *tw_lines,
        "",
        f"## Dependencies ({dep_state}) — full tree, not just prod",
        "",
        *dep_lines,
        "",
        f"## Semgrep ({sg_state})",
        "",
        *sg_lines[:100],
        ("\n_…truncated; see semgrep.json._" if len(sg_lines) > 100 else ""),
        "",
        f"## Secrets in git history ({gl_state})",
        "",
        *gl_lines,
        "",
        "## CodeQL",
        "",
        "Results are published to the repository's Security tab rather than "
        "duplicated here.",
    ]
    (ROOT / "security-report.md").write_text("\n".join(report), encoding="utf-8")

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"summary={summary}\n")
            fh.write(f"verdict={verdict}\n")
    # The report and the step outputs are already written; a console that
    # cannot encode this must not turn a successful audit into a failed step.
    try:
        print(summary)
    except UnicodeEncodeError:
        print(summary.encode('ascii', 'replace').decode('ascii'))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
