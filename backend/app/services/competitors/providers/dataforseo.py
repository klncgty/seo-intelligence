"""DataForSEO v3 Labs and Backlinks API adapter."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from app.services.competitors.providers.base import (
    BacklinkSummary,
    KeywordGapResult,
    KeywordGapRow,
    ReferringDomain,
)
from app.services.integrations.retry import GoogleApiError, request_with_retry

_API_ROOT = "https://api.dataforseo.com/v3"


class DataForSEOError(RuntimeError):
    """DataForSEO returned an unsuccessful or malformed API response."""


class DataForSEOProvider:
    name = "dataforseo"

    def __init__(
        self,
        login: str,
        password: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.login = login
        self.password = password
        self.client = client

    def is_configured(self) -> bool:
        return bool(self.login.strip() and self.password.strip())

    async def _post(self, path: str, payload: list[dict[str, Any]]) -> dict[str, Any]:
        owned_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=30.0)
        try:
            response = await request_with_retry(
                client,
                "POST",
                f"{_API_ROOT}{path}",
                json=payload,
                auth=(self.login, self.password),
            )
        except GoogleApiError as exc:
            raise DataForSEOError(f"DataForSEO request failed: {exc}") from exc
        finally:
            if owned_client:
                await client.aclose()
        try:
            data = response.json()
        except (ValueError, httpx.DecodingError) as exc:
            raise DataForSEOError("DataForSEO returned invalid JSON.") from exc
        if not isinstance(data, dict):
            raise DataForSEOError("DataForSEO returned an unexpected response.")
        if data.get("status_code") not in (None, 20000):
            raise DataForSEOError(
                f"DataForSEO error {data.get('status_code')}: "
                f"{data.get('status_message', 'request failed')}"
            )
        tasks = data.get("tasks")
        if not isinstance(tasks, list):
            raise DataForSEOError("DataForSEO response did not contain tasks.")
        for task in tasks:
            if isinstance(task, dict) and task.get("status_code") not in (None, 20000):
                raise DataForSEOError(
                    f"DataForSEO task error {task.get('status_code')}: "
                    f"{task.get('status_message', 'request failed')}"
                )
        return data

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _int(value: Any) -> int | None:
        if isinstance(value, bool) or value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _task_result(task: dict[str, Any]) -> list[dict[str, Any]]:
        result = task.get("result")
        if not isinstance(result, list):
            return []
        rows: list[dict[str, Any]] = []
        for item in result:
            if not isinstance(item, dict):
                continue
            nested = item.get("items")
            if isinstance(nested, list):
                rows.extend(row for row in nested if isinstance(row, dict))
            else:
                rows.append(item)
        return rows

    @staticmethod
    def _rank(value: Any) -> int | None:
        if not isinstance(value, dict):
            return None
        serp = value.get("serp_item")
        source = serp if isinstance(serp, dict) else value
        for field in ("rank_group", "rank_absolute", "position"):
            rank = DataForSEOProvider._int(source.get(field))
            if rank is not None:
                return rank
        return None

    async def keyword_gap(
        self, own_domain: str, competitor_domains: list[str], limit: int = 100
    ) -> KeywordGapResult:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        domains = sorted({domain.strip() for domain in competitor_domains if domain.strip()})
        if not domains:
            return KeywordGapResult(provider=self.name, rows=[], fetched_at=self._now())
        payload = [
            {
                "target1": own_domain,
                "target2": domain,
                "intersection_mode": "target2",
                "location_code": 2840,
                "language_code": "en",
                "limit": limit,
            }
            for domain in domains
        ]
        data = await self._post(
            "/dataforseo_labs/google/domain_intersection/live", payload
        )
        rows: list[KeywordGapRow] = []
        tasks = data["tasks"]
        for index, task in enumerate(tasks):
            if not isinstance(task, dict):
                continue
            task_data = task.get("data")
            competitor_domain = (
                task_data.get("target2")
                if isinstance(task_data, dict) and isinstance(task_data.get("target2"), str)
                else domains[index] if index < len(domains) else ""
            )
            for item in self._task_result(task):
                keyword_data = item.get("keyword_data")
                if not isinstance(keyword_data, dict):
                    keyword_data = item
                keyword = keyword_data.get("keyword")
                if not isinstance(keyword, str) or not keyword:
                    continue
                keyword_info = keyword_data.get("keyword_info")
                volume = (
                    self._int(keyword_info.get("search_volume"))
                    if isinstance(keyword_info, dict)
                    else self._int(item.get("search_volume"))
                )
                rows.append(KeywordGapRow(
                    keyword=keyword,
                    competitor_domain=competitor_domain,
                    competitor_position=self._rank(item.get("second_domain_serp_element")),
                    own_position=self._rank(item.get("first_domain_serp_element")),
                    search_volume=volume,
                    url=self._url(item.get("second_domain_serp_element")),
                ))
        rows.sort(key=lambda row: (
            row.competitor_domain, row.keyword, row.competitor_position or 0
        ))
        return KeywordGapResult(
            provider=self.name,
            rows=rows,
            fetched_at=self._now(),
        )

    @staticmethod
    def _url(value: Any) -> str | None:
        if not isinstance(value, dict):
            return None
        serp = value.get("serp_item")
        source = serp if isinstance(serp, dict) else value
        url = source.get("url")
        return url if isinstance(url, str) else None

    async def backlink_summary(self, domain: str) -> BacklinkSummary:
        data = await self._post(
            "/backlinks/summary/live",
            [{"target": domain, "include_subdomains": True, "rank_scale": "one_hundred"}],
        )
        result = self._first_result(data["tasks"])
        return BacklinkSummary(
            provider=self.name,
            domain=domain,
            backlinks=self._int(result.get("backlinks")),
            referring_domains=self._int(result.get("referring_domains")),
            fetched_at=self._now(),
        )

    async def referring_domains(
        self, domain: str, limit: int = 100
    ) -> list[ReferringDomain]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        data = await self._post(
            "/backlinks/referring_domains/live",
            [{"target": domain, "limit": limit, "order_by": ["backlinks,desc"]}],
        )
        domains: list[ReferringDomain] = []
        for item in self._task_result(self._first_task(data["tasks"])):
            name = item.get("domain")
            if not isinstance(name, str) or not name:
                continue
            domains.append(ReferringDomain(
                domain=name,
                backlinks=self._int(item.get("backlinks")),
                rank=self._int(item.get("rank")),
            ))
        domains.sort(key=lambda item: (-1 if item.backlinks is None else -item.backlinks, item.domain))
        return domains[:limit]

    @staticmethod
    def _first_task(tasks: list[Any]) -> dict[str, Any]:
        if not tasks or not isinstance(tasks[0], dict):
            raise DataForSEOError("DataForSEO response contained no task result.")
        return tasks[0]

    @classmethod
    def _first_result(cls, tasks: list[Any]) -> dict[str, Any]:
        task = cls._first_task(tasks)
        result = task.get("result")
        if not isinstance(result, list) or not result or not isinstance(result[0], dict):
            raise DataForSEOError("DataForSEO response contained no usable result.")
        nested = result[0].get("items")
        if isinstance(nested, list) and nested and isinstance(nested[0], dict):
            return nested[0]
        return result[0]
