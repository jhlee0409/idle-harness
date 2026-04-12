import os
import tempfile

import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from verifier import (
    CheckType, CheckStatus, TypedCheck, CheckResult, VerificationResult,
    classify_criteria, verify_api, verify_build, verify_check,
    run_verification, backup_db, restore_db,
)


# --- classify_criteria tests (deterministic regex-based) ---

def test_classify_criteria_api():
    criteria = "- [x] GET /api/recipes returns valid JSON with status 200"
    checks = classify_criteria(criteria)
    assert len(checks) == 1
    assert checks[0].check_type == CheckType.API
    assert checks[0].details["method"] == "GET"
    assert checks[0].details["path"] == "/api/recipes"


def test_classify_criteria_css():
    """Criteria without API/build/responsive patterns default to subjective
    (CSS requires Playwright, so it's deferred to Evaluator)."""
    criteria = "- [x] Background color is #0F0F0F"
    checks = classify_criteria(criteria)
    assert len(checks) == 1
    assert checks[0].check_type == CheckType.SUBJECTIVE


def test_classify_criteria_subjective():
    criteria = "- [x] Design feels cohesive and editorial"
    checks = classify_criteria(criteria)
    assert len(checks) == 1
    assert checks[0].check_type == CheckType.SUBJECTIVE


def test_classify_criteria_fallback_on_error():
    """Criteria with no matching patterns default to subjective."""
    criteria = "- [x] Button click opens a modal with a form"
    checks = classify_criteria(criteria)
    assert len(checks) == 1
    assert checks[0].check_type == CheckType.SUBJECTIVE


def test_classify_criteria_cached():
    """Deterministic classifier always returns same result — no caching needed."""
    criteria = "- [x] GET /api/tasks returns JSON\n- [x] Design is original"
    checks1 = classify_criteria(criteria)
    checks2 = classify_criteria(criteria)
    assert len(checks1) == len(checks2)
    assert checks1[0].check_type == checks2[0].check_type
    assert checks1[1].check_type == checks2[1].check_type


# --- verify_api tests ---

@pytest.mark.anyio
async def test_verify_api_success():
    with tempfile.TemporaryDirectory() as tmpdir:
        check = TypedCheck("API returns 200", CheckType.API, {"method": "GET", "path": "/api/test", "expect_status": 200})

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{"ok": true}'

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("verifier.httpx.AsyncClient", return_value=mock_client):
            result = await verify_api(check, "http://localhost:8006", tmpdir)
            assert result.status == CheckStatus.PASS
            assert result.artifact  # artifact path exists
            assert os.path.exists(result.artifact)


@pytest.mark.anyio
async def test_verify_api_failure():
    with tempfile.TemporaryDirectory() as tmpdir:
        check = TypedCheck("API returns 200", CheckType.API, {"method": "GET", "path": "/api/broken", "expect_status": 200})

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = 'Internal Server Error'

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("verifier.httpx.AsyncClient", return_value=mock_client):
            result = await verify_api(check, "http://localhost:8006", tmpdir)
            assert result.status == CheckStatus.FAIL
            assert "500" in result.message


@pytest.mark.anyio
async def test_verify_api_connection_refused():
    with tempfile.TemporaryDirectory() as tmpdir:
        check = TypedCheck("API returns 200", CheckType.API, {"method": "GET", "path": "/api/test", "expect_status": 200})

        import httpx
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("verifier.httpx.AsyncClient", return_value=mock_client):
            result = await verify_api(check, "http://localhost:8006", tmpdir)
            assert result.status == CheckStatus.FAIL
            assert "Connection refused" in result.message


# --- verify_build tests ---

@pytest.mark.anyio
async def test_verify_build_npm_success():
    with tempfile.TemporaryDirectory() as tmpdir:
        artifacts = os.path.join(tmpdir, "artifacts")
        check = TypedCheck("npm build succeeds", CheckType.BUILD, {"command": "echo success"})

        result = await verify_build(check, tmpdir, artifacts)
        assert result.status == CheckStatus.PASS
        assert os.path.exists(result.artifact)


@pytest.mark.anyio
async def test_verify_build_npm_failure():
    with tempfile.TemporaryDirectory() as tmpdir:
        artifacts = os.path.join(tmpdir, "artifacts")
        os.makedirs(artifacts, exist_ok=True)
        check = TypedCheck("npm build succeeds", CheckType.BUILD, {"command": "exit 1"})

        result = await verify_build(check, tmpdir, artifacts)
        assert result.status == CheckStatus.FAIL
        assert "exit 1" in result.message


# --- verify_check routing tests ---

@pytest.mark.anyio
async def test_verify_css_deferred_to_evaluator():
    """CSS checks are deferred to LLM Evaluator (requires Playwright MCP)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        check = TypedCheck("Background is dark", CheckType.CSS, {})
        result = await verify_check(check, "http://localhost:8005", tmpdir, tmpdir)
        assert result.status == CheckStatus.SKIPPED


@pytest.mark.anyio
async def test_verify_responsive_deferred():
    with tempfile.TemporaryDirectory() as tmpdir:
        check = TypedCheck("Mobile layout works", CheckType.RESPONSIVE, {})
        result = await verify_check(check, "http://localhost:8005", tmpdir, tmpdir)
        assert result.status == CheckStatus.SKIPPED


# --- run_verification tests ---

@pytest.mark.anyio
async def test_run_all_mixed_results():
    """Mixed pass/fail results → overall FAIL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        artifacts = os.path.join(tmpdir, "artifacts")
        checks = [
            TypedCheck("API works", CheckType.API, {"method": "GET", "path": "/api/test", "expect_status": 200}),
            TypedCheck("Design is nice", CheckType.SUBJECTIVE, {}),
        ]

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "error"

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("verifier.httpx.AsyncClient", return_value=mock_client):
            result = await run_verification(checks, "http://localhost:8006", tmpdir, artifacts)
            assert not result.overall_pass
            assert len(result.failed) == 1
            assert len(result.subjective) == 1


@pytest.mark.anyio
async def test_inconclusive_retry_then_pass():
    """INCONCLUSIVE → retry → PASS."""
    with tempfile.TemporaryDirectory() as tmpdir:
        artifacts = os.path.join(tmpdir, "artifacts")
        check = TypedCheck("API works", CheckType.API, {"method": "GET", "path": "/api/test", "expect_status": 200})

        # First call: timeout (INCONCLUSIVE), second call: success (PASS)
        import httpx as httpx_mod

        mock_response_ok = MagicMock()
        mock_response_ok.status_code = 200
        mock_response_ok.text = '{"ok": true}'

        call_count = 0

        async def mock_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx_mod.TimeoutException("timeout")
            return mock_response_ok

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("verifier.httpx.AsyncClient", return_value=mock_client):
            result = await run_verification([check], "http://localhost:8006", tmpdir, artifacts)
            assert result.overall_pass
            assert len(result.passed) == 1


@pytest.mark.anyio
async def test_inconclusive_retry_then_fail():
    """INCONCLUSIVE → retry → still INCONCLUSIVE → FAIL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        artifacts = os.path.join(tmpdir, "artifacts")
        check = TypedCheck("API works", CheckType.API, {"method": "GET", "path": "/api/test", "expect_status": 200})

        import httpx as httpx_mod

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx_mod.TimeoutException("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("verifier.httpx.AsyncClient", return_value=mock_client):
            result = await run_verification([check], "http://localhost:8006", tmpdir, artifacts)
            assert not result.overall_pass
            assert len(result.failed) == 1
            assert "retry" in result.failed[0].message


# --- DB backup/restore tests ---

def test_backup_and_restore_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a fake DB file
        db_path = os.path.join(tmpdir, "data.db")
        with open(db_path, "w") as f:
            f.write("original")

        backup_dir = backup_db(tmpdir)
        assert backup_dir is not None

        # Modify the DB
        with open(db_path, "w") as f:
            f.write("modified")

        # Restore
        restore_db(tmpdir, backup_dir)
        with open(db_path) as f:
            assert f.read() == "original"


def test_backup_no_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        result = backup_db(tmpdir)
        assert result is None


# --- VerificationResult.feedback_text ---

def test_feedback_text_with_failures():
    result = VerificationResult(
        passed=[CheckResult("API ok", CheckType.API, CheckStatus.PASS)],
        failed=[CheckResult("Build broken", CheckType.BUILD, CheckStatus.FAIL, message="exit 1")],
        subjective=["Design quality"],
    )
    text = result.feedback_text()
    assert "FAIL" in text
    assert "Build broken" in text
    assert "exit 1" in text
    assert "1 check(s) passed" in text
    assert "1 subjective" in text


def test_feedback_text_no_failures():
    result = VerificationResult(passed=[], failed=[], subjective=[])
    assert result.feedback_text() == ""
