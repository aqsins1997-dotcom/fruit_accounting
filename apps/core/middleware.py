import logging
import os
import time

from django.db import connection


logger = logging.getLogger("fruit.performance")


def _env_enabled(name):
    return os.getenv(name, "").lower() in {"1", "true", "yes", "on"}


class PerformanceLoggingMiddleware:
    WATCHED_PATHS = (
        "/",
        "/purchases/",
        "/purchases/add/",
        "/purchases/items/",
        "/sales/",
        "/sales/add/",
        "/sales/cash/",
        "/reports/suppliers/",
        "/reports/debtors/",
        "/stocks/",
    )

    def __init__(self, get_response):
        self.get_response = get_response
        self.enabled = _env_enabled("DJANGO_PERF_LOG")

    def __call__(self, request):
        if not self.enabled or not self._should_log(request.path_info):
            return self.get_response(request)

        previous_force_debug_cursor = connection.force_debug_cursor
        connection.force_debug_cursor = True
        start_query_index = len(connection.queries)
        started_at = time.perf_counter()
        response = None
        try:
            response = self.get_response(request)
            return response
        finally:
            elapsed = time.perf_counter() - started_at
            queries = connection.queries[start_query_index:]
            connection.force_debug_cursor = previous_force_debug_cursor
            self._log_request(request, response, elapsed, queries)

    def _should_log(self, path):
        for watched_path in self.WATCHED_PATHS:
            if watched_path == "/":
                if path == watched_path:
                    return True
            elif path == watched_path or path.startswith(watched_path):
                return True
        return False

    def _log_request(self, request, response, elapsed, queries):
        slow_queries = sorted(
            queries,
            key=lambda query: self._query_time(query),
            reverse=True,
        )[:3]
        slow_sql = [
            {
                "seconds": round(self._query_time(query), 4),
                "sql": " ".join(query.get("sql", "").split())[:500],
            }
            for query in slow_queries
        ]
        status_code = getattr(response, "status_code", "error")
        logger.warning(
            "view_perf path=%s method=%s status=%s elapsed=%.4fs queries=%s slow_queries=%s",
            request.path_info,
            request.method,
            status_code,
            elapsed,
            len(queries),
            slow_sql,
        )

    @staticmethod
    def _query_time(query):
        try:
            return float(query.get("time") or 0)
        except (TypeError, ValueError):
            return 0.0
