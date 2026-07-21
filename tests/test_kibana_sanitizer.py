import json
import unittest
from pathlib import Path

from src.kibana_sanitizer import redact_free_text, sanitize_hit


ROOT = Path(__file__).resolve().parents[1]
TEST_KEY = b"local-test-hmac-key-that-is-at-least-32-bytes"


def raw_hit():
    return json.loads((ROOT / "examples" / "kibana_raw.json").read_text(encoding="utf-8"))


class KibanaSanitizerTest(unittest.TestCase):
    def test_info_log_is_parsed_but_not_selected_for_issue(self) -> None:
        result = sanitize_hit(raw_hit(), TEST_KEY)

        self.assertEqual("INFO", result["event"]["level"])
        self.assertFalse(result["event"]["is_error"])
        self.assertFalse(result["event"]["is_issue_candidate"])
        self.assertEqual("synthetic-backend", result["target"]["service"])
        self.assertEqual("pageResourcesNew", result["target"]["business_method"])
        self.assertEqual(56, result["target"]["logger_line"])
        self.assertEqual(14, result["event"]["duration_ms"])

    def test_sensitive_and_infrastructure_values_do_not_leave_the_sanitizer(self) -> None:
        payload = raw_hit()
        forbidden = [
            payload["_id"],
            "0123456789abcdef0123456789abcdef.1.2",
            "synthetic-device-id-0001",
            "synthetic-api-key-value-that-must-not-leak",
            "synthetic-pod-id-001",
            "ip-10-0-0-1.example.internal",
            payload["_source"]["kubernetes"]["docker_id"],
        ]

        result = sanitize_hit(payload, TEST_KEY)
        serialized = json.dumps(result, ensure_ascii=False)

        for value in forbidden:
            self.assertNotIn(value, serialized)
        self.assertIn("event_ref:", result["source"]["event_ref"])
        self.assertIn("trace_ref:", result["event"]["trace_ref"])
        self.assertTrue(result["sanitization"]["security_review_required"])
        self.assertFalse(result["sanitization"]["github_issue_allowed"])

    def test_hmac_references_are_stable_and_source_specific(self) -> None:
        first = sanitize_hit(raw_hit(), TEST_KEY)
        second = sanitize_hit(raw_hit(), TEST_KEY)
        changed = raw_hit()
        changed["_id"] = "synthetic-document-id-002"
        third = sanitize_hit(changed, TEST_KEY)

        self.assertEqual(first["source"]["event_ref"], second["source"]["event_ref"])
        self.assertNotEqual(first["source"]["event_ref"], third["source"]["event_ref"])

    def test_placeholder_trace_is_not_treated_as_a_shared_trace(self) -> None:
        payload = raw_hit()
        payload["_source"]["message"] = payload["_source"]["message"].replace(
            "TID: 0123456789abcdef0123456789abcdef.1.2", "TID: -"
        )

        result = sanitize_hit(payload, TEST_KEY)

        self.assertEqual(result["event"]["trace_ref"], "")

    def test_known_secret_formats_are_removed_without_echoing_values(self) -> None:
        samples = {
            "password": ("password=very-simple-password", "very-simple-password"),
            "authorization": (
                "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
                "abcdefghijklmnopqrstuvwxyz",
            ),
            "cookie": ("Cookie: session=secret-session-value; locale=en", "secret-session-value"),
            "connection_string": (
                "postgres://user:database-password@db.internal/example",
                "database-password",
            ),
            "private_key": (
                "-----BEGIN PRIVATE KEY-----\nsecretmaterial\n-----END PRIVATE KEY-----",
                "secretmaterial",
            ),
            "token": (
                "eyJabcdefghijk.abcdefghijklmnop.abcdefghijklmnop",
                "eyJabcdefghijk.abcdefghijklmnop.abcdefghijklmnop",
            ),
        }
        for expected_category, (value, secret) in samples.items():
            with self.subTest(category=expected_category):
                sanitized, findings = redact_free_text(value)
                self.assertNotIn(secret, sanitized)
                self.assertTrue(any(finding.category == expected_category for finding in findings))
                self.assertFalse(any(secret in str(finding) for finding in findings))

    def test_json_quoted_secrets_and_encrypted_private_keys_are_removed(self) -> None:
        samples = (
            ('{"Authorization":"Bearer quoted-token-value"}', "quoted-token-value"),
            ('{"Cookie":"session=quoted-cookie-value"}', "quoted-cookie-value"),
            ('{"password":"quoted-password-value"}', "quoted-password-value"),
            (
                "-----BEGIN ENCRYPTED PRIVATE KEY-----\nencryptedmaterial\n"
                "-----END ENCRYPTED PRIVATE KEY-----",
                "encryptedmaterial",
            ),
        )
        for value, secret in samples:
            with self.subTest(secret=secret):
                sanitized, findings = redact_free_text(value)
                self.assertNotIn(secret, sanitized)
                self.assertTrue(findings)

    def test_unknown_high_entropy_value_blocks_processing(self) -> None:
        payload = raw_hit()
        payload["_source"]["message"] += " unknownBlob=QWxhZGRpbjpvcGVuIHNlc2FtZV9yYW5kb21WYWx1ZQ=="

        result = sanitize_hit(payload, TEST_KEY)
        serialized = json.dumps(result, ensure_ascii=False)

        self.assertEqual("blocked", result["sanitization"]["status"])
        self.assertFalse(result["sanitization"]["ai_allowed"])
        self.assertFalse(result["event"]["is_issue_candidate"])
        self.assertNotIn("QWxhZGRpbjpvcGVuIHNlc2FtZV9yYW5kb21WYWx1ZQ==", serialized)

    def test_request_context_is_minimized_without_relaxing_entropy_gate(self) -> None:
        payload = raw_hit()
        payload["_source"]["message"] = (
            "[2026-07-21 15:34:35.853] [TID: -] ERROR [worker-1] "
            "com.example.BusinessExceptionHandler:43 - Throws while processing request: "
            "https://internal.example.test/v1/api/user/block/resourceList?"
            "sign=b3da3d22b9e1383d439d4fd92359724b&"
            "appKey=private-application-key&"
            "opaque=QWxhZGRpbjpvcGVuIHNlc2FtZV9yYW5kb21WYWx1ZQ== "
            "com.example.sample.application/4197 "
            "(f88d4d215f074792971543c8f1f94a08/4108130a5ef56b6ae98e14d03b1b274a) "
            "Country/US org.springframework.jdbc.UncategorizedSQLException: failed | "
            "at com.example.command.SensitiveTextCommand.execute"
            "(SensitiveTextCommand.java:119) | "
            "class path resource [com/example/VeryLongAssetResourceMapper.xml]"
        )

        result = sanitize_hit(payload, TEST_KEY)
        serialized = json.dumps(result, ensure_ascii=False)

        self.assertTrue(result["sanitization"]["ai_allowed"])
        self.assertTrue(result["sanitization"]["security_review_required"])
        self.assertFalse(result["sanitization"]["github_issue_allowed"])
        self.assertNotIn("unclassified_high_entropy", serialized)
        self.assertIn("request_path=/v1/api/user/block/resourceList", serialized)
        self.assertIn("UncategorizedSQLException", serialized)
        self.assertIn("VeryLongAssetResourceMapper.xml", serialized)
        for forbidden in (
            "internal.example.test",
            "b3da3d22b9e1383d439d4fd92359724b",
            "private-application-key",
            "QWxhZGRpbjpvcGVuIHNlc2FtZV9yYW5kb21WYWx1ZQ==",
            "f88d4d215f074792971543c8f1f94a08",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_high_entropy_url_path_identifier_is_removed(self) -> None:
        identifier = "c3de2802001e4cb9a76c5124df1dfd2f"

        sanitized, findings = redact_free_text(
            f"https://internal.example.test/v1/api/category/{identifier}/resources"
        )

        self.assertEqual(
            sanitized,
            "request_path=/v1/api/category/[REDACTED:path_segment]/resources",
        )
        self.assertNotIn(identifier, sanitized)
        self.assertTrue(any(item.category == "path_identifier" for item in findings))

    def test_java_identifiers_are_allowed_only_in_code_contexts(self) -> None:
        samples = (
            "java.lang.StringIndexOutOfBoundsException: index -2",
            "org.springframework.jdbc.UncategorizedSQLException: failed",
            "at com.example.command.SensitiveTextCommand.execute"
            "(SensitiveTextCommand.java:119)",
            "class path resource [com/example/VeryLongAssetResourceMapper.xml]",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                sanitized, findings = redact_free_text(sample)

                self.assertEqual(sanitized, sample)
                self.assertFalse(any(item.action == "blocked" for item in findings))

    def test_sql_statement_is_removed_before_entropy_detection(self) -> None:
        statement = (
            "### SQL: select very_long_internal_column_name from private_table "
            "where api_token = 'QWxhZGRpbjpvcGVuIHNlc2FtZV9yYW5kb21WYWx1ZQ==' "
            "| ### Cause: java.sql.SQLException: Illegal mix of collations"
        )

        sanitized, findings = redact_free_text(statement)

        self.assertEqual(
            sanitized,
            "### SQL: [REDACTED:sql_statement] "
            "| ### Cause: java.sql.SQLException: Illegal mix of collations",
        )
        self.assertTrue(
            any(
                item.category == "sql_statement" and item.action == "removed"
                for item in findings
            )
        )
        self.assertFalse(any(item.action == "blocked" for item in findings))
        self.assertNotIn("private_table", sanitized)
        self.assertNotIn("QWxhZGRpbjpvcGVuIHNlc2FtZV9yYW5kb21WYWx1ZQ==", sanitized)

    def test_error_level_is_selected_after_sanitization(self) -> None:
        payload = raw_hit()
        payload["_source"]["message"] = payload["_source"]["message"].replace(" INFO ", " ERROR ")

        result = sanitize_hit(payload, TEST_KEY)

        self.assertTrue(result["event"]["is_error"])
        self.assertTrue(result["event"]["is_issue_candidate"])

    def test_short_hmac_key_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            sanitize_hit(raw_hit(), b"too-short")


if __name__ == "__main__":
    unittest.main()
