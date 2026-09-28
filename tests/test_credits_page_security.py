from pathlib import Path


TEMPLATE = (
    Path(__file__).parents[1]
    / "src/landppt/web/templates/pages/account/user_credits.html"
)


def test_checkout_retry_reuses_request_id_until_server_success():
    source = TEMPLATE.read_text(encoding="utf-8")

    assert "sessionStorage.getItem(requestKeyStorageKey)" in source
    assert "sessionStorage.setItem(requestKeyStorageKey, idempotencyKey)" in source
    assert "sessionStorage.removeItem(requestKeyStorageKey)" in source
    assert source.index("sessionStorage.removeItem(requestKeyStorageKey)") > source.index(
        "if (!res.ok) throw"
    )


def test_api_text_is_rendered_as_text_not_html():
    source = TEMPLATE.read_text(encoding="utf-8")

    assert "alert.textContent = String(message || '')" in source
    assert "description.textContent = String(item.description || '')" in source
    assert "cell.textContent = String(value ?? '')" in source
    assert "tbody.replaceChildren(...rows)" in source
    assert "grid.innerHTML = Object.entries(data.pricing)" not in source
    assert "tbody.innerHTML = transactions.map" not in source
