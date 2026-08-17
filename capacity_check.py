#!/usr/bin/env python3
"""Small deployment-host concurrency check for an Unlimited Wiki HTTP endpoint."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import statistics
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_once(url: str, *, cookie: str = "", timeout: float = 10) -> tuple[int, float]:
    if cookie and urlsplit(url).scheme != "https":
        raise ValueError("WIKI_CAPACITY_COOKIE requires an HTTPS target")
    headers = {"Accept": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(NoRedirectHandler())
    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout) as response:
            response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except Exception as exc:
        status = getattr(exc, "code", 0)
    return status, (time.monotonic() - started) * 1000


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
    if cookie and urlsplit(args.url).scheme != "https":
        parser.error("WIKI_CAPACITY_COOKIE requires an HTTPS target")

    def one(_index: int) -> tuple[int, float]:
        return request_once(args.url, cookie=cookie, timeout=args.timeout)

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
