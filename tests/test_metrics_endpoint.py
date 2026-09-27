from fastapi.testclient import TestClient


def test_metrics_endpoint_is_prometheus_text(monkeypatch):
    import landppt.main as main_module

    async def fake_startup():
        return True

    monkeypatch.setattr(main_module, "run_startup_initialization", fake_startup)
    client = TestClient(main_module.app, follow_redirects=False)

    client.get("/health")
    response = client.get("/metrics")

    assert response.status_code not in {200, 204}
    assert "landppt_http_requests_total" not in response.text
    rendered = main_module.metrics_collector.render_prometheus()
    assert "landppt_http_requests_total" in rendered
    assert "landppt_http_request_duration_seconds" in rendered
    assert "landppt_http_requests_in_progress" in rendered

