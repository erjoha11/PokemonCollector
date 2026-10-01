import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bw_env import parse_env, render_env  # noqa: E402


def test_parse_env_skips_comments_blanks_and_keeps_raw_values():
    text = "# comment\n\nA=1\nexport B=x=y\nnot a pair\nC=\n"
    assert parse_env(text) == {"A": "1", "B": "x=y", "C": ""}


def test_render_env_fills_template_and_keeps_comments():
    template = "# Dropbox\nKEY=\nFOLDER=/Dex Exports\n"
    out = render_env(template, {"KEY": "secret"})
    assert out == "# Dropbox\nKEY=secret\nFOLDER=/Dex Exports\n"


def test_render_env_appends_vault_keys_missing_from_template():
    out = render_env("A=\n", {"A": "1", "EXTRA": "2"})
    assert out.startswith("A=1\n")
    assert out.rstrip().endswith("EXTRA=2")


def test_round_trip():
    values = {"DATABASE_URL": "postgresql://u:p@h:6543/db?x=1", "CRON_SECRET": "abc"}
    assert parse_env(render_env("", values)) == values
