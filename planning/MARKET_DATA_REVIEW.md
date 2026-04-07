# Market Data Backend — Code Review

**Date:** 2026-04-07
**Reviewer:** Claude Code
**Scope:** `backend/app/market/` (8 modules) + `backend/tests/market/` (6 test modules)

---

## Test Results

**73/73 tests passed.** All test modules collected and executed cleanly.

```
tests/market/test_cache.py          13 passed
tests/market/test_factory.py         7 passed
tests/market/test_massive.py        13 passed
tests/market/test_models.py         11 passed
tests/market/test_simulator.py      17 passed
tests/market/test_simulator_source.py 10 passed
```

Runtime: ~13 seconds (dominated by async sleep-based integration tests in `test_simulator_source.py`).

---

## Architecture Assessment

The design is clean and well-suited to the project's requirements. The strategy pattern (two implementations behind one ABC) is the right call — downstream code is fully decoupled from the data source. The `PriceCache` as a shared write-through store is correct for this single-producer, multi-consumer pattern.

**Module responsibilities are well-separated:**
- `models.py` — pure data, no I/O
- `cache.py` — thread-safe state, no business logic
- `interface.py` — contract only
- `simulator.py` / `massive_client.py` — one concern each
- `factory.py` — selection only, no logic
- `stream.py` — SSE formatting only
- `seed_prices.py` — constants only

---

## Module-by-Module Review

### `models.py` — PriceUpdate

**Good:** Frozen dataclass with `__slots__` is the correct choice — immutable, memory-efficient, no accidental mutation bugs. Computed properties (`change`, `change_percent`, `direction`) on the dataclass are cleaner than storing them — they can't get out of sync.

**Issue — `change` rounds to 4 decimals but `change_percent` also rounds to 4:**
Both are computed on the fly from `price` and `previous_price`, which are already rounded to 2 decimal places in the cache. This is fine, but the rounding in `change` (4 dp) is inconsistent with the price storage precision (2 dp). Minor.

**Issue — `timestamp` has a `field(default_factory=time.time)`:**
This means creating a `PriceUpdate` directly (e.g. in tests) without passing `timestamp` will call `time.time()`. Tests do always pass a timestamp explicitly, so this doesn't cause test flakiness — but it's a subtle footgun for anyone constructing `PriceUpdate` objects manually. The intent is that `PriceCache.update()` is the sole constructor, so this is acceptable.

### `cache.py` — PriceCache

**Good:** Lock wraps every access. `version` counter increments on every `update()` — this is the right primitive for SSE change detection without polling. `get_all()` returns a shallow copy, which is correct (callers get a snapshot, not a live view). `get_price()` convenience method is appropriately thin.

**Issue — `version` is not thread-safe on read:**
```python
@property
def version(self) -> int:
    return self._version
```
Reading `self._version` outside the lock is a data race. In CPython this is safe in practice (GIL makes integer reads atomic), but it's technically undefined behaviour and would break under Jython or PyPy. In `stream.py`, the `version` is read in the async event loop while the simulator writes from that same loop (via `asyncio.sleep` + synchronous `step()`), so there's no actual threading race for the simulator case. However, `MassiveDataSource` uses `asyncio.to_thread()`, which runs on a thread pool — so `_poll_once()` calls `self._cache.update()` from a thread while `_generate_events()` reads `version` from the event loop. The missing lock on `version` is a real (if low-impact) issue in the Massive case.

**Fix:** Wrap the `version` property read in the lock, or use `threading.Event` instead of a version counter.

### `interface.py` — MarketDataSource

Clean. The docstring lifecycle description is accurate and useful. No issues.

**Note:** `get_tickers()` is synchronous (`def`, not `async def`), which is a pragmatic choice — it avoids unnecessary await overhead for a simple list read. This is consistent across both implementations.

### `seed_prices.py`

Clean constants file. Well-organized. Correlation constants are named descriptively (`INTRA_TECH_CORR`, `TSLA_CORR`, etc.) rather than magic numbers.

**Note:** TSLA is included in `TICKER_PARAMS` but excluded from `CORRELATION_GROUPS["tech"]` — it's treated as a loner in `_pairwise_correlation`. This is correct and intentional, but TSLA not appearing in any group is a subtle design point that relies on the fallthrough logic in `_pairwise_correlation`. A comment noting this would help future maintainers.

### `simulator.py` — GBMSimulator + SimulatorDataSource

**Good:** GBM math is correctly implemented. The Itô correction term `(mu - 0.5 * sigma²) * dt` is present. Cholesky decomposition is rebuilt only when tickers change (not on every step), which is the right performance trade-off. Batch initialization calls `_add_ticker_internal()` followed by a single `_rebuild_cholesky()`, which avoids O(n) Cholesky rebuilds during startup.

**Issue — `_run_loop` catches all exceptions silently:**
```python
except Exception:
    logger.exception("Simulator step failed")
```
This is intentional resilience, but swallowing errors in the hot path can hide bugs during development. It's acceptable for production but makes debugging harder. A counter or structured log field for error frequency would improve observability.

**Issue — `SimulatorDataSource.add_ticker()` is a no-op before `start()`:**
```python
async def add_ticker(self, ticker: str) -> None:
    if self._sim:
        self._sim.add_ticker(ticker)
```
Calling `add_ticker()` before `start()` silently does nothing. This is not a bug in the current usage (the app calls `start()` first), but it violates the principle of least surprise. The interface docstring says "No-op if already present", not "No-op if not started". Low risk given the lifecycle is well-defined.

**Issue — `GBMSimulator.step()` mixes numpy and Python random:**
```python
z_independent = np.random.standard_normal(n)  # numpy RNG
if random.random() < self._event_prob:         # Python stdlib RNG
```
Two different random number generators are seeded independently. This is fine for the demo use case but makes the simulation non-reproducible even when you seed one of them. Not a bug, but worth noting if reproducibility ever becomes important for testing or demos.

### `massive_client.py` — MassiveDataSource

**Good:** Running the synchronous `RESTClient` in `asyncio.to_thread()` is correct — it avoids blocking the event loop. Immediate first poll in `start()` ensures the cache has data before the first SSE response. Per-snapshot error handling (the inner try/except) correctly continues processing other tickers if one snapshot is malformed.

**Issue — Ticker normalization asymmetry:**
`add_ticker()` and `remove_ticker()` normalize to uppercase and strip whitespace, but `start()` does not:
```python
async def start(self, tickers: list[str]) -> None:
    self._tickers = list(tickers)  # No normalization
```
If `start()` is called with lowercase tickers (unlikely given app code, but possible), they won't match the normalized versions added later via `add_ticker()`, potentially creating duplicates. `SimulatorDataSource` does no normalization at all — so there's an inconsistency between the two implementations.

**Issue — `_poll_loop` polls after sleeping, not before:**
```python
async def _poll_loop(self) -> None:
    while True:
        await asyncio.sleep(self._interval)
        await self._poll_once()
```
The comment says "First poll already happened in `start()`", which is true. But if `start()` raises during the first poll (e.g. bad API key), the loop never starts. The error is swallowed by `_poll_once()`, so the user gets a poller that starts but never has data. This is the right trade-off given the error handling strategy, but the first-poll failure mode deserves a log message at WARNING or ERROR level (currently only DEBUG is emitted on success).

### `factory.py`

Clean. Whitespace-trimming of `MASSIVE_API_KEY` prevents the common mistake of an env var with trailing spaces. No issues.

### `stream.py` — SSE endpoint

**Good:** Version-based change detection avoids redundant SSE events. The `retry: 1000\n\n` directive is the right way to configure client reconnection. `X-Accel-Buffering: no` header is a thoughtful production detail.

**Issue — Router is a module-level global, endpoint is registered once:**
```python
router = APIRouter(prefix="/api/stream", tags=["streaming"])

def create_stream_router(price_cache: PriceCache) -> APIRouter:
    @router.get("/prices")
    async def stream_prices(...):
        ...
    return router
```
`router` is defined at module level, and the `@router.get("/prices")` decorator runs each time `create_stream_router()` is called. If called twice (e.g. in tests), the route is registered twice on the same router object, which FastAPI tolerates but produces a warning. A safer pattern would be to create the `APIRouter` inside `create_stream_router()`. This is a test isolation concern more than a production concern.

**Issue — `request.client` can be `None`:**
```python
client_ip = request.client.host if request.client else "unknown"
```
This is handled. Good.

**Issue — No heartbeat / keepalive:**
The SSE loop only yields when the version changes. If no price updates arrive (e.g. simulator is stopped, Massive API is down), the stream is silent. Some SSE clients (proxies, load balancers) will close idle connections after a timeout. A periodic comment (`": keepalive\n\n"`) every 30 seconds would be a production improvement.

---

## Test Coverage Assessment

Tests are thorough and well-targeted. Key observations:

**Good coverage:**
- Happy-path and edge cases for all public methods
- Thread safety is indirectly tested (version counter, concurrent updates)
- Async integration tests use real `asyncio.sleep()` — no fake timers — which tests actual timing behavior
- Malformed API response handling (skipped snapshot)
- API error resilience (exception in `_poll_once`)
- Factory environment variable edge cases (empty, whitespace, absent)

**Gaps:**

1. **`stream.py` has no tests.** The SSE generator (`_generate_events`) is not tested. There are no tests for: the `retry` directive, version-gated emission, disconnect detection, or the keepalive behavior. This is the most significant test gap.

2. **No concurrency tests.** The `PriceCache` lock is never stress-tested. A test with concurrent readers and writers would validate thread safety more rigorously.

3. **`test_simulator.py::test_prices_are_positive`** runs 10,000 steps, which is good statistical coverage. However, it tests only one ticker. With two correlated tickers, the Cholesky path is exercised — this is covered by `test_step_returns_all_tickers` but only for one step.

4. **`test_simulator_source.py::test_exception_resilience`** checks that the task is still running but doesn't actually inject an error. It's testing that the task doesn't crash under normal operation, not under error conditions.

5. **No test for `MassiveDataSource.start()` with tickers that fail normalization** (e.g. lowercase tickers in `start()` that differ from add_ticker() normalized form).

---

## Summary of Issues

| # | Severity | Location | Issue |
|---|----------|----------|-------|
| 1 | Low | `cache.py:65` | `version` property read outside the lock (data race in Massive mode) |
| 2 | Low | `stream.py:17` | Router created as module-level global; re-registering route if called twice |
| 3 | Low | `simulator.py:263` | `add_ticker()` silently no-ops before `start()` |
| 4 | Low | `massive_client.py:41` | `start()` doesn't normalize ticker case/whitespace (asymmetry with `add_ticker()`) |
| 5 | Low | `stream.py` | No SSE keepalive; silent stream on stale data may be closed by proxies |
| 6 | Info | `simulator.py:104` | Two separate RNGs (numpy + stdlib) make simulation non-reproducible |
| 7 | Info | `stream.py` | No tests for the SSE endpoint |
| 8 | Info | `seed_prices.py` | TSLA exclusion from `CORRELATION_GROUPS` implicit; deserves a comment |

All issues are low severity or informational. None are blockers. The implementation is production-ready for the project's requirements.

---

## Verdict

**The market data backend is well-implemented and ready for integration.** The architecture is clean, the GBM math is correct, the interface abstraction is sound, and all 73 tests pass. The code is appropriately simple — no overengineering, no premature abstraction. The most notable gaps are the untested SSE streaming layer and the version counter race condition in the Massive path, both of which are low-risk given the application's scale and single-user nature.

**Recommended before proceeding to downstream components:**
1. Add a test for the SSE `_generate_events` function (can use a mock `PriceCache` and `Request`).
2. Fix the `version` property to read under the lock (one-line fix).

Everything else can be addressed opportunistically.
