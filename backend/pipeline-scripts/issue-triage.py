"""Issue Triage Pipeline

Triggered on new issue creation or when an issue is labelled "triage".
Reads the issue body, analyses it via opencode to determine category,
priority, and required labels, then applies the suggested labels and
assigns to the appropriate team member based on Modulo's delivery plan.

Environment Variables:
    GITHUB_TOKEN                 — GitHub PAT for issue access and label management
    GITHUB_REPO                  — Repository full name (e.g. "farnalabs/modulo")
    GITHUB_ISSUE_NUMBER          — Issue number to triage
    APP_MODULO_OPENCODE_API_KEY  — API key for the opencode CLI

Output:
    Writes status, summary, wall_clock_ms, issue details, and labels to output.json
"""

import json
import os
import subprocess
import time

from _common import check_env, exit_completed, exit_failed, setup_opencode_auth

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


def check_env_extra():
    missing = [k for k in ["GITHUB_REPO", "GITHUB_ISSUE_NUMBER"] if k not in os.environ]
    if missing:
        exit_failed(f"Missing required env vars: {', '.join(missing)}")
    return os.environ["GITHUB_REPO"], os.environ["GITHUB_ISSUE_NUMBER"]


def get_issue_details(repo_full_name, issue_number, token):
    r = subprocess.run(
        ["gh", "issue", "view", issue_number, "--repo", repo_full_name, "--json", "title,body,labels,author"],
        env={**os.environ, "GH_TOKEN": token},
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        exit_failed(f"Failed to get issue details: {r.stderr[:200]}")
    return json.loads(r.stdout)


def classify_issue(issue):
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


def apply_labels(repo_full_name, issue_number, classification, token):
    labels = CATEGORIES.get(classification["category"], ["needs-triage"])
    priority_label = PRIORITY_LABELS.get(classification["priority"], "priority:medium")
    labels.append(priority_label)

    r = subprocess.run(
        ["gh", "issue", "edit", issue_number, "--repo", repo_full_name, "--add-label", ",".join(labels)],
        env={**os.environ, "GH_TOKEN": token},
        capture_output=True,
    )
    if r.returncode != 0:
        exit_failed(f"Failed to apply labels: {r.stderr[:200]}")
    return labels


def main():
    token, api_key = check_env()
    model = setup_opencode_auth(api_key)
    github_repo, issue_number = check_env_extra()

    issue = get_issue_details(github_repo, issue_number, token)

    start = time.time()
    classification = classify_issue(issue)
    wall_clock_ms = int((time.time() - start) * 1000)

    labels = apply_labels(github_repo, issue_number, classification, token)

    exit_completed(
        summary=f"Triaged issue #{issue_number}: {classification['category']} ({classification['priority']})",
        extra={
            "issue_number": int(issue_number),
            "issue_title": issue["title"],
            "category": classification["category"],
            "priority": classification["priority"],
            "labels_applied": labels,
            "confidence": classification.get("confidence", "unknown"),
            "wall_clock_ms": wall_clock_ms,
            "usage": {"model": model, "wall_clock_ms": wall_clock_ms},
        },
    )


if __name__ == "__main__":
    main()
