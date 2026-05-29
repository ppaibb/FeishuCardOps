import json
import logging
from typing import Dict, Any, Optional

import anthropic
from core.config import load_config

logger = logging.getLogger("feishu_gitlab_card_http")

SYSTEM_PROMPT = """你是一个智能的意图解析助手。
用户的发言意图是触发 GitLab 的流水线部署发版。
请从用户发言中提取发版所需的 4 个核心要素：项目 (project)、仓库 (repo)、分支 (branch)、环境 (env)。

以下是当前系统支持的所有项目及选项列表配置：
{projects_context}

提取要求：
1. 请参考上面的支持列表，尽可能将用户的语言映射为列表中的准确名称。如果用户说的不是精确名称但意思是同一个，请纠正为列表中的精确名称。
2. 如果用户没有提及某个要素，或者无法确定，则该字段值为 null。
3. 输出必须是一个纯 JSON 对象，不要包含任何 markdown 代码块标记，不要多余的解释。

输出格式：
{
  "project": "项目名或 null",
  "repo": "仓库名或 null",
  "branch": "分支名或 null",
  "env": "环境名或 null"
}
"""

async def extract_deploy_intent(text: str, projects_cfg: list) -> Dict[str, Optional[str]]:
    """从文本中提取发版参数"""
    cfg = load_config()
    ai_cfg = cfg.get("ai", {})
    api_key = ai_cfg.get("api_key", "")
    base_url = ai_cfg.get("base_url", "")
    model = ai_cfg.get("model", "")
    
    if not api_key or not model:
        return {}

    # 构建上下文
    projects_context = []
    for p in projects_cfg:
        repos = [r["name"] for r in p.get("repos", [])]
        envs = p.get("environments", [])
        projects_context.append(f"项目: {p['name']} | 仓库: {repos} | 环境: {envs}")
    
    sys_prompt = SYSTEM_PROMPT.replace("{projects_context}", "\n".join(projects_context))

    try:
        client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)
        message = await client.messages.create(
            model=model,
            max_tokens=500,
            system=sys_prompt,
            messages=[{"role": "user", "content": text}],
        )
        
        reply_json = ""
        for block in message.content:
            if block.type == "text":
                reply_json = block.text
                break
        if not reply_json and len(message.content) > 0:
            reply_json = getattr(message.content[0], "text", "")

        if not reply_json:
            return {}

        reply_json = reply_json.strip()
        if reply_json.startswith("```json"): reply_json = reply_json[7:].strip()
        elif reply_json.startswith("```"): reply_json = reply_json[3:].strip()
        if reply_json.endswith("```"): reply_json = reply_json[:-3].strip()

        data = json.loads(reply_json)
        return {
            "project": data.get("project"),
            "repo": data.get("repo"),
            "branch": data.get("branch"),
            "env": data.get("env")
        }
    except Exception as e:
        logger.error(f"提取发版意图失败: {e}")
        return {}
