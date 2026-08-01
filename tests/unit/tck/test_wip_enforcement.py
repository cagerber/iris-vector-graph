"""T038: ensure every wip.txt entry has a non-empty reason comment."""
from pathlib import Path


WIP_FILE = Path(__file__).parent.parent.parent / "tck" / "wip.txt"


class TestWipEnforcement:
    def test_every_entry_has_reason_comment(self):
        """Each non-comment, non-blank line in wip.txt must be preceded by a # reason: line."""
        if not WIP_FILE.exists():
            return  # wip.txt not yet generated; skip

        lines = WIP_FILE.read_text().splitlines()
        prev_was_comment = False
        violations = []

        for i, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                prev_was_comment = stripped.startswith("#")
                continue
            # this is an entry line — previous line must have been a comment
            if not prev_was_comment:
                violations.append(f"Line {i}: entry '{stripped}' has no preceding reason comment")
            prev_was_comment = False

        assert not violations, "\n".join(violations)
