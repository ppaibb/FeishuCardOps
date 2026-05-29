import json
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("feishu_gitlab_card_http")



async def _log_req(request):
    logger.info(f"GitLab API Request: {request.method} {request.url}")

async def _log_resp(response):
    logger.info(f"GitLab API Response: {response.request.method} {response.url} - Status {response.status_code}")

class GitLabClient:
    """GitLab API v4 客户端（全异步）"""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    async def trigger_pipeline(self, project_id: int, ref: str, variables: Dict[str, str]) -> Dict[str, Any]:
        url = f"{self.base_url}/api/v4/projects/{project_id}/pipeline"
        headers = {"PRIVATE-TOKEN": self.token}
        payload = {"ref": ref, "variables": [{"key": k, "value": v} for k, v in variables.items()]}
        logger.info("Triggering GitLab pipeline project_id=%s ref=%s variables=%s", project_id, ref, variables)
        async with httpx.AsyncClient(timeout=30, event_hooks={'request': [_log_req], 'response': [_log_resp]}) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        logger.info("GitLab pipeline triggered successfully pipeline_id=%s web_url=%s", data.get("id"), data.get("web_url"))
        return data

    async def list_pipelines(self, project_id: int, ref: Optional[str] = None, per_page: int = 10) -> Any:
        url = f"{self.base_url}/api/v4/projects/{project_id}/pipelines"
        headers = {"PRIVATE-TOKEN": self.token}
        params: Dict[str, Any] = {"per_page": per_page}
        if ref:
            params["ref"] = ref
        async with httpx.AsyncClient(timeout=30, event_hooks={'request': [_log_req], 'response': [_log_resp]}) as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
        return data

    async def get_pipeline(self, project_id: int, pipeline_id: int) -> Dict[str, Any]:
        url = f"{self.base_url}/api/v4/projects/{project_id}/pipelines/{pipeline_id}"
        headers = {"PRIVATE-TOKEN": self.token}
        async with httpx.AsyncClient(timeout=30, event_hooks={'request': [_log_req], 'response': [_log_resp]}) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
            return {}

    async def get_commit(self, project_id: int, sha: str) -> Dict[str, Any]:
        url = f"{self.base_url}/api/v4/projects/{project_id}/repository/commits/{sha}"
        headers = {"PRIVATE-TOKEN": self.token}
        async with httpx.AsyncClient(timeout=30, event_hooks={'request': [_log_req], 'response': [_log_resp]}) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
            return {}

    async def get_pipeline_jobs(self, project_id: int, pipeline_id: int) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/api/v4/projects/{project_id}/pipelines/{pipeline_id}/jobs"
        headers = {"PRIVATE-TOKEN": self.token}
        async with httpx.AsyncClient(timeout=30, event_hooks={'request': [_log_req], 'response': [_log_resp]}) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
            return []

    async def get_job_trace(self, project_id: int, job_id: int) -> str:
        url = f"{self.base_url}/api/v4/projects/{project_id}/jobs/{job_id}/trace"
        headers = {"PRIVATE-TOKEN": self.token}
        async with httpx.AsyncClient(timeout=20, event_hooks={'request': [_log_req], 'response': [_log_resp]}) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.text
            return ""

    async def get_branches(self, project_id: int) -> List[str]:
        url = f"{self.base_url}/api/v4/projects/{project_id}/repository/branches"
        headers = {"PRIVATE-TOKEN": self.token}
        try:
            async with httpx.AsyncClient(timeout=10, event_hooks={'request': [_log_req], 'response': [_log_resp]}) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    return [b["name"] for b in resp.json()][:20]
                else:
                    logger.error(f"GitLab API Error: Failed to fetch branches for project {project_id}, status={resp.status_code}, body={resp.text[:200]}")
        except Exception as e:
            logger.error(f"GitLab API Exception: Failed to fetch branches for project {project_id}, error={e}")
        return ["main"]

    async def get_branches_cached(self, project_id: int, ttl: int = 90) -> List[str]:
        """获取分支列表，优先从 Redis 缓存读取（TTL 秒），降低对 GitLab 的频繁请求。"""
        from core.redis_client import get_redis
        cache_key = f"cardops:branches:{project_id}"
        redis = get_redis()
        if redis:
            try:
                cached = await redis.get(cache_key)
                if cached:
                    logger.debug("branches cache hit project_id=%s", project_id)
                    return json.loads(cached)
            except Exception as e:
                logger.warning("branches cache read failed project_id=%s err=%s", project_id, e)

        # 缓存未命中，真正请求 GitLab
        branches = await self.get_branches(project_id)

        if redis:
            try:
                await redis.set(cache_key, json.dumps(branches), ex=ttl)
                logger.debug("branches cache set project_id=%s ttl=%s", project_id, ttl)
            except Exception as e:
                logger.warning("branches cache write failed project_id=%s err=%s", project_id, e)

        return branches

    async def invalidate_branches_cache(self, project_id: int) -> None:
        """主动删除指定项目的分支缓存，用于刷新场景，确保下次能从 GitLab 拉最新数据。"""
        from core.redis_client import get_redis
        redis = get_redis()
        if redis:
            try:
                await redis.delete(f"cardops:branches:{project_id}")
                logger.debug("branches cache invalidated project_id=%s", project_id)
            except Exception as e:
                logger.warning("branches cache invalidate failed project_id=%s err=%s", project_id, e)

    async def latest_pipeline(self, project_id: int, ref: Optional[str] = None) -> Optional[Dict[str, Any]]:
        pipelines = await self.list_pipelines(project_id=project_id, ref=ref, per_page=1)
        if isinstance(pipelines, list) and pipelines:
            return pipelines[0]
        return None

    async def get_project(self, project_id: int) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}/api/v4/projects/{project_id}"
        headers = {"PRIVATE-TOKEN": self.token}
        async with httpx.AsyncClient(timeout=10, event_hooks={'request': [_log_req], 'response': [_log_resp]}) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
        return None

    async def get_default_branch(self, project_id: int) -> str:
        url = f"{self.base_url}/api/v4/projects/{project_id}"
        headers = {"PRIVATE-TOKEN": self.token}
        async with httpx.AsyncClient(timeout=10, event_hooks={'request': [_log_req], 'response': [_log_resp]}) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json().get("default_branch", "main")
        return "main"

    async def compare_branches(self, project_id: int, from_branch: str, to_branch: str) -> Dict[str, Any]:
        """对比两个分支的差异，返回 commits 和 diffs"""
        url = f"{self.base_url}/api/v4/projects/{project_id}/repository/compare"
        headers = {"PRIVATE-TOKEN": self.token}
        params = {"from": from_branch, "to": to_branch, "straight": "false"}
        async with httpx.AsyncClient(timeout=30, event_hooks={'request': [_log_req], 'response': [_log_resp]}) as client:
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code == 200:
                return resp.json()
            logger.error("compare_branches failed status=%s body=%s", resp.status_code, resp.text[:500])
            return {}

    async def get_latest_mr(self, project_id: int, source_branch: str) -> Optional[Dict[str, Any]]:
        """获取指定源分支的最新 Merge Request"""
        url = f"{self.base_url}/api/v4/projects/{project_id}/merge_requests"
        headers = {"PRIVATE-TOKEN": self.token}
        params = {"source_branch": source_branch, "order_by": "updated_at", "sort": "desc", "per_page": 1}
        async with httpx.AsyncClient(timeout=15, event_hooks={'request': [_log_req], 'response': [_log_resp]}) as client:
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code == 200:
                mrs = resp.json()
                return mrs[0] if mrs else None
        return None

    async def get_latest_merged_mr(self, project_id: int, target_branch: str) -> Optional[Dict[str, Any]]:
        """获取最近合并到指定目标分支的 Merge Request"""
        url = f"{self.base_url}/api/v4/projects/{project_id}/merge_requests"
        headers = {"PRIVATE-TOKEN": self.token}
        params = {"target_branch": target_branch, "state": "merged", "order_by": "updated_at", "sort": "desc", "per_page": 1}
        async with httpx.AsyncClient(timeout=15, event_hooks={'request': [_log_req], 'response': [_log_resp]}) as client:
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code == 200:
                mrs = resp.json()
                return mrs[0] if mrs else None
        return None

    async def get_mr_changes(self, project_id: int, mr_iid: int) -> Dict[str, Any]:
        """获取 MR 的代码变更详情"""
        url = f"{self.base_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/changes"
        headers = {"PRIVATE-TOKEN": self.token}
        async with httpx.AsyncClient(timeout=30, event_hooks={'request': [_log_req], 'response': [_log_resp]}) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
            logger.error("get_mr_changes failed status=%s body=%s", resp.status_code, resp.text[:500])
            return {}

    async def get_branch_commits(self, project_id: int, branch: str, per_page: int = 1) -> List[Dict[str, Any]]:
        """获取分支的最新提交列表"""
        url = f"{self.base_url}/api/v4/projects/{project_id}/repository/commits"
        headers = {"PRIVATE-TOKEN": self.token}
        params = {"ref_name": branch, "per_page": per_page}
        async with httpx.AsyncClient(timeout=15, event_hooks={'request': [_log_req], 'response': [_log_resp]}) as client:
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code == 200:
                return resp.json()
            return []

    async def get_commit_diff(self, project_id: int, sha: str) -> List[Dict[str, Any]]:
        """获取单个提交的代码变更详情"""
        url = f"{self.base_url}/api/v4/projects/{project_id}/repository/commits/{sha}/diff"
        headers = {"PRIVATE-TOKEN": self.token}
        async with httpx.AsyncClient(timeout=30, event_hooks={'request': [_log_req], 'response': [_log_resp]}) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
            return []

    async def cancel_pipeline(self, project_id: int, pipeline_id: int) -> Dict[str, Any]:
        """取消流水线"""
        url = f"{self.base_url}/api/v4/projects/{project_id}/pipelines/{pipeline_id}/cancel"
        headers = {"PRIVATE-TOKEN": self.token}
        async with httpx.AsyncClient(timeout=30, event_hooks={'request': [_log_req], 'response': [_log_resp]}) as client:
            resp = await client.post(url, headers=headers)
            if resp.status_code in [200, 201]:
                return resp.json()
            return {}

