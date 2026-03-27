"""
Pipeline 历史记录服务（Redis 存储）

按 repo_id 分组保存近 MAX_HISTORY_PER_REPO 条发版记录。
"""
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.redis_client import get_redis

logger = logging.getLogger("feishu_gitlab_card_http")

MAX_HISTORY_PER_REPO = 100


async def add_record(
    repo_id: int, pipeline_id: int, project_name: str, repo_name: str, branch: str, env: str,
    operator_open_id: str, operator_name: str = "", module: Optional[str] = None, status: str = "created",
) -> Dict[str, Any]:
    record = {
        "pipeline_id": pipeline_id, "project": project_name, "repo": repo_name, "branch": branch,
        "env": env, "module": module, "operator_open_id": operator_open_id, "operator_name": operator_name,
        "status": status, "triggered_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "finished_at": None,
    }
    r = get_redis()
    key = f"pipeline_history:{repo_id}"
    await r.lpush(key, json.dumps(record, ensure_ascii=False))
    await r.ltrim(key, 0, MAX_HISTORY_PER_REPO - 1)
    logger.info("history record added repo_id=%s pipeline_id=%s", repo_id, pipeline_id)
    return record


async def update_record_status(repo_id: int, pipeline_id: int, status: str) -> None:
    r = get_redis()
    key = f"pipeline_history:{repo_id}"
    records_str = await r.lrange(key, 0, -1)
    for i, rec_str in enumerate(records_str):
        rec = json.loads(rec_str)
        if rec.get("pipeline_id") == pipeline_id:
            rec["status"] = status
            rec["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            await r.lset(key, i, json.dumps(rec, ensure_ascii=False))
            logger.info("history record updated repo_id=%s status=%s", repo_id, status)
            return


async def get_history(repo_id: int, limit: int = 15) -> List[Dict[str, Any]]:
    r = get_redis()
    records_str = await r.lrange(f"pipeline_history:{repo_id}", 0, limit - 1)
    return [json.loads(s) for s in records_str]
