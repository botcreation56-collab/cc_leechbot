"""
metrics.py — Bot performance metrics and monitoring

Tracks:
- Request counts and latencies
- Error rates
- Queue depths
- Memory usage
- Custom business metrics

Exposes metrics via:
- /metrics endpoint (Prometheus-compatible)
- In-memory counters for dashboard
- Periodic health checks
"""

import asyncio
import logging
import time
import psutil
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger("filebot.metrics")


@dataclass
class Counter:
    """Thread-safe counter with rate calculation."""

    value: int = 0
    last_reset: float = field(default_factory=time.time)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def increment(self, amount: int = 1):
        async with self._lock:
            self.value += amount

    def get_and_reset(self) -> int:
        now = time.time()
        elapsed = now - self.last_reset
        rate = self.value / elapsed if elapsed > 0 else 0
        val = self.value
        self.value = 0
        self.last_reset = now
        return val

    def get_rate(self) -> float:
        elapsed = time.time() - self.last_reset
        return self.value / elapsed if elapsed > 0 else 0


@dataclass
class Histogram:
    """Tracks distribution of values."""

    values: list = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    max_size: int = 1000

    async def observe(self, value: float):
        async with self._lock:
            self.values.append(value)
            if len(self.values) > self.max_size:
                self.values.pop(0)

    def get_stats(self) -> Dict[str, float]:
        if not self.values:
            return {
                "count": 0,
                "min": 0,
                "max": 0,
                "avg": 0,
                "p50": 0,
                "p95": 0,
                "p99": 0,
            }

        sorted_vals = sorted(self.values)
        count = len(sorted_vals)

        return {
            "count": count,
            "min": sorted_vals[0],
            "max": sorted_vals[-1],
            "avg": sum(sorted_vals) / count,
            "p50": sorted_vals[int(count * 0.5)],
            "p95": sorted_vals[int(count * 0.95)] if count > 20 else sorted_vals[-1],
            "p99": sorted_vals[int(count * 0.99)] if count > 100 else sorted_vals[-1],
        }


class MetricsCollector:
    """
    Central metrics collector for the bot.

    Usage:
        metrics = MetricsCollector.get_instance()

        # Count something
        await metrics.increment("requests.total")
        await metrics.increment("requests.errors")

        # Time something
        start = time.time()
        await do_work()
        await metrics.observe("latency.requests", time.time() - start)

        # Get all metrics
        stats = await metrics.get_all()
    """

    _instance: Optional["MetricsCollector"] = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized") and self._initialized:
            return

        self._counters: Dict[str, Counter] = defaultdict(Counter)
        self._histograms: Dict[str, Histogram] = defaultdict(Histogram)
        self._gauges: Dict[str, float] = {}
        self._started_at = time.time()
        self._initialized = True
        self._update_task: Optional[asyncio.Task] = None
        self._running = False

        self._initialize_default_metrics()

    @classmethod
    def get_instance(cls) -> "MetricsCollector":
        if not cls._instance:
            cls._instance = cls()
        return cls._instance

    def _initialize_default_metrics(self):
        """Initialize default metrics."""
        self._counters["requests.total"]
        self._counters["requests.success"]
        self._counters["requests.errors"]
        self._counters["queue.tasks.created"]
        self._counters["queue.tasks.completed"]
        self._counters["queue.tasks.failed"]
        self._counters["webhook.received"]
        self._counters["webhook.processed"]
        self._counters["webhook.dropped"]

        self._histograms["latency.requests"]
        self._histograms["latency.db"]
        self._histograms["latency.queue"]
        self._histograms["queue.depth"]

    async def increment(self, metric: str, amount: int = 1):
        """Increment a counter metric."""
        if metric not in self._counters:
            self._counters[metric] = Counter()
        await self._counters[metric].increment(amount)

    async def observe(self, metric: str, value: float):
        """Observe a value for histogram metrics."""
        if metric not in self._histograms:
            self._histograms[metric] = Histogram()
        await self._histograms[metric].observe(value)

    def gauge(self, metric: str, value: float):
        """Set a gauge metric."""
        self._gauges[metric] = value

    async def start(self):
        """Start background metrics collection."""
        if self._running:
            return
        self._running = True
        self._update_task = asyncio.create_task(self._update_loop())
        logger.info("✅ MetricsCollector started")

    async def stop(self):
        """Stop background metrics collection."""
        self._running = False
        if self._update_task:
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass
        logger.info("🛑 MetricsCollector stopped")

    async def _update_loop(self):
        """Background loop to update system metrics."""
        while self._running:
            try:
                await self._update_system_metrics()
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Metrics update error: {e}")
                await asyncio.sleep(10)

    async def _update_system_metrics(self):
        """Update system resource metrics."""
        try:
            process = psutil.Process()

            self._gauges["system.memory.rss_mb"] = (
                process.memory_info().rss / 1024 / 1024
            )
            self._gauges["system.cpu.percent"] = process.cpu_percent()

            mem = psutil.virtual_memory()
            self._gauges["system.memory.available_mb"] = mem.available / 1024 / 1024
            self._gauges["system.memory.percent"] = mem.percent

        except Exception as e:
            logger.debug(f"Could not update system metrics: {e}")

    async def get_all(self) -> Dict[str, Any]:
        """Get all metrics in Prometheus-compatible format."""
        now = time.time()
        uptime = now - self._started_at

        result = {
            "uptime_seconds": uptime,
            "timestamp": datetime.utcnow().isoformat(),
            "counters": {},
            "histograms": {},
            "gauges": dict(self._gauges),
        }

        for name, counter in self._counters.items():
            rate = counter.get_rate()
            result["counters"][name] = {
                "total": counter.value,
                "rate_per_second": rate,
            }

        for name, histogram in self._histograms.items():
            result["histograms"][name] = histogram.get_stats()

        return result

    def get_prometheus_format(self) -> str:
        """Get metrics in Prometheus text format."""
        lines = []
        lines.append("# HELP bot_uptime_seconds Bot uptime in seconds")
        lines.append("# TYPE bot_uptime_seconds gauge")
        lines.append(f"bot_uptime_seconds {time.time() - self._started_at}")

        for name, counter in self._counters.items():
            safe_name = name.replace(".", "_").replace("-", "_")
            lines.append(f"# HELP bot_{safe_name}_total Bot {name} total")
            lines.append(f"# TYPE bot_{safe_name}_total counter")
            lines.append(f"bot_{safe_name}_total {counter.value}")

            lines.append(f"# HELP bot_{safe_name}_rate Bot {name} rate per second")
            lines.append(f"# TYPE bot_{safe_name}_rate gauge")
            lines.append(f"bot_{safe_name}_rate {counter.get_rate():.2f}")

        for name, value in self._gauges.items():
            safe_name = name.replace(".", "_").replace("-", "_")
            lines.append(f"# HELP bot_{safe_name} Bot {name}")
            lines.append(f"# TYPE bot_{safe_name} gauge")
            lines.append(f"bot_{safe_name} {value}")

        return "\n".join(lines)


# Singleton accessor
def get_metrics() -> MetricsCollector:
    return MetricsCollector.get_instance()


# Convenience functions
async def count_request(success: bool = True):
    """Count a request."""
    metrics = get_metrics()
    await metrics.increment("requests.total")
    if success:
        await metrics.increment("requests.success")
    else:
        await metrics.increment("requests.errors")


async def time_request(callback):
    """Time a request and record latency."""
    import functools

    metrics = get_metrics()
    start = time.time()
    try:
        result = await callback()
        await metrics.increment("requests.success")
        return result
    except Exception:
        await metrics.increment("requests.errors")
        raise
    finally:
        await metrics.observe("latency.requests", time.time() - start)


async def count_queue_task(status: str):
    """Count a queue task event."""
    metrics = get_metrics()
    await metrics.increment(f"queue.tasks.{status}")
