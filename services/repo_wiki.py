"""
RepoWiki 自动化生成与 GitLab 写回服务（纯异步机器人端执行）

职责：
1. 独立于发版锁（repo_lock）和 Sonar 锁（sonar_lock），使用 wiki_lock 控制并发
2. 基于目标分支 commit_sha + Redis 基线缓存实现无变更快速跳过（支持 force 强制重建）
3. 通过 GitLab Archive API 下载源码快照并在内存安全解包，调用 Anthropic 兼容 LLM 生成证据驱动的 Wiki
4. 使用 Multi-file Commits API 写回目标分支（含 [skip ci] 防循环）；遇保护分支 400/403 自动切出 docs/repowiki-<branch> 分支并提交 MR
5. 实时刷新飞书进度卡片并同步发送结果通知（含审计群同步）
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from core.card_builder import build_wiki_card
from core.feishu_client import FeishuClient
from core.gitlab_client import GitLabClient
from core.metrics import WIKI_GEN_COMPLETED, WIKI_GEN_DURATION, WIKI_GEN_TRIGGERED
from core.redis_client import get_redis
from core.state import (
    clear_wiki_cancel,
    get_wiki_cancel_info,
    is_wiki_locked,
    request_wiki_cancel,
    unlock_wiki,
)
from core.wiki_engine import (
    DEFAULT_CONTEXT_CHARS,
    DEFAULT_FILE_CHAR_LIMIT,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_MAX_PAGES,
    DEFAULT_PAGE_CONCURRENCY,
    DEFAULT_WIKI_DIR,
    MAX_PAGES_HARD_CEILING,
    AnthropicWikiModel,
    compute_wiki_config_hash,
    generate_wiki_bundle,
    git_blob_sha1,
    scan_archive_bytes,
)

logger = logging.getLogger("feishu_gitlab_card_http")

# 当前进程内正在运行的 RepoWiki 后台任务映射（lock_scope -> asyncio.Task）
_running_wiki_tasks: Dict[str, asyncio.Task] = {}


async def cancel_running_wiki(
    lock_scope: str,
    canceler_open_id: str = "",
    canceler_name: str = "",
) -> Dict[str, Any]:
    """取消正在进行的 RepoWiki 生成任务（同时清理重启遗留锁）"""
    task = _running_wiki_tasks.get(lock_scope)
    locked = await is_wiki_locked(lock_scope)
    if (task is None or task.done()) and not locked:
        return {"ok": False, "stale_unlocked": False, "msg": "当前 RepoWiki 任务已结束或未在运行中"}

    existing = await get_wiki_cancel_info(lock_scope)
    if existing and task and not task.done():
        return {"ok": True, "stale_unlocked": False, "msg": "⏳ 正在中止 RepoWiki 任务，请稍候..."}

    await request_wiki_cancel(
        lock_scope,
        {"canceled_by": canceler_open_id, "canceled_by_name": canceler_name},
    )
    if task and not task.done():
        task.cancel()
        return {"ok": True, "stale_unlocked": False, "msg": "🛑 已发送取消指令，正在停止生成..."}

    await unlock_wiki(lock_scope)
    await clear_wiki_cancel(lock_scope)
    return {"ok": True, "stale_unlocked": True, "msg": "🛑 原后台任务已结束，已释放并发锁"}


def build_wiki_lock_scope(
    state: Dict[str, Any], subpath: str = "", cfg: Optional[Dict[str, Any]] = None
) -> str:
    """生成独立的 RepoWiki 并发锁标识（按仓库 + 分支 + 子目录隔离）"""
    repo_id = state.get("repo_id", 0)
    branch = state.get("branch", "main")
    resolved_sub = subpath or state.get("wiki_subpath") or ""
    if not resolved_sub and cfg:
        repo_conf = _find_repo_conf(cfg, state)
        resolved_sub = resolve_wiki_settings(cfg, repo_conf)["subpath"]
    clean_sub = str(resolved_sub).strip("/") or "all"
    return f"{repo_id}:{branch}:{clean_sub}"


def _find_repo_conf(cfg: Dict[str, Any], state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    target_id = state.get("repo_id")
    target_name = state.get("repo")
    for proj in cfg.get("projects") or []:
        for r in proj.get("repos") or []:
            if target_id and r.get("id") == target_id:
                return r
            if target_name and r.get("name") == target_name:
                return r
    return None


def resolve_wiki_settings(cfg: Dict[str, Any], repo_conf: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    global_wiki = cfg.get("wiki") or {}
    repo_cfg = repo_conf or {}
    ai_cfg = cfg.get("ai") or {}

    def _int_opt(val: Any, default: int, minimum: int = 1) -> int:
        try:
            parsed = int(val)
            return parsed if parsed >= minimum else default
        except (TypeError, ValueError):
            return default

    wiki_dir = str(repo_cfg.get("wiki_dir") or global_wiki.get("dir") or DEFAULT_WIKI_DIR).strip("/") or DEFAULT_WIKI_DIR
    subpath = str(repo_cfg.get("wiki_subpath") or global_wiki.get("subpath") or "").strip("/")
    locale = str(repo_cfg.get("wiki_locale") or global_wiki.get("locale") or "zh-CN").strip() or "zh-CN"

    raw_pages = repo_cfg.get("wiki_max_pages")
    if raw_pages is None:
        raw_pages = global_wiki.get("max_pages")
    if raw_pages is None or str(raw_pages).strip().lower() in {"", "auto", "none", "null"}:
        max_pages = DEFAULT_MAX_PAGES
    else:
        parsed_pages = _int_opt(raw_pages, DEFAULT_MAX_PAGES, minimum=0)
        max_pages = 0 if parsed_pages <= 0 else min(parsed_pages, MAX_PAGES_HARD_CEILING)

    context_chars = _int_opt(repo_cfg.get("wiki_context_chars") or global_wiki.get("context_chars"), DEFAULT_CONTEXT_CHARS, 4000)
    max_bytes = _int_opt(repo_cfg.get("wiki_max_bytes") or global_wiki.get("max_bytes"), DEFAULT_MAX_BYTES, 1024)
    file_char_limit = _int_opt(
        repo_cfg.get("wiki_file_char_limit") or global_wiki.get("file_char_limit"),
        DEFAULT_FILE_CHAR_LIMIT,
        2000,
    )
    max_output_tokens = _int_opt(
        repo_cfg.get("wiki_max_output_tokens") or global_wiki.get("max_output_tokens") or ai_cfg.get("wiki_max_output_tokens"),
        DEFAULT_MAX_OUTPUT_TOKENS,
        512,
    )
    concurrency = min(
        _int_opt(repo_cfg.get("wiki_concurrency") or global_wiki.get("concurrency"), DEFAULT_PAGE_CONCURRENCY, 1),
        6,
    )

    return {
        "wiki_dir": wiki_dir,
        "subpath": subpath,
        "locale": locale,
        "max_pages": max_pages,
        "context_chars": context_chars,
        "max_bytes": max_bytes,
        "file_char_limit": file_char_limit,
        "max_output_tokens": max_output_tokens,
        "concurrency": concurrency,
    }


def _baseline_cache_key(repo_id: int, branch: str, subpath: str = "") -> str:
    clean_sub = subpath.strip("/") or "all"
    return f"cardops:wiki_baseline:{repo_id}:{branch}:{clean_sub}"


def _build_doc_url(project_web_url: str, branch: str, wiki_dir: str) -> str:
    if not project_web_url:
        return ""
    clean_dir = wiki_dir.strip("/")
    return f"{project_web_url.rstrip('/')}/-/blob/{quote(branch, safe='/')}/{clean_dir}/README.md"


def _build_commit_url(project_web_url: str, commit_sha: str) -> str:
    if not project_web_url or not commit_sha:
        return ""
    return f"{project_web_url.rstrip('/')}/-/commit/{commit_sha}"


def _compute_commit_actions(
    bundle_files: Dict[str, str],
    existing_tree: List[Dict[str, Any]],
    wiki_dir: str,
) -> List[Dict[str, Any]]:
    """
    结合现有 GitLab 仓库目录树与 Git blob SHA-1 计算精准增删改动作，避免无变更文件产生无效 diff。
    """
    clean_dir = wiki_dir.strip("/")
    existing_blobs: Dict[str, str] = {}
    for entry in existing_tree or []:
        if entry.get("type") == "blob" and entry.get("path"):
            existing_blobs[entry["path"]] = str(entry.get("id") or "")

    actions: List[Dict[str, Any]] = []
    for path, content in sorted(bundle_files.items()):
        if path not in existing_blobs:
            actions.append({"action": "create", "file_path": path, "content": content, "encoding": "text"})
        else:
            if existing_blobs[path] != git_blob_sha1(content):
                actions.append({"action": "update", "file_path": path, "content": content, "encoding": "text"})

    managed_prefixes = (f"{clean_dir}/sections/", f"{clean_dir}/knowledge/")
    for existing_path in sorted(existing_blobs.keys()):
        if existing_path not in bundle_files and existing_path.startswith(managed_prefixes):
            actions.append({"action": "delete", "file_path": existing_path})

    return actions


async def _resolve_wiki_mr_context(
    gitlab_client: GitLabClient,
    project_id: int,
    branch: str,
    proj_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """检测当前分支是否存在活跃 MR（源分支模式）或近期已合入 MR 信息"""
    active_mr: Optional[Dict[str, Any]] = None
    recent_merged_mr: Optional[Dict[str, Any]] = None

    try:
        mr = await gitlab_client.get_latest_open_mr(project_id, branch)
        if isinstance(mr, dict) and mr.get("iid"):
            mr_iid = int(mr["iid"])
            changed_files_count: Optional[int] = None
            try:
                mr_detail = await gitlab_client.get_mr_changes(project_id, mr_iid)
                if isinstance(mr_detail, dict) and isinstance(mr_detail.get("changes"), list):
                    changed_files_count = len(mr_detail["changes"])
            except Exception as e:
                logger.warning("fetch active mr changes failed project_id=%s mr_iid=%s err=%s", project_id, mr_iid, e)

            author_obj = mr.get("author")
            active_mr = {
                "iid": mr_iid,
                "title": str(mr.get("title") or ""),
                "web_url": str(mr.get("web_url") or ""),
                "source_branch": str(mr.get("source_branch") or branch),
                "target_branch": str(mr.get("target_branch") or ""),
                "state": str(mr.get("state") or "opened"),
                "author_name": str(author_obj.get("name") or "") if isinstance(author_obj, dict) else "",
                "changed_files_count": changed_files_count,
            }
    except Exception as e:
        logger.warning("resolve active mr failed project_id=%s branch=%s err=%s", project_id, branch, e)

    if not active_mr:
        default_branch = str((proj_info or {}).get("default_branch") or "")
        if default_branch and branch == default_branch:
            try:
                merged = await gitlab_client.get_latest_merged_mr(project_id, branch)
                if isinstance(merged, dict) and merged.get("iid"):
                    recent_merged_mr = {
                        "iid": int(merged["iid"]),
                        "title": str(merged.get("title") or ""),
                        "web_url": str(merged.get("web_url") or ""),
                        "source_branch": str(merged.get("source_branch") or ""),
                        "target_branch": str(merged.get("target_branch") or branch),
                    }
            except Exception as e:
                logger.warning("resolve recent merged mr failed project_id=%s branch=%s err=%s", project_id, branch, e)

    return {"active_mr": active_mr, "recent_merged_mr": recent_merged_mr}


async def _commit_wiki_with_fallback(
    gitlab_client: GitLabClient,
    project_id: int,
    target_branch: str,
    commit_sha: str,
    wiki_dir: str,
    bundle: Dict[str, Any],
    progress_cb=None,
    active_mr_iid: Optional[int] = None,
) -> Dict[str, Any]:
    """
    优先直推目标分支（带 [skip ci]）；遇到保护分支 HTTP 400/403 时自动切出 docs/repowiki-<branch> 并创建或复用 MR。
    """
    proj_info = await gitlab_client.get_project(project_id)
    project_web_url = (proj_info or {}).get("web_url") or ""
    bundle_files: Dict[str, str] = bundle["files"]
    short_sha = commit_sha[:8] if commit_sha else "HEAD"
    mr_ref_part = f"MR !{active_mr_iid}, " if active_mr_iid else ""
    commit_msg = f"docs(repowiki): update RepoWiki for {target_branch} ({mr_ref_part}{short_sha}) [skip ci]"

    # 1. 尝试直推目标分支
    existing_tree = await gitlab_client.list_repository_tree(project_id, ref=target_branch, path=wiki_dir, recursive=True)
    direct_actions = _compute_commit_actions(bundle_files, existing_tree, wiki_dir)
    if not direct_actions:
        return {
            "write_mode": "no_diff",
            "write_branch": target_branch,
            "write_commit_sha": commit_sha,
            "write_commit_url": _build_commit_url(project_web_url, commit_sha),
            "doc_url": _build_doc_url(project_web_url, target_branch, wiki_dir),
            "mr_iid": None,
            "mr_url": "",
            "mr_reused": False,
        }

    try:
        commit_res = await gitlab_client.create_commit(
            project_id=project_id,
            branch=target_branch,
            commit_message=commit_msg,
            actions=direct_actions,
        )
        new_sha = str(commit_res.get("id") or "")
        return {
            "write_mode": "direct",
            "write_branch": target_branch,
            "write_commit_sha": new_sha,
            "write_commit_url": commit_res.get("web_url") or _build_commit_url(project_web_url, new_sha),
            "doc_url": _build_doc_url(project_web_url, target_branch, wiki_dir),
            "mr_iid": None,
            "mr_url": "",
            "mr_reused": False,
        }
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in {400, 403}:
            raise
        logger.info(
            "Direct RepoWiki commit blocked on project_id=%s branch=%s (HTTP %s), falling back to docs MR",
            project_id, target_branch, exc.response.status_code,
        )

    # 2. 保护分支降级：切换至 docs/repowiki-<branch> 并创建/更新 MR
    if progress_cb:
        await progress_cb("🛡️ 目标分支受保护，正在切出文档分支并提交 Merge Request...")

    safe_branch = re.sub(r"[^a-zA-Z0-9._-]+", "-", target_branch).strip("-") or "main"
    mr_branch = f"docs/repowiki-{safe_branch}"

    open_mr = await gitlab_client.find_open_merge_request(
        project_id=project_id, source_branch=mr_branch, target_branch=target_branch
    )
    existing_mr_branch = await gitlab_client.get_branch(project_id, mr_branch)

    # 若存在遗留旧分支但无活跃 Open MR，先清理后基于目标分支重建，防止合入冲突
    if existing_mr_branch and not open_mr:
        deleted = await gitlab_client.delete_branch(project_id, mr_branch)
        if deleted:
            existing_mr_branch = None

    if not existing_mr_branch:
        await gitlab_client.create_branch(project_id, mr_branch, commit_sha or target_branch)

    mr_tree = await gitlab_client.list_repository_tree(project_id, ref=mr_branch, path=wiki_dir, recursive=True)
    mr_actions = _compute_commit_actions(bundle_files, mr_tree, wiki_dir)

    if mr_actions:
        commit_res = await gitlab_client.create_commit(
            project_id=project_id,
            branch=mr_branch,
            commit_message=commit_msg,
            actions=mr_actions,
        )
        new_sha = str(commit_res.get("id") or "")
        new_commit_url = commit_res.get("web_url") or _build_commit_url(project_web_url, new_sha)
    else:
        branch_meta = await gitlab_client.get_branch(project_id, mr_branch)
        new_sha = str((branch_meta or {}).get("commit", {}).get("id") or "")
        new_commit_url = _build_commit_url(project_web_url, new_sha)

    if open_mr:
        mr = open_mr
        mr_reused = True
    else:
        topic_bullets = "\n".join(
            f"- **{t['title']}**: `{t['path']}`" for t in (bundle.get("topics") or [])
        )
        mr_desc = (
            f"🤖 由 **FeishuCardOps** 自动生成 RepoWiki 文档。\n\n"
            f"- **目标分支**：`{target_branch}`\n"
            f"- **基线提交**：`{short_sha}`\n"
            f"- **文档目录**：`{wiki_dir}/README.md`\n\n"
            f"### 专题列表\n{topic_bullets}\n"
        )
        mr = await gitlab_client.create_merge_request(
            project_id=project_id,
            source_branch=mr_branch,
            target_branch=target_branch,
            title=f"docs(repowiki): 更新 {target_branch} 分支 RepoWiki 文档 ({short_sha})",
            description=mr_desc,
            remove_source_branch=True,
        )
        mr_reused = False

    return {
        "write_mode": "mr",
        "write_branch": mr_branch,
        "write_commit_sha": new_sha,
        "write_commit_url": new_commit_url,
        "doc_url": _build_doc_url(project_web_url, mr_branch, wiki_dir),
        "mr_iid": mr.get("iid"),
        "mr_url": mr.get("web_url") or "",
        "mr_reused": mr_reused,
    }


async def background_run_wiki(
    feishu_client: FeishuClient,
    gitlab_client: GitLabClient,
    cfg: Dict[str, Any],
    state: Dict[str, Any],
    operator_open_id: str,
    open_chat_id: str,
    lock_scope: str,
    force: bool = False,
) -> None:
    """后台执行 RepoWiki 源码扫描、AI 文档生成、写回 GitLab 与飞书卡片汇报"""
    start_time = asyncio.get_event_loop().time()
    final_status = "failed"
    project_id = int(state["repo_id"])
    branch = state["branch"]
    repo_conf = _find_repo_conf(cfg, state)
    settings = resolve_wiki_settings(cfg, repo_conf)
    wiki_dir = settings["wiki_dir"]
    subpath = settings["subpath"]

    sub_message_ids: List[str] = []
    commit_sha = ""
    commit_url = ""
    active_mr: Optional[Dict[str, Any]] = None
    recent_merged_mr: Optional[Dict[str, Any]] = None
    current_stage = {"text": "🔎 正在校验提交基线与增量缓存..."}
    canceling_flag = {"active": False}
    current_task = asyncio.current_task()
    ticker_task: Optional[asyncio.Task] = None

    if current_task:
        _running_wiki_tasks[lock_scope] = current_task

    def _elapsed() -> float:
        return max(0.0, asyncio.get_event_loop().time() - start_time)

    def _base_meta(**extra: Any) -> Dict[str, Any]:
        meta: Dict[str, Any] = {
            "subpath": subpath,
            "wiki_dir": wiki_dir,
            "lock_scope": lock_scope,
            "elapsed_seconds": _elapsed(),
            "active_mr": active_mr,
            "recent_merged_mr": recent_merged_mr,
        }
        meta.update(extra)
        return meta

    async def _send_or_update_cards(card: Dict[str, Any]) -> None:
        if not sub_message_ids:
            if open_chat_id:
                try:
                    resp = await feishu_client.send_card(open_chat_id, card)
                    mid = (resp.get("data") or {}).get("message_id")
                    if mid:
                        sub_message_ids.append(mid)
                except Exception as e:
                    logger.warning("send wiki card failed chat_id=%s err=%s", open_chat_id, e)
            audit_chat_id = (cfg.get("feishu") or {}).get("audit_chat_id")
            if audit_chat_id and audit_chat_id != open_chat_id:
                try:
                    resp = await feishu_client.send_card(audit_chat_id, card)
                    mid = (resp.get("data") or {}).get("message_id")
                    if mid:
                        sub_message_ids.append(mid)
                except Exception as e:
                    logger.warning("send wiki audit card failed chat_id=%s err=%s", audit_chat_id, e)
            return

        for mid in sub_message_ids:
            try:
                await feishu_client.update_card(mid, card)
            except Exception as e:
                logger.warning("update wiki card failed msg_id=%s err=%s", mid, e)

    async def _push_running_card(stage_text: str) -> None:
        if await get_wiki_cancel_info(lock_scope):
            raise asyncio.CancelledError()
        current_stage["text"] = stage_text
        await _send_or_update_cards(
            build_wiki_card(
                state=state,
                operator_open_id=operator_open_id,
                w_status="running",
                stage_text=stage_text,
                commit_sha=commit_sha,
                commit_url=commit_url,
                lock_scope=lock_scope,
                result_info=_base_meta(),
            )
        )

    async def _progress_ticker() -> None:
        while True:
            await asyncio.sleep(15)
            if canceling_flag["active"]:
                return
            if await get_wiki_cancel_info(lock_scope):
                if current_task and not current_task.done() and not canceling_flag["active"]:
                    current_task.cancel()
                return
            await _send_or_update_cards(
                build_wiki_card(
                    state=state,
                    operator_open_id=operator_open_id,
                    w_status="running",
                    stage_text=current_stage["text"],
                    commit_sha=commit_sha,
                    commit_url=commit_url,
                    lock_scope=lock_scope,
                    result_info=_base_meta(),
                )
            )

    async def _stop_ticker() -> None:
        nonlocal ticker_task
        if ticker_task and not ticker_task.done():
            ticker_task.cancel()
            await asyncio.gather(ticker_task, return_exceptions=True)
        ticker_task = None

    try:
        await clear_wiki_cancel(lock_scope)

        ai_cfg = cfg.get("ai") or {}
        api_key = str(ai_cfg.get("api_key") or "").strip()
        base_url = str(ai_cfg.get("base_url") or "").strip()
        model_name = str(ai_cfg.get("model") or "").strip()
        if not api_key or not model_name:
            err_card = build_wiki_card(
                state=state,
                operator_open_id=operator_open_id,
                w_status="failed",
                result_info={"subpath": subpath, "wiki_dir": wiki_dir, "elapsed_seconds": _elapsed()},
                error_info="未在 config.yaml 中配置 `ai.api_key` 或 `ai.model`，无法生成 RepoWiki。",
            )
            await _send_or_update_cards(err_card)
            return

        # 1. 解析当前分支最新 Commit SHA
        proj_info = await gitlab_client.get_project(project_id)
        project_web_url = (proj_info or {}).get("web_url") or ""

        branch_info = await gitlab_client.get_branch(project_id, branch)
        if branch_info and isinstance(branch_info.get("commit"), dict):
            commit_sha = str(branch_info["commit"].get("id") or "")
        if not commit_sha:
            commits = await gitlab_client.get_branch_commits(project_id, branch, per_page=1)
            if commits:
                commit_sha = str(commits[0].get("id") or "")
        if not commit_sha:
            err_card = build_wiki_card(
                state=state,
                operator_open_id=operator_open_id,
                w_status="failed",
                result_info={"subpath": subpath, "wiki_dir": wiki_dir, "elapsed_seconds": _elapsed()},
                error_info=f"未能获取分支 `{branch}` 的最新提交记录，请检查分支是否存在。",
            )
            await _send_or_update_cards(err_card)
            return

        commit_url = _build_commit_url(project_web_url, commit_sha)

        # 1.5 检测当前分支是否绑定活跃 Merge Request（Pre-Merge 协同写入 MR 源分支）
        mr_ctx = await _resolve_wiki_mr_context(gitlab_client, project_id, branch, proj_info)
        active_mr = mr_ctx["active_mr"]
        recent_merged_mr = mr_ctx["recent_merged_mr"]

        # 2. 检查增量基线缓存（非 force 且 commit_sha / 写入提交与 config_hash 一致且文档仍存在则跳过）
        expected_cfg_hash = compute_wiki_config_hash(
            locale=settings["locale"],
            max_pages=settings["max_pages"],
            context_chars=settings["context_chars"],
            max_output_tokens=settings["max_output_tokens"],
            file_char_limit=settings["file_char_limit"],
            model_name=model_name,
            wiki_dir=wiki_dir,
            subpath=subpath,
        )
        redis = get_redis()
        baseline_key = _baseline_cache_key(project_id, branch, subpath)
        if not force and redis:
            raw_base = await redis.get(baseline_key)
            if raw_base:
                try:
                    cached_base = json.loads(raw_base.decode("utf-8") if isinstance(raw_base, bytes) else str(raw_base))
                except Exception:
                    cached_base = None
                known_shas = {
                    s for s in (
                        (cached_base or {}).get("commit_sha"),
                        (cached_base or {}).get("write_commit_sha"),
                    ) if s
                }
                if (
                    isinstance(cached_base, dict)
                    and commit_sha in known_shas
                    and cached_base.get("config_hash") == expected_cfg_hash
                ):
                    check_branch = cached_base.get("write_branch") or branch
                    tree = await gitlab_client.list_repository_tree(
                        project_id, ref=check_branch, path=wiki_dir, recursive=False
                    )
                    names = {item.get("name") for item in tree}
                    if "README.md" in names or "repowiki-metadata.json" in names:
                        final_status = "skipped"
                        skipped_info = {
                            **cached_base,
                            "elapsed_seconds": _elapsed(),
                            "active_mr": active_mr or cached_base.get("active_mr"),
                            "recent_merged_mr": recent_merged_mr or cached_base.get("recent_merged_mr"),
                        }
                        skipped_card = build_wiki_card(
                            state=state,
                            operator_open_id=operator_open_id,
                            w_status="skipped",
                            commit_sha=commit_sha,
                            commit_url=commit_url,
                            result_info=skipped_info,
                        )
                        if open_chat_id:
                            await feishu_client.send_card(open_chat_id, skipped_card)
                        return

        # 3. 发送初始运行卡片（同步至当前会话及审计群），启动耗时心跳刷新
        if active_mr:
            init_stage = (
                f"🔀 已识别活跃 MR !{active_mr['iid']}（`{active_mr['source_branch']}` → `{active_mr['target_branch']}`），"
                "正在拉取快照并准备将文档写回 MR 源分支协同评审..."
            )
        else:
            init_stage = "🌿 未找到活跃 MR，已按单分支模式拉取 GitLab 仓库快照并提取源码..."
        await _push_running_card(init_stage)
        ticker_task = asyncio.create_task(_progress_ticker())

        archive_bytes = await gitlab_client.download_repository_archive(project_id, commit_sha)
        if await get_wiki_cancel_info(lock_scope):
            raise asyncio.CancelledError()

        source = scan_archive_bytes(
            archive_bytes=archive_bytes,
            branch=branch,
            commit_sha=commit_sha,
            wiki_dir=wiki_dir,
            subpath=subpath,
            max_bytes=settings["max_bytes"],
            file_char_limit=settings["file_char_limit"],
        )

        # 5. 对接 AI 模型与 Redis 断点阶段缓存
        llm = AnthropicWikiModel(
            api_key=api_key,
            base_url=base_url,
            model=model_name,
            locale=settings["locale"],
            max_output_tokens=settings["max_output_tokens"],
        )

        async def _guarded_model_call(purpose: str, instructions: str, context: Dict[str, Any]) -> str:
            if await get_wiki_cancel_info(lock_scope):
                raise asyncio.CancelledError()
            res = await llm.call(purpose, instructions, context)
            if await get_wiki_cancel_info(lock_scope):
                raise asyncio.CancelledError()
            return res

        async def _stage_cache_get(stage_name: str, item_id: str, sig: str) -> Optional[Any]:
            if force or not redis:
                return None
            key = f"cardops:wiki_stage:{project_id}:{branch}:{stage_name}:{item_id}:{sig[:16]}"
            raw = await redis.get(key)
            if not raw:
                return None
            return json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))

        async def _stage_cache_set(stage_name: str, item_id: str, sig: str, val: Any) -> None:
            if not redis:
                return
            key = f"cardops:wiki_stage:{project_id}:{branch}:{stage_name}:{item_id}:{sig[:16]}"
            await redis.set(key, json.dumps(val, ensure_ascii=False), ex=86400)

        repo_display = state.get("repo_path") or state.get("repo") or str(project_id)
        bundle = await generate_wiki_bundle(
            source=source,
            repo_name=repo_display,
            model_call=_guarded_model_call,
            wiki_dir=wiki_dir,
            locale=settings["locale"],
            model_name=model_name,
            max_pages=settings["max_pages"],
            context_chars=settings["context_chars"],
            max_output_tokens=settings["max_output_tokens"],
            file_char_limit=settings["file_char_limit"],
            concurrency=settings["concurrency"],
            stage_cache_get=_stage_cache_get,
            stage_cache_set=_stage_cache_set,
            progress_cb=_push_running_card,
        )

        # 6. 写回 GitLab 仓库（直推优先，400/403 保护分支自动切 MR）
        if active_mr:
            await _push_running_card(
                f"📤 正在将生成的 RepoWiki 写回 MR !{active_mr['iid']} 源分支 `{active_mr['source_branch']}`（随 MR 一并评审合入）..."
            )
        else:
            await _push_running_card("📤 正在将生成的 RepoWiki 写回 GitLab 代码仓库...")
        if await get_wiki_cancel_info(lock_scope):
            raise asyncio.CancelledError()

        write_res = await _commit_wiki_with_fallback(
            gitlab_client=gitlab_client,
            project_id=project_id,
            target_branch=branch,
            commit_sha=commit_sha,
            wiki_dir=wiki_dir,
            bundle=bundle,
            progress_cb=_push_running_card,
            active_mr_iid=active_mr["iid"] if active_mr else None,
        )

        await _stop_ticker()
        elapsed = _elapsed()
        result_info = _base_meta(
            commit_sha=commit_sha,
            config_hash=bundle["config_hash"],
            fingerprint=source["fingerprint"],
            eligible_files=len(source["files"]),
            omitted_files=len(source["omitted"]),
            pages_count=bundle["pages_count"],
            cards_count=bundle["cards_count"],
            topics=bundle["topics"],
            model_calls=llm.calls,
            reported_tokens=llm.tokens,
            elapsed_seconds=elapsed,
            **write_res,
        )

        if redis:
            try:
                await redis.set(baseline_key, json.dumps(result_info, ensure_ascii=False), ex=90 * 86400)
            except Exception as e:
                logger.warning("save wiki baseline failed repo_id=%s err=%s", project_id, e)

        final_status = "success"
        await _send_or_update_cards(
            build_wiki_card(
                state=state,
                operator_open_id=operator_open_id,
                w_status="success",
                commit_sha=commit_sha,
                commit_url=commit_url,
                result_info=result_info,
            )
        )

    except asyncio.CancelledError:
        final_status = "canceled"
        canceling_flag["active"] = True
        await _stop_ticker()
        cancel_info = (await get_wiki_cancel_info(lock_scope)) or {}
        logger.info("background_run_wiki canceled scope=%s by=%s", lock_scope, cancel_info.get("canceled_by"))
        try:
            await _send_or_update_cards(
                build_wiki_card(
                    state=state,
                    operator_open_id=operator_open_id,
                    w_status="canceled",
                    stage_text=current_stage["text"],
                    commit_sha=commit_sha,
                    commit_url=commit_url,
                    result_info=_base_meta(**cancel_info),
                )
            )
        except Exception as e:
            logger.warning("failed updating canceled wiki card scope=%s err=%s", lock_scope, e)
    except httpx.HTTPStatusError as exc:
        await _stop_ticker()
        detail = ""
        try:
            detail = exc.response.text[:260]
        except Exception:
            detail = str(exc)
        logger.exception("background_run_wiki GitLab HTTP error status=%s", exc.response.status_code)
        await _send_or_update_cards(
            build_wiki_card(
                state=state,
                operator_open_id=operator_open_id,
                w_status="failed",
                commit_sha=commit_sha,
                commit_url=commit_url,
                result_info=_base_meta(),
                error_info=f"GitLab API 请求失败 (HTTP {exc.response.status_code})：{detail}",
            )
        )
    except Exception as exc:
        await _stop_ticker()
        logger.exception("background_run_wiki failed: %s", exc)
        await _send_or_update_cards(
            build_wiki_card(
                state=state,
                operator_open_id=operator_open_id,
                w_status="failed",
                commit_sha=commit_sha,
                commit_url=commit_url,
                result_info=_base_meta(),
                error_info=str(exc),
            )
        )
    finally:
        await _stop_ticker()
        if current_task and _running_wiki_tasks.get(lock_scope) is current_task:
            _running_wiki_tasks.pop(lock_scope, None)
        await clear_wiki_cancel(lock_scope)
        await unlock_wiki(lock_scope)
        elapsed = _elapsed()
        WIKI_GEN_COMPLETED.labels(
            project=state.get("project", ""),
            repo=state.get("repo", ""),
            status=final_status,
        ).inc()
        WIKI_GEN_DURATION.labels(
            project=state.get("project", ""),
            repo=state.get("repo", ""),
        ).observe(elapsed)
