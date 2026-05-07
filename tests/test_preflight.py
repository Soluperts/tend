"""Boot-time preflight for the claude CLI. Logs a warning, never raises."""

from unittest.mock import MagicMock, patch


def test_preflight_returns_true_when_claude_ok():
    from tend.preflight import claude_cli_preflight

    fake_run = MagicMock(side_effect=[
        MagicMock(returncode=0, stdout="claude 1.2.3\n", stderr=""),
        MagicMock(returncode=0, stdout="logged in", stderr=""),
    ])
    with patch("tend.preflight.subprocess.run", fake_run), \
         patch("tend.preflight.shutil.which", return_value="/usr/bin/claude"):
        assert claude_cli_preflight() is True


def test_preflight_returns_false_when_claude_missing():
    from tend.preflight import claude_cli_preflight

    with patch("tend.preflight.shutil.which", return_value=None):
        assert claude_cli_preflight() is False


def test_preflight_returns_false_when_auth_status_fails():
    from tend.preflight import claude_cli_preflight

    fake_run = MagicMock(side_effect=[
        MagicMock(returncode=0, stdout="claude 1.2.3\n", stderr=""),
        MagicMock(returncode=1, stdout="", stderr="not logged in"),
    ])
    with patch("tend.preflight.subprocess.run", fake_run), \
         patch("tend.preflight.shutil.which", return_value="/usr/bin/claude"):
        assert claude_cli_preflight() is False


def test_preflight_does_not_raise_on_subprocess_error():
    from tend.preflight import claude_cli_preflight

    with patch("tend.preflight.shutil.which", return_value="/usr/bin/claude"), \
         patch("tend.preflight.subprocess.run", side_effect=OSError("boom")):
        assert claude_cli_preflight() is False
