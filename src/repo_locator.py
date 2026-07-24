"""Locate likely files, symbols, and lines for an Issue in a local repository."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, DefaultDict, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from src.kibana_sanitizer import Finding, redact_free_text


SOURCE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".hpp", ".java", ".js", ".jsx",
    ".kt", ".kts", ".php", ".py", ".rb", ".rs", ".scala", ".swift", ".ts", ".tsx",
}
EXCLUDED_PARTS = {".git", "build", "dist", "node_modules", "vendor", "vendors"}
MAX_FILE_BYTES = 512_000
MAX_CANDIDATES = 20
STOP_WORDS = {
    "about", "after", "again", "and", "attribute", "because", "before", "being", "between",
    "bug", "but", "change", "class", "could", "does", "empty", "error", "exists", "from",
    "given", "had", "has", "have", "instance", "instances", "into", "issue", "may", "now",
    "purpose", "returns", "since", "some", "that", "the", "this", "version", "where", "with",
    "would",
}
STRUCTURAL_WORDS = {"parent", "base", "inherit", "inherits", "mixin", "slots", "dict"}
NOTEBOOK_FRAME_PATTERN = re.compile(r"<(?:(?:i)?python-)?input-\d+-[A-Fa-f0-9]+>", re.IGNORECASE)
SOURCE_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.-])"
    r"((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+"
    r"(?:\.(?:c|cc|cpp|cs|go|h|hpp|java|js|jsx|kt|kts|php|py|rb|rs|scala|swift|ts|tsx)))"
    r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9])"
)


@dataclass(frozen=True)
class ClassInfo:
    name: str
    path: str
    line: int
    end_line: int
    bases: Tuple[str, ...]
    has_slots: bool


@dataclass
class Candidate:
    path: str
    score: float = 0.0
    line_start: int = 1
    line_end: int = 1
    symbol: str = ""
    reasons: List[str] = field(default_factory=list)

    def add(self, score: float, reason: str, line: int = 1, symbol: str = "") -> None:
        self.score += score
        if reason not in self.reasons:
            self.reasons.append(reason)
        if line > 0 and (self.line_start == 1 or score >= 8):
            self.line_start = line
            self.line_end = line
        if symbol and (not self.symbol or score >= 8):
            self.symbol = symbol


def _run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def _tracked_files(repo: Path) -> List[str]:
    try:
        output = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-z"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
        paths = [item.decode("utf-8", "surrogateescape") for item in output.split(b"\0") if item]
    except (OSError, subprocess.CalledProcessError):
        paths = [str(path.relative_to(repo)) for path in repo.rglob("*") if path.is_file()]
    return sorted(paths)


def _eligible(path: str) -> bool:
    candidate = Path(path)
    return (
        candidate.suffix.lower() in SOURCE_SUFFIXES
        and not any(part in EXCLUDED_PARTS for part in candidate.parts)
        and not candidate.name.endswith((".min.js", ".min.css"))
    )


def _read_source(repo: Path, relative: str) -> Optional[str]:
    path = repo / relative
    try:
        if path.is_symlink():
            return None
        path.resolve(strict=True).relative_to(repo.resolve(strict=True))
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _camel_parts(value: str) -> Iterable[str]:
    for part in re.split(r"[^A-Za-z0-9]+", value):
        if not part:
            continue
        yield part.lower()
        for camel in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+", part):
            if camel:
                yield camel.lower()


def _snake_case(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


def extract_terms(title: str, body: str) -> Tuple[List[str], List[str]]:
    text = f"{title}\n{body}"
    code_terms: Set[str] = set()
    for value in re.findall(r"`+([^`\n]+)`+", text):
        for term in re.findall(r"[A-Za-z_][A-Za-z0-9_.]*", value):
            if len(term) >= 2:
                code_terms.add(term)
                code_terms.add(term.rsplit(".", 1)[-1])
    for term in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", text):
        if len(term) >= 2:
            code_terms.add(term)
    words = {
        part
        for part in _camel_parts(text)
        if len(part) >= 3 and part not in STOP_WORDS and not any(character.isdigit() for character in part)
    }
    for term in code_terms:
        words.update(part for part in _camel_parts(term) if len(part) >= 3)
    return sorted(code_terms, key=lambda item: (-len(item), item)), sorted(words)


def _base_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _base_name(node.value)
    return ""


def _has_slots(node: ast.ClassDef) -> bool:
    for statement in node.body:
        targets: Sequence[ast.expr] = ()
        if isinstance(statement, ast.Assign):
            targets = statement.targets
        elif isinstance(statement, ast.AnnAssign):
            targets = (statement.target,)
        if any(isinstance(target, ast.Name) and target.id == "__slots__" for target in targets):
            return True
    return False


def _python_classes(path: str, source: str) -> List[ClassInfo]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    classes: List[ClassInfo] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            classes.append(
                ClassInfo(
                    name=node.name,
                    path=path,
                    line=node.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno),
                    bases=tuple(name for name in (_base_name(base) for base in node.bases) if name),
                    has_slots=_has_slots(node),
                )
            )
    return classes


def _best_line(source: str, code_terms: Sequence[str], words: Sequence[str]) -> Tuple[int, float, List[str]]:
    best_line = 1
    best_score = 0.0
    best_matches: List[str] = []
    for number, line in enumerate(source.splitlines(), start=1):
        matches: List[str] = []
        score = 0.0
        for term in code_terms:
            if term in line:
                score += min(12.0, 5.0 + len(term) / 4)
                matches.append(term)
        line_words = set(_camel_parts(line))
        word_matches = sorted(line_words.intersection(words))
        score += float(len(word_matches))
        matches.extend(word_matches)
        if score > best_score:
            best_line, best_score, best_matches = number, score, matches
    return best_line, best_score, best_matches[:6]


def _structural_candidates(
    classes: Sequence[ClassInfo],
    query_classes: Set[str],
    structural_query: bool,
) -> List[Tuple[ClassInfo, int, bool]]:
    by_name: DefaultDict[str, List[ClassInfo]] = defaultdict(list)
    for info in classes:
        by_name[info.name].append(info)

    discovered: List[Tuple[ClassInfo, int, bool]] = []
    queue: deque[Tuple[str, int]] = deque((name, 0) for name in sorted(query_classes))
    visited: Set[Tuple[str, str]] = set()
    while queue:
        name, depth = queue.popleft()
        if depth > 8:
            continue
        for info in by_name.get(name, []):
            identity = (info.path, info.name)
            if identity in visited:
                continue
            visited.add(identity)
            suspect = structural_query and depth > 0 and not info.has_slots
            discovered.append((info, depth, suspect))
            for base in info.bases:
                queue.append((base, depth + 1))
    return discovered


def locate_issue(repo: Path, title: str, body: str, top_k: int = 10) -> Dict[str, Any]:
    if not repo.is_dir():
        raise ValueError(f"repository path does not exist: {repo}")
    normalized_body, notebook_frame_count = NOTEBOOK_FRAME_PATTERN.subn("", body)
    safe_title, title_findings = redact_free_text(title, "issue.title")
    safe_body, body_findings = redact_free_text(normalized_body, "issue.body")
    normalization_findings = [
        Finding("issue.body", "public_notebook_frame", "normalized", "notebook-frame")
        for _ in range(notebook_frame_count)
    ]
    safety_findings: List[Finding] = normalization_findings + title_findings + body_findings
    if any(finding.action == "blocked" for finding in safety_findings):
        raise ValueError("Issue text contains unclassified high-entropy data")
    if not safe_title.strip() and not safe_body.strip():
        raise ValueError("issue title or body is required")
    top_k = max(1, min(top_k, MAX_CANDIDATES))

    code_terms, words = extract_terms(safe_title, safe_body)
    referenced_paths = sorted(set(SOURCE_PATH_PATTERN.findall(f"{safe_title}\n{safe_body}")))
    files: Dict[str, str] = {}
    classes: List[ClassInfo] = []
    candidates: Dict[str, Candidate] = {}
    for relative in _tracked_files(repo):
        if not _eligible(relative):
            continue
        source = _read_source(repo, relative)
        if source is None:
            continue
        files[relative] = source
        if relative.endswith(".py"):
            classes.extend(_python_classes(relative, source))

        line, line_score, matches = _best_line(source, code_terms, words)
        path_lower = relative.lower()
        path_score = sum(3.0 for word in words if word in path_lower)
        exact_file_score = sum(8.0 for term in code_terms if term.lower() in Path(relative).stem.lower())
        occurrence_score = 0.0
        for term in code_terms:
            count = source.count(term)
            if count:
                occurrence_score += min(10.0, 3.0 + math.log2(count + 1))
        total = line_score + path_score + exact_file_score + occurrence_score
        if "/tests/" in f"/{relative}" or Path(relative).name.startswith("test_"):
            total *= 0.82
        if total > 0:
            candidate = candidates.setdefault(relative, Candidate(relative))
            candidate.add(total, f"文本命中：{', '.join(matches) if matches else '路径关键词'}", line)

    for relative in referenced_paths:
        if relative not in files:
            continue
        candidate = candidates.setdefault(relative, Candidate(relative))
        candidate.add(80.0, f"Issue 直接引用文件 {relative}", 1)

    class_names = {info.name for info in classes}
    query_classes = {
        term.rsplit(".", 1)[-1]
        for term in code_terms
        if term.rsplit(".", 1)[-1] in class_names
    }
    query_lower = f"{safe_title}\n{safe_body}".lower()
    structural_query = "__slots__" in query_lower or "__dict__" in query_lower
    structural_query = structural_query and any(word in query_lower for word in STRUCTURAL_WORDS)
    structural = _structural_candidates(classes, query_classes, structural_query)
    reached_names: Set[str] = set()
    for info, depth, suspect in structural:
        reached_names.add(info.name)
        candidate = candidates.setdefault(info.path, Candidate(info.path))
        if depth == 0:
            candidate.add(24.0, f"Issue 直接引用类 {info.name}", info.line, info.name)
        else:
            candidate.add(
                max(4.0, 13.0 - depth),
                f"{next(iter(query_classes), '目标类')} 的第 {depth} 层父类 {info.name}",
                info.line,
                info.name,
            )
        if suspect:
            candidate.add(60.0, f"结构异常：父类 {info.name} 未定义 __slots__", info.line, info.name)

    if reached_names:
        for relative, source in files.items():
            if not ("/tests/" in f"/{relative}" or Path(relative).name.startswith("test_")):
                continue
            imported = [name for name in sorted(reached_names) if re.search(rf"\b{re.escape(name)}\b", source)]
            if not imported:
                continue
            line, _, _ = _best_line(source, imported, ())
            candidate = candidates.setdefault(relative, Candidate(relative))
            candidate.add(min(18.0, 7.0 + len(imported) * 2), f"相关回归测试候选：{', '.join(imported[:5])}", line)
            stem = Path(relative).stem.lower()
            matched_modules = [
                name
                for name in imported
                if stem == f"test_{_snake_case(name)}"
            ]
            if matched_modules:
                candidate.add(
                    34.0,
                    f"测试模块名对应继承链类：{', '.join(matched_modules)}",
                    line,
                )

    ranked = sorted(candidates.values(), key=lambda item: (-item.score, item.path))[:top_k]
    try:
        commit = _run_git(repo, "rev-parse", "HEAD")
    except (OSError, subprocess.CalledProcessError):
        commit = ""
    result = {
        "schema_version": "repo-location/v1",
        "repository_path": str(repo),
        "commit": commit,
        "query": {
            "title": safe_title.strip(),
            "body_sha256": hashlib.sha256(safe_body.encode("utf-8")).hexdigest(),
            "code_terms": code_terms[:30],
            "keywords": words[:50],
            "query_classes": sorted(query_classes),
            "referenced_paths": referenced_paths[:20],
            "safety": {
                "status": "passed_with_redactions" if safety_findings else "passed",
                "handled_categories": sorted({finding.category for finding in safety_findings}),
                "findings": [asdict(finding) for finding in safety_findings],
            },
        },
        "index": {
            "source_files": len(files),
            "python_classes": len(classes),
        },
        "candidates": [
            {
                **asdict(candidate),
                "score": round(candidate.score, 3),
                "line_end": min(candidate.line_start + 4, candidate.line_end + 4),
            }
            for candidate in ranked
        ],
    }
    return result


def load_github_issue(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid GitHub Issue JSON at line {exc.lineno}") from exc
    if not isinstance(payload, dict) or "title" not in payload or "body" not in payload:
        raise ValueError("input must be one GitHub Issue API object")
    if "pull_request" in payload:
        raise ValueError("input is a pull request, not a GitHub Issue")
    return payload


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Locate code for one GitHub Issue in a local repository.")
    parser.add_argument("issue_json", type=Path, help="GitHub Issue API JSON.")
    parser.add_argument("--repo", type=Path, required=True, help="Checked-out local repository.")
    parser.add_argument("--output", type=Path, required=True, help="Location report JSON.")
    parser.add_argument("--top-k", type=int, default=10, help="Maximum candidates to return.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        issue = load_github_issue(args.issue_json)
        result = locate_issue(args.repo, str(issue.get("title", "")), str(issue.get("body") or ""), args.top_k)
        result["source"] = {
            "type": "github_issue",
            "url": str(issue.get("html_url", "")),
            "number": issue.get("number"),
        }
        _atomic_write_json(args.output, result)
    except (FileExistsError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
