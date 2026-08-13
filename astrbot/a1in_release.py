"""Identity and update policy for A1in-maintained AstrBot releases."""

from __future__ import annotations

import os
from typing import Final

from astrbot import __version__

A1IN_RELEASE: Final = "a1in-v4.27.2.3"
A1IN_RELEASE_REVISION: Final = 3
A1IN_UPSTREAM_BASE_TAG: Final = f"v{__version__}"
A1IN_SOURCE_REVISION_ENV: Final = "A1IN_SOURCE_REVISION"
A1IN_ALLOW_OFFICIAL_UPDATES_ENV: Final = "A1IN_ALLOW_OFFICIAL_UPDATES"
A1IN_OFFICIAL_UPDATES_DISABLED_MESSAGE: Final = (
    "This A1in-managed AstrBot release does not install official AstrBot updates. "
    "Deploy a verified A1in image through the release process instead."
)


class A1inOfficialUpdateDisabledError(RuntimeError):
    """Raised when an A1in-managed runtime attempts an official self-update."""


def is_official_updates_enabled() -> bool:
    """Return whether the explicit maintainer-only official-update override is set.

    Returns:
        True only when the explicit environment override has the value ``"1"``.
    """

    return os.environ.get(A1IN_ALLOW_OFFICIAL_UPDATES_ENV, "").strip() == "1"


def ensure_official_updates_enabled() -> None:
    """Raise when official self-updates are disabled for this managed release.

    Raises:
        A1inOfficialUpdateDisabledError: If the explicit maintainer-only override
            is not enabled.
    """

    if not is_official_updates_enabled():
        raise A1inOfficialUpdateDisabledError(A1IN_OFFICIAL_UPDATES_DISABLED_MESSAGE)


def get_a1in_release_identity() -> dict[str, str | int | bool | None]:
    """Return safe runtime identity fields for an A1in release.

    Returns:
        Release, upstream compatibility, source revision, and update-policy fields.
    """

    source_revision = os.environ.get(A1IN_SOURCE_REVISION_ENV, "").strip() or None
    return {
        "a1in_release": A1IN_RELEASE,
        "a1in_release_revision": A1IN_RELEASE_REVISION,
        "a1in_upstream_base": A1IN_UPSTREAM_BASE_TAG,
        "a1in_source_revision": source_revision,
        "official_updates_enabled": is_official_updates_enabled(),
    }
