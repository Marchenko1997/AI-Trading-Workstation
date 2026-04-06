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


---

## 6. GBM Simulator

**File: `backend/app/market/simulator.py`**

Two classes: `GBMSimulator` (pure math engine) and `SimulatorDataSource` (async wrapper that writes to `PriceCache`).

### 6.1 GBMSimulator — Core Engine

Each tick applies: `S(t+dt) = S(t) * exp((mu - 0.5*sigma^2)*dt + sigma*sqrt(dt)*Z)`

Where `dt = 0.5 / (252 * 6.5 * 3600) ≈ 8.48e-8` (500ms as fraction of trading year).

```python
class GBMSimulator:
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR

    def __init__(self, tickers: list[str], dt=DEFAULT_DT, event_probability=0.001):
        self._dt = dt
        self._event_prob = event_probability
        self._tickers, self._prices, self._params = [], {}, {}
        self._cholesky: np.ndarray | None = None
        for ticker in tickers:
            self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def step(self) -> dict[str, float]:
        """Advance all tickers one tick. Returns {ticker: new_price}."""
        n = len(self._tickers)
        if n == 0:
            return {}
        z = np.random.standard_normal(n)
        if self._cholesky is not None:
            z = self._cholesky @ z
        result = {}
        for i, ticker in enumerate(self._tickers):
            mu, sigma = self._params[ticker]["mu"], self._params[ticker]["sigma"]
            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z[i]
            self._prices[ticker] *= math.exp(drift + diffusion)
            # Random shock: ~0.1% chance per tick → event every ~50s with 10 tickers
            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= (1 + shock)
            result[ticker] = round(self._prices[ticker], 2)
        return result
```

Correlation uses Cholesky decomposition of a sector-based matrix. `_rebuild_cholesky()` is called on add/remove — O(n^2) but n < 50.


### 6.2 SimulatorDataSource — Async Wrapper

Wraps `GBMSimulator` in a background `asyncio.Task` and writes to `PriceCache`.

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache, update_interval=0.5, event_probability=0.001):
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        # Seed cache immediately so SSE has data on first tick
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def add_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.add_ticker(ticker)
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    async def _run_loop(self) -> None:
        while True:
            try:
                if self._sim:
                    for ticker, price in self._sim.step().items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

**Key behaviors**: immediate cache seeding on `start()`, graceful cancellation on `stop()`, exception resilience per-step (one bad tick doesn't kill the feed).


---

## 7. Massive API Client

**File: `backend/app/market/massive_client.py`**

Polls the Massive (Polygon.io) REST snapshot endpoint. Synchronous client runs in `asyncio.to_thread()` to avoid blocking the event loop.

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

class MassiveDataSource(MarketDataSource):
    def __init__(self, api_key: str, price_cache: PriceCache, poll_interval=15.0):
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)
        await self._poll_once()  # Immediate first poll
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")

    async def _poll_once(self) -> None:
        if not self._tickers or not self._client:
            return
        try:
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            for snap in snapshots:
                try:
                    self._cache.update(
                        ticker=snap.ticker,
                        price=snap.last_trade.price,
                        timestamp=snap.last_trade.timestamp / 1000.0,  # ms → s
                    )
                except (AttributeError, TypeError) as e:
                    logger.warning("Skipping snapshot for %s: %s",
                                   getattr(snap, "ticker", "???"), e)
        except Exception as e:
            logger.error("Massive poll failed: %s", e)

    def _fetch_snapshots(self) -> list:
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### Error Resilience

| Error | Behavior |
|-------|----------|
| 401 Unauthorized | Logged. Poller keeps running — user may fix `.env` and restart. |
| 429 Rate Limited | Logged. Retries automatically on next interval. |
| Network timeout | Logged. Cache retains last-known prices. |
| Malformed snapshot | Individual ticker skipped. Others still processed. |

All tickers are fetched in a **single API call** (`get_snapshot_all`) — critical for staying within the free tier's 5 req/min limit.


---

## 8. Factory

**File: `backend/app/market/factory.py`**

Selects the data source at startup based on environment variables.

```python
import os
from .cache import PriceCache
from .interface import MarketDataSource

def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Create the appropriate market data source.

    - MASSIVE_API_KEY set and non-empty → MassiveDataSource (real data)
    - Otherwise → SimulatorDataSource (GBM simulation)

    Returns an unstarted source. Caller must await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        from .massive_client import MassiveDataSource
        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        from .simulator import SimulatorDataSource
        logger.info("Market data source: GBM Simulator")
        return SimulatorDataSource(price_cache=price_cache)
```

### Usage

```python
from app.market import PriceCache, create_market_data_source

cache = PriceCache()
source = create_market_data_source(cache)
await source.start(["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
                     "NVDA", "META", "JPM", "V", "NFLX"])
```

### Selection Logic

| `MASSIVE_API_KEY` env var | Result |
|---------------------------|--------|
| Not set | `SimulatorDataSource` |
| Empty string / whitespace | `SimulatorDataSource` |
| Valid key | `MassiveDataSource` |

Imports are lazy — `massive` package is only imported when the API key is present. Simulator path requires only `numpy`.


---

## 9. SSE Streaming Endpoint

**File: `backend/app/market/stream.py`**

Long-lived HTTP connection that pushes price updates to the browser via `text/event-stream`. Uses a router factory to inject the `PriceCache` without globals.

```python
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from collections.abc import AsyncGenerator
from .cache import PriceCache

router = APIRouter(prefix="/api/stream", tags=["streaming"])

def create_stream_router(price_cache: PriceCache) -> APIRouter:
    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
            },
        )
    return router

async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    yield "retry: 1000\n\n"  # Browser auto-reconnects after 1s

    last_version = -1
    try:
        while True:
            if await request.is_disconnected():
                break
            current_version = price_cache.version
            if current_version != last_version:
                last_version = current_version
                prices = price_cache.get_all()
                if prices:
                    data = {t: u.to_dict() for t, u in prices.items()}
                    yield f"data: {json.dumps(data)}\n\n"
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
```

### Client-Side Usage

```javascript
const source = new EventSource("/api/stream/prices");
source.onmessage = (event) => {
    const prices = JSON.parse(event.data);
    // prices = { "AAPL": { ticker, price, previous_price, change, direction, ... }, ... }
    updateWatchlist(prices);
};
```

### SSE Event Format

```
retry: 1000

data: {"AAPL":{"ticker":"AAPL","price":191.23,"previous_price":191.20,...},...}

data: {"AAPL":{"ticker":"AAPL","price":191.25,"previous_price":191.23,...},...}
```

- Version-based change detection skips sends when nothing changed (important for Massive's 15s intervals)
- `retry: 1000` tells the browser to reconnect after 1 second on disconnect
- `X-Accel-Buffering: no` prevents nginx from buffering the stream


---

## 10. FastAPI Lifecycle Integration

Wire up market data using FastAPI's `lifespan` context manager.

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.market import PriceCache, create_market_data_source, create_stream_router

# Module-level references (accessed by route handlers)
price_cache = PriceCache()
market_source = None

DEFAULT_TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
                   "NVDA", "META", "JPM", "V", "NFLX"]

@asynccontextmanager
async def lifespan(app: FastAPI):
    global market_source
    # --- Startup ---
    market_source = create_market_data_source(price_cache)
    # Load tickers from DB watchlist (fallback to defaults)
    tickers = await load_watchlist_tickers() or DEFAULT_TICKERS
    await market_source.start(tickers)

    yield  # App is running

    # --- Shutdown ---
    await market_source.stop()

app = FastAPI(title="FinAlly", lifespan=lifespan)

# Register SSE streaming router
stream_router = create_stream_router(price_cache)
app.include_router(stream_router)
```

### Startup Sequence

1. Create `PriceCache` (empty)
2. `create_market_data_source(cache)` — reads `MASSIVE_API_KEY`, returns unstarted source
3. Load watchlist tickers from SQLite (or use defaults on first run)
4. `await source.start(tickers)` — seeds cache, starts background task
5. SSE endpoint is ready — first client gets data immediately

### Shutdown Sequence

1. `await source.stop()` — cancels background task, awaits clean exit
2. `PriceCache` is garbage collected (in-memory only, no persistence needed)

### Watchlist Sync

When the user adds/removes a ticker via API or chat:

```python
@router.post("/api/watchlist")
async def add_to_watchlist(body: AddTickerRequest):
    # 1. Persist to SQLite
    await db_add_watchlist(body.ticker)
    # 2. Tell the data source to start tracking
    await market_source.add_ticker(body.ticker)

@router.delete("/api/watchlist/{ticker}")
async def remove_from_watchlist(ticker: str):
    await db_remove_watchlist(ticker)
    await market_source.remove_ticker(ticker)  # Also clears from PriceCache
```


---

## 11. Watchlist Coordination

The watchlist is the bridge between the database (persistent) and the market data source (in-memory). Three actors can modify it:

### Modification Sources

| Actor | How | Example |
|-------|-----|---------|
| User (UI) | `POST /api/watchlist` / `DELETE /api/watchlist/{ticker}` | Click "Add PYPL" |
| AI Chat | LLM structured output `watchlist_changes` | "Add PYPL to your watchlist" |
| App startup | Load from SQLite `watchlist` table | Initial 10 default tickers |

### Flow: Adding a Ticker

```
User/AI request
    │
    ▼
API handler validates ticker (uppercase, strip whitespace)
    │
    ├──→ INSERT into SQLite watchlist table (persist)
    │
    ├──→ await market_source.add_ticker("PYPL")
    │       │
    │       ├── Simulator: adds to GBMSimulator, seeds cache, rebuilds Cholesky
    │       └── Massive: appends to ticker list, appears on next poll
    │
    └──→ Return success to client
```

### Flow: Removing a Ticker

```
User/AI request
    │
    ▼
API handler
    │
    ├──→ DELETE from SQLite watchlist table
    │
    ├──→ await market_source.remove_ticker("PYPL")
    │       │
    │       ├── Simulator: removes from GBMSimulator, rebuilds Cholesky
    │       └── Massive: removes from ticker list
    │
    ├──→ price_cache.remove("PYPL")  (handled inside remove_ticker)
    │
    └──→ Return success — SSE stops including PYPL on next tick
```

### Edge Cases

| Case | Behavior |
|------|----------|
| Add duplicate ticker | No-op at both DB (UNIQUE constraint) and source level |
| Remove non-existent ticker | No-op — `remove_ticker` is idempotent |
| Add ticker while Massive is between polls | Ticker appears on next poll cycle |
| Remove ticker with open position | Watchlist removal succeeds; position remains in portfolio |
| Ticker not in `SEED_PRICES` | Simulator assigns random price $50-$300 and `DEFAULT_PARAMS` |


---

## 12. Testing Strategy

Tests live in `backend/tests/market/`. 73 tests across 6 modules, 84% coverage.

### Test Modules

| Module | Tests | What it covers |
|--------|-------|----------------|
| `test_models.py` | 11 | PriceUpdate creation, computed properties, `to_dict()`, edge cases (zero price) |
| `test_cache.py` | 13 | Update, get, get_all, remove, version counter, `__contains__`, `__len__` |
| `test_simulator.py` | 17 | GBM step math, add/remove ticker, Cholesky rebuild, shock events, seed prices |
| `test_simulator_source.py` | 10 | Start/stop lifecycle, cache seeding, add/remove integration |
| `test_factory.py` | 7 | Env var selection logic, returns correct type, unstarted state |
| `test_massive.py` | 13 | Poll parsing, timestamp conversion, malformed snapshot handling, error resilience |

### Key Test Patterns

```python
# test_models.py — computed properties
def test_direction_up():
    update = PriceUpdate(ticker="AAPL", price=191.0, previous_price=190.0)
    assert update.direction == "up"
    assert update.change == 1.0

# test_cache.py — version increments
def test_version_increments():
    cache = PriceCache()
    assert cache.version == 0
    cache.update("AAPL", 190.0)
    assert cache.version == 1

# test_simulator.py — prices stay positive
def test_prices_never_negative():
    sim = GBMSimulator(["AAPL", "TSLA"])
    for _ in range(1000):
        prices = sim.step()
        assert all(p > 0 for p in prices.values())

# test_factory.py — env-based selection
def test_simulator_when_no_key(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    source = create_market_data_source(PriceCache())
    assert isinstance(source, SimulatorDataSource)
```

### Coverage Gaps (Acceptable)

| Module | Coverage | Why |
|--------|----------|-----|
| `massive_client.py` | 56% | Real API methods mocked; full coverage requires live API |
| `stream.py` | 31% | SSE generator needs running ASGI server (`httpx.AsyncClient`) |


---

## 13. Error Handling & Edge Cases

### Simulator Errors

| Scenario | Handling |
|----------|----------|
| `step()` throws exception | Caught in `_run_loop`, logged, loop continues next tick |
| Cholesky decomposition fails | Only possible with invalid correlation matrix (won't happen with our constants) |
| Price goes to extreme value | GBM is multiplicative (`exp()`), so prices stay positive. Rounding to 2 decimals caps precision |
| Zero tickers | `step()` returns `{}` — no crash, no cache writes |

### Massive API Errors

| Scenario | Handling |
|----------|----------|
| 401 Invalid API key | Logged as error. Poller retries next interval (user may fix `.env`) |
| 429 Rate limited | Logged. Next poll after `poll_interval` seconds |
| Network timeout | Logged. Cache retains last-known prices |
| Malformed snapshot (missing `last_trade`) | Individual ticker skipped via `AttributeError` catch. Others processed |
| API returns empty list | No cache updates. SSE keeps streaming stale data |
| Market closed | `last_trade.price` reflects last traded price (may be after-hours) |

### Cache Edge Cases

| Scenario | Handling |
|----------|----------|
| First update for a ticker | `previous_price = price`, direction = `flat` |
| `get_price()` for unknown ticker | Returns `None` |
| Concurrent reads/writes | `threading.Lock` protects all access |
| Remove ticker not in cache | `pop(ticker, None)` — no-op, no error |

### SSE Edge Cases

| Scenario | Handling |
|----------|----------|
| Client disconnects | Detected via `request.is_disconnected()`, generator exits cleanly |
| No price data yet | Empty cache → nothing sent. Client waits for first `data:` event |
| Multiple SSE clients | Each gets independent generator, all read from same cache |
| Cache unchanged between ticks | Version check skips send — no redundant payloads |

### Watchlist Edge Cases

| Scenario | Handling |
|----------|----------|
| Add invalid/nonexistent symbol | Simulator: random seed price. Massive: no snapshot returned, silently skipped |
| Add ticker already in watchlist | No-op at both DB (UNIQUE constraint) and source level |
| Remove ticker with open position | Allowed — position persists, just no live price updates |


---

## 14. Configuration Summary

### Environment Variables

| Variable | Required | Default | Effect |
|----------|----------|---------|--------|
| `MASSIVE_API_KEY` | No | (empty) | If set → real market data via Polygon.io. If empty → GBM simulator |

### Tunable Constants

| Constant | Location | Default | Description |
|----------|----------|---------|-------------|
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` s | Simulator tick rate |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` s | Massive API poll rate (free tier safe) |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Chance of random shock per tick per ticker |
| `DEFAULT_DT` | `GBMSimulator` | `~8.48e-8` | Time step as fraction of trading year |
| SSE interval | `_generate_events` | `0.5` s | How often SSE checks cache for changes |
| SSE retry | `_generate_events` | `1000` ms | Browser reconnect delay on disconnect |

### Dependencies

| Package | Version | Used By |
|---------|---------|---------|
| `fastapi` | >=0.115.0 | SSE endpoint, API routes |
| `uvicorn[standard]` | >=0.32.0 | ASGI server |
| `numpy` | >=2.0.0 | GBM simulator (Cholesky, random normals) |
| `massive` | >=1.0.0 | Polygon.io REST client (only when `MASSIVE_API_KEY` set) |

### Quick Start

```python
from app.market import PriceCache, create_market_data_source, create_stream_router

# 1. Create shared cache
cache = PriceCache()

# 2. Create source (reads MASSIVE_API_KEY automatically)
source = create_market_data_source(cache)

# 3. Start with initial tickers
await source.start(["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
                     "NVDA", "META", "JPM", "V", "NFLX"])

# 4. Read prices anywhere in the app
price = cache.get_price("AAPL")       # float or None
update = cache.get("AAPL")            # PriceUpdate or None
all_prices = cache.get_all()          # dict[str, PriceUpdate]

# 5. Dynamic watchlist management
await source.add_ticker("PYPL")
await source.remove_ticker("NFLX")

# 6. Clean shutdown
await source.stop()
```


---

## 6 Addendum: Cholesky Correlation Detail

The GBM simulator's key differentiator is **correlated** random moves across tickers. This section covers the implementation omitted from Section 6.

### Correlation Matrix Construction

```python
def _rebuild_cholesky(self) -> None:
    n = len(self._tickers)
    if n <= 1:
        self._cholesky = None
        return
    corr = np.eye(n)
    for i in range(n):
        for j in range(i + 1, n):
            rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
            corr[i, j] = rho
            corr[j, i] = rho
    self._cholesky = np.linalg.cholesky(corr)
```

### Pairwise Correlation Rules

```python
@staticmethod
def _pairwise_correlation(t1: str, t2: str) -> float:
    tech = CORRELATION_GROUPS["tech"]
    finance = CORRELATION_GROUPS["finance"]
    if t1 == "TSLA" or t2 == "TSLA":
        return TSLA_CORR           # 0.3 — TSLA does its own thing
    if t1 in tech and t2 in tech:
        return INTRA_TECH_CORR     # 0.6
    if t1 in finance and t2 in finance:
        return INTRA_FINANCE_CORR  # 0.5
    return CROSS_GROUP_CORR        # 0.3
```

### How Correlation Applies Each Tick

```
Z_independent = [z1, z2, ..., zn]    # n independent N(0,1) draws
Z_correlated  = L @ Z_independent     # L = cholesky(correlation_matrix)
```

After this transform, `Z_correlated[i]` and `Z_correlated[j]` have correlation `rho(i,j)`. When AAPL draws a large positive Z, MSFT's Z is pulled positive too (rho=0.6), while JPM's is only weakly pulled (rho=0.3).

### Example: 3-Ticker Correlation Matrix

For tickers `[AAPL, MSFT, JPM]`:

```
     AAPL  MSFT  JPM
AAPL [1.0   0.6   0.3]
MSFT [0.6   1.0   0.3]
JPM  [0.3   0.3   1.0]
```

Cholesky `L` of this matrix ensures `L @ L^T = C`. Independent draws multiplied by `L` produce the desired correlations — tech moves together, finance diverges.

### Dynamic Ticker Management

- `add_ticker()`: appends ticker, assigns seed price + params, rebuilds Cholesky
- `remove_ticker()`: removes from list/dicts, rebuilds Cholesky
- Rebuild is O(n^2) but n < 50 tickers — negligible cost

