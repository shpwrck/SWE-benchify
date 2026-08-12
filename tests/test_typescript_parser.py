"""Tests for TypeScriptJestJSONParser (jest --json / vitest --reporter=json)."""

import json

from swebenchify.parsers import (
    TypeScriptJestJSONParser,
    normalize_typescript_f2p,
)


def _report(suites, **top):
    """Build a compact jest-schema JSON report string."""
    base = {
        "numFailedTestSuites": 0,
        "numFailedTests": 0,
        "numPassedTests": 0,
        "numTotalTests": 0,
        "success": True,
        "testResults": suites,
    }
    base.update(top)
    return json.dumps(base)


def _suite(name, assertions, status="passed"):
    return {"name": name, "status": status, "assertionResults": assertions}


def _assertion(full_name, status, ancestors=None, title=""):
    a = {"fullName": full_name, "status": status}
    if ancestors is not None:
        a["ancestorTitles"] = ancestors
    if title:
        a["title"] = title
    return a


class TestTypeScriptParserBasics:
    def test_passing_report(self):
        raw = _report([
            _suite("/repo/src/__tests__/util.test.ts", [
                _assertion("util parses input", "passed"),
                _assertion("util rejects bad input", "passed"),
            ]),
        ], numPassedTests=2, numTotalTests=2)
        result = TypeScriptJestJSONParser().parse(raw)
        assert result["compiled"] is True
        assert result["tests"] == {
            "src/__tests__/util.test.ts::util parses input": "passed",
            "src/__tests__/util.test.ts::util rejects bad input": "passed",
        }

    def test_failing_report(self):
        raw = _report([
            _suite("/repo/src/cli.test.ts", [
                _assertion("cli exits cleanly", "failed"),
                _assertion("cli prints help", "passed"),
            ], status="failed"),
        ], numFailedTests=1, numPassedTests=1, numTotalTests=2, success=False)
        result = TypeScriptJestJSONParser().parse(raw)
        assert result["compiled"] is True
        assert result["tests"]["src/cli.test.ts::cli exits cleanly"] == "failed"
        assert result["tests"]["src/cli.test.ts::cli prints help"] == "passed"

    def test_pending_and_todo_are_skipped(self):
        raw = _report([
            _suite("/repo/a.test.ts", [
                _assertion("t1", "pending"),
                _assertion("t2", "todo"),
                _assertion("t3", "skipped"),
            ]),
        ], numTotalTests=3)
        result = TypeScriptJestJSONParser().parse(raw)
        assert set(result["tests"].values()) == {"skipped"}

    def test_suite_failed_to_run_is_compile_failure(self):
        """A transform/tsc error yields zero assertions and a failed suite."""
        raw = _report(
            [_suite("/repo/src/broken.test.ts", [], status="failed")],
            numFailedTestSuites=1, success=False,
        )
        result = TypeScriptJestJSONParser().parse(raw)
        assert result["compiled"] is False
        assert result["tests"] == {}

    def test_empty_output(self):
        result = TypeScriptJestJSONParser().parse("")
        assert result["compiled"] is False
        assert result["tests"] == {}

    def test_no_report_in_output(self):
        result = TypeScriptJestJSONParser().parse("npm ERR! missing script: test\n")
        assert result["compiled"] is False


class TestTypeScriptParserRobustness:
    def test_report_amid_noise(self):
        """git-apply output and phase markers around the JSON line are ignored."""
        report = _report([
            _suite("/repo/lib/x.test.ts", [_assertion("x works", "passed")]),
        ], numPassedTests=1, numTotalTests=1)
        raw = (
            "Checking patch lib/x.test.ts...\n"
            "Applied patch lib/x.test.ts cleanly.\n"
            f"{report}\n"
            "\n"
        )
        result = TypeScriptJestJSONParser().parse(raw)
        assert result["tests"] == {"lib/x.test.ts::x works": "passed"}

    def test_pretty_printed_report(self):
        report = json.dumps(json.loads(_report([
            _suite("/repo/a.test.ts", [_assertion("a", "passed")]),
        ])), indent=2)
        result = TypeScriptJestJSONParser().parse("noise before\n" + report)
        assert result["tests"] == {"a.test.ts::a": "passed"}

    def test_testbed_prefix_stripped(self):
        raw = _report([
            _suite("/testbed/src/a.test.ts", [_assertion("a", "passed")]),
        ])
        result = TypeScriptJestJSONParser().parse(raw)
        assert "src/a.test.ts::a" in result["tests"]

    def test_relative_dot_prefix_stripped(self):
        raw = _report([
            _suite("./src/a.test.ts", [_assertion("a", "passed")]),
        ])
        result = TypeScriptJestJSONParser().parse(raw)
        assert "src/a.test.ts::a" in result["tests"]

    def test_fullname_fallback_from_ancestors(self):
        raw = _report([
            _suite("/repo/a.test.ts", [
                {"ancestorTitles": ["outer", "inner"], "title": "does it",
                 "status": "passed"},
            ]),
        ])
        result = TypeScriptJestJSONParser().parse(raw)
        assert "a.test.ts::outer inner does it" in result["tests"]

    def test_duplicate_id_failed_takes_priority(self):
        raw = _report([
            _suite("/repo/a.test.ts", [
                _assertion("each case", "passed"),
                _assertion("each case", "failed"),
            ]),
        ], success=False, numFailedTests=1)
        result = TypeScriptJestJSONParser().parse(raw)
        assert result["tests"]["a.test.ts::each case"] == "failed"

    def test_same_signature_as_go(self):
        from swebenchify.parsers import GoJSONParser
        go_result = GoJSONParser().parse("")
        ts_result = TypeScriptJestJSONParser().parse("")
        assert set(go_result.keys()) == set(ts_result.keys())


class TestNormalizeTypescriptF2p:
    def test_dedupe_and_sort(self):
        ids = ["b.test.ts::b", "a.test.ts::a", "b.test.ts::b"]
        assert normalize_typescript_f2p(ids) == ["a.test.ts::a", "b.test.ts::b"]

    def test_empty(self):
        assert normalize_typescript_f2p([]) == []
