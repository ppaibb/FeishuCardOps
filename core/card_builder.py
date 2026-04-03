import logging
from typing import Any, Dict, List, Optional

from core.gitlab_client import GitLabClient

logger = logging.getLogger("feishu_gitlab_card_http")


def status_meta(status: str, latest_result_text: str, latest_pipeline_text: str) -> Dict[str, str]:
    lowered_status = (status or "").lower()
    lowered_result = (latest_result_text or "").lower()
    lowered_pipeline = (latest_pipeline_text or "").lower()

    if any(x in lowered_result for x in ["失败", "error", "异常"]) or "failed" in lowered_pipeline:
        return {"emoji": "🔴", "label": "异常"}
    if any(x in lowered_status for x in ["执行中", "刷新中"]) or any(x in lowered_result for x in ["执行中", "刷新中"]) or any(x in lowered_pipeline for x in ["running", "pending", "created"]):
        return {"emoji": "🟡", "label": "处理中"}
    if "success" in lowered_status or "success" in lowered_result or "success" in lowered_pipeline:
        return {"emoji": "🟢", "label": "就绪"}
    return {"emoji": "🟢", "label": "就绪"}


async def normalize_selection(
    cfg: Dict[str, Any],
    gitlab_client: Optional[GitLabClient] = None,
    selected_project: Optional[str] = None,
    selected_repo: Optional[str] = None,
    selected_branch: Optional[str] = None,
    selected_env: Optional[str] = None,
    cached_branches: Optional[list] = None,
    selected_vars: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    projects = cfg.get("projects", [])
    if not projects:
        projects = [{"name": "demo", "environments": ["test"], "repos": [{"name": "demo", "repo": "demo", "id": 1}]}]

    project = next((p for p in projects if p["name"] == selected_project), None) or projects[0]

    repos = project.get("repos", [])
    if not repos:
        repos = [{"name": "demo", "repo": "demo", "id": 1}]

    repo = next((r for r in repos if r["name"] == selected_repo), None) or repos[0]

    env_value = selected_env or project["environments"][0]
    if env_value not in project["environments"]:
        env_value = project["environments"][0]

    branches: List[str] = []
    if cached_branches:
        branches = cached_branches
    elif gitlab_client:
        try:
            branches = await gitlab_client.get_branches(repo["id"])
        except Exception as e:
            logger.error(f"failed to fetch branches: {e}")
            branches = ["⚠️ 获取分支超时"]

    if not branches:
        branches = ["⚠️ 暂无分支"]

    branch_value = selected_branch if selected_branch in branches else branches[0]

    variables_def = list(repo.get("variables", []))
    if "modules" in repo:
        variables_def.append({"key": "TARGET_MODULE", "label": "🧩 选微服务 (Module)", "options": repo["modules"]})
        
    vars_val = {}
    selected_vars = selected_vars or {}
    for vdef in variables_def:
        vk = vdef["key"]
        vopts = vdef["options"]
        if not vopts: continue
        val = selected_vars.get(vk) if selected_vars.get(vk) in vopts else vopts[0]
        vars_val[vk] = val

    return {
        "project": project["name"],
        "repo": repo["name"],
        "repo_id": repo["id"],
        "repo_path": repo["repo"],
        "branch": branch_value,
        "env": env_value,
        "branches": branches,
        "variables_def": variables_def,
        "variables": vars_val,
    }


def build_sub_card(state: Dict[str, Any], operator_open_id: str, pipeline_id: int, p_status: str, active_job_name: str, failed_job_info: str = "", commit_info: str = "") -> Dict[str, Any]:
    commit_line = f"\n**提交**：{commit_info}" if commit_info else ""
    if p_status == "success":
        color = "green"
        emoji = "✅"
        content = f"<at id=\"{operator_open_id}\"></at> **发版任务已执行成功！** 🎉\n\n**项目**：{state['project']} - {state['repo']}\n**分支**：{state['branch']}{commit_line}\n**环境**：{state['env']}\n**流水线**：#{pipeline_id}"
    elif p_status in {"failed", "canceled"}:
        color = "red"
        emoji = "❌"
        content = f"<at id=\"{operator_open_id}\"></at> **发版任务执行异常终止！**\n\n**项目**：{state['project']} - {state['repo']}\n**分支**：{state['branch']}{commit_line}\n**环境**：{state['env']}\n**流水线**：#{pipeline_id} [{p_status}]"
        if failed_job_info:
            content += f"\n\n**失败点**：\n🚨 {failed_job_info}"
    else:
        color = "blue"
        emoji = "⏳"
        content = f"<at id=\"{operator_open_id}\"></at> **发版任务追踪中...**\n\n**项目**：{state['project']} - {state['repo']}\n**分支**：{state['branch']}{commit_line}\n**环境**：{state['env']}\n**流水线**：#{pipeline_id}\n**当前进展**：正在跟进 {active_job_name}"

    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": color, "title": {"tag": "plain_text", "content": f"{emoji} 发版进度指示器"}},
        "elements": [{"tag": "markdown", "content": content}]
    }


def build_history_card(
    history_records: List[Dict[str, Any]],
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """构建 Pipeline 历史记录卡片"""
    if not history_records:
        content = "📭 暂无发版历史记录"
    else:
        lines = []
        for i, rec in enumerate(history_records[:15], 1):
            status_emoji = {"success": "✅", "failed": "❌", "canceled": "⚠️", "running": "🔄"}.get(rec.get("status", ""), "⏳")
            line = f"{i}. {status_emoji} **#{rec['pipeline_id']}** | `{rec['branch']}` → `{rec['env']}`"
            if rec.get("module"): line += f" / `{rec['module']}`"
            if rec.get("operator_name") and rec["operator_name"] != rec.get("operator_open_id"): line += f" | 👤 {rec['operator_name']}"
            elif rec.get("operator_open_id"): line += f" | 👤 <at id=\"{rec['operator_open_id']}\"></at>"
            if rec.get("triggered_at"): line += f"\n    🕐 {rec['triggered_at']}"
            if rec.get("finished_at"):
                line += f" → {rec['finished_at']}"
            lines.append(line)
        content = "\n".join(lines)

    action_base_val = {
        "project": state["project"],
        "repo": state["repo"],
        "branch": state["branch"],
        "env": state["env"],
    }
    for k, v in state.get("variables", {}).items():
        action_base_val[f"var_{k}"] = v

    elements = [
        {"tag": "markdown", "content": content},
        {"tag": "hr"},
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🔄 刷新历史"},
                    "value": {**action_base_val, "action": "history"},
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "◀️ 返回控制台"},
                    "value": {**action_base_val, "action": "refresh"},
                },
            ],
        },
    ]

    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "indigo", "title": {"tag": "plain_text", "content": f"📊 发版历史 — {state['project']} / {state['repo']}"}},
        "elements": elements,
    }


def build_approval_card(
    state: Dict[str, Any],
    requester_open_id: str,
    approval_id: str,
    approvers: List[str] = None,
) -> Dict[str, Any]:
    """构建审批请求卡片"""
    variables_line = ""
    if state.get("variables"):
        v_lines = []
        for vdef in state.get("variables_def", []):
            k = vdef["key"]
            val = state["variables"].get(k)
            label = vdef.get("label", k).split(" ", 1)[-1] if " " in vdef.get("label", k) else vdef.get("label", k)
            v_lines.append(f"**{label}**：{val}")
        if v_lines:
            variables_line = "\n" + "\n".join(v_lines)
    
    at_approvers = ""
    if approvers:
        at_approvers = " ".join([f"<at id=\"{uid}\"></at>" for uid in approvers])
        
    content = (
        f"<at id=\"{requester_open_id}\"></at> **请求发布至生产环境**\n\n"
        f"**项目**：{state['project']} - {state['repo']}\n"
        f"**分支**：{state['branch']}\n"
        f"**环境**：{state['env']}"
        f"{variables_line}"
    )
    if at_approvers:
        content += f"\n\n**请审批**：{at_approvers}"

    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "orange", "title": {"tag": "plain_text", "content": "🔐 发版审批请求"}},
        "elements": [
            {"tag": "markdown", "content": content},
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "✅ 批准发布"},
                        "type": "primary",
                        "value": {"action": "approve", "approval_id": approval_id},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "❌ 驳回"},
                        "type": "danger",
                        "value": {"action": "reject", "approval_id": approval_id},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "🤖 获取 AI 审查报告"},
                        "value": {
                            "action": "review",
                            "project": state["project"],
                            "repo": state["repo"],
                            "branch": state["branch"],
                            "env": state["env"]
                        },
                    },
                ],
            },
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": "⚠️ 发版前建议点击「获取 AI 审查报告」进行风险排查。仅配置的审批人可操作。"}],
            },
        ],
    }


def build_card(
    cfg: Dict[str, Any], status: str, state: Dict[str, Any], latest_pipeline_text: Optional[str] = None,
    latest_result_text: Optional[str] = None, show_details: bool = False, is_locked: bool = False,
) -> Dict[str, Any]:
    projects = cfg.get("projects", [])
    project = next((p for p in projects if p["name"] == state["project"]), projects[0])

    latest_result_text = latest_result_text or status
    latest_pipeline_text = latest_pipeline_text or "暂无"

    display_project_name = project["name"]
    repo_name = state["repo"]

    meta = status_meta(status, latest_result_text, latest_pipeline_text)

    project_options = [
        {"text": {"tag": "plain_text", "content": p["name"]},"value": p["name"]}
        for p in projects
    ]
    repo_options = [
        {"text": {"tag": "plain_text", "content": r["name"]},"value": r["name"]}
        for r in project["repos"]
    ]
    branch_options = [
        {"text": {"tag": "plain_text", "content": b},"value": b}
        for b in state["branches"]
    ]

    def get_env_display(e: str) -> str:
        le = e.lower()
        if le in ["test", "测试"]:
            return f"{e}环境 (Test)"
        if le in ["prod", "production", "线上", "生产"]:
            return f"{e}环境 (Prod)"
        return f"{e}环境"

    env_options_enriched = [
        {"text": {"tag": "plain_text", "content": get_env_display(e)}, "value": e}
        for e in project["environments"]
    ]
    env_placeholder = get_env_display(state["env"])

    status_color = "green"
    status_label = "空闲中 (Idle)"
    if meta["label"] == "处理中":
        status_color = "orange"
        status_label = "处理中 (Running)"
    elif meta["label"] == "异常":
        status_color = "red"
        status_label = "异常 (Failed)"

    pipeline_line = latest_pipeline_text if latest_pipeline_text and latest_pipeline_text != "暂无" else "暂无"

    helper_line = None
    if show_details and latest_result_text:
        helper_line = latest_result_text

    action_base_val = {
        "project": state["project"],
        "repo": state["repo"],
        "branch": state["branch"],
        "env": state["env"]
    }
    for k, v in state.get("variables", {}).items():
        action_base_val[f"var_{k}"] = v

    elements = [
        {
            "tag": "markdown",
            "content": f"当前流水线状态： {meta['emoji']} {status_label}\n流水线链接：{pipeline_line}"
        },
        {"tag": "hr"},
        {
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "default",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "vertical_align": "center",
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": "**🏢 选项目 (Project)**"
                        }
                    ]
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 2,
                    "vertical_align": "center",
                    "elements": [
                        {
                            "tag": "select_static",
                            "placeholder": {"tag": "plain_text", "content": display_project_name},
                            "name": "project",
                            "options": project_options,
                            "value": {**action_base_val, "current_field": "project"},
                        }
                    ]
                }
            ]
        },
        {
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "default",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "vertical_align": "center",
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": "**📦 选仓库 (Repository)**"
                        }
                    ]
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 2,
                    "vertical_align": "center",
                    "elements": [
                        {
                            "tag": "select_static",
                            "placeholder": {"tag": "plain_text", "content": repo_name},
                            "name": "repo",
                            "options": repo_options,
                            "value": {**action_base_val, "current_field": "repo"},
                        }
                    ]
                }
            ]
        },
        {
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "default",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "vertical_align": "center",
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": "**✂️ 选分支 (Branch)**"
                        }
                    ]
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 2,
                    "vertical_align": "center",
                    "elements": [
                        {
                            "tag": "select_static",
                            "placeholder": {"tag": "plain_text", "content": state["branch"]},
                            "name": "branch",
                            "options": branch_options,
                            "value": {**action_base_val, "current_field": "branch"},
                        }
                    ]
                }
            ]
        },
        {
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "default",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "vertical_align": "center",
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": "**🚀 选环境 (Environment)**"
                        }
                    ]
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 2,
                    "vertical_align": "center",
                    "elements": [
                        {
                            "tag": "select_static",
                            "placeholder": {"tag": "plain_text", "content": env_placeholder},
                            "name": "env",
                            "options": env_options_enriched,
                            "value": {**action_base_val, "current_field": "env"},
                        }
                    ]
                }
            ]
        }
    ]

    for vdef in state.get("variables_def", []):
         vk = vdef["key"]
         vlabel = vdef.get("label", f"🎛️ 变量 {vk}")
         vopts = [{"text": {"tag": "plain_text", "content": opt}, "value": opt} for opt in vdef["options"]]
         
         elements.append({
             "tag": "column_set",
             "flex_mode": "none",
             "background_style": "default",
             "columns": [
                 {
                     "tag": "column", "width": "weighted", "weight": 1, "vertical_align": "center",
                     "elements": [{"tag": "markdown", "content": f"**{vlabel}**"}]
                 },
                 {
                     "tag": "column", "width": "weighted", "weight": 2, "vertical_align": "center",
                     "elements": [
                         {
                             "tag": "select_static",
                             "placeholder": {"tag": "plain_text", "content": state["variables"][vk]},
                             "name": f"var_{vk}",
                             "options": vopts,
                             "value": {**action_base_val, "current_field": f"var_{vk}"},
                         }
                     ]
                 }
             ]
         })

    elements.append({"tag": "hr"})

    if is_locked:
        action_elements = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔒 当前仓库发版中..."},
                "type": "default",
                "value": {**action_base_val, "action": "locked"},
            },
        ]
    else:
        action_elements = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "✅ 执行触发 / 申请发布"},
                "type": "primary",
                "value": {**action_base_val, "action": "run"},
                "confirm": {
                    "title": {"tag": "plain_text", "content": "⚠️ 发版最终确认"},
                    "text": {"tag": "plain_text", "content": "您即将直接触发部署流程。\n\n• 如果你已经点过 AI Review，或者确信代码无风险，请点击【确定】。\n• 如果你想先让 AI 把关，请点击【取消】，然后点击旁边的【🤖 AI Review】按钮。\n\n确定要跳过审查、直接发版吗？"}
                }
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🤖 AI Review"},
                "value": {**action_base_val, "action": "review"},
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "📋 历史记录"},
                "value": {**action_base_val, "action": "history"},
            },
        ]

    elements.append({
        "tag": "action",
        "actions": action_elements,
    })

    if helper_line:
        elements.append(
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": helper_line,
                    }
                ],
            }
        )

    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": "🚀 GitLab 智能发版控制台"}},
        "elements": elements,
    }
