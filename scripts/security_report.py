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

#: Findings that are KNOWN, REVIEWED, and deliberately not counted in the
#: verdict. They still appear in the report, under their own heading — this
#: hides nothing, it only stops a standing condition from being re-reported as
#: news every week.
#:
#: ⚠️ THE VERDICT IS ONLY WORTH READING IF IT IS TRUE. The first real Slack
#: message this job sent was ":rotating_light: 1 high, 22 medium" — and every
#: one of those 23 was already understood. A siren that fires every week for
#: something nobody intends to act on trains you to ignore the week it matters.
#:
#: Each entry needs a reason. Deleting an entry puts the finding back in the
#: verdict, which is the right move the moment one stops being acceptable.
ACCEPTED_SEMGREP_RULES = {
    "use-defused-xml": (
        "False positive, verified 2026-08-24. It flags "
        "xml.sax.saxutils.escape — an ESCAPER that parses nothing, so XXE and "
        "billion-laughs do not apply — in a local template-generation script "
        "that never runs in the API."
    ),
    "github-actions-mutable-action-tag": (
        "Known and accepted 2026-08-24: our workflows use actions/checkout@v4 "
        "rather than a pinned commit SHA. This is REAL supply-chain hardening "
        "we have chosen not to do yet, not a false positive — pinning would "
        "protect against a compromised tag on a GitHub-owned action, at the "
        "cost of having to bump the hashes by hand. It is a standing condition, "
        "so it belongs in the report body rather than in a weekly siren. Delete "
        "this entry the day we decide to pin."
    ),
}


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


def semgrep() -> tuple[str, list[str], dict[str, int], list[str]]:
    """Returns (state, lines, counts, accepted_lines).

    ``counts`` excludes anything in ACCEPTED_SEMGREP_RULES; those are summarised
    separately so the report still shows them without the verdict shouting.
    """
    data = _load("semgrep.json")
    if data is None:
        return "UNKNOWN", ["Semgrep did not produce a report."], {}, []
    counts: dict[str, int] = {}
    lines = []
    accepted: dict[str, int] = {}
    for r in data.get("results", []):
        sev = (r.get("extra", {}).get("severity") or "INFO").lower()
        sev = {"error": "high", "warning": "medium", "info": "low"}.get(sev, sev)
        rule = r.get("check_id", "").split(".")[-1]
        if rule in ACCEPTED_SEMGREP_RULES:
            accepted[rule] = accepted.get(rule, 0) + 1
            continue
        counts[sev] = counts.get(sev, 0) + 1
        where = f"{r.get('path')}:{r.get('start', {}).get('line')}"
        msg = (r.get("extra", {}).get("message") or "").strip().splitlines()[0][:200]
        lines.append(f"- **{sev.upper()}** `{rule}` — {where}\n  - {msg}")
    accepted_lines = [
        f"- `{rule}` × {n} — {ACCEPTED_SEMGREP_RULES[rule]}" for rule, n in sorted(accepted.items())
    ]
    actionable = sum(n for sev, n in counts.items() if sev not in ("info", "low"))
    return (
        ("OK" if not actionable else "FINDINGS"),
        (lines or ["No findings."]),
        counts,
        accepted_lines,
    )


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
    sg_state, sg_lines, sg_counts, sg_accepted = semgrep()
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

    # THE VERDICT COMES FIRST AND IN PLAIN ENGLISH. The question being answered
    # on a Sunday night is "do I need to do something?", not "how many rules
    # matched?". Counts go after the answer, and only when the answer is yes.
    bits = []
    if critical:
        bits.append(f"{critical} critical")
    if high:
        bits.append(f"{high} high")
    if medium:
        bits.append(f"{medium} medium")
    if dep_n:
        bits.append(f"{dep_n} vulnerable dependencies")

    needs_attention = critical + high + medium + dep_n
    if unknown:
        # Not "all clear": part of the sweep did not happen, and saying so is
        # the whole reason a missing report is tracked separately from a clean one.
        verdict = "INCOMPLETE"
        summary = (
            f"INCOMPLETE - {', '.join(unknown)} did not run, so this week is "
            f"only a partial check"
        )
        if bits:
            summary += f" (and {', '.join(bits)} in what did run)"
    elif not needs_attention:
        verdict = "ALL CLEAR"
        summary = "all clear, nothing needs you"
    else:
        verdict = "NEEDS A LOOK"
        thing = "thing needs" if needs_attention == 1 else "things need"
        summary = f"{needs_attention} {thing} a look - {', '.join(bits)}"

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
        *(
            [
                "## Known and accepted — deliberately not in the verdict",
                "",
                "Reviewed, understood, and excluded from the count above so a "
                "standing condition is not re-reported as news every week. "
                "Removing an entry from ACCEPTED_SEMGREP_RULES puts it straight "
                "back into the verdict.",
                "",
                *sg_accepted,
                "",
            ]
            if sg_accepted
            else []
        ),
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
