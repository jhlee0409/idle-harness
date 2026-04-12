"""Deterministic verifier for harness pipeline.

Classifies testable criteria into typed checks and runs deterministic
verification before the expensive LLM Evaluator. Fail-closed: no artifact = FAIL.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum

import httpx

from config import CONFIG


class CheckType(str, Enum):
    API = "api"
    CSS = "css"
    UI = "ui"
    RESPONSIVE = "responsive"
    BUILD = "build"
    PERSISTENCE = "persistence"
    PERF = "perf"
    SUBJECTIVE = "subjective"


class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    SKIPPED = "skipped"  # subjective, passed to LLM


@dataclass
class TypedCheck:
    criterion: str
    check_type: CheckType
    details: dict = field(default_factory=dict)


@dataclass
class CheckResult:
    criterion: str
    check_type: CheckType
    status: CheckStatus
    artifact: str = ""  # path to screenshot, response file, etc.
    message: str = ""


@dataclass
class VerificationResult:
    passed: list[CheckResult] = field(default_factory=list)
    failed: list[CheckResult] = field(default_factory=list)
    inconclusive: list[CheckResult] = field(default_factory=list)
    subjective: list[str] = field(default_factory=list)  # criteria text for LLM eval
    overall_pass: bool = False
    duration_s: int = 0

    def feedback_text(self) -> str:
        """Generate feedback for the Generator on failed checks."""
        if not self.failed:
            return ""
        lines = ["## Verifier Results: FAIL\n"]
        lines.append(f"{len(self.failed)} deterministic check(s) failed:\n")
        for r in self.failed:
            lines.append(f"- [{r.check_type.value}] {r.criterion}")
            if r.message:
                lines.append(f"  Error: {r.message}")
        if self.passed:
            lines.append(f"\n{len(self.passed)} check(s) passed.")
        if self.subjective:
            lines.append(f"\n{len(self.subjective)} subjective criteria skipped (will be tested by Evaluator).")
        return "\n".join(lines)


# --- Criteria Classification (deterministic, regex-based) ---

# Patterns ordered by specificity. First match wins.
_CLASSIFY_RULES: list[tuple[CheckType, re.Pattern, dict]] = [
    # API: explicit endpoint mentions
    (CheckType.API, re.compile(
        r"(GET|POST|PUT|PATCH|DELETE)\s+/api/|/api/\w+|"
        r"returns?\s+(valid\s+)?JSON|status\s+(code\s+)?\d{3}|"
        r"endpoint|REST\s+API",
        re.IGNORECASE,
    ), {}),
    # Build: compilation, npm/pip commands
    (CheckType.BUILD, re.compile(
        r"npm\s+run\s+build|npx\s+|pip\s+install|vite\s+build|"
        r"tsc\s+|webpack|compilation|build\s+succeed",
        re.IGNORECASE,
    ), {}),
    # Responsive: viewport widths, mobile/tablet keywords
    (CheckType.RESPONSIVE, re.compile(
        r"\b(375|390|414|768|1024|1280|1440)px\b.*("
        r"viewport|mobile|tablet|desktop|hamburger|"
        r"horizontal\s+scroll|breakpoint|bottom\s+nav|"
        r"icon.?rail|collapses?)",
        re.IGNORECASE,
    ), {}),
    # Persistence: data survives refresh
    (CheckType.PERSISTENCE, re.compile(
        r"persist|page\s+refresh|reload|survives?\s+|"
        r"still\s+(visible|present|shows?|exists?)|"
        r"after\s+(a\s+)?(full\s+)?page\s+refresh",
        re.IGNORECASE,
    ), {}),
    # Perf: timing, speed
    (CheckType.PERF, re.compile(
        r"load\s+time|response\s+time|latency|"
        r"within\s+\d+\s*ms|milliseconds|performance",
        re.IGNORECASE,
    ), {}),
]

# API detail extraction: pull method + path from criterion text
_API_DETAIL_RE = re.compile(
    r"(GET|POST|PUT|PATCH|DELETE)\s+(/api/[\w/:?=&-]+)", re.IGNORECASE
)


def _classify_one(criterion: str) -> TypedCheck:
    """Classify a single criterion using regex rules. Deterministic."""
    for check_type, pattern, _ in _CLASSIFY_RULES:
        if pattern.search(criterion):
            details = {}
            if check_type == CheckType.API:
                m = _API_DETAIL_RE.search(criterion)
                if m:
                    details = {"method": m.group(1).upper(), "path": m.group(2)}
            return TypedCheck(criterion=criterion, check_type=check_type, details=details)
    # Default: subjective (requires Playwright / LLM Evaluator)
    return TypedCheck(criterion=criterion, check_type=CheckType.SUBJECTIVE)


def classify_criteria(criteria_text: str) -> list[TypedCheck]:
    """Classify criteria into typed checks using deterministic regex rules.

    No LLM call. Same input always produces same output.
    """
    checks = []
    for line in criteria_text.split("\n"):
        stripped = line.strip()
        if re.match(r"^- \[[x ]\] ", stripped):
            text = re.sub(r"^- \[[x ]\] ", "", stripped)
            checks.append(_classify_one(text))
    return checks


# --- Deterministic Verification ---

async def verify_api(check: TypedCheck, base_url: str, artifacts_dir: str) -> CheckResult:
    """Verify an API endpoint returns expected status.

    Handles :id placeholders by first fetching the resource list via GET
    and substituting the first available ID.
    """
    details = check.details
    method = details.get("method", "GET").upper()
    path = details.get("path", "/")
    expect_status = details.get("expect_status", 200)
    base = base_url.rstrip("/")

    timeout = CONFIG.get("verifier_check_timeout", 30)
    try:
        body = details.get("body")
        async with httpx.AsyncClient(timeout=timeout) as client:
            # Resolve :id placeholder — fetch the collection and use first ID
            if ":id" in path:
                collection_path = re.sub(r"/:[^/]+$", "", path)
                collection_url = f"{base}{collection_path}"
                try:
                    list_resp = await client.get(collection_url)
                    items = list_resp.json()
                    if isinstance(items, list) and items and "id" in items[0]:
                        path = path.replace(":id", str(items[0]["id"]))
                    else:
                        return CheckResult(
                            criterion=check.criterion, check_type=check.check_type,
                            status=CheckStatus.INCONCLUSIVE,
                            message=f"No resources found at {collection_url} to resolve :id",
                        )
                except Exception:
                    return CheckResult(
                        criterion=check.criterion, check_type=check.check_type,
                        status=CheckStatus.INCONCLUSIVE,
                        message=f"Failed to resolve :id from {collection_url}",
                    )

            url = f"{base}{path}"
            if method == "POST":
                resp = await client.post(url, json=body if body else {})
            elif method == "PUT":
                resp = await client.put(url, json=body if body else {})
            elif method == "PATCH":
                resp = await client.patch(url, json=body) if body else await client.patch(url)
            elif method == "DELETE":
                resp = await client.delete(url)
            else:
                resp = await client.get(url)

        # Save response as artifact
        artifact_path = os.path.join(artifacts_dir, f"api_{path.replace('/', '_')}.json")
        with open(artifact_path, "w") as f:
            json.dump({"status": resp.status_code, "body": resp.text[:2000]}, f, indent=2)

        if resp.status_code == expect_status:
            return CheckResult(
                criterion=check.criterion, check_type=check.check_type,
                status=CheckStatus.PASS, artifact=artifact_path,
                message=f"HTTP {resp.status_code} (expected {expect_status})",
            )
        return CheckResult(
            criterion=check.criterion, check_type=check.check_type,
            status=CheckStatus.FAIL, artifact=artifact_path,
            message=f"HTTP {resp.status_code} (expected {expect_status})",
        )
    except httpx.ConnectError:
        return CheckResult(
            criterion=check.criterion, check_type=check.check_type,
            status=CheckStatus.FAIL,
            message=f"Connection refused: {url}",
        )
    except (httpx.TimeoutException, httpx.HTTPError) as exc:
        return CheckResult(
            criterion=check.criterion, check_type=check.check_type,
            status=CheckStatus.INCONCLUSIVE,
            message=f"HTTP error: {exc}",
        )


async def verify_build(check: TypedCheck, output_dir: str, artifacts_dir: str) -> CheckResult:
    """Verify build commands succeed."""
    details = check.details
    command = details.get("command", "npm run build")
    timeout = CONFIG.get("verifier_check_timeout", 30)

    try:
        result = subprocess.run(
            command, shell=True, cwd=output_dir,
            capture_output=True, text=True, timeout=timeout,
        )
        artifact_path = os.path.join(artifacts_dir, "build_output.txt")
        os.makedirs(artifacts_dir, exist_ok=True)
        with open(artifact_path, "w") as f:
            f.write(f"Command: {command}\nExit: {result.returncode}\n")
            f.write(f"Stdout:\n{result.stdout[:2000]}\n")
            f.write(f"Stderr:\n{result.stderr[:2000]}\n")

        if result.returncode == 0:
            return CheckResult(
                criterion=check.criterion, check_type=check.check_type,
                status=CheckStatus.PASS, artifact=artifact_path,
                message=f"{command} succeeded",
            )
        return CheckResult(
            criterion=check.criterion, check_type=check.check_type,
            status=CheckStatus.FAIL, artifact=artifact_path,
            message=f"{command} failed (exit {result.returncode}): {result.stderr[:200]}",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            criterion=check.criterion, check_type=check.check_type,
            status=CheckStatus.INCONCLUSIVE,
            message=f"{command} timed out after {timeout}s",
        )


async def verify_check(check: TypedCheck, base_url: str, output_dir: str,
                        artifacts_dir: str) -> CheckResult:
    """Route a typed check to the appropriate verifier."""
    if check.check_type == CheckType.API:
        return await verify_api(check, base_url, artifacts_dir)
    if check.check_type == CheckType.BUILD:
        return await verify_build(check, output_dir, artifacts_dir)
    if check.check_type in (CheckType.CSS, CheckType.UI, CheckType.RESPONSIVE,
                            CheckType.PERSISTENCE, CheckType.PERF):
        # These require Playwright — run via subprocess to avoid MCP conflicts
        # For now, mark as INCONCLUSIVE (Evaluator will handle via MCP)
        return CheckResult(
            criterion=check.criterion, check_type=check.check_type,
            status=CheckStatus.SKIPPED,
            message=f"{check.check_type.value} check deferred to Evaluator (requires Playwright MCP)",
        )
    # Subjective
    return CheckResult(
        criterion=check.criterion, check_type=check.check_type,
        status=CheckStatus.SKIPPED,
        message="Subjective — deferred to LLM Evaluator",
    )


async def run_verification(
    typed_checks: list[TypedCheck],
    base_url: str,
    output_dir: str,
    artifacts_dir: str,
) -> VerificationResult:
    """Run all deterministic checks. Returns VerificationResult."""
    os.makedirs(artifacts_dir, exist_ok=True)
    start = time.time()
    max_retries = CONFIG.get("verifier_retry_inconclusive", 1)

    result = VerificationResult()

    for check in typed_checks:
        if check.check_type == CheckType.SUBJECTIVE:
            result.subjective.append(check.criterion)
            continue

        check_result = await verify_check(check, base_url, output_dir, artifacts_dir)

        # Retry INCONCLUSIVE
        if check_result.status == CheckStatus.INCONCLUSIVE:
            for _ in range(max_retries):
                check_result = await verify_check(check, base_url, output_dir, artifacts_dir)
                if check_result.status != CheckStatus.INCONCLUSIVE:
                    break
            # Still inconclusive after retries → FAIL
            if check_result.status == CheckStatus.INCONCLUSIVE:
                check_result.status = CheckStatus.FAIL
                check_result.message += " (failed after retry)"

        if check_result.status == CheckStatus.PASS:
            result.passed.append(check_result)
        elif check_result.status == CheckStatus.FAIL:
            result.failed.append(check_result)
        elif check_result.status == CheckStatus.SKIPPED:
            # Skipped checks (CSS/UI/responsive) go to subjective for LLM eval
            result.subjective.append(check.criterion)

    result.overall_pass = len(result.failed) == 0
    result.duration_s = int(time.time() - start)
    return result


def backup_db(output_dir: str) -> str | None:
    """Backup SQLite databases before verification. Returns backup dir or None."""
    db_files = []
    for root, _, files in os.walk(output_dir):
        for f in files:
            if f.endswith(".db") or f.endswith(".sqlite") or f.endswith(".sqlite3"):
                db_files.append(os.path.join(root, f))

    if not db_files:
        return None

    backup_dir = os.path.join(output_dir, ".verifier_db_backup")
    os.makedirs(backup_dir, exist_ok=True)
    for db_path in db_files:
        rel = os.path.relpath(db_path, output_dir)
        dest = os.path.join(backup_dir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(db_path, dest)

    return backup_dir


def restore_db(output_dir: str, backup_dir: str):
    """Restore SQLite databases from backup after verification."""
    if not backup_dir or not os.path.isdir(backup_dir):
        return
    for root, _, files in os.walk(backup_dir):
        for f in files:
            src = os.path.join(root, f)
            rel = os.path.relpath(src, backup_dir)
            dest = os.path.join(output_dir, rel)
            shutil.copy2(src, dest)
    shutil.rmtree(backup_dir, ignore_errors=True)
