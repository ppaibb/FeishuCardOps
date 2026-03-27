"""
AI Code Review 服务

通过 GitLab MR API 获取当前分支最新 Merge Request 的代码变更，
发送给 AI 进行代码审查，结果以飞书副卡片形式展示。
"""
import logging
from typing import Any, Dict

import anthropic

from core.config import load_config
from core.feishu_client import FeishuClient
from core.gitlab_client import GitLabClient

logger = logging.getLogger("feishu_gitlab_card_http")

SYSTEM_PROMPT = """你是一位资深的高级软件工程师，正在对一个 Merge Request 的代码变更进行 Code Review。

请按照以下格式输出你的审查报告：

## 📊 总体评估
给出一个风险等级：🟢 低风险 / 🟡 中风险 / 🔴 高风险
一句话总结本次变更的核心内容和整体质量。

## 🔍 逐项审查
对每个有价值的发现，按以下格式列出（最多列出 8 条最重要的）：
- **[类型]** `文件名` — 问题描述和改进建议
  类型包括：🐛 Bug风险 / ⚠️ 安全隐患 / 💡 优化建议 / ✅ 优秀实践

## 📝 部署建议
简短列出 1-3 条部署前需要注意的事项（如果有的话）。

注意事项：
- 保持简洁，每条发现不超过 2 行
- 只关注真正重要的问题，不要吹毛求疵
- 如果代码质量良好，大方表扬
- 使用中文输出
- 总输出不要超过 800 字"""


def _truncate_diff(changes: list, max_chars: int = 15000) -> str:
    """将 MR changes 列表拼接为 diff 文本，超长时截断"""
    lines = []
    total = 0
    for c in changes:
        path = c.get("new_path") or c.get("old_path", "unknown")
        diff_text = c.get("diff", "")
        chunk = f"--- {path} ---\n{diff_text}\n"
        if total + len(chunk) > max_chars:
            lines.append(f"\n... (剩余 {len(changes) - len(lines)} 个文件的 diff 因长度限制被截断)")
            break
        lines.append(chunk)
        total += len(chunk)
    return "\n".join(lines)


async def run_code_review(
    feishu_client: FeishuClient,
    gitlab: GitLabClient,
    state: Dict[str, Any],
    open_chat_id: str,
    operator_open_id: str,
) -> None:
    """后台执行 AI Code Review 并将结果发送到群聊"""
    cfg = load_config()
    ai_cfg = cfg.get("ai", {})
    api_key = ai_cfg.get("api_key", "")
    base_url = ai_cfg.get("base_url", "")
    model = ai_cfg.get("model", "")

    if not api_key or not model:
        error_card = _build_review_card(state, "❌ 配置错误", "未配置 AI 相关参数，请在 config.yaml 中添加 `ai` 配置段。", "red")
        await feishu_client.send_card(open_chat_id, error_card)
        return

    branch = state["branch"]

    # 1. 发送"正在分析"的占位卡片
    loading_card = _build_review_card(
        state, "🔄 AI 正在分析中...",
        f"<at id=\"{operator_open_id}\"></at> 正在查找 `{branch}` 分支的最新 Merge Request 并交给 AI 审查，请稍候...",
        "blue"
    )
    resp = await feishu_client.send_card(open_chat_id, loading_card)
    review_msg_id = resp.get("data", {}).get("message_id")

    try:
        # 2. 查找该分支的最新 MR
        mr = await gitlab.get_latest_mr(state["repo_id"], branch)

        if mr:
            # ── MR 模式 ──────────────────────────────────────────
            mr_iid = mr["iid"]
            mr_title = mr.get("title", "")
            mr_state = mr.get("state", "")
            mr_author = mr.get("author", {}).get("name", "")
            mr_url = mr.get("web_url", "")
            target_branch = mr.get("target_branch", "")

            mr_detail = await gitlab.get_mr_changes(state["repo_id"], mr_iid)
            changes = mr_detail.get("changes", [])

            if not changes:
                result_card = _build_review_card(state, "ℹ️ MR 无代码变更", f"MR !{mr_iid}「{mr_title}」没有代码变更。", "blue")
                if review_msg_id:
                    await feishu_client.update_card(review_msg_id, result_card)
                return

            diff_text = _truncate_diff(changes)
            mr_link = f"[!{mr_iid}]({mr_url})" if mr_url else f"!{mr_iid}"
            user_message = f"""以下是一个 Merge Request 的代码变更，请进行 Code Review：

项目：{state['project']} / {state['repo']}
MR：!{mr_iid} — {mr_title}
方向：{branch} → {target_branch}
状态：{mr_state}
作者：{mr_author}
涉及 {len(changes)} 个文件变更

### 代码差异
{diff_text}

请对以上代码变更进行 Code Review。"""
            summary_line = f"**MR**：{mr_link}　**{mr_title}**\n**方向**：`{branch}` → `{target_branch}`　|　**文件数**：{len(changes)}　|　**状态**：{mr_state}"

        else:
            # ── 降级：分支对比模式 ────────────────────────────────
            if review_msg_id:
                fallback_card = _build_review_card(
                    state, "🔄 AI 正在分析中...",
                    f"<at id=\"{operator_open_id}\"></at> 未找到 MR，已降级为分支对比模式：`{branch}` vs 默认分支，请稍候...",
                    "blue"
                )
                await feishu_client.update_card(review_msg_id, fallback_card)

            default_branch = await gitlab.get_default_branch(state["repo_id"])
            if default_branch == branch:
                result_card = _build_review_card(state, "⚠️ 无法对比", f"当前分支 `{branch}` 就是默认分支，没有可对比的差异。", "yellow")
                if review_msg_id:
                    await feishu_client.update_card(review_msg_id, result_card)
                return

            compare = await gitlab.compare_branches(state["repo_id"], default_branch, branch)
            commits = compare.get("commits", [])
            diffs = compare.get("diffs", [])

            if not diffs:
                result_card = _build_review_card(state, "ℹ️ 无代码差异", f"`{default_branch}` → `{branch}` 之间没有代码变更。", "blue")
                if review_msg_id:
                    await feishu_client.update_card(review_msg_id, result_card)
                return

            diff_text = _truncate_diff(diffs)
            commits_text = "\n".join(
                f"- `{c.get('short_id', c.get('id', '')[:8])}` {c.get('title', '')} ({c.get('author_name', '')})"
                for c in commits[:15]
            )
            if len(commits) > 15:
                commits_text += f"\n- ... 另有 {len(commits) - 15} 个提交"

            user_message = f"""以下是即将从 `{branch}` 分支部署到 `{state['env']}` 环境的代码变更：

项目：{state['project']} / {state['repo']}
对比：{default_branch} → {branch}
涉及 {len(commits)} 个提交、{len(diffs)} 个文件变更

### 提交记录
{commits_text}

### 代码差异
{diff_text}

请对以上代码变更进行 Code Review。"""
            summary_line = f"**模式**：分支对比（无 MR）\n**对比**：`{default_branch}` → `{branch}`　|　**提交数**：{len(commits)}　|　**文件数**：{len(diffs)}"

        # ── 调用 AI（MR 和分支对比共用）─────────────────────────
        client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)
        message = await client.messages.create(
            model=model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        review_text = ""
        for block in message.content:
            if block.type == "text":
                review_text = block.text
                break
        if not review_text:
            review_text = getattr(message.content[0], "text", "") or getattr(message.content[0], "thinking", "AI 未返回有效内容")

        # ── 构建结果卡片 ──────────────────────────────────────────
        header_text = f"🤖 AI Code Review — {state['project']} / {state['repo']}"
        full_content = f"<at id=\"{operator_open_id}\"></at>\n{summary_line}\n\n{review_text}"
        result_card = _build_review_card(state, header_text, full_content, "purple")

        if review_msg_id:
            await feishu_client.update_card(review_msg_id, result_card)
        else:
            await feishu_client.send_card(open_chat_id, result_card)

    except Exception as e:
        logger.exception("AI code review failed")
        error_content = f"AI Code Review 执行失败：{str(e)[:200]}"
        error_card = _build_review_card(state, "❌ Review 失败", error_content, "red")
        if review_msg_id:
            await feishu_client.update_card(review_msg_id, error_card)


def _build_review_card(state: Dict[str, Any], title: str, content: str, color: str = "blue") -> Dict[str, Any]:
    """构建 Code Review 结果卡片"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": color, "title": {"tag": "plain_text", "content": title}},
        "elements": [{"tag": "markdown", "content": content}],
    }
