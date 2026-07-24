"""Use the local employee's Copilot CLI as a no-tools structured Issue provider."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from src.ai_issue_generator import Completion
from src.copilot_code_modifier import _run_process


MAX_RESPONSE_BYTES = 1_000_000
MODEL_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,100}")


def _structured_prompt(
    system_prompt: str,
    user_payload: Mapping[str, Any],
    schema_name: str,
    schema: Mapping[str, Any],
) -> str:
    return (
        f"{system_prompt}\n\n"
        "You are running without tools, network access, memory, or repository access.\n"
        "Return exactly one JSON object and no Markdown fence, commentary, or preamble.\n"
        f"Required schema name: {schema_name}\n"
        "Required JSON Schema:\n"
        f"{json.dumps(schema, ensure_ascii=False, sort_keys=True)}\n"
        "Untrusted input payload:\n"
        f"{json.dumps(user_payload, ensure_ascii=False, sort_keys=True)}\n"
    )


def _parse_json_object(output: str) -> Dict[str, Any]:
    text = output.strip()
    if len(text.encode("utf-8", "replace")) > MAX_RESPONSE_BYTES:
        raise ValueError("Copilot structured response exceeded the size limit")
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[0] in {"```", "```json"} and lines[-1] == "```":
            text = "\n".join(lines[1:-1]).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("Copilot did not return one valid JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError("Copilot structured response must be a JSON object")
    return payload


class CopilotCLIIssueProvider:
    """Generate one structured object without giving Copilot any tools."""

    def __init__(
        self,
        model: str,
        *,
        executable: str = "copilot",
        timeout_seconds: int = 180,
    ):
        if not MODEL_PATTERN.fullmatch(model):
            raise ValueError("Copilot Issue model is invalid")
        if not 1 <= timeout_seconds <= 300:
            raise ValueError("Copilot Issue timeout must be between 1 and 300 seconds")
        self.model = model
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    def _environment(self) -> Dict[str, str]:
        inherited: Sequence[str] = (
            "HOME",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "LOGNAME",
            "PATH",
            "SHELL",
            "TERM",
            "TMPDIR",
            "USER",
        )
        return {key: value for key, value in os.environ.items() if key in inherited}

    def complete(
        self,
        *,
        system_prompt: str,
        user_payload: Dict[str, Any],
        schema_name: str,
        schema: Dict[str, Any],
    ) -> Completion:
        if not shutil.which(self.executable):
            raise ValueError("Copilot CLI is not installed")
        prompt = _structured_prompt(system_prompt, user_payload, schema_name, schema)
        args = [
            self.executable,
            "-s",
            "--no-ask-user",
            "--no-auto-update",
            "--no-custom-instructions",
            "--no-experimental",
            "--no-remote",
            "--no-remote-export",
            "--disable-builtin-mcps",
            "--disallow-temp-dir",
            "--available-tools=",
            "--model",
            self.model,
            "--deny-tool=shell,write,edit,view,grep,glob",
            "--deny-url",
            "--log-level=none",
        ]
        started = time.monotonic()
        environment = self._environment()
        with tempfile.TemporaryDirectory(prefix="issue-copilot-provider-") as directory:
            root = Path(directory)
            environment["COPILOT_HOME"] = str(root / "copilot-home")
            args.append(f"--log-dir={root / 'logs'}")
            result = _run_process(
                args,
                root,
                self.timeout_seconds,
                input_text=prompt,
                env=environment,
            )
        if result.returncode != 0:
            raise ValueError("Copilot structured Issue generation failed")
        content = _parse_json_object(result.stdout)
        request_material = (
            f"{self.model}\n{schema_name}\n"
            f"{hashlib.sha256(prompt.encode('utf-8')).hexdigest()}\n"
            f"{time.monotonic() - started:.6f}"
        )
        return Completion(
            content=content,
            request_id="copilot-local:" + hashlib.sha256(
                request_material.encode("utf-8")
            ).hexdigest()[:24],
            model=self.model,
            usage={},
        )
