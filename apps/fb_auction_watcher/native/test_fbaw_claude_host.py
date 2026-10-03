"""Tests for the Claude bridge host (runs with the repo's `python -m pytest`, offline)."""

import importlib.util
import pathlib

import pytest

_spec = importlib.util.spec_from_file_location("fbaw_claude_host", pathlib.Path(__file__).with_name("fbaw_claude_host.py"))
host = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(host)

MSG = {"system": "s", "input": "i", "schema": {"type": "object"}, "task": "bid"}


@pytest.mark.parametrize("has_images", [False, True])
def test_command_allows_no_tools_connectors_or_settings(has_images):
    # Text from strangers on Facebook must not be able to make Claude act on anything.
    cmd = host.build_command("/bin/claude", MSG, has_images)
    i = cmd.index("--tools")
    assert cmd[i + 1] == ""
    assert "--strict-mcp-config" in cmd and "--mcp-config" not in cmd
    assert "--restricted" in cmd
    assert "--disallowedTools" in cmd


def test_model_falls_back_to_haiku_unless_allowed():
    cmd = host.build_command("c", {**MSG, "model": "sonnet"}, False)
    assert cmd[cmd.index("--model") + 1] == "sonnet"
    cmd = host.build_command("c", {**MSG, "model": "opus-anything"}, False)
    assert cmd[cmd.index("--model") + 1] == "haiku"


def test_photos_use_stream_json():
    cmd = host.build_command("c", MSG, True)
    assert cmd[cmd.index("--input-format") + 1] == "stream-json"


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://scontent.fosl6-1.fna.fbcdn.net/v/t39/1_n.jpg?x=1", True),
        ("https://fbcdn.net/a.jpg", True),
        ("http://scontent.fbcdn.net/a.jpg", False),  # Not https.
        ("https://evil.com/x.fbcdn.net/a.jpg", False),  # The host is evil.com.
        ("https://x.fbcdn.net.evil.com/a.jpg", False),
        ("https://www.facebook.com/photo/?fbid=1", False),
        ("file:///etc/passwd", False),
    ],
)
def test_only_facebook_cdn_images(url, ok):
    assert host.is_allowed_image_url(url) is ok
