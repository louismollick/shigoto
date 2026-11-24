import pytest
from pytest import MonkeyPatch

from shigoto.sources import cloudflare

CLEARED = cloudflare.Clearance("http://proxy.test", "Firefox/159.0", {"cf_clearance": "ok"})


@pytest.fixture
def zip_clearance(monkeypatch: MonkeyPatch) -> cloudflare.Clearance:
    """ZipRecruiter configured with a clearance a search already solved, so detail fetches send requests
    until something invalidates it."""
    monkeypatch.setenv("SHIGOTO_ZIPRECRUITER_PROXY", CLEARED.proxy)
    monkeypatch.setenv("SHIGOTO_BYPARR_URL", "http://byparr.test")
    monkeypatch.setattr(cloudflare, "_current", CLEARED)
    monkeypatch.setattr(cloudflare, "apply", lambda session, cleared: None)  # sessions are plain Mocks
    return CLEARED
