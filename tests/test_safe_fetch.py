from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://[::1]/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.10.10.10/private",
        "http://metadata.google.internal/",
        "http://service.internal/",
        "http://user:password@example.com/",
        "file:///etc/passwd",
    ],
)
def test_validate_public_http_url_rejects_ssrf_targets(url):
    from landppt.utils.safe_fetch import UnsafeURLError, validate_public_http_url

    with pytest.raises(UnsafeURLError):
        validate_public_http_url(url)


def test_validate_public_http_url_accepts_public_urls():
    from landppt.utils.safe_fetch import validate_public_http_url

    assert validate_public_http_url("https://example.com/article?q=1") == (
        "https://example.com/article?q=1"
    )
    assert validate_public_http_url("https://8.8.8.8/") == "https://8.8.8.8/"


class _FakeContent:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self._chunks:
            yield chunk


@pytest.mark.asyncio
async def test_read_response_limited_enforces_declared_and_streamed_size():
    from landppt.utils.safe_fetch import ResponseTooLargeError, read_response_limited

    declared = SimpleNamespace(
        headers={"content-length": "11"}, content=_FakeContent([])
    )
    with pytest.raises(ResponseTooLargeError):
        await read_response_limited(declared, 10)

    streamed = SimpleNamespace(headers={}, content=_FakeContent([b"12345", b"678901"]))
    with pytest.raises(ResponseTooLargeError):
        await read_response_limited(streamed, 10)

    valid = SimpleNamespace(headers={}, content=_FakeContent([b"12345", b"67890"]))
    assert await read_response_limited(valid, 10) == b"1234567890"
