import logging
import re
from typing import Any, Dict, List, Optional

from core.gitlab_client import GitLabClient, build_gitlab_client, resolve_gitlab_instance_name

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


def get_env_display(e: str, display_map: Optional[Dict[str, str]] = None) -> str:
    # 优先使用项目级自定义映射（config 中 project.env_display），实现按项目区分显示名
    if display_map and e in display_map:
        return display_map[e]
    le = e.lower()
    if le in ["test", "测试"]:
        return "本地机房"
    if le in ["prod", "production", "线上", "生产"]:
        return "阿里云"
    return f"{e}环境"


def format_variable_display_label(raw_label: str, key: str = "") -> str:
    """
    格式化发版卡片、审批卡片等展示中的变量标签。
    将「🧩 选服务」、「选服务」、「🧩 选微服务 (Module)」等统一规范展示为「服务」，
    并在结果卡片中清理多余的提示动词（如「选」）及表情符号，使排版更加自然美观。
    """
    if not raw_label:
        return "服务" if key == "TARGET_MODULE" else (key or "变量")

    # 若为 TARGET_MODULE 或包含“选服务”/“选微服务”/“微服务”
    if key == "TARGET_MODULE" or any(x in raw_label for x in ["选服务", "选微服务", "微服务"]):
        return "服务"

    # 去除常见的开头部 Emoji (如 🧩, 🎨, ⚙️, 🎛️ 等) 及紧随的空格
    label = re.sub(r"^[\U00010000-\U0010ffff\u2600-\u27bf\u2300-\u23ff\s]+", "", raw_label).strip()

    # 如果去掉 emoji 后是以“选”开头的词（例如“选服务”->“服务”），去掉“选”
    if label.startswith("选") and len(label) > 1:
        label = label[1:].strip()

    # 去掉末尾可能存在的英文注释如 (Module)
    label = re.sub(r"\s*\([^)]*\)$", "", label).strip()

    return label or raw_label


async def normalize_selection(
    cfg: Dict[str, Any],
    gitlab_client: Optional[GitLabClient] = None,
    selected_project: Optional[str] = None,
    selected_repo: Optional[str] = None,
    selected_branch: Optional[str] = None,
    selected_env: Optional[str] = None,
    cached_branches: Optional[list] = None,
    selected_vars: Optional[Dict[str, str]] = None,
    force_refresh_branches: bool = False,
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

    # 根据 repo 绑定的实例名解析出对应的 GitLab 客户端；
    # 若 repo 未指定实例或该实例不存在，则回退到调用方传入的客户端。
    gitlab_instance = resolve_gitlab_instance_name(cfg, repo=repo)
    repo_gitlab_client = build_gitlab_client(cfg, repo=repo) or gitlab_client

    branches: List[str] = []
    if not force_refresh_branches and cached_branches:
        branches = cached_branches
    elif repo_gitlab_client:
        try:
            if force_refresh_branches:
                await repo_gitlab_client.invalidate_branches_cache(repo["id"])
            branches = await repo_gitlab_client.get_branches_cached(repo["id"])
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

    # 解析当前环境对应的访问地址（access 可为 {env: url 或 [url...]}、字符串或列表结构）。
    access_cfg = repo.get("access", {})
    access_urls: List[str] = []
    if isinstance(access_cfg, dict):
        raw = access_cfg.get(env_value) or access_cfg.get("default")
        if isinstance(raw, str) and raw:
            access_urls = [raw]
        elif isinstance(raw, list):
            access_urls = [u for u in raw if isinstance(u, str) and u]
    elif isinstance(access_cfg, str) and access_cfg:
        access_urls = [access_cfg]
    elif isinstance(access_cfg, list):
        access_urls = [u for u in access_cfg if isinstance(u, str) and u]

    # 解析访问提示信息（access_note 可为 {env: note} 字典或通用字符串）。
    access_note_raw = repo.get("access_note", "")
    access_note = ""
    if isinstance(access_note_raw, dict):
        access_note = str(access_note_raw.get(env_value) or access_note_raw.get("default") or "")
    elif isinstance(access_note_raw, str):
        access_note = access_note_raw

    return {
        "project": project["name"],
        "repo": repo["name"],
        "repo_id": repo["id"],
        "repo_path": repo["repo"],
        "gitlab_instance": gitlab_instance,
        "sonar_project_key": repo.get("sonar_project_key", ""),
        "env_display": project.get("env_display", {}),
        "access_urls": access_urls,
        "access_note": access_note,
        "branch": branch_value,
        "env": env_value,
        "branches": branches,
        "variables_def": variables_def,
        "variables": vars_val,
    }


def build_sub_card(
    state: Dict[str, Any],
    operator_open_id: str,
    pipeline_id: int,
    p_status: str,
    active_job_name: str,
    failed_job_info: str = "",
    commit_info: str = "",
    ack_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    commit_line = f"\n**提交**：{commit_info}" if commit_info else ""
    env_display = get_env_display(state['env'], state.get('env_display'))

    # 组装自定义构建变量信息行
    variables_line = ""
    if state.get("variables"):
        v_lines = []
        defined_keys = set()
        for vdef in state.get("variables_def", []):
            k = vdef["key"]
            defined_keys.add(k)
            if k in state["variables"]:
                val = state["variables"][k]
                label = format_variable_display_label(vdef.get("label", k), k)
                v_lines.append(f"**{label}**：`{val}`")
        for k, val in state["variables"].items():
            if k not in defined_keys:
                label = format_variable_display_label(k, k)
                v_lines.append(f"**{label}**：`{val}`")
        if v_lines:
            variables_line = "\n" + "\n".join(v_lines)

    # 组装访问信息行（仅发版成功时展示，放在流水线信息后面）
    access_line = ""
    access_urls = state.get("access_urls") or []
    if access_urls:
        if len(access_urls) == 1:
            access_line += f"\n**访问地址**：[{access_urls[0]}]({access_urls[0]})"
        else:
            links = "、".join(f"[入口{idx}]({u})" for idx, u in enumerate(access_urls, 1))
            access_line += f"\n**访问地址**：{links}"
            
    access_note = state.get("access_note") or ""
    if access_note:
        access_line += f"\n**💡 温馨提示**：{access_note}"

    if p_status == "success":
        color = "green"
        emoji = "✅"
        content = f"<at id=\"{operator_open_id}\"></at> **发版任务已执行成功！** 🎉\n\n**项目**：{state['project']} - {state['repo']}\n**分支**：{state['branch']}{commit_line}\n**环境**：{env_display}{variables_line}\n**流水线**：#{pipeline_id}{access_line}"
    elif p_status in {"failed", "canceled"}:
        color = "red"
        emoji = "❌"
        content = f"<at id=\"{operator_open_id}\"></at> **发版任务执行异常终止！**\n\n**项目**：{state['project']} - {state['repo']}\n**分支**：{state['branch']}{commit_line}\n**环境**：{env_display}{variables_line}\n**流水线**：#{pipeline_id} [{p_status}]"
        if failed_job_info:
            content += f"\n\n**失败点**：\n🚨 {failed_job_info}"
    else:
        color = "blue"
        emoji = "⏳"
        content = f"<at id=\"{operator_open_id}\"></at> **发版任务追踪中...**\n\n**项目**：{state['project']} - {state['repo']}\n**分支**：{state['branch']}{commit_line}\n**环境**：{env_display}{variables_line}\n**流水线**：#{pipeline_id}\n**当前进展**：正在跟进 {active_job_name}"

    # 终态且已确认时，在卡片正文追加反馈确认记录
    if p_status in {"success", "failed", "canceled"} and ack_info:
        ack_by = ack_info.get("ack_by_name") or ack_info.get("ack_by") or "责任人"
        ack_at = ack_info.get("ack_at") or ""
        at_suffix = f" 已于 {ack_at}" if ack_at else ""
        content += f"\n\n📌 **知晓确认**：✅ {ack_by}{at_suffix} 确认知晓"

    elements: List[Dict[str, Any]] = [{"tag": "markdown", "content": content}]

    action_buttons = []
    if p_status == "success" and access_urls:
        for idx, url in enumerate(access_urls, 1):
            btn_text = "🌐 一键打开网页" if len(access_urls) == 1 else f"🌐 打开入口 {idx}"
            action_buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": btn_text},
                "type": "primary" if idx == 1 else "default",
                "url": url,
            })

    # 流水线到达终态且尚未确认时，为责任人提供「收到/排查」确认按钮
    if p_status in {"success", "failed", "canceled"} and not ack_info:
        if p_status == "success":
            btn_text = "👌 收到，已阅"
            btn_type = "default" if action_buttons else "primary"
        else:
            btn_text = "👀 收到，排查中"
            btn_type = "danger"

        action_buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": btn_text},
            "type": btn_type,
            "value": {
                "action": "acknowledge_sub_card",
                "operator_open_id": operator_open_id,
                "pipeline_id": pipeline_id,
                "p_status": p_status,
                "repo_id": state.get("repo_id"),
            }
        })

    if action_buttons:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "action",
            "actions": action_buttons
        })

    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": color, "title": {"tag": "plain_text", "content": f"{emoji} 发版进度指示器"}},
        "elements": elements
    }


def _sanitize_feishu_md(text: str, max_len: int = 90) -> str:
    """清理可能干扰飞书 Markdown 解析的字符并做长度截断"""
    cleaned = str(text or "").replace("\r\n", " ").replace("\n", " ").replace("\r", " ").strip()
    cleaned = cleaned.replace("<", "＜").replace(">", "＞").replace("`", "'")
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    if len(cleaned) > max_len:
        return cleaned[: max_len - 3] + "..."
    return cleaned


def _short_location(location: str, max_len: int = 56) -> str:
    loc = str(location or "").strip()
    if len(loc) <= max_len:
        return loc
    head = max_len // 3
    tail = max_len - head - 3
    return f"{loc[:head]}...{loc[-tail:]}"


def _sonar_sev_icon(severity: str) -> str:
    s = (severity or "").upper()
    if s in {"BLOCKER", "CRITICAL"}:
        return "🔴"
    if s in {"HIGH", "MAJOR"}:
        return "🟠"
    if s in {"MEDIUM", "MINOR"}:
        return "🟡"
    return "⚪"


def sonar_report_needs_ack(p_status: str, report: Optional[Dict[str, Any]] = None) -> bool:
    """当流水线异常、质量门禁未通过/警告，或存在高危问题/高危热点时，需要责任人确认闭环"""
    if p_status in {"failed", "canceled"}:
        return True
    if not report:
        return False
    if str(report.get("qg_status") or "").upper() in {"ERROR", "WARN"}:
        return True
    for iss in report.get("issues") or []:
        if str(iss.get("severity") or "").upper() in {"BLOCKER", "CRITICAL", "HIGH"}:
            return True
    for hs in report.get("hotspots") or []:
        if str(hs.get("probability") or "").upper() == "HIGH":
            return True
    return False


def build_sonar_card(
    state: Dict[str, Any],
    operator_open_id: str,
    pipeline_id: int,
    p_status: str,
    active_job_name: str = "",
    report: Optional[Dict[str, Any]] = None,
    failed_job_info: str = "",
    commit_info: str = "",
    pipeline_url: str = "",
    ack_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构建 SonarQube 代码扫描追踪与结果汇报卡片"""
    commit_line = f"\n**提交**：{commit_info}" if commit_info else ""

    # 提取关键目标参数（如 TARGET_MODULE / 服务），避免冗长展示无关发版环境
    var_parts: List[str] = []
    if state.get("variables"):
        defined_keys = set()
        for vdef in state.get("variables_def", []):
            k = vdef["key"]
            defined_keys.add(k)
            if k in state["variables"]:
                label = format_variable_display_label(vdef.get("label", k), k)
                var_parts.append(f"**{label}**：`{state['variables'][k]}`")
        for k, val in state["variables"].items():
            if k not in defined_keys and k != "ACTION":
                label = format_variable_display_label(k, k)
                var_parts.append(f"**{label}**：`{val}`")
    var_inline = ("　|　" + "　|　".join(var_parts)) if var_parts else ""

    pipeline_ref = f"[#{pipeline_id}]({pipeline_url})" if pipeline_url else f"#{pipeline_id}"
    is_finished = p_status in {"success", "failed", "canceled"}
    qg_status = str((report or {}).get("qg_status") or "").upper()
    sonar_proj_name = (report or {}).get("project_name") or (report or {}).get("project_key") or ""
    proj_suffix = f"（`{sonar_proj_name}`）" if sonar_proj_name else ""

    has_high_issue = any(
        str(iss.get("severity") or "").upper() in {"BLOCKER", "CRITICAL", "HIGH"}
        for iss in ((report or {}).get("issues") or [])
    )

    if not is_finished:
        header_color = "blue"
        header_title = "⏳ SonarQube 代码扫描追踪中"
        intro = f"<at id=\"{operator_open_id}\"></at> **正在执行代码质量扫描...**"
    elif qg_status == "ERROR":
        header_color = "red"
        header_title = "❌ SonarQube 质量门禁未通过"
        intro = f"<at id=\"{operator_open_id}\"></at> **代码扫描完成，质量门禁未通过！**"
    elif qg_status == "WARN":
        header_color = "orange"
        header_title = "⚠️ SonarQube 质量门禁警告"
        intro = f"<at id=\"{operator_open_id}\"></at> **代码扫描完成，质量门禁存在警告**"
    elif p_status in {"failed", "canceled"} and not (report and (report.get("measures") or report.get("qg_conditions"))):
        header_color = "red"
        header_title = "❌ SonarQube 代码扫描异常"
        intro = f"<at id=\"{operator_open_id}\"></at> **SonarQube 扫描流水线异常终止！**"
    elif qg_status == "OK":
        header_color = "green"
        if has_high_issue:
            header_title = "✅ SonarQube 门禁通过（含高危待处理问题）"
        else:
            header_title = "✅ SonarQube 代码扫描通过"
        intro = f"<at id=\"{operator_open_id}\"></at> **SonarQube 代码扫描完成！** 🎉"
    else:
        header_color = "red" if p_status in {"failed", "canceled"} else "blue"
        header_title = "🛡️ SonarQube 代码质量扫描报告"
        intro = f"<at id=\"{operator_open_id}\"></at> **SonarQube 代码扫描完成**"

    qg_badge_map = {
        "OK": "✅ **通过 (OK)**",
        "WARN": "⚠️ **警告 (WARN)**",
        "ERROR": "❌ **未通过 (ERROR)**",
    }
    qg_line = f"　|　**质量门禁**：{qg_badge_map[qg_status]}" if qg_status in qg_badge_map else ""

    summary_lines = [
        intro,
        "",
        f"**项目**：{state['project']} - {state['repo']}{proj_suffix}",
        f"**分支**：`{state['branch']}`{var_inline}{commit_line}",
        f"**流水线**：{pipeline_ref}{qg_line}",
    ]

    if not is_finished:
        summary_lines.append(f"**当前进展**：正在跟进 `{active_job_name or '阶段衔接中'}`")

    if report and report.get("qg_conditions"):
        summary_lines.append("")
        summary_lines.append("**📈 质量门禁明细**")
        for cond in report["qg_conditions"]:
            cst = str(cond.get("status") or "").upper()
            cicon = "✅" if cst == "OK" else ("❌" if cst == "ERROR" else "⚠️")
            label = cond.get("label") or cond.get("metric_key") or ""
            actual = cond.get("actual_value", "-")
            comp = cond.get("comparator", "")
            thr = cond.get("threshold", "")
            thr_text = f"（失败阈值 {comp} {thr}）".strip() if (comp or thr) else ""
            summary_lines.append(f"• {cicon} {label}：实际 `{actual}` {thr_text}")

    elements: List[Dict[str, Any]] = [{"tag": "markdown", "content": "\n".join(summary_lines)}]

    # 指标、重点问题与热点分段展示
    detail_sections: List[str] = []
    if report:
        measures = report.get("measures") or {}
        if measures:
            ncloc = measures.get("ncloc", "-")
            dup = measures.get("duplicated_lines_density", "-")
            cov = measures.get("coverage", "-")
            sec = measures.get("security_issues", "-")
            rel = measures.get("reliability_issues", "-")
            mnt = measures.get("maintainability_issues", "-")
            hsp = measures.get("security_hotspots", str(report.get("hotspot_total")) if report.get("hotspot_total", -1) >= 0 else "-")
            sec_r = f" ({measures['security_rating']})" if measures.get("security_rating") else ""
            rel_r = f" ({measures['reliability_rating']})" if measures.get("reliability_rating") else ""
            mnt_r = f" ({measures['maintainability_rating']})" if measures.get("maintainability_rating") else ""
            detail_sections.append(
                f"**📊 全量代码指标**（代码行 `{ncloc}` · 重复率 `{dup}` · 覆盖率 `{cov}`）\n"
                f"• 🔓 安全性：**{sec}**{sec_r}　|　🐛 可靠性：**{rel}**{rel_r}\n"
                f"• 🔧 可维护性：**{mnt}**{mnt_r}　|　🔥 安全热点：**{hsp}**"
            )

        issue_total = report.get("issue_total", -1)
        issues = report.get("issues") or []
        if issue_total == 0:
            detail_sections.append("🎉 **未解决代码问题**：`0` 个！")
        elif issues:
            header_tot = f"共 `{issue_total}` 个，展示前 {len(issues)} 条" if issue_total > 0 else f"展示前 {len(issues)} 条"
            issue_lines = [f"**🚨 未解决重点问题**（{header_tot}）"]
            for idx, iss in enumerate(issues, 1):
                sev = str(iss.get("severity") or "INFO").upper()
                qual = str(iss.get("quality") or "").upper()
                badge = f"{sev}/{qual}" if qual else sev
                icon = _sonar_sev_icon(sev)
                loc = _short_location(iss.get("location") or "-")
                msg = _sanitize_feishu_md(iss.get("message") or "", max_len=85)
                rule = str(iss.get("rule") or "").strip()
                rule_part = f" (`{rule}`)" if rule else ""
                issue_lines.append(f"{idx}. {icon} `[{badge}]` `{loc}` — {msg}{rule_part}")
            detail_sections.append("\n".join(issue_lines))

        hotspot_total = report.get("hotspot_total", -1)
        hotspots = report.get("hotspots") or []
        if hotspot_total and hotspot_total > 0 and hotspots:
            hs_lines = [f"**🔥 待审查安全热点**（共 `{hotspot_total}` 个，展示前 {len(hotspots)} 条）"]
            for idx, hs in enumerate(hotspots, 1):
                prob = str(hs.get("probability") or "MEDIUM").upper()
                loc = _short_location(hs.get("location") or "-")
                msg = _sanitize_feishu_md(hs.get("message") or "", max_len=80)
                hs_lines.append(f"{idx}. 🔥 `[{prob}]` `{loc}` — {msg}")
            detail_sections.append("\n".join(hs_lines))

        if report.get("auth_denied") and not measures and not issues:
            detail_sections.append(
                "⚠️ **无法拉取明细指标 (HTTP 401/403)**：请在 `config.yaml` 的 `sonarqube.token` 或 GitLab `SONAR_API_TOKEN` 中配置 SonarQube User Token (`squ_...`)。"
            )
        elif report.get("error") and not measures and not issues:
            detail_sections.append(f"⚠️ **SonarQube 明细拉取提示**：{_sanitize_feishu_md(report['error'], max_len=140)}")

    if failed_job_info:
        detail_sections.append(f"**🚨 异常详情**\n{failed_job_info}")

    if is_finished and ack_info:
        ack_by = ack_info.get("ack_by_name") or ack_info.get("ack_by") or "责任人"
        ack_at = ack_info.get("ack_at") or ""
        at_suffix = f" 已于 {ack_at}" if ack_at else ""
        detail_sections.append(f"📌 **知晓确认**：✅ {ack_by}{at_suffix} 确认知晓")

    if detail_sections:
        elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": "\n\n".join(detail_sections)})

    action_buttons: List[Dict[str, Any]] = []
    dashboard_url = str((report or {}).get("dashboard_url") or "").strip()
    if is_finished and dashboard_url:
        action_buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🔍 打开 SonarQube 报告"},
            "type": "primary",
            "url": dashboard_url,
        })
    if is_finished and pipeline_url:
        action_buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🔗 GitLab 流水线"},
            "type": "default" if action_buttons else "primary",
            "url": pipeline_url,
        })

    needs_ack = is_finished and bool(pipeline_id) and sonar_report_needs_ack(p_status, report)
    if needs_ack and not ack_info:
        is_severe_fail = qg_status in {"ERROR", "WARN"} or (p_status in {"failed", "canceled"} and not (report and report.get("measures")))
        btn_text = "👀 收到，排查中" if is_severe_fail else "👌 收到，已知晓"
        btn_type = "danger" if is_severe_fail else "default"
        action_buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": btn_text},
            "type": btn_type,
            "value": {
                "action": "acknowledge_sub_card",
                "card_kind": "sonar",
                "operator_open_id": operator_open_id,
                "pipeline_id": pipeline_id,
                "p_status": p_status,
                "repo_id": state.get("repo_id"),
            },
        })

    if action_buttons:
        elements.append({"tag": "hr"})
        elements.append({"tag": "action", "actions": action_buttons})

    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": header_color, "title": {"tag": "plain_text", "content": header_title}},
        "elements": elements,
    }


def build_wiki_card(
    state: Dict[str, Any],
    operator_open_id: str,
    w_status: str,
    stage_text: str = "",
    commit_sha: str = "",
    commit_url: str = "",
    result_info: Optional[Dict[str, Any]] = None,
    error_info: str = "",
) -> Dict[str, Any]:
    """构建 RepoWiki 生成进度与结果汇报卡片"""
    branch = state.get("branch", "-")
    project_name = state.get("project", "-")
    repo_name = state.get("repo", "-")
    subpath = (result_info or {}).get("subpath") or ""
    subpath_line = f"　|　**子目录**：`{subpath}`" if subpath else ""
    short_sha = commit_sha[:8] if commit_sha else "-"
    commit_ref = f"[`{short_sha}`]({commit_url})" if commit_url and short_sha != "-" else f"`{short_sha}`"

    result = result_info or {}
    write_mode = result.get("write_mode", "")

    if w_status == "running":
        header_color = "blue"
        header_title = "⏳ RepoWiki 生成中..."
        intro = f"<at id=\"{operator_open_id}\"></at> **正在分析源码并生成 RepoWiki 文档...**"
    elif w_status == "skipped":
        header_color = "green"
        header_title = "⚡ RepoWiki 已是最新版本"
        intro = f"<at id=\"{operator_open_id}\"></at> **当前提交与基线一致，无需重复生成**"
    elif w_status == "success":
        if write_mode == "mr":
            header_color = "green"
            header_title = "🔀 RepoWiki 已生成并提交 Merge Request"
            intro = f"<at id=\"{operator_open_id}\"></at> **RepoWiki 生成完成！由于目标分支受保护，已自动切出文档分支并创建 MR** 🎉"
        else:
            header_color = "green"
            header_title = "✅ RepoWiki 生成并写回成功"
            intro = f"<at id=\"{operator_open_id}\"></at> **RepoWiki 已成功生成并写入代码仓库！** 🎉"
    else:
        header_color = "red"
        header_title = "❌ RepoWiki 生成失败"
        intro = f"<at id=\"{operator_open_id}\"></at> **RepoWiki 生成过程中发生异常**"

    summary_lines = [
        intro,
        "",
        f"**项目**：{project_name} - {repo_name}",
        f"**目标分支**：`{branch}`{subpath_line}　|　**基线提交**：{commit_ref}",
    ]

    if w_status == "running":
        summary_lines.append(f"**当前进展**：{stage_text or '准备中...'}")

    elements: List[Dict[str, Any]] = [{"tag": "markdown", "content": "\n".join(summary_lines)}]
    detail_sections: List[str] = []

    if w_status == "skipped":
        wiki_dir = result.get("wiki_dir", "docs/repowiki")
        pages_cnt = result.get("pages_count") or "-"
        cards_cnt = result.get("cards_count") or "-"
        skipped_lines = [
            f"• **文档目录**：`{wiki_dir}`（`{pages_cnt}` 篇文档 · `{cards_cnt}` 张知识卡片）",
            "• **说明**：当前代码指纹未发生变化（`0` 次模型调用）。如需强制重建，可在群聊发送 `强制生成 <项目/仓库> wiki`。",
        ]
        detail_sections.append("\n".join(skipped_lines))
    elif w_status == "success":
        wiki_dir = result.get("wiki_dir", "docs/repowiki")
        eligible = result.get("eligible_files", 0)
        omitted = result.get("omitted_files", 0)
        pages_cnt = result.get("pages_count", 0)
        cards_cnt = result.get("cards_count", 0)
        model_calls = result.get("model_calls", 0)
        elapsed_sec = result.get("elapsed_seconds")
        elapsed_text = f" · 耗时 `{elapsed_sec:.1f}s`" if isinstance(elapsed_sec, (int, float)) else ""

        stats_lines = [
            f"**📊 生成概览**：纳入 `{eligible}` 个源码文件（过滤 `{omitted}` 个）",
            f"• **产物统计**：文档 `{pages_cnt}` 篇（含总览 README）· 知识卡片 `{cards_cnt}` 张 · AI 调用 `{model_calls}` 次{elapsed_text}",
            f"• **写入路径**：`{wiki_dir}/README.md`",
        ]

        write_branch = result.get("write_branch") or branch
        new_commit_sha = result.get("write_commit_sha") or ""
        new_commit_short = new_commit_sha[:8] if new_commit_sha else ""
        if write_mode == "mr":
            mr_iid = result.get("mr_iid")
            mr_label = f"!{mr_iid}" if mr_iid else "已创建"
            reused_tip = "（已更新现有 MR）" if result.get("mr_reused") else "（已自动创建）"
            stats_lines.append(
                f"• **写回方式**：🛡️ 保护分支自动降级 → 已推送分支 `{write_branch}` 并提交 MR **{mr_label}** {reused_tip}"
            )
        elif write_mode == "no_diff":
            stats_lines.append(f"• **写回方式**：⚡ 仓库 `{branch}` 已有完全一致文档内容，跳过空提交")
        else:
            c_part = f"（提交 `{new_commit_short}`，含 `[skip ci]`）" if new_commit_short else "（含 `[skip ci]`）"
            stats_lines.append(f"• **写回方式**：✅ 已直接提交至分支 `{write_branch}` {c_part}")

        detail_sections.append("\n".join(stats_lines))

        topics = result.get("topics") or []
        if topics:
            topic_lines = ["**📑 已生成专题目录**"]
            for idx, t in enumerate(topics, 1):
                topic_lines.append(f"{idx}. **{t.get('title', t.get('id', '专题'))}** — `{t.get('path', '')}`")
            detail_sections.append("\n".join(topic_lines))

    if w_status == "failed" and error_info:
        detail_sections.append(f"**🚨 失败原因**\n{_sanitize_feishu_md(error_info, max_len=280)}")

    if detail_sections:
        elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": "\n\n".join(detail_sections)})

    action_buttons: List[Dict[str, Any]] = []
    if w_status in {"success", "skipped"}:
        mr_url = str(result.get("mr_url") or "").strip()
        doc_url = str(result.get("doc_url") or "").strip()
        write_commit_url = str(result.get("write_commit_url") or "").strip()

        if mr_url:
            action_buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔀 查看 Merge Request"},
                "type": "primary",
                "url": mr_url,
            })
        if doc_url:
            action_buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "📖 查看 GitLab 文档"},
                "type": "primary" if not action_buttons else "default",
                "url": doc_url,
            })
        if write_commit_url and write_commit_url != doc_url:
            action_buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔖 查看提交记录"},
                "type": "default",
                "url": write_commit_url,
            })

    if action_buttons:
        elements.append({"tag": "hr"})
        elements.append({"tag": "action", "actions": action_buttons})

    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": header_color, "title": {"tag": "plain_text", "content": header_title}},
        "elements": elements,
    }


def build_history_card(
    history_records: List[Dict[str, Any]],
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """构建 Pipeline 历史记录卡片"""
    if not history_records:
        content = "📭 暂无该仓库的发版历史记录。"
    else:
        lines = []
        status_map = {"success": "✅ 成功", "failed": "❌ 失败", "canceled": "⚠️ 取消", "running": "🔄 执行中"}
        for i, rec in enumerate(history_records[:15], 1):
            status_text = status_map.get(rec.get("status", ""), "⏳ 未知")
            
            operator = ""
            if rec.get("operator_name") and rec["operator_name"] != rec.get("operator_open_id"): 
                operator = f"👤 {rec['operator_name']}"
            elif rec.get("operator_open_id"): 
                operator = f"👤 <at id=\"{rec['operator_open_id']}\"></at>"

            line = f"**{i}. 流水线 #{rec['pipeline_id']}**  |  {status_text}  |  {operator}\n"
            line += f"　 🌿 分支：`{rec['branch']}`  ➡️  🚀 环境：`{get_env_display(rec['env'], state.get('env_display'))}`"
            
            if rec.get("module"): 
                line += f"\n　 🧩 附加参数：`{rec['module']}`"
                
            time_line = ""
            if rec.get("triggered_at"): 
                time_line += f"{rec['triggered_at']}"
            if rec.get("finished_at"):
                time_line += f"  完毕于  {rec['finished_at']}"
            if time_line:
                line += f"\n　 🕐 调度周期：{time_line}"
                
            lines.append(line + "\n")
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
    approvers: Optional[List[str]] = None,
    resolved_by: Optional[str] = None,
    status: str = "pending",
) -> Dict[str, Any]:
    """构建审批请求卡片"""
    variables_line = ""
    if state.get("variables"):
        v_lines = []
        for vdef in state.get("variables_def", []):
            k = vdef["key"]
            val = state["variables"].get(k)
            label = format_variable_display_label(vdef.get("label", k), k)
            v_lines.append(f"**{label}**：{val}")
        if v_lines:
            variables_line = "\n" + "\n".join(v_lines)
    
    at_approvers = ""
    if approvers and status == "pending":
        at_approvers = " ".join([f"<at id=\"{uid}\"></at>" for uid in approvers])
        
    content = (
        f"<at id=\"{requester_open_id}\"></at> **请求发布至生产环境**\n\n"
        f"**项目**：{state['project']} - {state['repo']}\n"
        f"**分支**：{state['branch']}\n"
        f"**环境**：{get_env_display(state['env'], state.get('env_display'))}"
        f"{variables_line}"
    )
    if at_approvers:
        content += f"\n\n**请审批**：{at_approvers}"

    elements: List[Dict[str, Any]] = [
        {"tag": "markdown", "content": content},
        {"tag": "hr"}
    ]

    header_template = "orange"
    header_title = "🔐 发版审批请求"

    if status == "pending":
        elements.append({
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
        })
        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": "⚠️ 发版前建议点击「获取 AI 审查报告」进行风险排查。仅配置的审批人可操作。"}],
        })
    else:
        if status == "approved":
            header_template = "green"
            header_title = "✅ 审批已通过"
            status_text = f"✅ **已批准发布**\n审批人：<at id=\"{resolved_by}\"></at>"
        else:
            header_template = "red"
            header_title = "❌ 审批已驳回"
            status_text = f"❌ **已驳回**\n操作人：<at id=\"{resolved_by}\"></at>"
        
        elements.append({
            "tag": "markdown",
            "content": status_text
        })

    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": header_template, "title": {"tag": "plain_text", "content": header_title}},
        "elements": elements,
    }


def build_project_management_card(
    all_projects: List[Dict[str, Any]],
    dynamic_project_names: set,
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """构建项目管理面板卡片"""
    elements = [
        {
            "tag": "markdown",
            "content": "**🗂️ 已加载的项目列表**\n在此管理系统中注册的所有发版项目。"
        },
        {"tag": "hr"}
    ]
    
    action_base_val = {
        "project": state.get("project", ""),
        "repo": state.get("repo", ""),
        "branch": state.get("branch", ""),
        "env": state.get("env", ""),
    }

    if not all_projects:
        elements.append({
            "tag": "markdown",
            "content": "暂无任何项目配置。"
        })
    else:
        for p in all_projects:
            p_name = p["name"]
            repos_str = ", ".join([r["name"] for r in p.get("repos", [])])
            envs_str = ", ".join(p.get("environments", []))
            
            is_dynamic = p_name in dynamic_project_names
            
            if is_dynamic:
                # 动态项目，提供删除按钮
                elements.append({
                    "tag": "column_set",
                    "flex_mode": "none",
                    "background_style": "default",
                    "columns": [
                        {
                            "tag": "column", "width": "weighted", "weight": 4, "vertical_align": "center",
                            "elements": [
                                {
                                    "tag": "markdown", 
                                    "content": f"**{p_name}** `[动态配置]`\n环境: {envs_str} | 仓库: {repos_str}"
                                }
                            ]
                        },
                        {
                            "tag": "column", "width": "weighted", "weight": 1, "vertical_align": "center",
                            "elements": [
                                {
                                    "tag": "button",
                                    "text": {"tag": "plain_text", "content": "🗑️ 移除"},
                                    "type": "danger",
                                    "value": {**action_base_val, "action": "delete_project", "delete_name": p_name},
                                    "confirm": {
                                        "title": {"tag": "plain_text", "content": "确认删除"},
                                        "text": {"tag": "plain_text", "content": f"您确定要彻底删除动态项目【{p_name}】吗？删除后将无法在此操作其对应的仓库发版。"}
                                    }
                                }
                            ]
                        }
                    ]
                })
            else:
                # 静态配置，不可删除
                elements.append({
                    "tag": "markdown",
                    "content": f"**{p_name}** `[🔒静态配置(不可从卡片删除)]`\n环境: {envs_str} | 仓库: {repos_str}"
                })
            
            elements.append({"tag": "hr"})

    elements.append({
        "tag": "note",
        "elements": [
            {
                "tag": "plain_text",
                "content": "💡 提示：如需新增项目，请在群内直接发送文字：\n「@机器人 帮我加一个名叫XXX的项目，测试环境和生产环境，仓库叫后端服务，GitLab ID是123」"
            }
        ]
    })
    
    elements.append({
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "◀️ 返回控制台"},
                "value": {**action_base_val, "action": "refresh"},
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "📤 导出动态配置 (YAML)"},
                "value": {**action_base_val, "action": "export_yaml"},
            }
        ]
    })

    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "purple", "title": {"tag": "plain_text", "content": "⚙️ 卡片管理员 - 项目管理"}},
        "elements": elements,
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

    env_display_map = project.get("env_display", {})
    env_options_enriched = [
        {"text": {"tag": "plain_text", "content": get_env_display(e, env_display_map)}, "value": e}
        for e in project["environments"]
    ]
    env_placeholder = get_env_display(state["env"], env_display_map)

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
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "⏹️ 停止发版"},
                "type": "danger",
                "value": {**action_base_val, "action": "cancel_pipeline"},
                "confirm": {
                    "title": {"tag": "plain_text", "content": "确认停止发版"},
                    "text": {"tag": "plain_text", "content": "确认要取消当前正在运行的流水线吗？这可能会导致部署处于中间状态。"}
                }
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
                "text": {"tag": "plain_text", "content": "🛡️ Sonar 扫描"},
                "value": {**action_base_val, "action": "sonar_scan"},
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "📚 生成 Wiki"},
                "value": {**action_base_val, "action": "repo_wiki"},
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "📋 历史记录"},
                "value": {**action_base_val, "action": "history"},
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "⚙️ 项目管理"},
                "value": {**action_base_val, "action": "manage_projects"},
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
