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


WIKI_SYSTEM_PROMPT = """你是一个智能的意图解析助手。
用户的发言意图是为某个 GitLab 代码仓库生成 RepoWiki 代码知识库文档。
请从用户发言中提取所需的 4 个要素：项目 (project)、仓库 (repo)、分支 (branch)、是否强制重建 (force)。

以下是当前系统支持的所有项目与仓库配置：
{projects_context}

提取要求：
1. 参考上述列表，将用户的口语或仓库路径（如 kitefly/ui）映射为配置中精确的 project 名称与 repo 名称。
2. 如果用户未提及某个要素或无法确定其所属仓库（例如项目下有多个仓库但用户没说具体哪一个），对应字段填 null，不要猜选第一个。
3. 若用户提到“强制”“重跑”“重新生成”“全量”“force”，force 为 true，否则为 false。
4. 输出必须是一个纯 JSON 对象，不要包含 markdown 代码块或额外解释。

输出格式：
{
  "project": "项目名或 null",
  "repo": "仓库名或 null",
  "branch": "分支名或 null",
  "force": false
}
"""


def _strip_json_reply(raw: str) -> str:
    txt = str(raw or "").strip()
    if txt.startswith("```json"):
        txt = txt[7:].strip()
    elif txt.startswith("```"):
        txt = txt[3:].strip()
    if txt.endswith("```"):
        txt = txt[:-3].strip()
    return txt


def resolve_wiki_target(text: str, ai_intent: Dict[str, Any], projects_cfg: list) -> Dict[str, Any]:
    """结合 AI 意图结果与确定性规则解析目标项目/仓库/分支，识别是否存在多仓库歧义。"""
    lowered = (text or "").lower()
    force = bool(ai_intent.get("force")) or any(
        kw in lowered for kw in ("强制", "重跑", "重新生成", "全量重建", "force")
    )

    project_name = ai_intent.get("project")
    repo_name = ai_intent.get("repo")
    branch_name = ai_intent.get("branch")

    # 验证 AI 返回名是否在当前配置中；无效则置空交由规则匹配
    matched_proj = next((p for p in projects_cfg if p.get("name") == project_name), None)
    if not matched_proj:
        project_name = None

    if not project_name:
        for p in projects_cfg:
            pname = str(p.get("name") or "")
            if pname and pname.lower() in lowered:
                matched_proj = p
                project_name = pname
                break

    def _find_repo_in_list(repos: list, candidate_name: Optional[str]) -> Optional[Dict[str, Any]]:
        if candidate_name:
            exact = next((r for r in repos if r.get("name") == candidate_name), None)
            if exact:
                return exact
        # 优先匹配完整仓库显示名或 GitLab 路径
        candidates = []
        for r in repos:
            r_name = str(r.get("name") or "")
            r_path = str(r.get("repo") or "")
            r_slug = r_path.rsplit("/", 1)[-1] if r_path else ""
            if r_name and r_name.lower() in lowered:
                candidates.append((len(r_name), r))
            elif r_path and r_path.lower() in lowered:
                candidates.append((len(r_path), r))
            elif r_slug and len(r_slug) >= 2 and r_slug.lower() in lowered:
                candidates.append((len(r_slug), r))
        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            return candidates[0][1]
        return None

    matched_repo = None
    if matched_proj:
        matched_repo = _find_repo_in_list(matched_proj.get("repos") or [], repo_name)
    else:
        # 全项目反查唯一仓库匹配
        hits = []
        for p in projects_cfg:
            hit = _find_repo_in_list(p.get("repos") or [], repo_name)
            if hit:
                hits.append((p, hit))
        if len(hits) == 1:
            matched_proj, matched_repo = hits[0]
            project_name = matched_proj.get("name")
        elif len(hits) > 1 and repo_name:
            # 恰好多个项目同名仓库，保持 AI 指定的 repo_name 但标记不明确
            repo_name = hits[0][1].get("name")

    if matched_repo:
        repo_name = matched_repo.get("name")
    else:
        repo_name = None

    # 单项目单仓库自动消歧
    unambiguous = False
    if matched_proj and matched_repo:
        unambiguous = True
    elif matched_proj and not matched_repo:
        proj_repos = matched_proj.get("repos") or []
        if len(proj_repos) == 1:
            repo_name = proj_repos[0].get("name")
            unambiguous = True
    elif not matched_proj and len(projects_cfg) == 1 and len(projects_cfg[0].get("repos") or []) == 1:
        project_name = projects_cfg[0].get("name")
        repo_name = projects_cfg[0]["repos"][0].get("name")
        unambiguous = True

    return {
        "project": project_name,
        "repo": repo_name,
        "branch": branch_name,
        "force": force,
        "unambiguous": unambiguous,
    }


async def extract_wiki_intent(text: str, projects_cfg: list) -> Dict[str, Any]:
    """从对话文本中提取 RepoWiki 生成参数"""
    cfg = load_config()
    ai_cfg = cfg.get("ai", {})
    api_key = ai_cfg.get("api_key", "")
    base_url = ai_cfg.get("base_url", "")
    model = ai_cfg.get("model", "")

    ai_data: Dict[str, Any] = {}
    if api_key and model and projects_cfg:
        projects_context = []
        for p in projects_cfg:
            repos_desc = [f"{r.get('name')}({r.get('repo')})" for r in p.get("repos", [])]
            projects_context.append(f"项目: {p['name']} | 仓库: {repos_desc}")

        sys_prompt = WIKI_SYSTEM_PROMPT.replace("{projects_context}", "\n".join(projects_context))
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
                if getattr(block, "type", None) == "text":
                    reply_json = block.text
                    break
            if not reply_json and len(message.content) > 0:
                reply_json = getattr(message.content[0], "text", "")
            if reply_json:
                ai_data = json.loads(_strip_json_reply(reply_json))
        except Exception as e:
            logger.error(f"提取 RepoWiki 意图失败: {e}")

    return resolve_wiki_target(text, ai_data if isinstance(ai_data, dict) else {}, projects_cfg)
