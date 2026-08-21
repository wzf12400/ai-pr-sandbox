"""仓库画像生成 + Jira 需求 AI 分类。

画像：抓取仓库 README / 目录结构 / 依赖清单，让公司 AI 网关（gpt-5-mini）
总结成中文画像（这是干什么的、有哪些功能模块、关键词），存本地 JSON，
供 Jira 需求分类反复使用。

分类：把 Jira 需求（标题+描述）和候选仓库画像一起给 AI，输出
{repository, confidence, reason}。confidence 低于阈值时返回 None，
由上层标"待人工"——宁缺毋滥，与锚点路由同一哲学。

任何网络/解析失败都返回 None（fail-open），不影响现有链路。
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

SCOPE_PATH = Path("control-plane/config/repository-search-scope.json")
PROFILES_PATH = Path(".issue-entry-state/repo-profiles.json")
PROFILE_TTL_SECONDS = 7 * 24 * 3600  # 画像一周有效
GITHUB_TIMEOUT = 20
AI_TIMEOUT = 90
USER_AGENT = "ai-pr-sandbox-repo-profiler"

# 抓目录时关注的路径片段，用于推断功能模块
INTERESTING_DIR_PATTERN = ("controller", "service", "mapper", "module", "api", "pages", "components")

PROFILE_PROMPT = """你是资深工程师。根据下面的仓库材料，生成一段中文仓库画像 JSON。

仓库：{repo}

=== README（截断） ===
{readme}

=== 目录结构（部分） ===
{tree}

=== 依赖清单（截断） ===
{manifest}

要求只输出 JSON，不要任何其他文字：
{{
  "summary": "2-4 句中文：这个仓库是什么业务的前端/后端，负责哪些核心功能",
  "keywords": ["8-15 个关键词，中英文混合，覆盖业务名词、功能模块、技术栈"],
  "modules": ["3-8 个主要功能模块名"]
}}"""

CLASSIFY_PROMPT = """你是资深工程师，负责把 Jira 需求分配到正确的代码仓库。

=== Jira 需求 ===
项目：{project}
标题：{title}
描述：{description}

=== 候选仓库画像 ===
{profiles}

要求：
1. 逐条对比需求内容与各仓库画像，判断需求需要改哪些仓库的代码
2. 需求明显只涉及一个仓库时只返回一个；前后端都要改时（例如既要改接口
   又要改界面），把涉及的仓库全部列出，每个仓库单独给置信度和理由
3. 只输出 JSON，不要任何其他文字：
{{
  "repositories": [
    {{"repository": "候选列表中的仓库全名", "confidence": 0-100 的整数置信度, "reason": "一句中文理由，说明这个仓库要改什么"}}
  ],
  如果实在无法判断，"repositories" 返回空数组
}}
4. 拿不准就降低 confidence，不要硬猜；宁缺毋滥，不确定的仓库不要列"""


def _github_get(url: str, token: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=GITHUB_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _chat(prompt: str, base_url: str, api_key: str, model: str, safety_id: str) -> Optional[str]:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    if safety_id:
        payload["safety_identifier"] = safety_id
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=AI_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
        return (data.get("choices") or [{}])[0].get("message", {}).get("content")
    except (urllib.error.HTTPError, OSError, ValueError):
        return None


def _parse_json_object(text: Optional[str]) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def fetch_repo_materials(repo: str, token: str) -> Dict[str, str]:
    """抓 README + 目录结构 + 依赖清单，全部截断，失败部分留空。"""
    materials = {"readme": "", "tree": "", "manifest": ""}
    try:
        readme = _github_get(f"https://api.github.com/repos/{repo}/readme", token)
        content = readme.get("content") or ""
        materials["readme"] = base64.b64decode(content).decode("utf-8", "ignore")[:3000]
    except Exception:
        pass
    try:
        branch = _github_get(f"https://api.github.com/repos/{repo}", token).get("default_branch", "main")
        tree = _github_get(
            f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1", token
        )
        paths = [
            item["path"] for item in (tree.get("tree") or [])
            if item.get("type") == "blob"
        ]
        interesting = [p for p in paths if any(k in p.lower() for k in INTERESTING_DIR_PATTERN)]
        selected = (interesting[:120] + paths[:80])[:200]
        materials["tree"] = "\n".join(dict.fromkeys(selected))
    except Exception:
        pass
    for manifest_name in ("pom.xml", "package.json", "build.gradle"):
        try:
            raw = _github_get(
                f"https://api.github.com/repos/{repo}/contents/{manifest_name}", token
            )
            materials["manifest"] = base64.b64decode(
                raw.get("content") or ""
            ).decode("utf-8", "ignore")[:2000]
            break
        except Exception:
            continue
    return materials


def build_profile(
    repo: str, materials: Dict[str, str], ai: Dict[str, str]
) -> Optional[Dict[str, Any]]:
    prompt = PROFILE_PROMPT.format(repo=repo, **materials)
    payload = _parse_json_object(
        _chat(prompt, ai["base_url"], ai["api_key"], ai["model"], ai.get("safety_id", ""))
    )
    if not payload or not payload.get("summary"):
        return None
    return {
        "repository": repo,
        "summary": str(payload["summary"]),
        "keywords": [str(k) for k in (payload.get("keywords") or [])][:15],
        "modules": [str(m) for m in (payload.get("modules") or [])][:8],
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def load_profiles(path: Path = PROFILES_PATH) -> Dict[str, Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("profiles"), dict):
            return payload["profiles"]
    except (OSError, ValueError):
        pass
    return {}


def save_profiles(profiles: Dict[str, Dict[str, Any]], path: Path = PROFILES_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"version": 1, "profiles": profiles}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.chmod(0o600)
    tmp.replace(path)


def generate_profiles(
    repos: List[str],
    token: str,
    ai: Dict[str, str],
    path: Path = PROFILES_PATH,
    force: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """给每个仓库生成画像；已有未过期画像的跳过，除非 force。"""
    existing = load_profiles(path)
    profiles = dict(existing)
    for repo in repos:
        old = existing.get(repo)
        if old and not force:
            try:
                age = time.time() - time.mktime(
                    time.strptime(old.get("updatedAt", ""), "%Y-%m-%dT%H:%M:%SZ")
                )
                if age < PROFILE_TTL_SECONDS:
                    continue
            except ValueError:
                pass
        materials = fetch_repo_materials(repo, token)
        profile = build_profile(repo, materials, ai)
        if profile:
            profiles[repo] = profile
    save_profiles(profiles, path)
    return profiles


def classify_issue(
    project: str,
    title: str,
    description: str,
    candidate_repos: List[str],
    ai: Dict[str, str],
    profiles_path: Path = PROFILES_PATH,
    min_confidence: int = 70,
) -> Optional[Dict[str, Any]]:
    """用画像给 Jira 需求分类，可返回多个仓库（前后端都要改的场景）。

    置信度不足或失败返回 None（上层标待人工）。
    """
    all_profiles = load_profiles(profiles_path)
    candidates = [all_profiles[r] for r in candidate_repos if r in all_profiles]
    if not candidates:
        return None
    profiles_text = "\n\n".join(
        f"仓库 {p['repository']}\n简介：{p['summary']}\n"
        f"关键词：{'、'.join(p['keywords'])}\n模块：{'、'.join(p['modules'])}"
        for p in candidates
    )
    prompt = CLASSIFY_PROMPT.format(
        project=project,
        title=title[:300],
        description=(description or "")[:1500],
        profiles=profiles_text,
    )
    payload = _parse_json_object(
        _chat(prompt, ai["base_url"], ai["api_key"], ai["model"], ai.get("safety_id", ""))
    )
    if not payload:
        return None
    raw_matches = payload.get("repositories")
    if raw_matches is None and payload.get("repository"):
        # 兼容旧版单仓库回答格式
        raw_matches = [
            {
                "repository": payload.get("repository"),
                "confidence": payload.get("confidence"),
                "reason": payload.get("reason"),
            }
        ]
    if not isinstance(raw_matches, list):
        return None
    matches: List[Dict[str, Any]] = []
    seen: set = set()
    for item in raw_matches:
        if not isinstance(item, dict):
            continue
        repo = item.get("repository")
        if not repo or repo not in candidate_repos or repo in seen:
            continue
        try:
            confidence = int(item.get("confidence"))
        except (TypeError, ValueError):
            continue
        if confidence < min_confidence:
            continue
        seen.add(repo)
        matches.append(
            {
                "repository": repo,
                "confidence": confidence,
                "reason": str(item.get("reason") or ""),
            }
        )
    if not matches:
        return None
    matches.sort(key=lambda m: -int(m["confidence"]))
    top = matches[0]
    if len(matches) == 1:
        basis = f"ai classification (confidence {top['confidence']}): {top['reason']}"
    else:
        basis = "ai classification (multi-repo): " + "；".join(
            f"{m['repository']} (confidence {m['confidence']}): {m['reason']}"
            for m in matches
        )
    return {
        "repository": top["repository"],
        "confidence": top["confidence"],
        "reason": top["reason"],
        "matches": matches,
        "basis": basis,
    }
