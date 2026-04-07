import json
import os
import tempfile

import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from verifier import (
    CheckType, CheckStatus, TypedCheck, CheckResult, VerificationResult,
    classify_criteria, verify_api, verify_build, verify_check,
    run_verification, backup_db, restore_db,
)


def _mock_agent_result(text):
    """Create a mock AgentResult."""
    m = MagicMock()
    m.result = text
    m.input_tokens = 0
    m.output_tokens = 0
    m.turns = 1
    m.session_id = ""
    m.cost_usd = 0.01
    m.duration_ms = 100
    return m


SAMPLE_CLASSIFY_RESPONSE = json.dumps([
    {"criterion": "API returns 200", "type": "api", "details": {"method": "GET", "path": "/api/recipes", "expect_status": 200}},
    {"criterion": "Background is #0F0F0F", "type": "css", "details": {"selector": "html", "property": "background-color", "expected": "#0F0F0F"}},
    {"criterion": "Design feels cohesive", "type": "subjective", "details": {}},
])


# --- classify_criteria tests ---

@pytest.mark.anyio
async def test_classify_criteria_api():
    with tempfile.TemporaryDirectory() as tmpdir:
        mock = AsyncMock(return_value=_mock_agent_result(SAMPLE_CLASSIFY_RESPONSE))
        with patch("verifier.call_agent", mock), \
             patch("verifier._classification_cache", {}):
            criteria = "- [x] API returns 200\n- [x] Background is #0F0F0F\n- [x] Design feels cohesive"
            checks = await classify_criteria(criteria, tmpdir)
            assert len(checks) == 3
            assert checks[0].check_type == CheckType.API
            assert checks[0].details["path"] == "/api/recipes"


@pytest.mark.anyio
async def test_classify_criteria_css():
    with tempfile.TemporaryDirectory() as tmpdir:
        mock = AsyncMock(return_value=_mock_agent_result(SAMPLE_CLASSIFY_RESPONSE))
        with patch("verifier.call_agent", mock), \
             patch("verifier._classification_cache", {}):
            criteria = "- [x] API returns 200\n- [x] Background is #0F0F0F\n- [x] Design feels cohesive"
            checks = await classify_criteria(criteria, tmpdir)
            assert checks[1].check_type == CheckType.CSS


@pytest.mark.anyio
async def test_classify_criteria_subjective():
    with tempfile.TemporaryDirectory() as tmpdir:
        mock = AsyncMock(return_value=_mock_agent_result(SAMPLE_CLASSIFY_RESPONSE))
        with patch("verifier.call_agent", mock), \
             patch("verifier._classification_cache", {}):
            criteria = "- [x] API returns 200\n- [x] Background is #0F0F0F\n- [x] Design feels cohesive"
            checks = await classify_criteria(criteria, tmpdir)
            assert checks[2].check_type == CheckType.SUBJECTIVE


@pytest.mark.anyio
async def test_classify_criteria_fallback_on_error():
    """Unclassifiable criteria should fallback to subjective."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from cli import AgentError
        mock = AsyncMock(side_effect=AgentError("LLM failed"))
        with patch("verifier.call_agent", mock), \
             patch("verifier._classification_cache", {}):
            criteria = "- [x] Something complex"
            checks = await classify_criteria(criteria, tmpdir)
            assert len(checks) == 1
            assert checks[0].check_type == CheckType.SUBJECTIVE


@pytest.mark.anyio
async def test_classify_criteria_cached():
    """Same criteria text should not re-call LLM."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mock = AsyncMock(return_value=_mock_agent_result(SAMPLE_CLASSIFY_RESPONSE))
        with patch("verifier.call_agent", mock), \
             patch("verifier._classification_cache", {}):
            criteria = "- [x] API returns 200\n- [x] Background is #0F0F0F\n- [x] Design feels cohesive"
            await classify_criteria(criteria, tmpdir)
            await classify_criteria(criteria, tmpdir)  # second call
            assert mock.call_count == 1  # only called once


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
