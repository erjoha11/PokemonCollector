"""Set checklists (issue #368): the reference list of prints a set should
contain, owned or not. Read helpers and the track vocabulary; the seeding
(which talks to TCGdex) lives in set_checklist_seed.py.

A checklist is one `set_checklists` row per masterdata (language, set_code)
plus its members in `set_checklist_cards`, each a `master_cards` row on one
track. Whether a print is owned is never stored here: it's whether any
`Card` links to the member's master card (computed by whoever reads it).
See README "Master sets / checklists".
"""
from __future__ import annotations

from sqlalchemy.orm import Session, selectinload

from models import MasterCard, SetChecklist, SetChecklistCard

# The base print of each number, up to the set's official count (1-165)...
TRACK_MAIN = "main"
# ...and above it (166-210).
TRACK_SECRET = "secret"
# Reverse prints with a ball pattern.
TRACK_POKE_BALL = "poke_ball"
TRACK_MASTER_BALL = "master_ball"

# Track -> counts toward completion. Master Ball prints are listed but never
# count: the user doesn't collect them (epic #366, decision 3).
TRACKS = {
    TRACK_MAIN: True,
    TRACK_SECRET: True,
    TRACK_POKE_BALL: True,
    TRACK_MASTER_BALL: False,
}


def get_checklist(db: Session, language: str, set_code: str) -> SetChecklist | None:
    """The checklist for a masterdata (language, set_code), with its members
    and their master cards (and those cards' linked `Card`s) loaded, or None
    when the set has no checklist yet."""
    return (
        db.query(SetChecklist)
        .options(
            selectinload(SetChecklist.cards)
            .selectinload(SetChecklistCard.master_card)
            .selectinload(MasterCard.cards)
        )
        .filter(SetChecklist.language == language, SetChecklist.set_code == set_code)
        .one_or_none()
    )
