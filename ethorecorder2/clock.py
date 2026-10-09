"""Clock mapping between device clocks and the host's monotonic clock.

Everything in EthoRecorder2 is timed against one host clock
(``time.perf_counter_ns``).  A :class:`ClockMap` holds paired readings of a
device clock (camera nanoseconds, or audio sample index) and that host clock,
and converts between the two with a straight-line fit, which also captures the
slow drift between two crystal oscillators.
"""

from __future__ import annotations

import threading
import time
from collections import deque

import numpy as np

now_ns = time.perf_counter_ns


def fit_line(host, dev, nominal_slope: float, min_span_ns: float = 20e9):
    """Least-squares fit ``host = h0 + slope * (dev - d0)``.

    The slope is only trusted when the readings span ``min_span_ns``;
    otherwise the nominal slope is used and only the offset is fitted.
    """
    host = np.asarray(host, dtype=np.float64)
    dev = np.asarray(dev, dtype=np.float64)
    d0 = float(dev.mean())
    h0 = float(host.mean())
    slope = nominal_slope
    if len(host) >= 8 and host[-1] - host[0] >= min_span_ns:
        x = dev - d0
        denom = float((x * x).sum())
        if denom > 0:
            slope = float((x * (host - h0)).sum() / denom)
    return d0, h0, slope


def fit_arrivals(arrival, dev, nominal_slope: float, min_span_ns: float = 20e9, window_ns: float = 2e9):
    """Like :func:`fit_line` for data that reaches the host in blocks.

    A block cannot arrive before its last sample was captured, only later, by
    a transport delay that varies by tens of milliseconds (USB audio arrives
    in bursts).  The promptest arrival in each ``window_ns`` is delayed least
    and scatters far less, so the line is fitted through those and then
    shifted to the lower edge of all arrivals: capture time plus the smallest
    transport delay (about a millisecond over USB).
    """
    arrival = np.asarray(arrival, dtype=np.float64)
    dev = np.asarray(dev, dtype=np.float64)
    d0, h0, slope = fit_line(arrival, dev, nominal_slope, min_span_ns)
    resid = arrival - (h0 + slope * (dev - d0))
    if slope != nominal_slope:
        window = ((arrival - arrival[0]) // window_ns).astype(np.int64)
        starts = np.flatnonzero(np.diff(window, prepend=-1))
        prompt = np.array([a + np.argmin(resid[a:b]) for a, b in zip(starts, [*starts[1:], len(resid)])])
        if len(prompt) >= 8:
            d0, h0, slope = fit_line(arrival[prompt], dev[prompt], nominal_slope, min_span_ns)
            resid = arrival - (h0 + slope * (dev - d0))
    return d0, h0 + float(np.percentile(resid, 0.5)), slope


class ClockMap:
    """Running device-clock <-> host-clock conversion over a sliding window."""

    def __init__(self, nominal_slope: float = 1.0, window_s: float = 120.0, min_span_s: float = 10.0):
        self.nominal_slope = nominal_slope
        self.window_ns = window_s * 1e9
        self.min_span_ns = min_span_s * 1e9
        self._samples: deque[tuple[int, int]] = deque()
        self._lock = threading.Lock()
        self._fit = None

    def add(self, host_ns: int, dev: int) -> None:
        with self._lock:
            self._samples.append((host_ns, dev))
            while self._samples and host_ns - self._samples[0][0] > self.window_ns:
                self._samples.popleft()
            self._fit = None

    def __len__(self) -> int:
        return len(self._samples)

    def samples(self) -> list[tuple[int, int]]:
        with self._lock:
            return list(self._samples)

    def _get_fit(self):
        with self._lock:
            if self._fit is None and self._samples:
                a = np.array(self._samples, dtype=np.float64)
                self._fit = fit_line(a[:, 0], a[:, 1], self.nominal_slope, self.min_span_ns)
            return self._fit

    def to_host(self, dev: float) -> int:
        f = self._get_fit()
        if f is None:
            raise RuntimeError("clock map has no samples yet")
        d0, h0, slope = f
        return int(round(h0 + slope * (dev - d0)))

    def to_dev(self, host_ns: float) -> float:
        f = self._get_fit()
        if f is None:
            raise RuntimeError("clock map has no samples yet")
        d0, h0, slope = f
        return d0 + (host_ns - h0) / slope
