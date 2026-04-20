from bughound_agent import BugHoundAgent
from llm_client import MockClient


def test_workflow_runs_in_offline_mode_and_returns_shape():
    agent = BugHoundAgent(client=None)  # heuristic-only
    code = "def f():\n    print('hi')\n    return True\n"
    result = agent.run(code)

    assert isinstance(result, dict)
    assert "issues" in result
    assert "fixed_code" in result
    assert "risk" in result
    assert "logs" in result

    assert isinstance(result["issues"], list)
    assert isinstance(result["fixed_code"], str)
    assert isinstance(result["risk"], dict)
    assert isinstance(result["logs"], list)
    assert len(result["logs"]) > 0


def test_offline_mode_detects_print_issue():
    agent = BugHoundAgent(client=None)
    code = "def f():\n    print('hi')\n    return True\n"
    result = agent.run(code)

    assert any(issue.get("type") == "Code Quality" for issue in result["issues"])


def test_offline_mode_proposes_logging_fix_for_print():
    agent = BugHoundAgent(client=None)
    code = "def f():\n    print('hi')\n    return True\n"
    result = agent.run(code)

    fixed = result["fixed_code"]
    assert "logging" in fixed
    assert "logging.info(" in fixed


def test_mock_client_forces_llm_fallback_to_heuristics_for_analysis():
    # MockClient returns non-JSON for analyzer prompts, so agent should fall back.
    agent = BugHoundAgent(client=MockClient())
    code = "def f():\n    print('hi')\n    return True\n"
    result = agent.run(code)

    assert any(issue.get("type") == "Code Quality" for issue in result["issues"])
    # Ensure we logged the fallback path
    assert any("Falling back to heuristics" in entry.get("message", "") for entry in result["logs"])


def test_severity_normalization_maps_nonstandard_values():
    """LLM may return severities like 'Critical' that the risk assessor doesn't recognize.
    _normalize_severity should map them to High/Medium/Low so risk scoring works."""

    class SeverityClient:
        def complete(self, system_prompt, user_prompt):
            if "JSON" in system_prompt:
                return '[{"type": "Bug", "severity": "Critical", "msg": "bad"}, ' \
                       '{"type": "Style", "severity": "trivial", "msg": "nit"}, ' \
                       '{"type": "Smell", "severity": "bizarre", "msg": "huh"}]'
            return "# no fix"

    agent = BugHoundAgent(client=SeverityClient())
    issues = agent.analyze("x = 1")

    allowed = {"High", "Medium", "Low"}
    for issue in issues:
        assert issue["severity"] in allowed, f"Unexpected severity: {issue['severity']}"

    sevs = [i["severity"] for i in issues]
    assert sevs == ["High", "Low", "Medium"]  # Critical->High, trivial->Low, bizarre->Medium


def test_syntax_error_detected_for_missing_indentation():
    """Code with missing indentation should be flagged as a high-severity issue."""
    agent = BugHoundAgent(client=None)
    bad_code = "def add(a, b):\nlogging.info('Adding numbers')\nreturn a + b\n"
    result = agent.run(bad_code)

    assert any(issue.get("type") in ("Syntax", "Indentation") for issue in result["issues"])
    assert any(issue.get("severity") == "High" for issue in result["issues"] if issue.get("type") in ("Syntax", "Indentation"))
    assert result["risk"]["should_autofix"] is False


def test_llm_results_merged_with_heuristics():
    """Even when the LLM returns valid issues, heuristic checks should still run
    so that syntax errors and known patterns are never silently skipped."""

    class EmptyArrayClient:
        def complete(self, system_prompt, user_prompt):
            if "JSON" in system_prompt:
                return "[]"  # LLM found nothing
            return "# no fix"

    agent = BugHoundAgent(client=EmptyArrayClient())
    # Code has a print() and a bare except — heuristics should catch both
    code = "def f():\n    try:\n        print('hi')\n    except:\n        pass\n"
    issues = agent.analyze(code)

    types = [i["type"] for i in issues]
    assert "Code Quality" in types, "Heuristic print() check was skipped"
    assert "Reliability" in types, "Heuristic bare-except check was skipped"
