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


---

## 3. Price Cache

**File: `backend/app/market/cache.py`**

The central data hub. Data sources write to it; SSE streaming and portfolio valuation read from it. Thread-safe because the Massive client runs in `asyncio.to_thread()` while SSE reads happen on the async event loop.

```python
from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Bumped on every update

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        with self._lock:
            ts = timestamp or time.time()
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price
            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=round(previous_price, 2),
                timestamp=ts,
            )
            self._prices[ticker] = update
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        with self._lock:
            return dict(self._prices)

    def get_price(self, ticker: str) -> float | None:
        update = self.get(ticker)
        return update.price if update else None

    def remove(self, ticker: str) -> None:
        with self._lock:
            self._prices.pop(ticker, None)

    @property
    def version(self) -> int:
        return self._version
```

### Why a Version Counter?

The SSE loop polls the cache every ~500ms. Without a version counter, it would serialize and send all prices every tick — even when nothing changed (e.g., Massive API only updates every 15s). The version counter enables skip-when-unchanged:

```python
last_version = -1
while True:
    if price_cache.version != last_version:
        last_version = price_cache.version
        yield format_sse(price_cache.get_all())
    await asyncio.sleep(0.5)
```

### Why `threading.Lock` Instead of `asyncio.Lock`?

- The Massive client's synchronous `get_snapshot_all()` runs via `asyncio.to_thread()` in a real OS thread — `asyncio.Lock` would not protect against that.
- `threading.Lock` works correctly from both sync threads and the async event loop.
- Memory is bounded at O(tickers) since only the latest price per ticker is stored.


---

## 4. Abstract Interface

**File: `backend/app/market/interface.py`**

The strategy pattern contract. Both `SimulatorDataSource` and `MassiveDataSource` implement this ABC. Downstream code never knows which is active.

```python
from __future__ import annotations

from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the data source directly for prices —
    it reads from the cache.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])
        # ... app runs ...
        await source.add_ticker("TSLA")
        await source.remove_ticker("GOOGL")
        # ... app shutting down ...
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates. Starts a background task.
        Must be called exactly once."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop background task and release resources. Safe to call multiple times."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set. Also removes from PriceCache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

### Why Push-to-Cache Instead of Returning Prices?

The push model decouples timing. The simulator ticks at 500ms, Massive polls at 15s, but SSE always reads from the cache at its own 500ms cadence. The SSE layer doesn't need to know which data source is active or what its update interval is.

### Interface Guarantees

| Method | Idempotency | Thread Safety |
|--------|-------------|---------------|
| `start()` | Call once only | N/A — called at startup |
| `stop()` | Safe to call multiple times | N/A — called at shutdown |
| `add_ticker()` | No-op if already present | Safe from async context |
| `remove_ticker()` | No-op if not present | Safe from async context |
| `get_tickers()` | Always safe | Returns a copy |


---

## 5. Seed Prices & Ticker Parameters

**File: `backend/app/market/seed_prices.py`**

Constants only — no logic, no external imports. Shared by the simulator (initial prices, GBM params, correlation groups) and potentially by the Massive client as fallback prices.

```python
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00,  "GOOGL": 175.00, "MSFT": 420.00,
    "AMZN": 185.00,  "TSLA": 250.00,  "NVDA": 800.00,
    "META": 500.00,  "JPM": 195.00,   "V": 280.00,
    "NFLX": 600.00,
}

# Per-ticker GBM parameters (sigma = annualized volatility, mu = annualized drift)
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},   # High volatility
    "NVDA":  {"sigma": 0.40, "mu": 0.08},   # High volatility, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},   # Low volatility (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},   # Low volatility (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR = 0.6       # Tech stocks move together
INTRA_FINANCE_CORR = 0.5    # Finance stocks move together
CROSS_GROUP_CORR = 0.3      # Between sectors / unknown tickers
TSLA_CORR = 0.3             # TSLA does its own thing
```

### Parameter Rationale

| Ticker | Sigma | Why |
|--------|-------|-----|
| TSLA | 0.50 | Highest vol — meme stock energy, dramatic moves |
| NVDA | 0.40 | AI hype cycle, large intraday swings |
| NFLX | 0.35 | Earnings-driven vol |
| V, JPM | 0.17-0.18 | Stable blue-chips, low drama |
| Others | 0.20-0.30 | Moderate large-cap tech |

Dynamically added tickers (not in `SEED_PRICES`) get a random start price between $50-$300 and `DEFAULT_PARAMS`. They default to `CROSS_GROUP_CORR` (0.3) correlation with all other tickers.

