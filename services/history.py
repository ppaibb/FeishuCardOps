"""
Pipeline 历史记录服务（内存存储）

按 repo_id 分组保存最近 N 条发版记录，
用于在卡片中显示历史面板。
"""
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("feishu_gitlab_card_http")

MAX_HISTORY_PER_REPO = 20

# repo_id -> [HistoryRecord, ...]
_history_store: Dict[int, List[Dict[str, Any]]] = defaultdict(list)


def add_record(
    repo_id: int,
    pipeline_id: int,
    project_name: str,
    repo_name: str,
    branch: str,
    env: str,
    operator_open_id: str,
    operator_name: str = "",
    module: Optional[str] = None,
    status: str = "created",
) -> Dict[str, Any]:
    """添加一条发版记录"""
    record = {
        "pipeline_id": pipeline_id,
        "project": project_name,
        "repo": repo_name,
        "branch": branch,
        "env": env,
        "module": module,
        "operator_open_id": operator_open_id,
        "operator_name": operator_name,
        "status": status,
        "triggered_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": None,
    }
    store = _history_store[repo_id]
    store.insert(0, record)  # 最新的在前面
    if len(store) > MAX_HISTORY_PER_REPO:
        store[:] = store[:MAX_HISTORY_PER_REPO]
    logger.info("history record added repo_id=%s pipeline_id=%s", repo_id, pipeline_id)
    return record


def update_record_status(repo_id: int, pipeline_id: int, status: str) -> None:
    """更新某条记录的最终状态"""
    store = _history_store.get(repo_id, [])
    for rec in store:
        if rec["pipeline_id"] == pipeline_id:
            rec["status"] = status
            rec["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.info("history record updated repo_id=%s pipeline_id=%s status=%s", repo_id, pipeline_id, status)
            return


def get_history(repo_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    """获取指定仓库的发版历史"""
    return _history_store.get(repo_id, [])[:limit]


def get_all_history(limit: int = 20) -> List[Dict[str, Any]]:
    """获取所有仓库的发版历史（按时间倒序）"""
    all_records = []
    for records in _history_store.values():
        all_records.extend(records)
    all_records.sort(key=lambda r: r.get("triggered_at", ""), reverse=True)
    return all_records[:limit]
