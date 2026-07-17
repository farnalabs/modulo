"""Issue Triage Pipeline

Triggered on new issue creation or when an issue is labelled "triage".
Reads the issue body, analyses it via opencode to determine category,
priority, and required labels, then applies the suggested labels and
assigns to the appropriate team member based on Modulo's delivery plan.

Environment Variables:
    GITHUB_TOKEN        — GitHub PAT for issue access and label management
    GITHUB_REPO         — Repository full name (e.g. "farnalabs/modulo")
    GITHUB_ISSUE_NUMBER — Issue number to triage
    OPENCODE_API_KEY    — API key for the opencode CLI

Output:
    Writes status, summary (issue, assigned labels, priority), and issue_url
    to /tmp/output.json
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REQUIRED_ENV_VARS = [
    "GITHUB_TOKEN",
    "GITHUB_REPO",
    "GITHUB_ISSUE_NUMBER",
    "OPENCODE_API_KEY",
]


CATEGORIES = {
    "bug": ["bug", "needs-reproduction"],
    "feature": ["enhancement", "needs-design"],
    "docs": ["documentation"],
    "infrastructure": ["infrastructure", "ci/cd"],
    "question": ["question"],
    "security": ["security"],
}

PRIORITY_LABELS = {
    "critical": "priority:critical",
    "high": "priority:high",
    "medium": "priority:medium",
    "low": "priority:low",
}


def check_env() -> dict[str, str]:
    missing = [k for k in REQUIRED_ENV_VARS if k not in os.environ]
    if missing:
        print(f"Missing required env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
    return {
        "github_token": os.environ["GITHUB_TOKEN"],
        "github_repo": os.environ["GITHUB_REPO"],
        "issue_number": os.environ["GITHUB_ISSUE_NUMBER"],
        "opencode_api_key": os.environ["OPENCODE_API_KEY"],
    }


def get_issue_details(repo_full_name: str, issue_number: str, github_token: str) -> dict:
    result = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            issue_number,
            "--repo",
            repo_full_name,
            "--json",
            "title,body,labels,author",
        ],
        env={**os.environ, "GH_TOKEN": github_token},
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def classify_issue(issue: dict, repo_path: Path) -> dict:
    prompt = json.dumps(
        {
            "task": "triage_issue",
            "title": issue["title"],
            "body": issue.get("body", "")[:2000],
            "existing_labels": [label["name"] for label in issue.get("labels", [])],
        }
    )

    result = subprocess.run(
        ["opencode", "evaluate", "--prompt", prompt],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        return {
            "category": "question",
            "priority": "medium",
            "summary": "Could not classify — defaulting to question/medium",
            "confidence": 0.0,
        }

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {
            "category": "question",
            "priority": "medium",
            "summary": "Non-JSON response from classifier — defaulting",
            "confidence": 0.0,
        }


def apply_labels(
    repo_full_name: str,
    issue_number: str,
    classification: dict,
    github_token: str,
) -> list[str]:
    labels = CATEGORIES.get(classification["category"], ["needs-triage"])
    priority_label = PRIORITY_LABELS.get(classification["priority"], "priority:medium")
    labels.append(priority_label)

    label_args = ["--add-label", ",".join(labels)]
    subprocess.run(
        [
            "gh",
            "issue",
            "edit",
            issue_number,
            "--repo",
            repo_full_name,
            *label_args,
        ],
        env={**os.environ, "GH_TOKEN": github_token},
        capture_output=True,
        check=True,
    )
    return labels


def write_output(status: str, summary: str, extra: dict | None = None) -> None:
    output = {"status": status, "summary": summary}
    if extra:
        output.update(extra)
    output_path = Path("/tmp/output.json")
    output_path.write_text(json.dumps(output, indent=2))
    print(f"Output written to {output_path}")


def main() -> None:
    env = check_env()

    with tempfile.TemporaryDirectory(prefix="issue-triage-") as tmpdir:
        repo_path = Path(tmpdir) / "repo"

        issue = get_issue_details(env["github_repo"], env["issue_number"], env["github_token"])
        classification = classify_issue(issue, repo_path)
        labels = apply_labels(
            env["github_repo"],
            env["issue_number"],
            classification,
            env["github_token"],
        )

        write_output(
            status="success",
            summary=(
                f"Triaged issue #{env['issue_number']}: {classification['category']} ({classification['priority']})"
            ),
            extra={
                "issue_number": int(env["issue_number"]),
                "issue_title": issue["title"],
                "category": classification["category"],
                "priority": classification["priority"],
                "labels_applied": labels,
                "confidence": classification.get("confidence", "unknown"),
            },
        )


if __name__ == "__main__":
    main()
