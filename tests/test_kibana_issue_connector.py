import base64
import contextlib
import hashlib
import io
import json
import os
import ssl
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.kibana_issue_connector import (
    DashboardCredentials,
    DiscoverTarget,
    ErrorHitBatch,
    LEGACY_SCAN_CURSOR_SCHEMA_VERSION,
    LEGACY_SCAN_QUERY_VERSION,
    OpenSearchDashboardsClient,
    _blocked_error_preview,
    _credentials,
    _load_history_cursor,
    _load_scan_cursor,
    _save_history_cursor,
    _save_scan_cursor,
    _scan_source_sha256,
    _scan_window,
    main,
    parse_discover_url,
)
from src.kibana_sanitizer import sanitize_hit


DISCOVER_URL = (
    "https://logs.example.test/_dashboards/app/discover#/"
    "?_g=(filters:!(),time:(from:now-2h,to:now))"
    "&_a=(index:ee351460-8261-11f0-bb8a-4fb3796753f3,query:(language:kuery,query:''))"
)
HMAC_KEY = "0123456789abcdef0123456789abcdef"


def error_hit(
    timestamp="2099-01-01T00:00:00Z",
    document_id="raw-document-id",
):
    return {
        "_index": "logs-demo",
        "_id": document_id,
        "_source": {
            "@timestamp": timestamp,
            "stream": "stdout",
            "message": (
                "[2099-01-01 08:00:00.000] [TID: trace-demo] ERROR [worker-1] "
                "com.example.OrderController:87 - com.example.OrderService: createOrder: failed"
            ),
            "kubernetes": {
                "namespace_name": "demo",
                "container_name": "demo-checkout",
                "labels": {"app_kubernetes_io/name": "demo-checkout"},
            },
        },
    }


class FakeResponse:
    def __init__(self, url, payload):
        self.url = url
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload

    def geturl(self):
        return self.url


class FakeOpener:
    def __init__(self):
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        if request.method == "GET":
            payload = {"attributes": {"title": "logs-*", "timeFieldName": "@timestamp"}}
        else:
            payload = {"hits": {"hits": [error_hit()]}}
        return FakeResponse(request.full_url, payload)


class PagedOpener:
    def __init__(self):
        self.requests = []
        self.scroll_pages = [
            {"_scroll_id": "scroll-1", "hits": {"hits": [error_hit(), error_hit()]}},
            {"_scroll_id": "scroll-2", "hits": {"hits": [error_hit()]}},
        ]

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        if request.method == "GET":
            return FakeResponse(
                request.full_url,
                {"attributes": {"title": "logs-*", "timeFieldName": "@timestamp"}},
            )
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(request.full_url).query
        )
        proxy_method = query.get("method", [""])[0]
        proxy_path = query.get("path", [""])[0]
        if proxy_method == "DELETE" and proxy_path == "_search/scroll":
            return FakeResponse(request.full_url, {"succeeded": True})
        if self.scroll_pages:
            return FakeResponse(request.full_url, self.scroll_pages.pop(0))
        return FakeResponse(
            request.full_url,
            {"_scroll_id": "scroll-2", "hits": {"hits": []}},
        )


class KibanaIssueConnectorTest(unittest.TestCase):
    @mock.patch("src.kibana_issue_connector.getpass.getpass", return_value="password")
    @mock.patch("builtins.input", return_value="reader")
    def test_credentials_can_be_prompted_without_environment_storage(self, input_prompt, password_prompt):
        with mock.patch.dict(os.environ, {}, clear=True):
            credentials = _credentials(True, "")

        self.assertEqual(credentials.username, "reader")
        self.assertNotIn("password", repr(credentials))
        input_prompt.assert_called_once_with("OpenSearch username: ")
        password_prompt.assert_called_once_with("OpenSearch password: ")

    def test_blocked_error_preview_is_sanitized_again(self):
        hit = error_hit()
        hit["_source"]["message"] += (
            " mystery=AbCdEfGhIjKlMnOpQrStUvWxYz0123456789 "
            "contact=person@example.test"
        )
        sanitized = sanitize_hit(hit, HMAC_KEY.encode())

        preview = _blocked_error_preview(sanitized)

        encoded = json.dumps(preview)
        self.assertIn("unclassified_high_entropy", preview["blocked_categories"])
        self.assertEqual(preview["sanitized_summary"], "[REDACTED:sensitive_preview]")
        self.assertNotIn("AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", encoded)
        self.assertNotIn("person@example.test", encoded)

    def test_blocked_error_preview_reports_only_redacted_entropy_context(self):
        hit = error_hit()
        secret = "QWxhZGRpbjpvcGVuIHNlc2FtZV9yYW5kb21WYWx1ZQ=="
        hit["_source"]["message"] += " safe-prefix" * 120 + f" mystery={secret} tail"
        sanitized = sanitize_hit(hit, HMAC_KEY.encode())

        preview = _blocked_error_preview(sanitized)
        encoded = json.dumps(preview)

        self.assertEqual(len(preview["blocked_contexts"]), 1)
        self.assertIn("[REDACTED:unclassified_high_entropy]", encoded)
        self.assertNotIn("[REDACTED:[REDACTED:", encoded)
        self.assertNotIn(secret, encoded)

    def test_parses_discover_target(self):
        target = parse_discover_url(DISCOVER_URL)

        self.assertEqual(target.base_url, "https://logs.example.test/_dashboards")
        self.assertEqual(target.data_view_id, "ee351460-8261-11f0-bb8a-4fb3796753f3")
        self.assertEqual(target.time_from, "now-2h")
        self.assertEqual(target.time_to, "now")

    def test_rejects_non_https_and_absolute_time_ranges(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            parse_discover_url(DISCOVER_URL.replace("https://", "http://"))
        with self.assertRaisesRegex(ValueError, "bounded relative"):
            parse_discover_url(DISCOVER_URL.replace("now-2h", "2099-01-01"))

    def test_client_resolves_data_view_and_fetches_bounded_fields(self):
        opener = FakeOpener()
        target = DiscoverTarget(
            base_url="https://logs.example.test/_dashboards",
            data_view_id="data-view-1",
            time_from="now-2h",
            time_to="now",
        )
        credentials = DashboardCredentials("reader", "password")
        client = OpenSearchDashboardsClient(
            target, credentials, timeout_seconds=45, opener=opener
        )

        index_pattern, time_field = client.resolve_index_pattern()
        batch = client.fetch_error_hits(index_pattern, time_field, 25)
        hits = batch.hits

        self.assertEqual(index_pattern, "logs-*")
        self.assertEqual(len(hits), 1)
        self.assertTrue(all(timeout == 45 for _, timeout in opener.requests))
        self.assertNotIn("password", repr(credentials))
        request = opener.requests[1][0]
        expected_auth = "Basic " + base64.b64encode(b"reader:password").decode()
        self.assertEqual(request.headers["Authorization"], expected_auth)
        payload = json.loads(request.data)
        self.assertEqual(payload["size"], 25)
        self.assertNotIn("track_total_hits", payload)
        self.assertIn("message", payload["_source"])
        self.assertNotIn("kubernetes.pod_name", payload["_source"])
        self.assertEqual(
            payload["query"]["bool"]["filter"][0]["range"]["@timestamp"],
            {"gte": "now-2h", "lt": "now"},
        )
        self.assertEqual(
            payload["sort"],
            [
                {
                    "@timestamp": {
                        "order": "asc",
                        "unmapped_type": "date",
                    }
                },
                "_doc",
            ],
        )

    def test_client_scrolls_every_page_before_completing_scan(self):
        opener = PagedOpener()
        target = DiscoverTarget(
            base_url="https://logs.example.test/_dashboards",
            data_view_id="data-view-1",
            time_from="now-2h",
            time_to="now",
        )
        client = OpenSearchDashboardsClient(
            target,
            DashboardCredentials("reader", "password"),
            opener=opener,
        )

        batch = client.fetch_error_hits(
            "logs-*",
            "@timestamp",
            2,
            max_scan_hits=10,
        )
        hits = batch.hits

        self.assertEqual(len(hits), 3)
        self.assertFalse(batch.backlog_remaining)
        proxy_calls = [
            urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
            for request, _timeout in opener.requests
            if request.method == "POST"
        ]
        self.assertEqual(proxy_calls[0]["method"], ["POST"])
        self.assertIn("scroll=2m", proxy_calls[0]["path"][0])
        self.assertEqual(proxy_calls[1]["path"], ["_search/scroll"])
        self.assertEqual(proxy_calls[-1]["method"], ["DELETE"])

    def test_client_finds_earliest_nonempty_error_window_without_hits(self):
        client = OpenSearchDashboardsClient(
            DiscoverTarget(
                base_url="https://logs.example.test/_dashboards",
                data_view_id="data-view-1",
                time_from="now-2h",
                time_to="now",
            ),
            DashboardCredentials("reader", "password"),
        )
        bucket_start = datetime(
            2026, 7, 27, 10, 0, tzinfo=timezone.utc
        )
        with mock.patch.object(
            client,
            "_console_request",
            return_value={
                "aggregations": {
                    "error_windows": {
                        "buckets": [
                            {
                                "key": int(bucket_start.timestamp() * 1000),
                                "doc_count": 3,
                            }
                        ]
                    }
                }
            },
        ) as request:
            window = client.find_next_error_window(
                "logs-*",
                "@timestamp",
                time_from="2026-07-27T09:34:38.029Z",
                time_to="2026-07-28T09:34:38.029Z",
            )

        self.assertEqual(
            (
                "2026-07-27T10:00:00.000Z",
                "2026-07-27T10:05:00.000Z",
            ),
            window,
        )
        payload = request.call_args.args[2]
        self.assertEqual(payload["size"], 0)
        self.assertFalse(payload["track_total_hits"])
        self.assertNotIn("sort", payload)
        self.assertEqual(
            payload["aggs"]["error_windows"]["date_histogram"][
                "fixed_interval"
            ],
            "5m",
        )

    def test_client_finds_latest_nonempty_error_window_for_history_scan(self):
        client = OpenSearchDashboardsClient(
            DiscoverTarget(
                base_url="https://logs.example.test/_dashboards",
                data_view_id="data-view-1",
                time_from="now-2h",
                time_to="now",
            ),
            DashboardCredentials("reader", "password"),
        )
        with mock.patch.object(
            client,
            "_console_request",
            return_value={
                "aggregations": {
                    "error_windows": {
                        "buckets": [
                            {
                                "key": int(
                                    datetime(
                                        2026,
                                        7,
                                        27,
                                        9,
                                        5,
                                        tzinfo=timezone.utc,
                                    ).timestamp()
                                    * 1000
                                ),
                                "doc_count": 2,
                            }
                        ]
                    }
                }
            },
        ) as request:
            window = client.find_previous_error_window(
                "logs-*",
                "@timestamp",
                time_from="2026-07-27T08:00:00.000Z",
                time_to="2026-07-27T10:00:00.000Z",
            )

        self.assertEqual(
            (
                "2026-07-27T09:05:00.000Z",
                "2026-07-27T09:10:00.000Z",
            ),
            window,
        )
        payload = request.call_args.args[2]
        self.assertEqual(
            {"_key": "desc"},
            payload["aggs"]["error_windows"]["date_histogram"]["order"],
        )

    def test_history_cursor_keeps_forward_scan_state_separate(self):
        target = parse_discover_url(DISCOVER_URL)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history_path = root / "history.json"
            _save_history_cursor(
                history_path,
                target,
                range_from=datetime(
                    2026, 7, 27, 8, 0, tzinfo=timezone.utc
                ),
                next_before=datetime(
                    2026, 7, 27, 8, 50, tzinfo=timezone.utc
                ),
                pending_from=datetime(
                    2026, 7, 27, 8, 55, tzinfo=timezone.utc
                ),
                pending_to=datetime(
                    2026, 7, 27, 9, 0, tzinfo=timezone.utc
                ),
                summary_path=root / "summary.json",
            )
            cursor = _load_history_cursor(history_path, target)

        self.assertEqual(
            "2026-07-27T08:50:00.000Z",
            cursor["next_before"],
        )
        self.assertEqual(
            "2026-07-27T08:55:00.000Z",
            cursor["pending_from"],
        )

    def test_client_returns_a_complete_timestamp_bounded_backlog_batch(self):
        opener = PagedOpener()
        opener.scroll_pages = [
            {
                "_scroll_id": "scroll-1",
                "hits": {
                    "hits": [
                        error_hit("2026-07-27T08:00:00Z", "first"),
                        error_hit("2026-07-27T08:00:01Z", "second"),
                    ]
                },
            },
            {
                "_scroll_id": "scroll-2",
                "hits": {
                    "hits": [
                        error_hit("2026-07-27T08:00:02Z", "third"),
                        error_hit("2026-07-27T08:00:03Z", "fourth"),
                    ]
                },
            },
        ]
        client = OpenSearchDashboardsClient(
            DiscoverTarget(
                base_url="https://logs.example.test/_dashboards",
                data_view_id="data-view-1",
                time_from="now-2h",
                time_to="now",
            ),
            DashboardCredentials("reader", "password"),
            opener=opener,
        )

        batch = client.fetch_error_hits(
            "logs-*",
            "@timestamp",
            2,
            time_from="2026-07-27T08:00:00.000Z",
            time_to="2026-07-27T08:05:00.000Z",
            max_scan_hits=2,
        )

        self.assertTrue(batch.backlog_remaining)
        self.assertEqual(2, len(batch.hits))
        self.assertEqual(
            "2026-07-27T08:00:02.000Z",
            batch.completed_through,
        )

    def test_client_initial_scan_reads_latest_hits_without_scroll(self):
        opener = FakeOpener()
        target = DiscoverTarget(
            base_url="https://logs.example.test/_dashboards",
            data_view_id="data-view-1",
            time_from="now-2h",
            time_to="now",
        )
        client = OpenSearchDashboardsClient(
            target,
            DashboardCredentials("reader", "password"),
            opener=opener,
        )

        hits = client.fetch_latest_error_hits(
            "logs-*",
            "@timestamp",
            30,
            time_to="2026-07-27T08:05:00.000Z",
        )

        self.assertEqual(len(hits), 1)
        request = opener.requests[0][0]
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(request.full_url).query
        )
        payload = json.loads(request.data)
        self.assertEqual(query["path"], ["logs-*/_search"])
        self.assertEqual(payload["size"], 30)
        self.assertEqual(
            payload["sort"],
            [{"@timestamp": {"order": "desc", "unmapped_type": "date"}}],
        )
        self.assertNotIn("track_total_hits", payload)

    def test_client_does_not_silently_truncate_scan_backlog(self):
        opener = PagedOpener()
        target = DiscoverTarget(
            base_url="https://logs.example.test/_dashboards",
            data_view_id="data-view-1",
            time_from="now-2h",
            time_to="now",
        )
        client = OpenSearchDashboardsClient(
            target,
            DashboardCredentials("reader", "password"),
            opener=opener,
        )

        with self.assertRaisesRegex(ValueError, "cursor was not advanced"):
            client.fetch_error_hits(
                "logs-*",
                "@timestamp",
                2,
                max_scan_hits=2,
            )

    def test_scan_cursor_overlaps_previous_completed_window(self):
        target = DiscoverTarget(
            base_url="https://logs.example.test/_dashboards",
            data_view_id="data-view-1",
            time_from="now-2h",
            time_to="now",
        )
        first_cutoff = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cursor_path = root / "cursor.json"
            summary_path = root / "summary.json"
            _save_scan_cursor(cursor_path, target, first_cutoff, summary_path)
            cursor = _load_scan_cursor(cursor_path, target)
            time_from, time_to, cutoff = _scan_window(
                target,
                cursor,
                300,
                now=datetime(2026, 7, 27, 8, 5, tzinfo=timezone.utc),
            )

        self.assertEqual(time_from, "2026-07-27T07:55:00.000Z")
        self.assertEqual(time_to, "2026-07-27T08:05:00.000Z")
        self.assertEqual(cutoff, datetime(2026, 7, 27, 8, 5, tzinfo=timezone.utc))

    def test_scan_cursor_limits_remote_query_to_one_catchup_slice(self):
        target = DiscoverTarget(
            base_url="https://logs.example.test/_dashboards",
            data_view_id="data-view-1",
            time_from="now-2h",
            time_to="now",
        )
        completed = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cursor_path = root / "cursor.json"
            _save_scan_cursor(
                cursor_path,
                target,
                completed,
                root / "summary.json",
            )
            cursor = _load_scan_cursor(cursor_path, target)
            time_from, time_to, cutoff = _scan_window(
                target,
                cursor,
                300,
                300,
                now=datetime(2026, 7, 28, 8, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(time_from, "2026-07-27T07:55:00.000Z")
        self.assertEqual(time_to, "2026-07-27T08:05:00.000Z")
        self.assertEqual(cutoff, datetime(2026, 7, 27, 8, 5, tzinfo=timezone.utc))

    def test_scan_cursor_stops_at_delayed_ingestion_safety_cutoff(self):
        target = DiscoverTarget(
            base_url="https://logs.example.test/_dashboards",
            data_view_id="data-view-1",
            time_from="now-2h",
            time_to="now",
        )
        completed = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cursor_path = root / "cursor.json"
            _save_scan_cursor(
                cursor_path,
                target,
                completed,
                root / "summary.json",
            )
            cursor = _load_scan_cursor(cursor_path, target)
            time_from, time_to, cutoff = _scan_window(
                target,
                cursor,
                300,
                0,
                900,
                now=datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(time_from, "2026-07-27T07:55:00.000Z")
        self.assertEqual(time_to, "2026-07-27T08:45:00.000Z")
        self.assertEqual(cutoff, datetime(2026, 7, 27, 8, 45, tzinfo=timezone.utc))

    def test_pending_backlog_cursor_resumes_without_overlap_to_fixed_cutoff(self):
        target = DiscoverTarget(
            base_url="https://logs.example.test/_dashboards",
            data_view_id="data-view-1",
            time_from="now-2h",
            time_to="now",
        )
        completed = datetime(2026, 7, 27, 8, 2, tzinfo=timezone.utc)
        target_cutoff = datetime(2026, 7, 27, 8, 10, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cursor_path = root / "cursor.json"
            _save_scan_cursor(
                cursor_path,
                target,
                completed,
                root / "summary.json",
                backlog_pending=True,
                backlog_target_through=target_cutoff,
            )
            cursor = _load_scan_cursor(cursor_path, target)
            time_from, time_to, cutoff = _scan_window(
                target,
                cursor,
                300,
                now=datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(time_from, "2026-07-27T08:02:00.000Z")
        self.assertEqual(time_to, "2026-07-27T08:10:00.000Z")
        self.assertEqual(cutoff, target_cutoff)

    def test_legacy_cursor_is_accepted_and_migrated_on_next_save(self):
        target = DiscoverTarget(
            base_url="https://logs.example.test/_dashboards",
            data_view_id="data-view-1",
            time_from="now-2h",
            time_to="now",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cursor_path = root / "cursor.json"
            cursor_path.write_text(
                json.dumps(
                    {
                        "schema_version": LEGACY_SCAN_CURSOR_SCHEMA_VERSION,
                        "source_sha256": _scan_source_sha256(
                            target,
                            LEGACY_SCAN_QUERY_VERSION,
                        ),
                        "completed_through": "2026-07-27T08:00:00.000Z",
                        "last_summary": str(root / "old-summary.json"),
                    }
                ),
                encoding="utf-8",
            )

            cursor = _load_scan_cursor(cursor_path, target)
            _save_scan_cursor(
                cursor_path,
                target,
                datetime(2026, 7, 27, 8, 5, tzinfo=timezone.utc),
                root / "new-summary.json",
            )
            migrated = json.loads(cursor_path.read_text(encoding="utf-8"))

        self.assertEqual(
            "2026-07-27T08:00:00.000Z",
            cursor["completed_through"],
        )
        self.assertEqual("kibana-scan-cursor/v2", migrated["schema_version"])

    def test_new_cursor_initializes_from_latest_thirty_hits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cursor_path = root / "cursor.json"
            output = root / "output"
            with mock.patch.dict(
                os.environ,
                {
                    "LOG_SANITIZER_HMAC_KEY": HMAC_KEY,
                    "OPENSEARCH_PASSWORD": "password",
                },
                clear=True,
            ), mock.patch(
                "src.kibana_issue_connector.OpenSearchDashboardsClient.resolve_index_pattern",
                return_value=("logs-*", "@timestamp"),
            ), mock.patch(
                "src.kibana_issue_connector.OpenSearchDashboardsClient.fetch_latest_error_hits",
                return_value=[error_hit()],
            ) as latest, mock.patch(
                "src.kibana_issue_connector.OpenSearchDashboardsClient.fetch_error_hits"
            ) as incremental, contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "--discover-url",
                        DISCOVER_URL,
                        "--username",
                        "reader",
                        "--initial-scan-hits",
                        "30",
                        "--scan-state-file",
                        str(cursor_path),
                        "--output-dir",
                        str(output),
                        "--name",
                        "initial-scan",
                    ]
                )
            summary = json.loads(
                (output / "initial-scan" / "summary.json").read_text()
            )
            cursor_exists = cursor_path.exists()

        self.assertEqual(code, 0)
        latest.assert_called_once()
        incremental.assert_not_called()
        self.assertEqual(summary["query"]["scan_mode"], "initial_latest")
        self.assertEqual(summary["query"]["initial_scan_hits"], 30)
        self.assertTrue(cursor_exists)

    def test_history_scan_moves_only_the_separate_backward_cursor(self):
        window_end = datetime.now(timezone.utc) - timedelta(minutes=20)
        window_start = window_end - timedelta(minutes=5)
        hit_at = window_start + timedelta(seconds=1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history_path = root / "history.json"
            output = root / "output"
            with mock.patch.dict(
                os.environ,
                {
                    "LOG_SANITIZER_HMAC_KEY": HMAC_KEY,
                    "OPENSEARCH_PASSWORD": "password",
                },
                clear=True,
            ), mock.patch(
                "src.kibana_issue_connector.OpenSearchDashboardsClient.resolve_index_pattern",
                return_value=("logs-*", "@timestamp"),
            ), mock.patch(
                "src.kibana_issue_connector.OpenSearchDashboardsClient.find_previous_error_window",
                return_value=(
                    window_start.isoformat().replace("+00:00", "Z"),
                    window_end.isoformat().replace("+00:00", "Z"),
                ),
            ) as discover, mock.patch(
                "src.kibana_issue_connector.OpenSearchDashboardsClient.fetch_error_hits",
                return_value=ErrorHitBatch(
                    hits=[
                        error_hit(
                            hit_at.isoformat().replace("+00:00", "Z"),
                            "history-hit",
                        )
                    ],
                    completed_through=window_end.isoformat().replace(
                        "+00:00",
                        "Z",
                    ),
                    backlog_remaining=False,
                ),
            ), contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "--discover-url",
                        DISCOVER_URL,
                        "--username",
                        "reader",
                        "--history-state-file",
                        str(history_path),
                        "--scan-delay-seconds",
                        "900",
                        "--output-dir",
                        str(output),
                        "--name",
                        "history-scan",
                    ]
                )
            summary = json.loads(
                (output / "history-scan" / "summary.json").read_text()
            )
            cursor = json.loads(history_path.read_text())

        self.assertEqual(0, code)
        discover.assert_called_once()
        self.assertEqual("history_backfill_batch", summary["query"]["scan_mode"])
        self.assertEqual(
            window_start.isoformat(timespec="milliseconds").replace(
                "+00:00",
                "Z",
            ),
            cursor["next_before"],
        )
        self.assertTrue(summary["query"]["history_cursor_enabled"])

    def test_history_scan_finishes_pending_bucket_at_original_window_start(self):
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(minutes=30)
        pending_from = window_start + timedelta(minutes=2)
        window_end = window_start + timedelta(minutes=5)
        target = parse_discover_url(DISCOVER_URL)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history_path = root / "history.json"
            output = root / "output"
            _save_history_cursor(
                history_path,
                target,
                range_from=now - timedelta(hours=2),
                next_before=window_start,
                pending_from=pending_from,
                pending_to=window_end,
                summary_path=root / "previous.json",
            )
            with mock.patch.dict(
                os.environ,
                {
                    "LOG_SANITIZER_HMAC_KEY": HMAC_KEY,
                    "OPENSEARCH_PASSWORD": "password",
                },
                clear=True,
            ), mock.patch(
                "src.kibana_issue_connector.OpenSearchDashboardsClient.resolve_index_pattern",
                return_value=("logs-*", "@timestamp"),
            ), mock.patch(
                "src.kibana_issue_connector.OpenSearchDashboardsClient.find_previous_error_window",
            ) as discover, mock.patch(
                "src.kibana_issue_connector.OpenSearchDashboardsClient.fetch_error_hits",
                return_value=ErrorHitBatch(
                    hits=[],
                    completed_through=window_end.isoformat().replace(
                        "+00:00",
                        "Z",
                    ),
                    backlog_remaining=False,
                ),
            ), contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "--discover-url",
                        DISCOVER_URL,
                        "--username",
                        "reader",
                        "--history-state-file",
                        str(history_path),
                        "--output-dir",
                        str(output),
                        "--name",
                        "history-pending",
                    ]
                )
            cursor = json.loads(history_path.read_text())

        self.assertEqual(0, code)
        discover.assert_not_called()
        self.assertEqual(
            window_start.isoformat(timespec="milliseconds").replace(
                "+00:00",
                "Z",
            ),
            cursor["next_before"],
        )
        self.assertEqual("", cursor["pending_from"])

    def test_error_window_discovery_skips_empty_backlog_to_current_cutoff(self):
        target = parse_discover_url(DISCOVER_URL)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cursor_path = root / "cursor.json"
            output = root / "output"
            _save_scan_cursor(
                cursor_path,
                target,
                datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc),
                root / "previous-summary.json",
            )
            with mock.patch.dict(
                os.environ,
                {
                    "LOG_SANITIZER_HMAC_KEY": HMAC_KEY,
                    "OPENSEARCH_PASSWORD": "password",
                },
                clear=True,
            ), mock.patch(
                "src.kibana_issue_connector.OpenSearchDashboardsClient.resolve_index_pattern",
                return_value=("logs-*", "@timestamp"),
            ), mock.patch(
                "src.kibana_issue_connector.OpenSearchDashboardsClient.find_next_error_window",
                return_value=None,
            ) as discover, mock.patch(
                "src.kibana_issue_connector.OpenSearchDashboardsClient.fetch_error_hits"
            ) as fetch, contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "--discover-url",
                        DISCOVER_URL,
                        "--username",
                        "reader",
                        "--find-next-error-window",
                        "--scan-state-file",
                        str(cursor_path),
                        "--output-dir",
                        str(output),
                        "--name",
                        "empty-backlog",
                    ]
                )
            summary = json.loads(
                (output / "empty-backlog" / "summary.json").read_text()
            )
            cursor = json.loads(cursor_path.read_text())

        self.assertEqual(code, 0)
        discover.assert_called_once()
        fetch.assert_not_called()
        self.assertTrue(summary["query"]["error_window_discovery_used"])
        self.assertTrue(summary["query"]["empty_error_range_skipped"])
        self.assertEqual(
            cursor["completed_through"],
            summary["query"]["effective_time_to"],
        )

    def test_failed_scan_does_not_advance_existing_cursor(self):
        target = parse_discover_url(DISCOVER_URL)
        original_cutoff = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cursor_path = root / "cursor.json"
            _save_scan_cursor(
                cursor_path,
                target,
                original_cutoff,
                root / "previous-summary.json",
            )
            with mock.patch.dict(
                os.environ,
                {
                    "LOG_SANITIZER_HMAC_KEY": HMAC_KEY,
                    "OPENSEARCH_PASSWORD": "password",
                },
                clear=True,
            ), mock.patch(
                "src.kibana_issue_connector.OpenSearchDashboardsClient.resolve_index_pattern",
                return_value=("logs-*", "@timestamp"),
            ), mock.patch(
                "src.kibana_issue_connector.OpenSearchDashboardsClient.fetch_error_hits",
                side_effect=ValueError(
                    "error backlog exceeds the per-run limit; "
                    "scan cursor was not advanced"
                ),
            ), contextlib.redirect_stderr(io.StringIO()):
                code = main(
                    [
                        "--discover-url",
                        DISCOVER_URL,
                        "--username",
                        "reader",
                        "--scan-state-file",
                        str(cursor_path),
                        "--output-dir",
                        str(root / "output"),
                        "--name",
                        "failed-scan",
                    ]
                )
            cursor = json.loads(cursor_path.read_text(encoding="utf-8"))

        self.assertEqual(code, 2)
        self.assertEqual(cursor["completed_through"], "2026-07-27T08:00:00.000Z")

    def test_successful_backlog_batch_advances_only_to_safe_boundary(self):
        target = parse_discover_url(DISCOVER_URL)
        original_cutoff = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cursor_path = root / "cursor.json"
            output = root / "output"
            _save_scan_cursor(
                cursor_path,
                target,
                original_cutoff,
                root / "previous-summary.json",
            )
            with mock.patch.dict(
                os.environ,
                {
                    "LOG_SANITIZER_HMAC_KEY": HMAC_KEY,
                    "OPENSEARCH_PASSWORD": "password",
                },
                clear=True,
            ), mock.patch(
                "src.kibana_issue_connector.OpenSearchDashboardsClient.resolve_index_pattern",
                return_value=("logs-*", "@timestamp"),
            ), mock.patch(
                "src.kibana_issue_connector.OpenSearchDashboardsClient.fetch_error_hits",
                return_value=ErrorHitBatch(
                    hits=[error_hit("2026-07-27T08:01:00Z")],
                    completed_through="2026-07-27T08:02:00.000Z",
                    backlog_remaining=True,
                ),
            ), contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "--discover-url",
                        DISCOVER_URL,
                        "--username",
                        "reader",
                        "--scan-state-file",
                        str(cursor_path),
                        "--output-dir",
                        str(output),
                        "--name",
                        "backlog-batch",
                    ]
                )
            cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
            summary = json.loads(
                (output / "backlog-batch" / "summary.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(code, 0)
        self.assertEqual(cursor["completed_through"], "2026-07-27T08:02:00.000Z")
        self.assertTrue(cursor["backlog_pending"])
        self.assertTrue(summary["query"]["backlog_remaining"])
        self.assertEqual(
            "incremental_backlog_batch",
            summary["query"]["scan_mode"],
        )

    def test_deferred_backlog_batch_leaves_cursor_for_inbox_acknowledgement(self):
        target = parse_discover_url(DISCOVER_URL)
        original_cutoff = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cursor_path = root / "cursor.json"
            output = root / "output"
            _save_scan_cursor(
                cursor_path,
                target,
                original_cutoff,
                root / "previous-summary.json",
            )
            with mock.patch.dict(
                os.environ,
                {
                    "LOG_SANITIZER_HMAC_KEY": HMAC_KEY,
                    "OPENSEARCH_PASSWORD": "password",
                },
                clear=True,
            ), mock.patch(
                "src.kibana_issue_connector.OpenSearchDashboardsClient.resolve_index_pattern",
                return_value=("logs-*", "@timestamp"),
            ), mock.patch(
                "src.kibana_issue_connector.OpenSearchDashboardsClient.fetch_error_hits",
                return_value=ErrorHitBatch(
                    hits=[error_hit("2026-07-27T08:01:00Z")],
                    completed_through="2026-07-27T08:02:00.000Z",
                    backlog_remaining=True,
                ),
            ), contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "--discover-url",
                        DISCOVER_URL,
                        "--username",
                        "reader",
                        "--scan-state-file",
                        str(cursor_path),
                        "--defer-cursor-commit",
                        "--output-dir",
                        str(output),
                        "--name",
                        "deferred-backlog-batch",
                    ]
                )
            cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
            summary = json.loads(
                (output / "deferred-backlog-batch" / "summary.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(code, 0)
        self.assertEqual(cursor["completed_through"], "2026-07-27T08:00:00.000Z")
        self.assertTrue(summary["query"]["cursor_commit_deferred"])

    def test_candidate_overflow_does_not_advance_existing_cursor(self):
        target = parse_discover_url(DISCOVER_URL)
        original_cutoff = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
        first = error_hit()
        second = error_hit()
        second["_id"] = "second"
        second["_source"]["message"] = second["_source"]["message"].replace(
            "trace-demo", "trace-second"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cursor_path = root / "cursor.json"
            _save_scan_cursor(
                cursor_path,
                target,
                original_cutoff,
                root / "previous-summary.json",
            )
            with mock.patch.dict(
                os.environ,
                {
                    "LOG_SANITIZER_HMAC_KEY": HMAC_KEY,
                    "OPENSEARCH_PASSWORD": "password",
                },
                clear=True,
            ), mock.patch(
                "src.kibana_issue_connector.OpenSearchDashboardsClient.resolve_index_pattern",
                return_value=("logs-*", "@timestamp"),
            ), mock.patch(
                "src.kibana_issue_connector.OpenSearchDashboardsClient.fetch_error_hits",
                return_value=[first, second],
            ), contextlib.redirect_stderr(io.StringIO()):
                code = main(
                    [
                        "--discover-url",
                        DISCOVER_URL,
                        "--username",
                        "reader",
                        "--max-candidates",
                        "1",
                        "--scan-state-file",
                        str(cursor_path),
                        "--output-dir",
                        str(root / "output"),
                        "--name",
                        "candidate-overflow",
                    ]
                )
            cursor = json.loads(cursor_path.read_text(encoding="utf-8"))

        self.assertEqual(code, 2)
        self.assertEqual(cursor["completed_through"], "2026-07-27T08:00:00.000Z")

    def test_client_reports_read_timeout_without_remote_details(self):
        target = DiscoverTarget(
            base_url="https://logs.example.test/_dashboards",
            data_view_id="data-view-1",
            time_from="now-2h",
            time_to="now",
        )

        def timeout_opener(request, timeout):
            raise TimeoutError("synthetic transport detail")

        client = OpenSearchDashboardsClient(
            target,
            DashboardCredentials("reader", "password"),
            timeout_seconds=60,
            opener=timeout_opener,
        )

        with self.assertRaisesRegex(ValueError, "timed out after 60 seconds") as raised:
            client.resolve_index_pattern()

        self.assertNotIn("synthetic transport detail", str(raised.exception))

    def test_client_normalizes_ssl_read_timeout_without_remote_details(self):
        target = DiscoverTarget(
            base_url="https://logs.example.test/_dashboards",
            data_view_id="data-view-1",
            time_from="now-2h",
            time_to="now",
        )

        def timeout_opener(request, timeout):
            raise ssl.SSLError("The read operation timed out")

        client = OpenSearchDashboardsClient(
            target,
            DashboardCredentials("reader", "password"),
            timeout_seconds=60,
            opener=timeout_opener,
        )

        with self.assertRaisesRegex(ValueError, "timed out after 60 seconds") as raised:
            client.resolve_index_pattern()

        self.assertNotIn("read operation", str(raised.exception).casefold())

    def test_rejects_out_of_range_timeout_before_credentials(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code = main(
                [
                    "--discover-url",
                    DISCOVER_URL,
                    "--timeout-seconds",
                    "121",
                ]
            )

        self.assertEqual(code, 2)

    @mock.patch("src.kibana_issue_connector.OpenSearchDashboardsClient.fetch_error_hits")
    @mock.patch("src.kibana_issue_connector.OpenSearchDashboardsClient.resolve_index_pattern")
    def test_default_run_writes_only_sanitized_candidates(self, resolve, fetch):
        resolve.return_value = ("logs-*", "@timestamp")
        fetch.return_value = [error_hit()]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            state = root / "state.json"
            with mock.patch.dict(
                os.environ,
                {"LOG_SANITIZER_HMAC_KEY": HMAC_KEY, "OPENSEARCH_PASSWORD": "password"},
                clear=True,
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = main(
                        [
                            "--discover-url",
                            DISCOVER_URL,
                            "--username",
                            "reader",
                            "--output-dir",
                            str(output),
                            "--state-file",
                            str(state),
                            "--name",
                            "trial",
                        ]
                    )
            summary = json.loads((output / "trial" / "summary.json").read_text())
            event_text = (output / "trial" / "candidate-01" / "sanitized-incident.json").read_text()
            persisted_text = "".join(path.read_text() for path in output.rglob("*.json"))

        self.assertEqual(code, 0)
        self.assertEqual(summary["schema_version"], "kibana-issue-connector/v2")
        self.assertEqual(summary["mode"], "dry_run")
        self.assertEqual(summary["query"]["timeout_seconds"], 30)
        self.assertEqual(summary["candidates"][0]["status"], "sanitized")
        self.assertEqual(summary["candidates"][0]["event_count"], 1)
        self.assertEqual(summary["selection"]["parsed_levels"], {"ERROR": 1})
        self.assertEqual(summary["selection"]["accepted"], 1)
        self.assertNotIn("raw-document-id", event_text)
        self.assertNotIn("password", persisted_text)
        self.assertFalse(state.exists())

    @mock.patch("src.kibana_issue_connector.OpenSearchDashboardsClient.fetch_error_hits")
    @mock.patch("src.kibana_issue_connector.OpenSearchDashboardsClient.resolve_index_pattern")
    def test_candidate_limit_applies_after_incident_grouping(self, resolve, fetch):
        resolve.return_value = ("logs-*", "@timestamp")
        first = error_hit()
        first["_id"] = "aws-access-error"
        first["_source"]["@timestamp"] = "2026-07-21T07:34:44.765Z"
        first["_source"]["message"] = (
            "[2026-07-21 15:34:44.765] [TID: -] ERROR [worker-1] "
            "com.example.ObjectStorageUtils:248 - Amazon S3 returned 403 InvalidAccessKeyId"
        )
        second = error_hit()
        second["_id"] = "icon-upload-error"
        second["_source"]["@timestamp"] = "2026-07-21T07:34:44.765Z"
        second["_source"]["message"] = (
            "[2026-07-21 15:34:44.765] [TID: -] ERROR [worker-1] "
            "com.example.AssetUploadServiceImpl:108 - Fail to upload icon to S3"
        )
        unrelated = error_hit()
        unrelated["_id"] = "unrelated-error"
        unrelated["_source"]["@timestamp"] = "2026-07-21T07:34:43.000Z"
        unrelated["_source"]["message"] = (
            "[2026-07-21 15:34:43.000] [TID: -] ERROR [worker-1] "
            "com.example.PaymentService:90 - java.lang.NullPointerException"
        )
        fetch.return_value = [first, second, unrelated]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            with mock.patch.dict(
                os.environ,
                {"LOG_SANITIZER_HMAC_KEY": HMAC_KEY, "OPENSEARCH_PASSWORD": "password"},
                clear=True,
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = main(
                        [
                            "--discover-url",
                            DISCOVER_URL,
                            "--username",
                            "reader",
                            "--max-candidates",
                            "1",
                            "--output-dir",
                            str(output),
                            "--state-file",
                            str(root / "state.json"),
                            "--name",
                            "grouping-trial",
                        ]
                    )
            summary = json.loads((output / "grouping-trial" / "summary.json").read_text())

        self.assertEqual(code, 0)
        self.assertEqual(summary["selection"]["scanned_hits"], 3)
        self.assertEqual(summary["selection"]["eligible_events"], 3)
        self.assertEqual(summary["selection"]["grouped_incidents"], 2)
        self.assertEqual(summary["selection"]["accepted"], 1)
        self.assertEqual(summary["selection"]["accepted_events"], 2)
        self.assertEqual(summary["selection"]["rejected_candidate_limit"], 1)
        self.assertEqual(summary["candidates"][0]["event_count"], 2)
        self.assertEqual(summary["candidates"][0]["grouping_strategy"], "fallback_similarity")

    @mock.patch("src.kibana_issue_connector.OpenSearchDashboardsClient.fetch_error_hits")
    @mock.patch("src.kibana_issue_connector.OpenSearchDashboardsClient.resolve_index_pattern")
    def test_cross_trace_issue_signature_suppresses_duplicate_candidate(self, resolve, fetch):
        resolve.return_value = ("logs-*", "@timestamp")
        hits = []
        for document_id, trace, timestamp in (
            ("first", "trace-first", "2026-07-21T09:03:08.757Z"),
            ("second", "trace-second", "2026-07-21T09:02:59.144Z"),
        ):
            hit = error_hit()
            hit["_id"] = document_id
            hit["_source"]["@timestamp"] = timestamp
            hit["_source"]["message"] = (
                f"[2026-07-21 17:03:08.757] [TID: {trace}] ERROR [worker-1] "
                "com.example.BusinessExceptionHandler:43 - "
                "request_path=/v3/api/assistant/chat java.lang.NullPointerException: null | "
                "at com.example.AssistantChatCommand.execute(AssistantChatCommand.java:140)"
            )
            hits.append(hit)
        fetch.return_value = hits

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            with mock.patch.dict(
                os.environ,
                {"LOG_SANITIZER_HMAC_KEY": HMAC_KEY, "OPENSEARCH_PASSWORD": "password"},
                clear=True,
            ), contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "--discover-url",
                        DISCOVER_URL,
                        "--username",
                        "reader",
                        "--output-dir",
                        str(output),
                        "--state-file",
                        str(root / "state.json"),
                        "--name",
                        "signature-trial",
                    ]
                )
            summary = json.loads((output / "signature-trial" / "summary.json").read_text())

        self.assertEqual(code, 0)
        self.assertEqual(summary["selection"]["grouped_incidents"], 2)
        self.assertEqual(summary["selection"]["accepted"], 1)
        self.assertEqual(summary["selection"]["rejected_duplicate_issue_signature"], 1)
        duplicate = summary["selection"]["issue_signature_duplicates"][0]
        self.assertEqual(
            duplicate["issue_fingerprint"],
            summary["candidates"][0]["issue_signature"]["fingerprint"],
        )

    def test_publish_requires_generation_and_confirmation(self):
        with mock.patch.dict(
            os.environ,
            {"LOG_SANITIZER_HMAC_KEY": HMAC_KEY, "OPENSEARCH_PASSWORD": "password"},
            clear=True,
        ):
            with contextlib.redirect_stderr(io.StringIO()):
                code = main(
                    [
                        "--discover-url",
                        DISCOVER_URL,
                        "--username",
                        "reader",
                        "--publish",
                    ]
                )

        self.assertEqual(code, 2)

    def test_confirmed_publish_records_deduplication_state(self):
        generated = {
            "state": "ready_for_human_review",
            "validation": {"valid": True},
            "draft": {"title": "Demo issue"},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            state = root / "state.json"
            with mock.patch.dict(
                os.environ,
                {"LOG_SANITIZER_HMAC_KEY": HMAC_KEY, "OPENSEARCH_PASSWORD": "password"},
                clear=True,
            ), mock.patch(
                "src.kibana_issue_connector.OpenSearchDashboardsClient.resolve_index_pattern",
                return_value=("logs-*", "@timestamp"),
            ), mock.patch(
                "src.kibana_issue_connector.OpenSearchDashboardsClient.fetch_error_hits",
                return_value=[error_hit(), error_hit()],
            ), mock.patch(
                "src.kibana_issue_connector._gateway_config",
                return_value=SimpleNamespace(model="demo", review_model="demo"),
            ), mock.patch(
                "src.kibana_issue_connector.ai_issue_generator.generate_issue",
                return_value=generated,
            ), mock.patch(
                "src.kibana_issue_connector.ai_issue_generator.write_result"
            ), mock.patch(
                "src.kibana_issue_connector.publish_issue",
                return_value="https://github.com/acme/project/issues/12",
            ) as publish:
                with contextlib.redirect_stdout(io.StringIO()):
                    code = main(
                        [
                            "--discover-url",
                            DISCOVER_URL,
                            "--username",
                            "reader",
                            "--generate",
                            "--publish",
                            "--confirm",
                            "--max-candidates",
                            "1",
                            "--repository",
                            "acme/project",
                            "--output-dir",
                            str(output),
                            "--state-file",
                            str(state),
                            "--name",
                            "publish-trial",
                        ]
                    )
            state_payload = json.loads(state.read_text())
            summary = json.loads((output / "publish-trial" / "summary.json").read_text())

        self.assertEqual(code, 0)
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(summary["candidates"][0]["status"], "published")
        record = next(iter(state_payload["published"].values()))
        self.assertEqual(record["issue_url"], "https://github.com/acme/project/issues/12")

    def test_auto_publish_policy_skips_security_review_and_continues_safe_candidate(self):
        unsafe = error_hit()
        unsafe["_id"] = "unsafe"
        unsafe["_source"]["@timestamp"] = "2026-07-21T09:03:08.757Z"
        unsafe["_source"]["message"] = (
            "[2026-07-21 17:03:08.757] [TID: trace-unsafe] ERROR [worker-1] "
            "com.example.BusinessExceptionHandler:43 - Throws while processing request: "
            "https://internal.example.test/v3/api/assistant/chat?"
            "sign=b3da3d22b9e1383d439d4fd92359724b&appKey=private-application-key "
            "java.lang.NullPointerException | "
            "at com.example.AssistantChatCommand.execute(AssistantChatCommand.java:140)"
        )
        safe = error_hit()
        safe["_id"] = "safe"
        safe["_source"]["@timestamp"] = "2026-07-21T09:03:05.858Z"
        safe["_source"]["message"] = (
            "[2026-07-21 17:03:05.858] [TID: trace-safe] ERROR [worker-1] "
            "com.example.BusinessExceptionHandler:43 - "
            "request_path=/v1/api/resource/list java.sql.SQLException: collation failed | "
            "at com.example.ResourceQuery.execute(ResourceQuery.java:45)"
        )
        generated = {
            "state": "needs_human_context",
            "validation": {"valid": True},
            "draft": {"title": "Demo issue"},
        }
        policy = {
            "schema_version": "issue-auto-publish-policy/v1",
            "policy_id": "demo-errors-v1",
            "max_issues_per_run": 2,
            "allowed_states": ["needs_human_context"],
            "routes": [
                {
                    "route_id": "checkout",
                    "match": {"service": "demo-checkout"},
                    "provider": "github_cli",
                    "repository": "acme/project",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = root / "policy.json"
            policy_bytes = json.dumps(policy, sort_keys=True).encode()
            policy_path.write_bytes(policy_bytes)
            digest = hashlib.sha256(policy_bytes).hexdigest()
            output = root / "output"
            state = root / "state.json"
            with mock.patch.dict(
                os.environ,
                {"LOG_SANITIZER_HMAC_KEY": HMAC_KEY, "OPENSEARCH_PASSWORD": "password"},
                clear=True,
            ), mock.patch(
                "src.kibana_issue_connector.OpenSearchDashboardsClient.resolve_index_pattern",
                return_value=("logs-*", "@timestamp"),
            ), mock.patch(
                "src.kibana_issue_connector.OpenSearchDashboardsClient.fetch_error_hits",
                return_value=[unsafe, safe],
            ), mock.patch(
                "src.kibana_issue_connector._gateway_config",
                return_value=SimpleNamespace(model="demo", review_model="demo"),
            ), mock.patch(
                "src.kibana_issue_connector.ai_issue_generator.generate_issue",
                return_value=generated,
            ), mock.patch(
                "src.kibana_issue_connector.ai_issue_generator.write_result"
            ), mock.patch(
                "src.kibana_issue_connector.publish_issue",
                return_value="https://github.com/acme/project/issues/12",
            ) as publish, contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "--discover-url",
                        DISCOVER_URL,
                        "--username",
                        "reader",
                        "--generate",
                        "--auto-publish-policy",
                        str(policy_path),
                        "--confirm-policy-sha256",
                        digest,
                        "--max-candidates",
                        "2",
                        "--output-dir",
                        str(output),
                        "--state-file",
                        str(state),
                        "--name",
                        "auto-publish-trial",
                    ]
                )
            summary = json.loads((output / "auto-publish-trial" / "summary.json").read_text())

        self.assertEqual(code, 0)
        self.assertEqual(summary["mode"], "auto_publish")
        self.assertEqual(summary["selection"]["publication_blocked"], 1)
        self.assertEqual(summary["selection"]["published"], 1)
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(publish.call_args.args[2], "acme/project")
        statuses = [item["publication"]["status"] for item in summary["candidates"]]
        self.assertEqual(statuses, ["blocked", "published"])
        self.assertEqual(summary["candidates"][0]["publication"]["reason"], "security_review_required")


if __name__ == "__main__":
    unittest.main()
