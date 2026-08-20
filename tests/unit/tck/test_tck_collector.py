"""Unit tests for pytest TCK collector — mock JUnit XML → pytest items."""
import textwrap
import pytest


SAMPLE_JUNIT_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="utf-8"?>
    <testsuites>
      <testsuite name="Match1" tests="3" failures="1" skipped="1">
        <testcase classname="clauses/match/Match1.feature" name="[1] Match non-existent nodes returns empty" status="passed"/>
        <testcase classname="clauses/match/Match1.feature" name="[2] Matching all nodes" status="failed">
          <failure>AssertionError: mismatch</failure>
        </testcase>
        <testcase classname="clauses/match/Match1.feature" name="[3] wip scenario" status="skipped">
          <skipped/>
        </testcase>
      </testsuite>
    </testsuites>
""")


class TestParseBehaveJunit:
    def test_parse_returns_scenario_list(self):
        from tests.tck.conftest import parse_behave_junit
        scenarios = parse_behave_junit(SAMPLE_JUNIT_XML)
        assert len(scenarios) == 3

    def test_passed_scenario(self):
        from tests.tck.conftest import parse_behave_junit
        scenarios = parse_behave_junit(SAMPLE_JUNIT_XML)
        passed = [s for s in scenarios if s["status"] == "passed"]
        assert len(passed) == 1
        assert "[1]" in passed[0]["name"]

    def test_failed_scenario(self):
        from tests.tck.conftest import parse_behave_junit
        scenarios = parse_behave_junit(SAMPLE_JUNIT_XML)
        failed = [s for s in scenarios if s["status"] == "failed"]
        assert len(failed) == 1
        assert "mismatch" in failed[0]["message"]

    def test_skipped_scenario(self):
        from tests.tck.conftest import parse_behave_junit
        scenarios = parse_behave_junit(SAMPLE_JUNIT_XML)
        skipped = [s for s in scenarios if s["status"] == "skipped"]
        assert len(skipped) == 1

    def test_classname_preserved(self):
        from tests.tck.conftest import parse_behave_junit
        scenarios = parse_behave_junit(SAMPLE_JUNIT_XML)
        assert all("clauses/match/Match1.feature" in s["classname"] for s in scenarios)


class TestWipRegistry:
    def test_wip_match(self, tmp_path):
        from tests.tck.conftest import load_wip_registry
        wip = tmp_path / "wip.txt"
        wip.write_text(
            "# reason: not supported\n"
            "clauses/match/Match1.feature::Scenario: [3] wip scenario\n"
        )
        registry = load_wip_registry(str(wip))
        assert "clauses/match/Match1.feature::Scenario: [3] wip scenario" in registry

    def test_wip_empty(self, tmp_path):
        from tests.tck.conftest import load_wip_registry
        wip = tmp_path / "wip.txt"
        wip.write_text("# no entries yet\n")
        registry = load_wip_registry(str(wip))
        assert len(registry) == 0
