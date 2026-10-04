"""static/money-split.js's splitEvenly (#312, #309): the browser-side even
split behind the New Order cart's "Distribute evenly" and a Facebook lot
linked to several cards. Run under node, since it's JavaScript; skipped
where node isn't installed (GitHub's ubuntu runners have it)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "static" / "money-split.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node isn't installed")


def split(amount: float, n: int) -> list[float]:
    code = f"const {{splitEvenly}} = require({json.dumps(str(SCRIPT))}); console.log(JSON.stringify(splitEvenly({json.dumps(amount)}, {n})));"
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


def test_last_share_takes_the_rounding():
    assert split(100, 3) == [33.33, 33.33, 33.34]


@pytest.mark.parametrize(
    "amount,n,expected",
    [
        (100, 4, [25, 25, 25, 25]),  # even splits are unchanged
        (90, 3, [30, 30, 30]),
        (200, 3, [66.67, 66.67, 66.66]),
        (0.05, 2, [0.03, 0.02]),
        (150.5, 1, [150.5]),
    ],
)
def test_shares_add_up_exactly(amount, n, expected):
    shares = split(amount, n)
    assert shares == expected
    assert round(sum(shares) * 100) == round(amount * 100)


def test_nothing_to_split_into():
    assert split(100, 0) == []
