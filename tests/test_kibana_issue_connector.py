import base64
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.kibana_issue_connector import (
    DashboardCredentials,
    DiscoverTarget,
    OpenSearchDashboardsClient,
    _credentials,
    main,
    parse_discover_url,
)


DISCOVER_URL = (
    "https://logs.example.test/_dashboards/app/discover#/"
    "?_g=(filters:!(),time:(from:now-2h,to:now))"
    "&_a=(index:ee351460-8261-11f0-bb8a-4fb3796753f3,query:(language:kuery,query:''))"
)
HMAC_KEY = "0123456789abcdef0123456789abcdef"


def error_hit():
    return {
        "_index": "logs-demo",
        "_id": "raw-document-id",
        "_source": {
            "@timestamp": "2099-01-01T00:00:00Z",
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
        client = OpenSearchDashboardsClient(target, credentials, opener=opener)

        index_pattern, time_field = client.resolve_index_pattern()
        hits = client.fetch_error_hits(index_pattern, time_field, 25)

        self.assertEqual(index_pattern, "logs-*")
        self.assertEqual(len(hits), 1)
        self.assertNotIn("password", repr(credentials))
        request = opener.requests[1][0]
        expected_auth = "Basic " + base64.b64encode(b"reader:password").decode()
        self.assertEqual(request.headers["Authorization"], expected_auth)
        payload = json.loads(request.data)
        self.assertEqual(payload["size"], 25)
        self.assertIn("message", payload["_source"])
        self.assertNotIn("kubernetes.pod_name", payload["_source"])
        self.assertEqual(
            payload["query"]["bool"]["filter"][0]["range"]["@timestamp"],
            {"gte": "now-2h", "lte": "now"},
        )

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
            event_text = (output / "trial" / "candidate-01" / "sanitized-event.json").read_text()
            persisted_text = "".join(path.read_text() for path in output.rglob("*.json"))

        self.assertEqual(code, 0)
        self.assertEqual(summary["mode"], "dry_run")
        self.assertEqual(summary["candidates"][0]["status"], "sanitized")
        self.assertNotIn("raw-document-id", event_text)
        self.assertNotIn("password", persisted_text)
        self.assertFalse(state.exists())

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


if __name__ == "__main__":
    unittest.main()
