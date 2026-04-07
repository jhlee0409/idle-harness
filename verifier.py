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

from cli import call_agent, AgentError
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


# --- Criteria Classification ---

_CLASSIFY_PROMPT = """Classify each testable criterion into one of these types:
- api: Tests an API endpoint (HTTP request, status code, response body)
- css: Tests a CSS property (color, font, border-radius, shadow)
- ui: Tests UI element existence or interaction (button, form, navigation)
- responsive: Tests responsive behavior (viewport width, mobile layout)
- build: Tests build/compilation (npm build, python import)
- persistence: Tests data persistence (create, refresh, verify)
- perf: Tests performance (response time, load time)
- subjective: Design quality, originality, aesthetic judgment — cannot be verified deterministically

For each criterion, output a JSON object with:
  {"criterion": "original text", "type": "api|css|ui|responsive|build|persistence|perf|subjective", "details": {...}}

Details vary by type:
- api: {"method": "GET|POST", "path": "/api/...", "expect_status": 200}
- css: {"selector": "element", "property": "background-color", "expected": "#0F0F0F"}
- ui: {"url": "/path", "expect_elements": ["button", "form"]}
- responsive: {"viewport_width": 375, "expect": "no horizontal scroll"}
- build: {"command": "npm run build"}
- persistence: {"action": "create recipe, refresh, verify exists"}
- perf: {"metric": "response_time", "threshold_ms": 1000}
- subjective: {}

Output a JSON array. One object per criterion. No markdown, no explanation.

Criteria:
"""

# Cache for classified criteria (same criteria text → same classification)
_classification_cache: dict[str, list[TypedCheck]] = {}


async def classify_criteria(criteria_text: str, cwd: str) -> list[TypedCheck]:
    """Classify natural-language criteria into typed checks using an LLM."""
    cache_key = criteria_text.strip()
    if cache_key in _classification_cache:
        return _classification_cache[cache_key]

    # Extract criteria lines
    criteria_lines = []
    for line in criteria_text.split("\n"):
        stripped = line.strip()
        if re.match(r"^- \[[x ]\] ", stripped):
            # Strip the checkbox prefix
            text = re.sub(r"^- \[[x ]\] ", "", stripped)
            criteria_lines.append(text)

    if not criteria_lines:
        return []

    prompt = _CLASSIFY_PROMPT + "\n".join(f"- {c}" for c in criteria_lines)

    try:
        result = await call_agent(
            system_prompt="You are a criteria classifier. Output only valid JSON.",
            user_prompt=prompt,
            allowed_tools=[],
            cwd=cwd,
            timeout=60,
            model=CONFIG.get("model", "claude-opus-4-6"),
        )

        # Parse JSON from response (might be wrapped in markdown code block)
        text = result.result.strip()
        if text.startswith("```"):
            text = re.sub(r"^```\w*\n?", "", text)
            text = re.sub(r"\n?```$", "", text)

        checks_data = json.loads(text)
        checks = []
        for item in checks_data:
            try:
                check_type = CheckType(item.get("type", "subjective"))
            except ValueError:
                check_type = CheckType.SUBJECTIVE
            checks.append(TypedCheck(
                criterion=item.get("criterion", ""),
                check_type=check_type,
                details=item.get("details", {}),
            ))

        _classification_cache[cache_key] = checks
        return checks

    except (AgentError, json.JSONDecodeError, KeyError):
        # Fallback: all criteria are subjective
        return [
            TypedCheck(criterion=c, check_type=CheckType.SUBJECTIVE)
            for c in criteria_lines
        ]


# --- Deterministic Verification ---

async def verify_api(check: TypedCheck, base_url: str, artifacts_dir: str) -> CheckResult:
    """Verify an API endpoint returns expected status."""
    details = check.details
    method = details.get("method", "GET").upper()
    path = details.get("path", "/")
    expect_status = details.get("expect_status", 200)
    url = f"{base_url.rstrip('/')}{path}"

    timeout = CONFIG.get("verifier_check_timeout", 30)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if method == "POST":
                resp = await client.post(url, json=details.get("body", {}))
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
