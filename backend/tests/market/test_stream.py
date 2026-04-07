"""Tests for the SSE streaming endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.market.cache import PriceCache
from app.market.stream import _generate_events, create_stream_router


class TestCreateStreamRouter:
    """Tests for the create_stream_router factory."""

    def test_returns_router_with_prices_route(self):
        """Router should expose a GET /api/stream/prices route."""
        cache = PriceCache()
        router = create_stream_router(cache)
        routes = [r.path for r in router.routes]
        assert "/api/stream/prices" in routes

    def test_each_call_returns_fresh_router(self):
        """Calling factory twice should return two distinct routers."""
        cache = PriceCache()
        r1 = create_stream_router(cache)
        r2 = create_stream_router(cache)
        assert r1 is not r2

    def test_fresh_router_has_exactly_one_prices_route(self):
        """No duplicate route registration even if called twice."""
        cache = PriceCache()
        router = create_stream_router(cache)
        prices_routes = [r for r in router.routes if r.path == "/api/stream/prices"]
        assert len(prices_routes) == 1


def _make_request(disconnected_after: int = 999) -> MagicMock:
    """Create a mock Request that disconnects after N checks."""
    request = MagicMock()
    request.client = MagicMock()
    request.client.host = "127.0.0.1"
    call_count = {"n": 0}

    async def is_disconnected():
        call_count["n"] += 1
        return call_count["n"] > disconnected_after

    request.is_disconnected = is_disconnected
    return request


@pytest.mark.asyncio
class TestGenerateEvents:
    """Tests for the _generate_events async generator."""

    async def test_first_event_is_retry_directive(self):
        """First yielded event should be the SSE retry directive."""
        cache = PriceCache()
        request = _make_request(disconnected_after=0)
        events = []
        async for event in _generate_events(cache, request, interval=0):
            events.append(event)
            break

        assert events[0] == "retry: 1000\n\n"

    async def test_emits_data_when_prices_available(self):
        """Should emit a data event when the cache has prices."""
        cache = PriceCache()
        cache.update("AAPL", 190.50)

        request = _make_request(disconnected_after=2)
        events = []
        async for event in _generate_events(cache, request, interval=0):
            events.append(event)

        data_events = [e for e in events if e.startswith("data:")]
        assert len(data_events) >= 1
        assert "AAPL" in data_events[0]
        assert "190.5" in data_events[0]

    async def test_stops_on_disconnect(self):
        """Generator should stop when client disconnects."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)

        request = _make_request(disconnected_after=1)
        events = []
        async for event in _generate_events(cache, request, interval=0):
            events.append(event)

        # Should have terminated (not an infinite loop)
        assert len(events) < 100

    async def test_no_data_event_when_cache_empty(self):
        """Should not emit a data event when the cache is empty."""
        cache = PriceCache()
        request = _make_request(disconnected_after=2)
        events = []
        async for event in _generate_events(cache, request, interval=0):
            events.append(event)

        data_events = [e for e in events if e.startswith("data:")]
        assert len(data_events) == 0

    async def test_no_duplicate_events_when_version_unchanged(self):
        """Should not re-emit data if version hasn't changed."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)

        # Disconnect after 5 ticks — version won't change between ticks
        request = _make_request(disconnected_after=5)
        events = []
        async for event in _generate_events(cache, request, interval=0):
            events.append(event)

        data_events = [e for e in events if e.startswith("data:")]
        # Only one data event despite multiple ticks (version didn't change)
        assert len(data_events) == 1

    async def test_emits_keepalive_when_idle(self):
        """Should emit a keepalive comment after the keepalive interval."""
        cache = PriceCache()
        # Use a very short keepalive by passing a tiny interval and
        # patching _KEEPALIVE_INTERVAL isn't easy, so we verify the
        # keepalive constant logic: with interval=0 and keepalive_ticks=1
        # the keepalive fires every tick when version is unchanged.
        import app.market.stream as stream_module

        original = stream_module._KEEPALIVE_INTERVAL
        stream_module._KEEPALIVE_INTERVAL = 0  # Fires every tick
        try:
            request = _make_request(disconnected_after=5)
            events = []
            async for event in _generate_events(cache, request, interval=0):
                events.append(event)

            keepalive_events = [e for e in events if e.startswith(": keepalive")]
            assert len(keepalive_events) >= 1
        finally:
            stream_module._KEEPALIVE_INTERVAL = original

    async def test_data_event_contains_all_tickers(self):
        """Data event should include all tickers in the cache."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("GOOGL", 175.00)
        cache.update("MSFT", 420.00)

        request = _make_request(disconnected_after=2)
        events = []
        async for event in _generate_events(cache, request, interval=0):
            events.append(event)

        data_events = [e for e in events if e.startswith("data:")]
        assert len(data_events) >= 1
        payload = data_events[0]
        assert "AAPL" in payload
        assert "GOOGL" in payload
        assert "MSFT" in payload

    async def test_no_client_host(self):
        """Should handle request with no client gracefully."""
        cache = PriceCache()
        request = _make_request(disconnected_after=0)
        request.client = None  # No client info

        events = []
        async for event in _generate_events(cache, request, interval=0):
            events.append(event)
            break

        assert events[0] == "retry: 1000\n\n"
