from types import SimpleNamespace

import server


def _request(*, authorization="", query=None):
    return SimpleNamespace(
        headers={"Authorization": authorization} if authorization else {},
        query_params=query or {},
    )


def test_header_bearer_is_accepted():
    request = _request(authorization="Bearer candidate-secret")
    assert server._request_has_valid_bearer(request, "candidate-secret") is True


def test_wrong_or_missing_header_is_rejected():
    assert server._request_has_valid_bearer(_request(), "candidate-secret") is False
    assert (
        server._request_has_valid_bearer(
            _request(authorization="Bearer wrong"),
            "candidate-secret",
        )
        is False
    )


def test_query_token_is_rejected_by_default():
    request = _request(query={"token": "candidate-secret"})
    assert server._request_has_valid_bearer(request, "candidate-secret") is False


def test_query_token_requires_explicit_legacy_opt_in():
    request = _request(query={"token": "candidate-secret"})
    assert (
        server._request_has_valid_bearer(
            request,
            "candidate-secret",
            allow_query_token=True,
        )
        is True
    )


def test_empty_expected_token_always_fails_closed():
    request = _request(authorization="Bearer anything")
    assert server._request_has_valid_bearer(request, "") is False
