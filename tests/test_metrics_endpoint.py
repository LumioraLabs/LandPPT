from fastapi.testclient import TestClient


def test_metrics_endpoint_requires_dedicated_bearer_token(monkeypatch):
    import landppt.main as main_module

    async def fake_startup():
        return True

    monkeypatch.setattr(main_module, "run_startup_initialization", fake_startup)
    monkeypatch.setattr(main_module.app_config, "metrics_bearer_token", "metrics-secret")
    client = TestClient(main_module.app, follow_redirects=False)

    client.get("/health")
    anonymous = client.get("/metrics")
    wrong = client.get(
        "/metrics", headers={"Authorization": "Bearer wrong-secret"}
    )
    response = client.get(
        "/metrics", headers={"Authorization": "Bearer metrics-secret"}
    )

    assert anonymous.status_code == 401
    assert wrong.status_code == 401
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "landppt_http_requests_total" in response.text
    rendered = main_module.metrics_collector.render_prometheus()
    assert "landppt_http_requests_total" in rendered
    assert "landppt_http_request_duration_seconds" in rendered
    assert "landppt_http_requests_in_progress" in rendered
