# Market Data Backend — Detailed Design

Implementation-ready design for the FinAlly market data subsystem. Covers the unified interface, in-memory price cache, GBM simulator, Massive API client, SSE streaming endpoint, and FastAPI lifecycle integration.

Everything lives under `backend/app/market/`.

---

## Table of Contents

1. [File Structure](#1-file-structure)
2. [Data Model — `models.py`](#2-data-model)
3. [Price Cache — `cache.py`](#3-price-cache)
4. [Abstract Interface — `interface.py`](#4-abstract-interface)
5. [Seed Prices & Ticker Parameters — `seed_prices.py`](#5-seed-prices--ticker-parameters)
6. [GBM Simulator — `simulator.py`](#6-gbm-simulator)
7. [Massive API Client — `massive_client.py`](#7-massive-api-client)
8. [Factory — `factory.py`](#8-factory)
9. [SSE Streaming Endpoint — `stream.py`](#9-sse-streaming-endpoint)
10. [FastAPI Lifecycle Integration](#10-fastapi-lifecycle-integration)
11. [Watchlist Coordination](#11-watchlist-coordination)
12. [Testing Strategy](#12-testing-strategy)
13. [Error Handling & Edge Cases](#13-error-handling--edge-cases)
14. [Configuration Summary](#14-configuration-summary)

---

## 1. File Structure

```
backend/
  app/
    market/
      __init__.py             # Re-exports: PriceUpdate, PriceCache, MarketDataSource,
                              #   create_market_data_source, create_stream_router
      models.py               # PriceUpdate frozen dataclass
      cache.py                # PriceCache (thread-safe in-memory store with version counter)
      interface.py            # MarketDataSource ABC
      seed_prices.py          # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS, CORRELATION_GROUPS
      simulator.py            # GBMSimulator + SimulatorDataSource
      massive_client.py       # MassiveDataSource (Polygon.io REST poller)
      factory.py              # create_market_data_source() — env-based strategy selection
      stream.py               # SSE endpoint (FastAPI router factory)
```

Each file has a single responsibility. The `__init__.py` re-exports the public API so downstream code imports from `app.market` without reaching into submodules:

```python
from app.market import PriceCache, create_market_data_source, create_stream_router
```

### Data Flow

```
MarketDataSource (ABC)
├── SimulatorDataSource  →  GBM engine, ticks every 500ms
└── MassiveDataSource    →  Polygon.io REST, polls every 15s
        │
        ▼
   PriceCache (thread-safe, in-memory, version-counted)
        │
        ├──→ SSE stream endpoint  GET /api/stream/prices
        ├──→ Portfolio valuation   cache.get_price("AAPL")
        └──→ Trade execution       cache.get_price("AAPL")
```

Producers (data sources) write to the cache on their own schedule. Consumers (SSE, portfolio, trades) read from the cache independently. No direct coupling between producer and consumer timing.


---

## 2. Data Model

**File: `backend/app/market/models.py`**

`PriceUpdate` is the only data structure that leaves the market data layer. Every downstream consumer — SSE streaming, portfolio valuation, trade execution — works exclusively with this type.

```python
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""

    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        """Absolute price change from previous update."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percentage change from previous update."""
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat'."""
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        """Serialize for JSON / SSE transmission."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "direction": self.direction,
        }
```

### Design Decisions

- **`frozen=True`**: Immutable value objects — safe to share across async tasks without copying.
- **`slots=True`**: Memory optimization; many instances created per second.
- **Computed properties** (`change`, `direction`, `change_percent`): Derived from stored fields so they can never be stale or inconsistent.
- **`to_dict()`**: Single serialization point used by both SSE endpoint and REST API responses.
- **Stored fields are minimal** (4 fields). Everything else is computed. This keeps the constructor simple and eliminates the risk of passing inconsistent values.

