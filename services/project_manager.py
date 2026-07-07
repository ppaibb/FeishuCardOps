import json
import logging
from typing import List, Dict, Any, Optional

import anthropic

from core.config import load_config
from core.redis_client import get_redis
from core.gitlab_client import GitLabClient, build_gitlab_client, load_gitlab_instances
from core.metrics import PROJECT_REGISTERED

logger = logging.getLogger("feishu_gitlab_card_http")

DYNAMIC_PROJECTS_KEY = "cardops:dynamic_projects"

SYSTEM_PROMPT_ADD_PROJECT = """
你是一个配置解析助手。用户的发言旨在在系统中注册新的发版项目配置。
你需要从中提取字段，并输出一个严格的 JSON 对象。除 JSON 以外不要输出任何其他文本或解释。

JSON Schema要求如下：
{
  "name": "项目名称字符串",
  "environments": ["环境1", "环境2"],
  "repos": [
    {
      "name": "仓库显示名",
      "id": 整数仓库GitLabID,
      "repo": "比如 group/repo 字符串格式",
      "gitlab": "可选，GitLab 实例名。仅当用户明确指定该仓库属于哪个 GitLab 实例时填写，否则省略此字段"
    }
  ]
}

要求：
1. 如果用户没有提供足够信息，尽可能推断。
2. GitLab ID 必须提取为整数（如果用户没写，则设为null或默认随便写，但通常用户会写）。
3. gitlab 字段为可选，仅当用户明确说明仓库所属的 GitLab 实例时才填写，否则不要包含该字段。
4. 必须输出合法且只有一条的 JSON。不要加 markdown 代码块标签！直接大括号开头。
"""

async def get_all_projects() -> List[Dict[str, Any]]:
    """
    获取系统中所有的项目，包含 config.yaml 基础项目和被动态添加到 Redis 的项目。
    如果存在同名 project，Redis 里的动态项目将覆盖 yaml 里的配置。
    """
    cfg = load_config()
    base_projects = cfg.get("projects", [])
    
    redis = get_redis()
    dynamic_projects = []
    
    if redis:
        try:
            # Hash 中存的 val 都是序列化的 json string
            raw_data = await redis.hgetall(DYNAMIC_PROJECTS_KEY)
            for k, v in raw_data.items():
                dynamic_projects.append(json.loads(v))
        except Exception as e:
            logger.error(f"从 Redis 加载动态项目失败: {e}")
            
    merged = []
    dynamic_names = {p.get("name") for p in dynamic_projects if p.get("name")}
    
    for bp in base_projects:
        if bp.get("name") not in dynamic_names:
            merged.append(bp)
            
    merged.extend(dynamic_projects)
    return merged

async def get_dynamic_project_names() -> set:
    """
    获取所有动态配置的项目名称
    """
    redis = get_redis()
    if not redis:
        return set()
    try:
        keys = await redis.hkeys(DYNAMIC_PROJECTS_KEY)
        return set(keys)
    except Exception as e:
        logger.error(f"从 Redis 获取动态项目名称失败: {e}")
        return set()

async def parse_and_add_project(text: str) -> str:
    """
    通过 AI 解析一段文本，并将其加进动态项目池。返回给飞书回复的文字。
    """
    cfg = load_config()
    ai_cfg = cfg.get("ai", {})
    api_key = ai_cfg.get("api_key", "")
    base_url = ai_cfg.get("base_url", "")
    model = ai_cfg.get("model", "")
    
    if not api_key or not model:
        return "❌ 尚未配置正确的 AI Key，大模型解析失败。"

    client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)
    try:
        message = await client.messages.create(
            model=model,
            max_tokens=2048,
            system=SYSTEM_PROMPT_ADD_PROJECT,
            messages=[{"role": "user", "content": text}],
        )
        
        reply_json = ""
        for block in message.content:
            if block.type == "text":
                reply_json = block.text
                break
        if not reply_json:
            reply_json = getattr(message.content[0], "text", "")

        reply_json = reply_json.strip()
        if reply_json.startswith("```json"): reply_json = reply_json[7:].strip()
        elif reply_json.startswith("```"): reply_json = reply_json[3:].strip()
        if reply_json.endswith("```"): reply_json = reply_json[:-3].strip()

        project_data = json.loads(reply_json)
        
        # 字段健全性检查
        p_name = project_data.get("name")
        repos = project_data.get("repos", [])
        if not p_name or not repos:
            return "❌ AI 无法完整提取所需配置，请确保告知了项目名称、环境以及仓库详情。"
            
        # 拿第一个仓库做检验 （Gitlab 接入校验）
        test_repo = repos[0]
        test_id = test_repo.get("id")

        if not test_id:
            return f"❌ 必须提供仓库在 GitLab 的 Project ID。"

        # 校验 repo 绑定的 GitLab 实例名是否存在（若指定了）
        instances = load_gitlab_instances(cfg)
        for r in repos:
            inst_name = r.get("gitlab")
            if inst_name and inst_name not in instances:
                available = ", ".join(instances.keys()) or "（无）"
                return f"❌ 仓库【{r.get('name')}】指定的 GitLab 实例【{inst_name}】不存在。可用实例：{available}"

        # 按第一个仓库绑定的实例构建客户端进行连通性校验
        gl_client = build_gitlab_client(cfg, repo=test_repo)
        if not gl_client:
            return "❌ 未找到任何可用的 GitLab 实例配置，请检查 config.yaml。"

        try:
            gl_project = await gl_client.get_project(test_id)
            if not gl_project:
                return f"❌ 校验失败：机器人无权访问 ID 为 【{test_id}】 的仓库，或者该仓库不存在。"
        except Exception as e:
            logger.error(f"GitLab API Error: {str(e)}")
            return f"❌ GitLab 发生网络异常，请确认机器人的配置和连通性。"
            
        # 保存到 Redis
        redis = get_redis()
        if not redis:
            return "❌ Redis 连接不可用，无法保存。"
            
        await redis.hset(DYNAMIC_PROJECTS_KEY, p_name, json.dumps(project_data, ensure_ascii=False))
        PROJECT_REGISTERED.inc()
        
        return f"✅ **添加成功！**\n项目名称: {p_name}\n包含环境: {','.join(project_data.get('environments', []))}\n仓库名: {test_repo.get('name')} (ID: {test_id})\n已经生效，快试试对我说「{p_name} 发版」 吧！"

    except Exception as e:
        logger.error(f"解析和增加项目发生异常: {e}")
        return f"❌ 系统在解析和添加项目时发生内部异常：{str(e)}"

async def delete_project(text: str) -> str:
    """
    简单删除动态项目匹配 (通过精确匹配名称)
    """
    prefix = "删除项目"
    idx = text.find(prefix)
    if idx == -1: return "❌ 操作口令不对。"
    p_name = text[idx + len(prefix):].strip()
    
    if not p_name:
        # 去除掉可能的前后标点
        return "❌ 找不到项目名。请使用 `删除项目 [名称]`"

    # 清除一些多余符号    
    for ch in ["「", "」", "【", "】", " ", '"', "'"]:
        p_name = p_name.replace(ch, "")
        
    redis = get_redis()
    if not redis:
        return "❌ 无法连接到 Redis。"
        
    res = await redis.hdel(DYNAMIC_PROJECTS_KEY, p_name)
    if res > 0:
        return f"✅ 动态项目【{p_name}】已被彻底移除。"
    else:
        return f"❌ 在内存中未找到动态项目【{p_name}】（请注意：无法通过聊天删除 config.yaml 中硬编码的基础项目）。"
