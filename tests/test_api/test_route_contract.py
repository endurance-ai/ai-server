"""Provider-neutral public API route contract checks."""

from app.main import app


def test_native_app_web_routes_are_registered_without_telegram_routes() -> None:
    routes = {(route.path, method) for route in app.routes for method in (route.methods or ())}

    expected = {
        ("/v1/auth/social", "POST"),
        ("/v1/auth/refresh", "POST"),
        ("/v1/auth/logout", "POST"),
        ("/v1/chat/sessions", "GET"),
        ("/v1/chat/sessions", "POST"),
        ("/v1/chat/sessions/{session_id}", "PATCH"),
        ("/v1/chat/sessions/{session_id}", "DELETE"),
        ("/v1/chat/sessions/{session_id}/messages", "GET"),
        ("/v1/chat/sessions/{session_id}/messages", "POST"),
        ("/v1/chat/sessions/{session_id}/callback", "POST"),
    }

    assert expected <= routes
    assert not any("telegram" in route.path.lower() for route in app.routes)
