"""Thin client for api.eduskunta.fi with the quirks handled.

Known behaviour (verified against the live API, Sept 2026):
- Every request needs a User-Agent header, otherwise 403.
- POST /search* is rate-limited: 450 requests / 3000 s / IP.
- /search paging window: startFromIndex + maxResults <= 10000; maxResults up to 1000.
- /search/dataset starts a background job; the result is an NDJSON file on S3.
- `fields` = {"operation": "include"|"exclude", "list": [...]}.
- Date criteria: {"property", "fromDate", "toDate"} (from inclusive, to exclusive).
"""
from __future__ import annotations

import json
import logging
import time
from collections import deque
from typing import Any, Iterable

import requests

from . import config

log = logging.getLogger(__name__)


class RateLimiter:
    """Sliding-window limiter: at most `max_calls` in any `per_seconds` window."""

    def __init__(self, max_calls: int, per_seconds: float):
        self.max_calls, self.per = max_calls, per_seconds
        self.calls: deque[float] = deque()

    def wait(self) -> None:
        now = time.monotonic()
        while self.calls and now - self.calls[0] > self.per:
            self.calls.popleft()
        if len(self.calls) >= self.max_calls:
            pause = self.per - (now - self.calls[0]) + 1
            log.warning("POST rate limit reached, sleeping %.0f s", pause)
            time.sleep(pause)
        self.calls.append(time.monotonic())


def unwrap(obj: dict, category: str) -> dict:
    """Search results wrap the record: {"type": ..., "<category>": {...}}."""
    inner = obj.get(category) if isinstance(obj, dict) else None
    # a speech record also has a text field called `puheenvuoro` ({fi, sv}) - don't unwrap that
    if isinstance(inner, dict) and not set(inner) <= {"fi", "sv", "en"}:
        return inner
    return obj


class Client:
    def __init__(self, user_agent: str = config.USER_AGENT, base: str = config.API_BASE):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
        self.post_limiter = RateLimiter(*config.POST_LIMIT)
        self._last_get = 0.0

    # ------------------------------------------------------------------ core
    def _url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.base}/{path.lstrip('/')}"

    def _request(self, method: str, path: str, *, retries: int = 6, **kw) -> requests.Response:
        url = self._url(path)
        for attempt in range(1, retries + 1):
            if method == "POST":
                self.post_limiter.wait()
            else:
                gap = config.GET_DELAY_S - (time.monotonic() - self._last_get)
                if gap > 0:
                    time.sleep(gap)
                self._last_get = time.monotonic()
            try:
                r = self.s.request(method, url, timeout=120, **kw)
            except requests.RequestException as e:
                log.warning("%s %s failed (%s), attempt %d", method, url, e, attempt)
                time.sleep(5 * attempt)
                continue
            if r.status_code == 429:
                wait = 60 * attempt
                log.warning("429 from %s, sleeping %d s", url, wait)
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                log.warning("%d from %s, retrying", r.status_code, url)
                time.sleep(5 * attempt)
                continue
            return r
        raise RuntimeError(f"Giving up on {method} {url}")

    def get_json(self, path: str, params: dict | None = None) -> Any | None:
        r = self._request("GET", path, params=params)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def get_text(self, path: str) -> str | None:
        r = self._request("GET", path, headers={"Accept": "text/html,*/*"})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        r.encoding = r.encoding or "utf-8"
        return r.text

    def post_json(self, path: str, body: dict) -> Any:
        r = self._request("POST", path, json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"POST {path} -> {r.status_code}: {r.text[:500]}")
        return r.json()

    # ---------------------------------------------------------------- search
    def count(self, category: str, expression: dict | None = None, query: str | None = None) -> int:
        body: dict = {"category": category}
        if expression:
            body["expression"] = expression
        if query:
            body["query"] = query
        return int(self.post_json("search/count", body).get("count", 0))

    def search_page(self, body: dict) -> list[dict]:
        return self.post_json("search", body).get("results") or []

    def search_all(self, category: str, expression: dict, sort: list[dict],
                   fields: dict | None = None, page: int = 1000,
                   dataset_threshold: int = 5000) -> list[dict]:
        """All results for a criterion. Uses paging for small sets, a dataset job for large."""
        n = self.count(category, expression)
        log.info("%s: %d records match", category, n)
        if n > dataset_threshold:
            return self.dataset(category, expression, sort, fields)
        out: list[dict] = []
        start = 0
        while start < n and start + page <= 10000:
            body = {"category": category, "expression": expression, "sort": sort,
                    "maxResults": page, "startFromIndex": start}
            if fields:
                body["fields"] = fields
            batch = self.search_page(body)
            out.extend(unwrap(x, category) for x in batch)
            if len(batch) < page:
                break
            start += page
        return out

    def dataset(self, category: str, expression: dict, sort: list[dict],
                fields: dict | None = None, poll_s: int = 5, timeout_s: int = 1800) -> list[dict]:
        body = {"category": category, "expression": expression, "sort": sort}
        if fields:
            body["fields"] = fields
        job = self.post_json("search/dataset", body)["jobId"]
        log.info("dataset job %s started for %s", job, category)
        t0 = time.monotonic()
        while True:
            st = self.get_json(f"search/dataset/status/{job}") or {}
            status = st.get("status")
            if status == "COMPLETED":
                break
            if status == "FAILED":
                raise RuntimeError(f"dataset job {job} failed: {st}")
            if time.monotonic() - t0 > timeout_s:
                raise TimeoutError(f"dataset job {job} still {status}")
            time.sleep(poll_s)
        r = self.s.get(st["resultUrl"], timeout=600)
        r.raise_for_status()
        r.encoding = "utf-8"
        rows = [unwrap(json.loads(line), category) for line in r.text.splitlines() if line.strip()]
        log.info("dataset job %s: %d records", job, len(rows))
        return rows


def write_jsonl(path, rows: Iterable[dict]) -> int:
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
    return n


def read_jsonl(path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]
