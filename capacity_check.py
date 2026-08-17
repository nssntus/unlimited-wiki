#!/usr/bin/env python3
"""Small deployment-host concurrency check for an Unlimited Wiki HTTP endpoint."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import statistics
import time
import urllib.request


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()
    if args.requests < 1 or args.concurrency < 1 or args.concurrency > 500:
        parser.error("requests and concurrency must be positive; concurrency must not exceed 500")
    cookie = os.environ.get("WIKI_CAPACITY_COOKIE", "")

    def one(_index: int) -> tuple[int, float]:
        headers = {"Accept": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        request = urllib.request.Request(args.url, headers=headers)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=args.timeout) as response:
                response.read()
                status = response.status
        except Exception as exc:
            status = getattr(exc, "code", 0)
        return status, (time.monotonic() - started) * 1000

    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        results = list(executor.map(one, range(args.requests)))
    elapsed = time.monotonic() - started
    latencies = [latency for _status, latency in results]
    statuses: dict[str, int] = {}
    for status, _latency in results:
        statuses[str(status)] = statuses.get(str(status), 0) + 1
    report = {
        "url": args.url,
        "requests": args.requests,
        "concurrency": args.concurrency,
        "elapsed_seconds": round(elapsed, 3),
        "requests_per_second": round(args.requests / elapsed, 2),
        "latency_ms": {
            "mean": round(statistics.fmean(latencies), 2),
            "p50": round(percentile(latencies, 0.50), 2),
            "p95": round(percentile(latencies, 0.95), 2),
            "p99": round(percentile(latencies, 0.99), 2),
        },
        "statuses": statuses,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if statuses == {"200": args.requests} else 1


if __name__ == "__main__":
    raise SystemExit(main())
