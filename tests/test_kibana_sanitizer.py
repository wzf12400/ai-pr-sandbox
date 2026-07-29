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
            "at com.example.aop.OperationPlatformLogAspect.saveUserOperateLog"
            "(OperationPlatformLogAspect.java:93)",
            "at sun.reflect.DelegatingMethodAccessorImpl.invoke"
            "(DelegatingMethodAccessorImpl.java:43)",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                sanitized, findings = redact_free_text(sample)

                self.assertEqual(sanitized, sample)
                self.assertFalse(any(item.action == "blocked" for item in findings))

    def test_linux_versions_are_allowed_narrowly(self) -> None:
        sample = "Linux-4.4.0-148-generic-x86_64-with-Ubuntu-14.04-trusty"

        sanitized, findings = redact_free_text(sample)

        self.assertEqual(sample, sanitized)
        self.assertFalse(any(item.action == "blocked" for item in findings))

    def test_python_traceback_path_is_minimized_without_blocking(self) -> None:
        sample = (
            '  File "/usr/lib/python3/dist-packages/astropy/table/connect.py", '
            "line 129, in __call__"
        )

        sanitized, findings = redact_free_text(sample)

        self.assertIn("dist-packages/astropy/table/connect.py", sanitized)
        self.assertNotIn("/usr/lib/python3", sanitized)
        self.assertFalse(any(item.action == "blocked" for item in findings))
        self.assertTrue(
            any(item.category == "code_path_prefix" for item in findings)
        )

    def test_poetry_traceback_path_is_minimized_without_blocking(self) -> None:
        opaque_environment = "dj-bug-demo-FlhD0jMY-py3"
        sample = (
            '  File "/home/user/.cache/pypoetry/virtualenvs/'
            f"{opaque_environment}/lib/python3.10/site-packages/"
            'django/core/handlers/exception.py", line 34, in inner'
        )

        sanitized, findings = redact_free_text(sample)

        self.assertIn(
            "site-packages/django/core/handlers/exception.py",
            sanitized,
        )
        self.assertNotIn(opaque_environment, sanitized)
        self.assertFalse(any(item.action == "blocked" for item in findings))

    def test_relative_code_path_is_classified_without_blocking(self) -> None:
        sample = "django/db/backends/sqlite3/operations failed to adapt the value"

        sanitized, findings = redact_free_text(sample)

        self.assertIn("django/db/backends/sqlite3/operations", sanitized)
        self.assertFalse(any(item.action == "blocked" for item in findings))
        self.assertTrue(any(item.category == "code_path" for item in findings))

    def test_relative_code_path_redacts_only_opaque_segment(self) -> None:
        opaque = "qnfSqro0DlA9xZ4pW8vN6tR2"
        sample = f"topic/django-developers/{opaque}/thread"

        sanitized, findings = redact_free_text(sample)

        self.assertNotIn(opaque, sanitized)
        self.assertIn("[REDACTED:path_segment]", sanitized)
        self.assertFalse(any(item.action == "blocked" for item in findings))

    def test_python_traceback_path_with_opaque_segment_is_redacted_not_blocked(self) -> None:
        opaque = "QWxhZGRpbjpvcGVuIHNlc2FtZV9yYW5kb21WYWx1ZQ=="
        sample = f'  File "/tmp/{opaque}/connect.py", line 129, in __call__'

        sanitized, findings = redact_free_text(sample)

        self.assertNotIn(opaque, sanitized)
        self.assertIn("[REDACTED:path_segment]", sanitized)
        self.assertTrue(
            any(
                item.category == "path_identifier"
                and item.action == "removed"
                for item in findings
            )
        )
        self.assertFalse(any(item.action == "blocked" for item in findings))

    def test_git_object_identifier_is_redacted_without_blocking(self) -> None:
        commits = (
            "5ceaf14686ce626404afb6a5fbd3d8286410bf13",
            "6bb2b855498b5c68d7cca8cceb710365d58e604",
        )
        for commit in commits:
            with self.subTest(commit=commit):
                sanitized, findings = redact_free_text(
                    f"Regression introduced in commit {commit}."
                )

                self.assertNotIn(commit, sanitized)
                self.assertIn("[REDACTED:commit_identifier]", sanitized)
                self.assertFalse(any(item.action == "blocked" for item in findings))

    def test_notebook_frames_and_traceback_function_names_are_code_context(self) -> None:
        samples = (
            "<ipython-input-13-2486f2ccf928> in <module>",
            (
                '  File "/srv/app/tests/test_policy.py", line 481, '
                "in test_checkpolicywarning_by_fields"
            ),
        )
        for sample in samples:
            with self.subTest(sample=sample):
                sanitized, findings = redact_free_text(sample)

                self.assertFalse(any(item.action == "blocked" for item in findings))
                self.assertNotIn("[REDACTED:unclassified_high_entropy]", sanitized)

    def test_long_identifiers_are_allowed_only_in_explicit_code_syntax(self) -> None:
        samples = (
            (
                "return WSGIServer((self.host, self.port), "
                "QuietWSGIRequestHandler, allow_reuse_address=False)"
            ),
            (
                "ALTER TABLE `profile` ADD CONSTRAINT "
                "`b_manage_profile_account_id_ec864dcc_fk` "
                "FOREIGN KEY (`account_id`) REFERENCES `account` (`id`)"
            ),
            (
                "design.py:323:8: W0201: Attribute "
                "'actionLoop_Last_Split_Image_To_First_Image' "
                "defined outside __init__"
            ),
            (
                "compiler_nameop: Assertion "
                "`!_PyUnicode_EqualToASCIIString(name, \"None\")' failed"
            ),
        )
        for sample in samples:
            with self.subTest(sample=sample):
                sanitized, findings = redact_free_text(sample)

                self.assertEqual(sample, sanitized)
                self.assertFalse(any(item.action == "blocked" for item in findings))

    def test_existing_redaction_marker_is_not_redacted_again(self) -> None:
        marker = "[REDACTED:unclassified_high_entropy]"

        sanitized, findings = redact_free_text(f"before {marker} after")

        self.assertEqual(sanitized, f"before {marker} after")
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
