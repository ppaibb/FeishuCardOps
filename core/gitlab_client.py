import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("feishu_gitlab_card_http")


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
        async with httpx.AsyncClient(timeout=30) as client:
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
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
        return data

    async def get_pipeline(self, project_id: int, pipeline_id: int) -> Dict[str, Any]:
        url = f"{self.base_url}/api/v4/projects/{project_id}/pipelines/{pipeline_id}"
        headers = {"PRIVATE-TOKEN": self.token}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
            return {}

    async def get_commit(self, project_id: int, sha: str) -> Dict[str, Any]:
        url = f"{self.base_url}/api/v4/projects/{project_id}/repository/commits/{sha}"
        headers = {"PRIVATE-TOKEN": self.token}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
            return {}

    async def get_pipeline_jobs(self, project_id: int, pipeline_id: int) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/api/v4/projects/{project_id}/pipelines/{pipeline_id}/jobs"
        headers = {"PRIVATE-TOKEN": self.token}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
            return []

    async def get_job_trace(self, project_id: int, job_id: int) -> str:
        url = f"{self.base_url}/api/v4/projects/{project_id}/jobs/{job_id}/trace"
        headers = {"PRIVATE-TOKEN": self.token}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.text
            return ""

    async def get_branches(self, project_id: int) -> List[str]:
        url = f"{self.base_url}/api/v4/projects/{project_id}/repository/branches"
        headers = {"PRIVATE-TOKEN": self.token}
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return [b["name"] for b in resp.json()][:20]
            return ["main"]

    async def latest_pipeline(self, project_id: int, ref: Optional[str] = None) -> Optional[Dict[str, Any]]:
        pipelines = await self.list_pipelines(project_id=project_id, ref=ref, per_page=1)
        if isinstance(pipelines, list) and pipelines:
            return pipelines[0]
        return None

    async def get_default_branch(self, project_id: int) -> str:
        url = f"{self.base_url}/api/v4/projects/{project_id}"
        headers = {"PRIVATE-TOKEN": self.token}
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json().get("default_branch", "main")
        return "main"

    async def compare_branches(self, project_id: int, from_branch: str, to_branch: str) -> Dict[str, Any]:
        """对比两个分支的差异，返回 commits 和 diffs"""
        url = f"{self.base_url}/api/v4/projects/{project_id}/repository/compare"
        headers = {"PRIVATE-TOKEN": self.token}
        params = {"from": from_branch, "to": to_branch, "straight": "false"}
        async with httpx.AsyncClient(timeout=30) as client:
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
        async with httpx.AsyncClient(timeout=15) as client:
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
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code == 200:
                mrs = resp.json()
                return mrs[0] if mrs else None
        return None

    async def get_mr_changes(self, project_id: int, mr_iid: int) -> Dict[str, Any]:
        """获取 MR 的代码变更详情"""
        url = f"{self.base_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/changes"
        headers = {"PRIVATE-TOKEN": self.token}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
            logger.error("get_mr_changes failed status=%s body=%s", resp.status_code, resp.text[:500])
            return {}
