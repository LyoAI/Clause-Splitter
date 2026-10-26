"""Every business route is mounted under the single API namespace."""

from api.base_fastapi import app
from api.prefix import API_PREFIX


def test_business_routes_live_under_namespace():
    api_paths = [r.path for r in app.routes if r.path.startswith("/api/")]
    assert api_paths
    assert all(path.startswith(API_PREFIX + "/") for path in api_paths)

    for suffix in ("/regulations", "/regulations/push", "/library/documents", "/jobs"):
        assert API_PREFIX + suffix in api_paths
