"""Tests for the A1in release identity and update policy."""

import pytest

from astrbot import __version__
from astrbot.a1in_release import (
    A1IN_ALLOW_OFFICIAL_UPDATES_ENV,
    A1IN_RELEASE,
    A1IN_RELEASE_REVISION,
    A1IN_SOURCE_REVISION_ENV,
    A1IN_UPSTREAM_BASE_TAG,
    A1inOfficialUpdateDisabledError,
    ensure_official_updates_enabled,
    get_a1in_release_identity,
    is_official_updates_enabled,
)


def test_a1in_release_identity_tracks_the_upstream_compatibility_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The checked-in identity must expose the declared A1in release safely."""

    monkeypatch.delenv(A1IN_SOURCE_REVISION_ENV, raising=False)

    identity = get_a1in_release_identity()

    assert A1IN_RELEASE == "a1in-v4.26.8.9"
    assert A1IN_RELEASE_REVISION == 9
    assert A1IN_UPSTREAM_BASE_TAG == f"v{__version__}"
    assert identity["a1in_release"] == A1IN_RELEASE
    assert identity["a1in_upstream_base"] == A1IN_UPSTREAM_BASE_TAG
    assert identity["a1in_source_revision"] is None


def test_official_updates_are_disabled_without_explicit_maintainer_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A managed release must fail closed before contacting official update feeds."""

    monkeypatch.setenv(A1IN_ALLOW_OFFICIAL_UPDATES_ENV, "0")

    assert is_official_updates_enabled() is False
    with pytest.raises(A1inOfficialUpdateDisabledError):
        ensure_official_updates_enabled()


def test_a1in_release_identity_reads_the_image_source_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A release image may expose the immutable source revision it was built from."""

    monkeypatch.setenv(A1IN_SOURCE_REVISION_ENV, "abc123def456")

    identity = get_a1in_release_identity()

    assert identity["a1in_source_revision"] == "abc123def456"
