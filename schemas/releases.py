"""Request bodies for releases and fix versions (`routes/releases.py`, 09-restructuring.md §10).

A release is a space's promise of what ships together, with a start and a release date; an
issue's fix versions are the releases it ships in. Names and dates are validated here; the
rules that need the database — a name unique in its space, a target release of the same
space — are the controller's.
"""
from datetime import date
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

ReleaseState = Literal["unreleased", "released", "archived"]

#: `releases.name` is VARCHAR(100).
MAX_RELEASE_NAME = 100
#: A description is a paragraph or a short list of highlights. Ten thousand characters is
#: several pages, and it keeps the column inside MySQL's 65,535-byte TEXT even at four bytes
#: a character.
MAX_RELEASE_DESCRIPTION = 10_000
#: An issue ships in one release, sometimes back-ported to a couple more. Twenty covers every
#: supported branch of a product with a long support tail; a request naming more is a bulk
#: edit, which is a different tool.
MAX_FIX_VERSIONS = 20


def _clean_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise ValueError("A release needs a name.")
    return name


#: Stripped, and never blank: `min_length` alone would accept a name of three spaces.
ReleaseName = Annotated[str, Field(min_length=1, max_length=MAX_RELEASE_NAME),
                        AfterValidator(_clean_name)]


class ReleaseCreate(BaseModel):
    """A new, unreleased release of a space."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"name": "2.4", "description": "Saved cards and partial refunds.",
                      "start_date": "2026-10-05", "release_date": "2026-10-30"}],
    })

    name: ReleaseName = Field(description="Unique among the space's releases, ignoring case.")
    description: str | None = Field(default=None, max_length=MAX_RELEASE_DESCRIPTION,
                                    description="Plain text. Rendered as text, never as HTML.")
    start_date: date | None = Field(default=None, description="When work on it starts.")
    release_date: date | None = Field(default=None,
                                      description="When it is planned to ship. Not before "
                                                  "`start_date`.")

    @model_validator(mode="after")
    def _ordered(self) -> "ReleaseCreate":
        if self.start_date and self.release_date and self.release_date < self.start_date:
            raise ValueError("release_date is before start_date")
        return self


class ReleaseUpdate(BaseModel):
    """A partial update: only the fields present change. To clear a date send `null`.

    `state` moves a release between its states except into `released` from `unreleased` —
    releasing has its own route, because it sets the date and can move the open issues:
    `archived` from either state; `unreleased` from either (undoing a release clears
    `released_at`); `released` only back from `archived`, for a release that had shipped.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"release_date": "2026-11-06"}, {"state": "archived"}]})

    name: ReleaseName | None = Field(default=None,
                                     description="A new name, unique in the space ignoring "
                                                 "case. Cannot be null.")
    description: str | None = Field(default=None, max_length=MAX_RELEASE_DESCRIPTION,
                                    description="Plain text, or null to clear.")
    start_date: date | None = Field(default=None, description="A date, or null to clear.")
    release_date: date | None = Field(default=None,
                                      description="A date, or null to clear. Not before the "
                                                  "start date, whichever of the two moves.")
    state: ReleaseState | None = Field(default=None,
                                       description="See the model description for the moves "
                                                   "allowed. Cannot be null.")


class ReleaseShip(BaseModel):
    """Release it: mark it shipped, and optionally move what is still open to another
    release instead of shipping it unfinished."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"release_date": "2026-10-30",
                      "move_open_to_release_id": "<an unreleased release of the space>"}]})

    # Today rather than the planned date: a release that ships early or late shipped on the
    # day it was released, and the planned date is a plan. The SPA can pre-fill the plan.
    release_date: date | None = Field(default=None,
                                      description="The day it shipped. Default: today in the "
                                                  "workspace's time zone. Not before its "
                                                  "`start_date`.")
    move_open_to_release_id: str | None = Field(
        default=None, min_length=1, max_length=36,
        description="An unreleased release of the same space. Every issue in this release "
                    "whose stage is open (`todo`, `in_progress`) moves to it; done and "
                    "canceled ones stay. Omitted: the open issues stay in this release.")


class FixVersionsSet(BaseModel):
    """An issue's fix versions, replaced as a set. An empty list clears them."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"release_ids": ["<release id>"]}]})

    release_ids: list[str] = Field(
        max_length=MAX_FIX_VERSIONS,
        description="Every release the issue ships in afterwards; repeats are ignored. A "
                    "release being added must be live, of the issue's space, and not "
                    "archived (released is fine — recording a fix after the fact). One the "
                    "issue already has is kept as it is, archived or not.")
