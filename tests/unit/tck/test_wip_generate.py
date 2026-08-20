"""Unit tests for wip_generate.py."""
import textwrap
import pytest


SAMPLE_JUNIT = textwrap.dedent("""\
    <?xml version="1.0" encoding="utf-8"?>
    <testsuites>
      <testsuite name="Match1" tests="2">
        <testcase classname="clauses/match/Match1.feature" name="[1] passing" status="passed"/>
        <testcase classname="clauses/match/Match1.feature" name="[2] failing" status="failed">
          <failure>AssertionError: mismatch</failure>
        </testcase>
      </testsuite>
    </testsuites>
""")


class TestWipGenerate:
    def test_generates_entry_for_failure(self, tmp_path):
        from tests.tck.wip_generate import generate_wip_from_junit

        out_file = tmp_path / "wip.txt"
        generate_wip_from_junit(SAMPLE_JUNIT, str(out_file))

        content = out_file.read_text()
        assert "clauses/match/Match1.feature::Scenario: [2] failing" in content

    def test_does_not_include_passing(self, tmp_path):
        from tests.tck.wip_generate import generate_wip_from_junit

        out_file = tmp_path / "wip.txt"
        generate_wip_from_junit(SAMPLE_JUNIT, str(out_file))

        content = out_file.read_text()
        assert "[1] passing" not in content

    def test_header_comment_present(self, tmp_path):
        from tests.tck.wip_generate import generate_wip_from_junit

        out_file = tmp_path / "wip.txt"
        generate_wip_from_junit(SAMPLE_JUNIT, str(out_file))

        content = out_file.read_text()
        assert content.startswith("#")

    def test_reason_comment_before_entry(self, tmp_path):
        from tests.tck.wip_generate import generate_wip_from_junit

        out_file = tmp_path / "wip.txt"
        generate_wip_from_junit(SAMPLE_JUNIT, str(out_file))

        lines = out_file.read_text().splitlines()
        entry_idx = next(
            i for i, l in enumerate(lines)
            if "clauses/match/Match1.feature::Scenario: [2] failing" in l
        )
        # line before entry must be a comment
        assert lines[entry_idx - 1].startswith("#"), (
            f"Expected comment before entry at line {entry_idx}, got: {lines[entry_idx-1]!r}"
        )
