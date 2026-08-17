from __future__ import annotations

import urllib.request

import pytest

from capacity_check import NoRedirectHandler, request_once


@pytest.mark.parametrize(
    "location",
    ["https://other.intra.example/collect", "http://wiki.intra.example/downgrade"],
)
def test_capacity_cookie_redirects_are_never_followed(location: str):
    handler = NoRedirectHandler()
    request = urllib.request.Request(
        "https://wiki.intra.example/api/articles",
        headers={"Cookie": "__Host-wiki_session=secret-session"},
    )
    assert handler.redirect_request(request, None, 302, "Found", {}, location) is None


def test_capacity_cookie_rejects_non_https_initial_target():
    with pytest.raises(ValueError, match="HTTPS target"):
        request_once(
            "http://wiki.intra.example/api/articles",
            cookie="__Host-wiki_session=secret-session",
        )
