"""
AI Code Review 服务

通过 GitLab MR API 获取当前分支最新 Merge Request 的代码变更，
发送给 AI 进行代码审查，结果以飞书副卡片形式展示。
"""
import logging
from typing import Any, Dict
import uuid

import anthropic

from core.config import load_config
from core.feishu_client import FeishuClient
from core.gitlab_client import GitLabClient
from core.redis_client import get_redis

logger = logging.getLogger("feishu_gitlab_card_http")

SYSTEM_PROMPT = """你是一个专门负责代码审查的自动化机器人。
你的输出将直接嵌入企业内部通知卡片中，因此【严禁输出任何问候语、客套话、或者“好的”等废话】。请直接输出结果。
务必保持极端简洁，不要做代码细节的展开说教，确保只抓核心。

你必须「严格按照以下排版模板」输出，不要修改或增加一、二级标题：

**风险评估**：[🟢 低风险 | 🟡 中风险 | 🔴 高风险]
**核心总结**：[用最多 30 个字一句话总结本次变更是做什么的]

**关键发现**：
[只列出最重要的缺陷或建议，最多 5 条。如果没有发现任何风险或缺陷，切忌敷衍！你必须深度分析代码的优秀之处，列出至少 2 条你在阅读这段代码时发现的“✅ 优秀实践”（比如命名规范、处理了某边界情况、逻辑解耦等），以证明你执行了深度的代码审查。]
1. [类型] `[缩减版文件名]` - [直击要害的问题、建议或优秀实践，不超过 40 字]
2. [类型] `[缩减版文件名]` - [直击要害的问题、建议或优秀实践，不超过 40 字]

**部署提示**：
[简短的 1-2 条部署注意点，比如依赖更新、SQL同步。如果没有，只写一行“✅ 无特殊部署依赖”。]

(注：类型的图标请使用 🐛, ⚠️, 💡, ✅ 中的一种。不要将整体输出包裹在 ```markdown 代码块中，直接输出文本即可！)"""


def _truncate_diff(changes: list, max_chars: int = 60000) -> str:
    """将 MR changes 列表拼接为 diff 文本，过滤配置锁定文件并在超长时截断"""
    lines = []
    total = 0
    ignored_files = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock", "Cargo.lock", "go.sum"}
    
    for c in changes:
        path = c.get("new_path") or c.get("old_path", "unknown")
        filename = path.split("/")[-1]
        
        if filename in ignored_files or path.endswith(".min.js") or path.endswith(".map") or path.endswith(".svg"):
            continue
            
        diff_text = c.get("diff", "")
        chunk = f"--- {path} ---\n{diff_text}\n"
        if total + len(chunk) > max_chars:
            lines.append(f"\n... (由于长度限制，剩余及部分文件的 diff 被截断)")
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
        
        # 处理默认分支没有活跃 MR 的情况：获取最近已合并的 MR
        default_branch = await gitlab.get_default_branch(state["repo_id"])
        is_default = (branch == default_branch)
        if not mr and is_default:
            mr = await gitlab.get_latest_merged_mr(state["repo_id"], branch)
            if mr and review_msg_id:
                fallback_card = _build_review_card(
                    state, "🔄 AI 正在分析中...",
                    f"<at id=\"{operator_open_id}\"></at> 正在提取**最近一次合并至 `{branch}` 分支**的 Merge Request，请稍候...",
                    "blue"
                )
                await feishu_client.update_card(review_msg_id, fallback_card)

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
            mr_sha = mr.get("sha", mr.get("updated_at", str(mr_iid)))
            cache_key = f"ai_review:repo_{state['repo_id']}:mr_{mr_sha}"

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
            # ── 降级：最新提交模式 ────────────────────────────────
            if review_msg_id:
                fallback_card = _build_review_card(
                    state, "🔄 AI 正在分析中...",
                    f"<at id=\"{operator_open_id}\"></at> 未找到 MR，已降级为单分支模式：正在分析 `{branch}` 的最新提交...",
                    "blue"
                )
                await feishu_client.update_card(review_msg_id, fallback_card)

            commits = await gitlab.get_branch_commits(state["repo_id"], branch, per_page=1)

            if not commits:
                result_card = _build_review_card(state, "⚠️ 无法对比", f"分支 `{branch}` 获取不到任何提交记录。", "yellow")
                if review_msg_id:
                    await feishu_client.update_card(review_msg_id, result_card)
                return

            latest_commit = commits[0]
            latest_commit_sha = latest_commit.get("id", "empty")
            
            diffs = await gitlab.get_commit_diff(state["repo_id"], latest_commit_sha)

            if not diffs:
                result_card = _build_review_card(state, "ℹ️ 无代码差异", f"分支 `{branch}` 的最近一次提交 `{latest_commit_sha[:8]}` 没有实质性文件变更。", "blue")
                if review_msg_id:
                    await feishu_client.update_card(review_msg_id, result_card)
                return

            diff_text = _truncate_diff(diffs)
            commits_text = f"- `{latest_commit_sha[:8]}` {latest_commit.get('title', '')} ({latest_commit.get('author_name', '')})"

            cache_key = f"ai_review:repo_{state['repo_id']}:branch_{latest_commit_sha}"
            user_message = f"""以下是即将从 `{branch}` 分支部署到 `{state['env']}` 环境的最新代码变更：

项目：{state['project']} / {state['repo']}
分支：{branch}
最新的提交：
{commits_text}

### 代码差异
{diff_text}

请对以上代码变更进行 Code Review。"""
            summary_line = f"**模式**：当前分支最新提交（无 MR）\n**分支**：`{branch}`　|　**文件数**：{len(diffs)}"

        # ── 检查并发锁和缓存（使用 Redis）─────────────────────────
        redis = get_redis()
        
        lock_val = str(uuid.uuid4())
        lock_key = f"lock:{cache_key}"
        # 如果已经存在该锁，说明另一个进程正在跑同样的审查，避免重复发卡片
        if not await redis.set(lock_key, lock_val, nx=True, ex=300):
            logger.info("code review is already running for %s, skipping", cache_key)
            if review_msg_id:
                await feishu_client.update_card(review_msg_id, _build_review_card(state, "🔄 AI 正在分析中...", "已有相同版本的代码审查任务正在进行中，请耐心等待结果...", "blue"))
            return
            
        try:
            cached_review = await redis.get(cache_key)

            if cached_review:
                review_text = cached_review
                summary_line += " ⚡ *(已缓存)*"
            else:
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
                
                review_text = review_text.strip()
                if review_text.startswith("```markdown"): review_text = review_text[11:].strip()
                elif review_text.startswith("```"): review_text = review_text[3:].strip()
                if review_text.endswith("```"): review_text = review_text[:-3].strip()
                
                # 写入缓存，7天有效 (604800 秒)
                await redis.setex(cache_key, 604800, review_text)

            # ── 构建结果卡片 ──────────────────────────────────────────
            header_text = f"🤖 AI Code Review — {state['project']} / {state['repo']}"
            full_content = f"<at id=\"{operator_open_id}\"></at>\n{summary_line}\n\n{review_text}"
            result_card = _build_review_card(state, header_text, full_content, "purple")

            if review_msg_id:
                await feishu_client.update_card(review_msg_id, result_card)
            else:
                await feishu_client.send_card(open_chat_id, result_card)
        finally:
            # 执行完成后释放并发锁
            if await redis.get(lock_key) == lock_val:
                await redis.delete(lock_key)

    except Exception as e:
        logger.exception("AI code review failed")
        error_content = f"AI Code Review 执行失败：{str(e)[:200]}"
        error_card = _build_review_card(state, "❌ Review 失败", error_content, "red")
        if review_msg_id:
            await feishu_client.update_card(review_msg_id, error_card)


async def diagnose_job_log(cfg: Dict[str, Any], job_name: str, log_text: str) -> str:
    """让 AI 诊断失败的流水线日志"""
    ai_cfg = cfg.get("ai", {})
    api_key = ai_cfg.get("api_key", "")
    base_url = ai_cfg.get("base_url", "")
    model = ai_cfg.get("model", "")
    
    if not api_key or not model:
        return "未配置 AI 凭证，无法自动诊断"
        
    system_prompt = "你是一个专门用于解析 CI/CD 流水线报错日志的 AI 机器人。请从给定的日志截断中找出最核心的报错原因，给出最多两句话的纯文本中文诊断说明，严禁使用 markdown 格式或输出任何多余废话。格式如：错误原因是 xxx，建议检查/执行 xxx。"
    
    user_msg = f"以下是 GitLab Pipeline 阶段 [{job_name}] 失败时的最后几百行错误日志，请精准诊断：\n\n{log_text}"
    
    try:
        client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)
        message = await client.messages.create(
            model=model,
            max_tokens=300,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        
        # 增加解析鲁棒性，输出日志以便调试
        logger.info(f"AI Log Diagnosis Raw Response: {message.content}")
        
        if isinstance(message.content, list):
            texts = []
            for b in message.content:
                if hasattr(b, "text"):
                    texts.append(b.text)
                elif isinstance(b, dict) and "text" in b:
                    texts.append(str(b["text"]))
                else:
                    texts.append(str(b))
            cause = "".join(texts)
        elif isinstance(message.content, str):
            cause = message.content
        else:
            cause = str(message.content)
            
        cause = cause.replace('\n', ' ').strip()
        
        if not cause:
            cause = "未能从日志中提取明确错误（AI 返回空）"
        return cause
    except Exception as e:
        logger.exception("AI log diagnosis failed")
        return f"AI 诊断异常: {str(e)[:50]}"


def _build_review_card(state: Dict[str, Any], title: str, content: str, color: str = "blue") -> Dict[str, Any]:
    """构建 Code Review 结果卡片"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": color, "title": {"tag": "plain_text", "content": title}},
        "elements": [{"tag": "markdown", "content": content}],
    }
