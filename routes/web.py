"""
FeishuCardOps 纯 Web 运维控制台 RESTful API 路由

与飞书卡片构建器 (card_builder) 彻底解耦，提供纯净的 JSON API 供极简暗黑 Web UI 调用。
"""


import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.config import load_config
from core.gitlab_client import build_gitlab_client
from core.permissions import check_approval_required, check_permission
from core.state import is_repo_locked, lock_repo, unlock_repo, try_lock_repo
from services.approval import create_approval, get_approval, resolve_approval
from services.code_review import diagnose_job_log
from services.history import add_record, get_history, update_record_status
from services.project_manager import get_all_projects

logger = logging.getLogger("feishu_gitlab_card_http.web_api")

router = APIRouter(prefix="/api/v1", tags=["Web Console API"])


# 内存中存储正在运行的 Web 端 Pipeline 任务状态缓存（便于实时轮询）
_active_web_pipelines: Dict[str, Dict[str, Any]] = {}


class DeployRequest(BaseModel):
    project_name: str
    repo_name: str
    env: str
    ref: str  # 分支名或 Tag 名
    variables: Optional[Dict[str, str]] = None
    operator_name: str = "WebUser"


class ApprovalActionRequest(BaseModel):
    action: str  # "approve" 或 "reject"
    operator_name: str = "WebApprover"
    comment: Optional[str] = ""


class CodeReviewRequest(BaseModel):
    project_name: str
    repo_name: str
    target_branch: str = "main"
    source_branch: str = "dev"


@router.get("/config")
async def get_web_config():
    """获取当前服务配置概览及运行模式"""
    cfg = load_config()
    all_projects = await get_all_projects()
    return {
        "ok": True,
        "mode": "standalone_web",
        "service": "FeishuCardOps-WebConsole",
        "version": "v2.0.0",
        "project_count": len(all_projects),
    }



@router.get("/projects")
async def list_projects():
    """获取所有发版项目及对应的仓库、环境、自定义变量定义"""
    all_projects = await get_all_projects()
    formatted_projects = []
    
    for p in all_projects:
        p_name = p.get("name")
        envs = p.get("environments", ["test", "prod"])
        repos = p.get("repos", [])
        
        formatted_repos = []
        for r in repos:
            formatted_repos.append({
                "name": r.get("name"),
                "id": r.get("id"),
                "repo": r.get("repo"),
                "gitlab_instance": r.get("gitlab", "default"),
                "variables": r.get("variables", []),
                "access": r.get("access", {}),
                "access_note": r.get("access_note", ""),
            })
            
        formatted_projects.append({
            "name": p_name,
            "environments": envs,
            "repos": formatted_repos,
        })

    return {"ok": True, "projects": formatted_projects}


@router.get("/repos/{project_name}/{repo_name}/branches")
async def list_repo_branches(project_name: str, repo_name: str):
    """根据项目名与仓库名拉取 GitLab 对应的分支与近期 Tag 列表"""
    cfg = load_config()
    all_projects = await get_all_projects()
    
    target_project = next((p for p in all_projects if p.get("name") == project_name), None)
    if not target_project:
        raise HTTPException(status_code=404, detail=f"项目 {project_name} 未找到")

    target_repo = next((r for r in target_project.get("repos", []) if r.get("name") == repo_name), None)
    if not target_repo:
        raise HTTPException(status_code=404, detail=f"仓库 {repo_name} 未找到")

    gitlab_instance = target_repo.get("gitlab", "default")
    project_id = target_repo.get("id")

    try:
        gitlab_client = build_gitlab_client(cfg, gitlab_instance)
        branches = await gitlab_client.get_branches_cached(project_id)
        tags = await gitlab_client.get_tags(project_id)

        branch_list = [{"name": b if isinstance(b, str) else b.get("name"), "default": b == "main" or b == "master"} for b in branches]
        tag_list = [{"name": t if isinstance(t, str) else t.get("name")} for t in tags]

        return {
            "ok": True,
            "project_name": project_name,
            "repo_name": repo_name,
            "branches": branch_list,
            "tags": tag_list,
        }
    except Exception as e:
        logger.exception("拉取 GitLab 分支异常: %s", e)
        return {"ok": False, "error": str(e), "branches": [{"name": "main", "default": True}], "tags": []}



@router.post("/pipeline/deploy")
async def trigger_deploy(req: DeployRequest):
    """提交发版部署应用请求"""
    cfg = load_config()
    all_projects = await get_all_projects()

    target_project = next((p for p in all_projects if p.get("name") == req.project_name), None)
    if not target_project:
        raise HTTPException(status_code=404, detail="指定项目不存在")

    target_repo = next((r for r in target_project.get("repos", []) if r.get("name") == req.repo_name), None)
    if not target_repo:
        raise HTTPException(status_code=404, detail="指定仓库不存在")

    project_id = target_repo.get("id")
    gitlab_instance = target_repo.get("gitlab", "default")

    # 1. 检查锁状态
    if await is_repo_locked(req.project_name, req.repo_name, req.env):
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": f"仓库 [{req.repo_name}] 正在由其他发版流程占用，请稍后再试"}
        )

    # 2. 检查审批要求
    need_approval, approvers = check_approval_required(cfg, req.project_name, req.env)
    if need_approval:
        approval_id = await create_approval(
            project=req.project_name,
            repo=req.repo_name,
            env=req.env,
            ref=req.ref,
            operator=req.operator_name,
            approvers=approvers,
            pipeline_vars=req.variables or {},
        )
        return {
            "ok": True,
            "status": "approval_required",
            "approval_id": approval_id,
            "message": f"该环境 ({req.env}) 触发了生产审批流程，已生成审批单 #{approval_id}",
            "approvers": approvers,
        }

    # 3. 锁定并触发流水线
    locked = await try_lock_repo(req.project_name, req.repo_name, req.env, operator=req.operator_name, ttl=1800)
    if not locked:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "获取排他锁失败，可能其他发版正在提交"}
        )

    gitlab_client = build_gitlab_client(cfg, gitlab_instance)
    
    # 组装流水线自定义变量
    pipeline_vars = []
    if req.variables:
        for k, v in req.variables.items():
            pipeline_vars.append({"key": k, "value": v})
    pipeline_vars.append({"key": "DEPLOY_ENV", "value": req.env})
    pipeline_vars.append({"key": "DEPLOY_OPERATOR", "value": req.operator_name})

    try:
        pipeline = await gitlab_client.trigger_pipeline(project_id, req.ref, pipeline_vars)
        pipeline_id = pipeline.get("id")
        web_url = pipeline.get("web_url", "")

        record_id = await add_record(
            repo_id=project_id,
            pipeline_id=pipeline_id,
            project_name=req.project_name,
            repo_name=req.repo_name,
            branch=req.ref,
            env=req.env,
            operator_open_id="",
            operator_name=req.operator_name,
        )

        task_key = f"{req.project_name}:{req.repo_name}:{pipeline_id}"
        _active_web_pipelines[task_key] = {
            "project": req.project_name,
            "repo": req.repo_name,
            "project_id": project_id,
            "pipeline_id": pipeline_id,
            "env": req.env,
            "ref": req.ref,
            "operator": req.operator_name,
            "status": "running",
            "web_url": web_url,
            "gitlab_instance": gitlab_instance,
            "start_time": time.time(),
        }

        # 启动后台异步状态轮询与日志抓取
        asyncio.create_task(_async_watch_web_pipeline(cfg, task_key))

        return {
            "ok": True,
            "status": "triggered",
            "pipeline_id": pipeline_id,
            "web_url": web_url,
            "message": f"成功触发 GitLab CI/CD 流水线 #{pipeline_id}",
        }
    except Exception as e:
        await unlock_repo(req.project_name, req.repo_name, req.env)
        logger.exception("触发流水线异常: %s", e)
        raise HTTPException(status_code=500, detail=f"触发流水线失败: {str(e)}")


async def _async_watch_web_pipeline(cfg: Dict[str, Any], task_key: str):
    """后台轮询流水线状态并写入本地缓存"""
    info = _active_web_pipelines.get(task_key)
    if not info:
        return

    gitlab_client = build_gitlab_client(cfg, info["gitlab_instance"])
    project_id = info["project_id"]
    pipeline_id = info["pipeline_id"]

    for _ in range(720):  # 最多轮询 60 分钟
        await asyncio.sleep(4)
        try:
            p_data = await gitlab_client.get_pipeline(project_id, pipeline_id)
            if not p_data:
                continue

            status = p_data.get("status", "running")
            jobs = await gitlab_client.get_pipeline_jobs(project_id, pipeline_id)

            formatted_jobs = []
            for j in jobs:
                formatted_jobs.append({
                    "id": j.get("id"),
                    "name": j.get("name"),
                    "stage": j.get("stage"),
                    "status": j.get("status"),
                    "web_url": j.get("web_url"),
                    "duration": j.get("duration"),
                })

            info["status"] = status
            info["jobs"] = formatted_jobs
            info["updated_at"] = time.time()

            if status in {"success", "failed", "canceled"}:
                # 流程结束，写入历史记录并释放锁
                await update_record_status(project_id, pipeline_id, status)
                await unlock_repo(info["project"], info["repo"], info["env"])
                logger.info("Web 流水线结束 task_key=%s status=%s", task_key, status)
                break
        except Exception as e:
            logger.warning("轮询 Web 流水线异常 task_key=%s: %s", task_key, e)


@router.get("/pipeline/{project_name}/{repo_name}/{pipeline_id}/status")
async def get_pipeline_realtime_status(project_name: str, repo_name: str, pipeline_id: int):
    """实时查看指定流水线节点进度与控制台 Logs"""
    task_key = f"{project_name}:{repo_name}:{pipeline_id}"

    cfg = load_config()
    all_projects = await get_all_projects()
    target_project = next((p for p in all_projects if p.get("name") == project_name), None)
    target_repo = next((r for r in target_project.get("repos", []) if r.get("name") == repo_name), None) if target_project else None
    
    if not target_repo:
        raise HTTPException(status_code=404, detail="仓库未找到")

    project_id = target_repo.get("id")
    gitlab_instance = target_repo.get("gitlab", "default")
    gitlab_client = build_gitlab_client(cfg, gitlab_instance)

    try:
        pipeline = await gitlab_client.get_pipeline(project_id, pipeline_id)
        jobs = await gitlab_client.get_pipeline_jobs(project_id, pipeline_id)

        job_logs = []
        for j in jobs:
            if j.get("status") in {"running", "failed", "success"}:
                trace = await gitlab_client.get_job_trace(project_id, j["id"])
                # 仅保留最近的 200 行日志
                tail_lines = trace.splitlines()[-200:] if trace else []
                job_logs.append({
                    "job_id": j["id"],
                    "job_name": j["name"],
                    "stage": j["stage"],
                    "status": j["status"],
                    "log_tail": "\n".join(tail_lines),
                })

        return {
            "ok": True,
            "pipeline_id": pipeline_id,
            "status": pipeline.get("status", "unknown") if pipeline else "unknown",
            "ref": pipeline.get("ref") if pipeline else "",
            "sha": pipeline.get("sha", "")[:8] if pipeline else "",
            "web_url": pipeline.get("web_url") if pipeline else "",
            "jobs": jobs,
            "job_logs": job_logs,
        }
    except Exception as e:
        logger.exception("获取流水线实时状态异常: %s", e)
        return {"ok": False, "error": str(e)}


@router.get("/approvals")
async def list_pending_approvals():
    """获取所有待审批的发版任务列表"""
    return {"ok": True, "approvals": []}


@router.post("/approvals/{approval_id}/action")
async def handle_approval_action(approval_id: str, req: ApprovalActionRequest):
    """Web 端一键通过或驳回发版审批单"""
    appr = await get_approval(approval_id)
    if not appr:
        raise HTTPException(status_code=404, detail="审批单不存在或已过期")

    success, msg = await resolve_approval(
        approval_id=approval_id,
        action=req.action,
        operator=req.operator_name,
        comment=req.comment or "",
    )

    if not success:
        return JSONResponse(status_code=400, content={"ok": False, "error": msg})

    return {"ok": True, "message": f"审批单 #{approval_id} 已处理: {req.action}"}


@router.get("/history")
async def fetch_release_history(repo_id: Optional[int] = None, limit: int = Query(30, ge=1, le=100)):
    """获取发版审计历史记录"""
    all_records = []
    try:
        if repo_id:
            all_records = await get_history(repo_id=repo_id, limit=limit)
        else:
            # 遍历配置中所有 repo 汇总最新历史
            all_projects = await get_all_projects()
            for p in all_projects:
                for r in p.get("repos", []):
                    r_id = r.get("id")
                    if r_id:
                        recs = await get_history(repo_id=r_id, limit=limit)
                        all_records.extend(recs)
    except Exception as e:
        logger.warning("获取 Redis 发版历史失败 (Redis 未连接或离线): %s", e)

    return {"ok": True, "count": len(all_records), "history": all_records}


