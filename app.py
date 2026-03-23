import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

CONFIG_PATH = Path(__file__).parent / "config.yaml"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("feishu_gitlab_card_http")
CARD_STATE: Dict[str, Dict[str, str]] = {}
ACTION_DEDUP: Dict[str, float] = {}
RUN_DEDUP_SECONDS = 4.0
REFRESH_DEDUP_SECONDS = 1.5


def cleanup_action_dedup(now_ts: float) -> None:
    expired_keys = [k for k, v in ACTION_DEDUP.items() if now_ts - v > 30]
    for k in expired_keys:
        ACTION_DEDUP.pop(k, None)


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


async def delayed_update_card(feishu_client: "FeishuClient", open_message_id: str, card: Dict[str, Any], delay_seconds: float = 1.0) -> None:
    try:
        await asyncio.sleep(delay_seconds)
        result = feishu_client.update_card(open_message_id, card)
        logger.info("delayed updated current card open_message_id=%s result=%s", open_message_id, json.dumps(result, ensure_ascii=False)[:1000])
    except Exception:
        logger.exception("delayed update current card failed open_message_id=%s", open_message_id)


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Missing config.yaml. Copy from {CONFIG_PATH.with_name('config.example.yaml')}")
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str, base_url: str = "https://open.feishu.cn"):
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_url = base_url.rstrip("/")

    def tenant_access_token(self) -> str:
        url = f"{self.base_url}/open-apis/auth/v3/tenant_access_token/internal"
        payload = {"app_id": self.app_id, "app_secret": self.app_secret}
        with httpx.Client(timeout=20) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"get tenant_access_token failed: {data}")
        return data["tenant_access_token"]

    def send_card(self, chat_id: str, card: Dict[str, Any]) -> Dict[str, Any]:
        token = self.tenant_access_token()
        url = f"{self.base_url}/open-apis/im/v1/messages?receive_id_type=chat_id"
        headers = {"Authorization": f"Bearer {token}"}
        payload = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        with httpx.Client(timeout=20) as client:
            resp = client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                logger.error("Feishu send_card http_error status=%s body=%s", resp.status_code, resp.text[:3000])
            resp.raise_for_status()
            data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"send card failed: {data}")
        return data

    def update_card(self, message_id: str, card: Dict[str, Any]) -> Dict[str, Any]:
        token = self.tenant_access_token()
        url = f"{self.base_url}/open-apis/im/v1/messages/{message_id}"
        headers = {"Authorization": f"Bearer {token}"}
        payload = {
            "content": json.dumps(card, ensure_ascii=False),
        }
        with httpx.Client(timeout=20) as client:
            resp = client.patch(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                logger.error("Feishu update_card http_error status=%s body=%s", resp.status_code, resp.text[:3000])
            resp.raise_for_status()
            data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"update card failed: {data}")
        return data

    def reply_card(self, open_chat_id: str, card: Dict[str, Any]) -> Dict[str, Any]:
        return self.send_card(open_chat_id, card)


class GitLabClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def trigger_pipeline(self, project_id: int, ref: str, variables: Dict[str, str]) -> Dict[str, Any]:
        url = f"{self.base_url}/api/v4/projects/{project_id}/pipeline"
        headers = {"PRIVATE-TOKEN": self.token}
        payload = {"ref": ref, "variables": [{"key": k, "value": v} for k, v in variables.items()]}
        logger.info("Triggering GitLab pipeline project_id=%s ref=%s variables=%s", project_id, ref, variables)
        with httpx.Client(timeout=30) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        logger.info("GitLab pipeline triggered successfully pipeline_id=%s web_url=%s", data.get("id"), data.get("web_url"))
        return data

    def list_pipelines(self, project_id: int, ref: Optional[str] = None, per_page: int = 10) -> Any:
        url = f"{self.base_url}/api/v4/projects/{project_id}/pipelines"
        headers = {"PRIVATE-TOKEN": self.token}
        params: Dict[str, Any] = {"per_page": per_page}
        if ref:
            params["ref"] = ref
        with httpx.Client(timeout=30) as client:
            resp = client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
        return data

    def latest_pipeline(self, project_id: int, ref: Optional[str] = None) -> Optional[Dict[str, Any]]:
        pipelines = self.list_pipelines(project_id=project_id, ref=ref, per_page=1)
        if isinstance(pipelines, list) and pipelines:
            return pipelines[0]
        return None


def normalize_selection(
    cfg: Dict[str, Any],
    selected_project: Optional[str] = None,
    selected_branch: Optional[str] = None,
    selected_env: Optional[str] = None,
) -> Dict[str, str]:
    projects = cfg.get("projects", [])
    if not projects:
        projects = [{"name": "demo", "repo": "demo", "branches": ["main"], "environments": ["test"]}]

    project = next((p for p in projects if p["name"] == selected_project), None) or projects[0]
    branch_value = selected_branch or project["branches"][0]
    env_value = selected_env or project["environments"][0]

    if branch_value not in project["branches"]:
        branch_value = project["branches"][0]
    if env_value not in project["environments"]:
        env_value = project["environments"][0]

    return {
        "project": project["name"],
        "branch": branch_value,
        "env": env_value,
    }


def build_card(
    cfg: Dict[str, Any],
    status: str,
    current_ref: str,
    selected_project: Optional[str] = None,
    selected_branch: Optional[str] = None,
    selected_env: Optional[str] = None,
    latest_pipeline_text: Optional[str] = None,
    latest_result_text: Optional[str] = None,
    show_details: bool = False,
) -> Dict[str, Any]:
    projects = cfg.get("projects", [])
    if not projects:
        projects = [{"name": "demo", "repo": "demo", "branches": ["main"], "environments": ["test"]}]

    state = normalize_selection(cfg, selected_project, selected_branch, selected_env)
    project = next((p for p in projects if p["name"] == state["project"]), None) or projects[0]
    latest_result_text = latest_result_text or status
    latest_pipeline_text = latest_pipeline_text or "暂无"

    display_project_name = project.get("display_name") or project.get("title") or project["name"]
    repo_name = project.get("repo", "-")

    meta = status_meta(status, latest_result_text, latest_pipeline_text)
    rendered_status = meta["label"]
    rendered_result_text = latest_result_text

    project_options = [
        {
            "text": {
                "tag": "plain_text",
                "content": p.get("display_name") or p.get("title") or p["name"],
            },
            "value": p["name"],
        }
        for p in projects
    ]
    repo_options = [
        {
            "text": {"tag": "plain_text", "content": p.get("repo", p["name"])},
            "value": p["name"],
        }
        for p in projects
    ]
    branch_options = [
        {"text": {"tag": "plain_text", "content": b}, "value": b}
        for b in project["branches"]
    ]
    env_options = [
        {"text": {"tag": "plain_text", "content": e}, "value": e}
        for e in project["environments"]
    ]

    status_emoji = meta["emoji"]

    status_color = "green"
    if meta["label"] == "处理中":
        status_color = "orange"
    elif meta["label"] == "异常":
        status_color = "red"

    compact_lines = [
        f"**当前状态**：<font color='{status_color}'>{meta['label']}</font>",
        f"**流水线摘要**：{latest_pipeline_text}",
    ]
    if show_details:
        compact_lines.extend(
            [
                f"**操作结果**：{rendered_result_text}",
                f"**项目 / 仓库**：{display_project_name} / {repo_name}",
                f"**分支 / 环境**：{state['branch']} / {state['env']}",
            ]
        )
        if current_ref:
            compact_lines.append(f"**当前 Ref**：{current_ref}")
    else:
        compact_lines.append("**操作结果**：请选择参数后执行，或点击刷新查看最新流水线状态")

    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**GitLab 发布面板**\n<font color='grey'>简洁模式 / 当前项目可直接触发或刷新状态</font>",
            },
        },
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"<font color='{status_color}'>●</font> **{meta['label']}**    <font color='grey'>|</font>    **{latest_pipeline_text}**",
            },
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**项目 (Project)**"},
        },
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "select_static",
                    "placeholder": {"tag": "plain_text", "content": display_project_name},
                    "name": "project",
                    "options": project_options,
                    "value": {**state, "current_field": "project"},
                }
            ],
        },
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**仓库 (Repository)**"},
        },
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "select_static",
                    "placeholder": {"tag": "plain_text", "content": repo_name},
                    "name": "repo",
                    "options": repo_options,
                    "value": {**state, "current_field": "repo"},
                }
            ],
        },
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**分支 (Branch / Tag)**"},
        },
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "select_static",
                    "placeholder": {"tag": "plain_text", "content": state["branch"]},
                    "name": "branch",
                    "options": branch_options,
                    "value": {**state, "current_field": "branch"},
                }
            ],
        },
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**环境 (Environment)**"},
        },
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "select_static",
                    "placeholder": {"tag": "plain_text", "content": state["env"]},
                    "name": "env",
                    "options": env_options,
                    "value": {**state, "current_field": "env"},
                }
            ],
        },
        {"tag": "hr"},
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "执行触发"},
                    "type": "primary",
                    "value": {**state, "action": "run"},
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "刷新状态"},
                    "value": {**state, "action": "refresh"},
                },
            ],
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "\n".join(compact_lines),
            },
        },
    ]

    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": "GitLab 发布面板"}},
        "elements": elements,
    }


def extract_text_message(payload: Dict[str, Any]) -> Optional[Dict[str, str]]:
    event = payload.get("event", {}) or {}
    message = event.get("message") or payload.get("message") or {}
    sender = event.get("sender") or payload.get("sender") or {}
    if message.get("message_type") != "text":
        return None
    try:
        content = json.loads(message.get("content") or "{}")
    except Exception:
        content = {}
    text = (content.get("text") or "").strip()
    sender_id = (sender.get("sender_id", {}) or {}) if isinstance(sender, dict) else {}
    return {
        "chat_id": message.get("chat_id", ""),
        "text": text,
        "open_id": sender_id.get("open_id", ""),
    }


app = FastAPI(title="feishu-gitlab-card-http")


@app.get("/")
async def health():
    cfg = load_config()
    return {
        "ok": True,
        "service": "feishu-gitlab-card-http",
        "projects": [p.get("name") for p in cfg.get("projects", [])],
    }


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.post("/feishu/event")
async def feishu_event(request: Request):
    payload = await request.json()
    logger.info("/feishu/event payload=%s", json.dumps(payload, ensure_ascii=False)[:3000])

    challenge = payload.get("challenge")
    if payload.get("type") == "url_verification" or challenge:
        return JSONResponse({"challenge": challenge or ""})

    cfg = load_config()
    configured_token = ((cfg.get("feishu") or {}).get("verification_token") or "").strip()
    incoming_token = (
        payload.get("token")
        or ((payload.get("header") or {}).get("token"))
        or ((payload.get("event") or {}).get("token"))
        or ""
    )
    if configured_token and incoming_token and incoming_token != configured_token:
        logger.warning("verification token mismatch incoming=%s", incoming_token)
        return JSONResponse({"code": 403, "msg": "invalid token"}, status_code=403)

    header = payload.get("header", {}) or {}
    event_type = header.get("event_type") or payload.get("type")
    if event_type not in {"p2.im.message.receive_v1", "im.message.receive_v1"}:
        return JSONResponse({"code": 0, "msg": "ignored"})

    msg = extract_text_message(payload)
    if not msg:
        logger.info("message payload ignored after extract")
        return JSONResponse({"code": 0, "msg": "ignored"})

    text = msg["text"].strip().lower()
    chat_id = msg["chat_id"]
    logger.info("Received text callback chat_id=%s text=%s", chat_id, text)
    if text != "gitlab":
        return JSONResponse({"code": 0, "msg": "ignored"})

    card = build_card(
        cfg,
        status="就绪",
        current_ref="main",
        latest_result_text="就绪",
        latest_pipeline_text="暂无",
        show_details=False,
    )
    feishu = FeishuClient(cfg["feishu"]["app_id"], cfg["feishu"]["app_secret"])
    try:
        result = feishu.send_card(chat_id, card)
        logger.info("Card sent by HTTP callback chat_id=%s result=%s", chat_id, json.dumps(result, ensure_ascii=False)[:1000])
        return JSONResponse({"code": 0, "msg": "ok"})
    except Exception as e:
        logger.exception("send card failed chat_id=%s err=%s", chat_id, e)
        return JSONResponse({"code": 0, "msg": "send_card_failed"})


@app.post("/feishu/card")
async def feishu_card(request: Request):
    payload = await request.json()
    logger.info("/feishu/card payload=%s", json.dumps(payload, ensure_ascii=False)[:3000])

    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge", "")})

    event = payload.get("event", {})
    action = event.get("action", {}) or {}
    raw_value = action.get("value") or {}
    form_value = action.get("form_value") or {}

    def pick_value(key: str) -> Optional[str]:
        if isinstance(raw_value, dict):
            direct = raw_value.get(key)
            if isinstance(direct, str) and direct:
                return direct
        form_direct = form_value.get(key)
        if isinstance(form_direct, str) and form_direct:
            return form_direct
        if isinstance(form_direct, dict):
            for candidate_key in ("value", "key", key):
                candidate = form_direct.get(candidate_key)
                if isinstance(candidate, str) and candidate:
                    return candidate
        option = action.get("option")
        if action.get("name") == key and isinstance(option, str) and option:
            return option
        return None

    cfg = load_config()
    context = event.get("context", {}) or {}
    open_message_id = context.get("open_message_id") or ""
    stored_state = CARD_STATE.get(open_message_id, {}) if open_message_id else {}

    action_name = None
    if isinstance(raw_value, dict):
        action_name = raw_value.get("action")
    if not action_name and isinstance(form_value, dict):
        action_name = form_value.get("action")
    if not action_name and action.get("tag") == "button":
        text_obj = action.get("text") or {}
        button_text = text_obj.get("content", "") if isinstance(text_obj, dict) else ""
        if "执行" in button_text:
            action_name = "run"
        elif "刷新" in button_text:
            action_name = "refresh"
    if not action_name:
        action_name = action.get("name")
    current_field = raw_value.get("current_field") if isinstance(raw_value, dict) else None
    option = action.get("option") if isinstance(action.get("option"), str) else None

    if action.get("tag") == "select_static":
        selected_project = stored_state.get("project")
        selected_branch = stored_state.get("branch")
        selected_env = stored_state.get("env")
    elif action_name in {"run", "refresh"} and stored_state:
        selected_project = stored_state.get("project")
        selected_branch = stored_state.get("branch")
        selected_env = stored_state.get("env")
    else:
        selected_project = pick_value("project") or stored_state.get("project")
        selected_branch = pick_value("branch") or stored_state.get("branch")
        selected_env = pick_value("env") or stored_state.get("env")

    logger.info(
        "card action parsed open_message_id=%s action_name=%s current_field=%s option=%s action_tag=%s stored_state=%s selected_project=%s selected_branch=%s selected_env=%s",
        open_message_id,
        action_name,
        current_field,
        option,
        action.get("tag"),
        stored_state,
        selected_project,
        selected_branch,
        selected_env,
    )

    merged_project = selected_project
    merged_branch = selected_branch
    merged_env = selected_env
    if current_field == "project" and option:
        merged_project = option
    elif current_field == "repo" and option:
        merged_project = option
    elif current_field == "branch" and option:
        merged_branch = option
    elif current_field == "env" and option:
        merged_env = option

    logger.info(
        "card action merge before normalize open_message_id=%s merged_project=%s merged_branch=%s merged_env=%s",
        open_message_id,
        merged_project,
        merged_branch,
        merged_env,
    )

    state = normalize_selection(cfg, merged_project, merged_branch, merged_env)
    if open_message_id:
        CARD_STATE[open_message_id] = dict(state)
    logger.info("card action merged state=%s", state)

    dedup_key = ""
    if action_name in {"run", "refresh"}:
        dedup_key = f"{open_message_id}:{action_name}:{state['project']}:{state['branch']}:{state['env']}"
        now_ts = time.time()
        cleanup_action_dedup(now_ts)
        last_ts = ACTION_DEDUP.get(dedup_key, 0)
        dedup_window = RUN_DEDUP_SECONDS if action_name == "run" else REFRESH_DEDUP_SECONDS
        if now_ts - last_ts < dedup_window:
            logger.info("dedup hit key=%s", dedup_key)
            return JSONResponse({})
        ACTION_DEDUP[dedup_key] = now_ts

    status = "就绪"
    current_ref = state["branch"]
    latest_pipeline_text = "暂无"
    latest_result_text = "就绪"

    project = next((p for p in cfg.get("projects", []) if p["name"] == state["project"]), None) or cfg["projects"][0]
    gitlab = GitLabClient(cfg["gitlab"]["base_url"], cfg["gitlab"]["access_token"])

    if action_name == "run":
        try:
            status = "执行中"
            pipeline = gitlab.trigger_pipeline(
                project_id=project["id"],
                ref=state["branch"],
                variables={"ENV": state["env"]},
            )
            pipeline_id = pipeline.get("id")
            pipeline_status = pipeline.get("status") or "created"
            latest_result_text = f"已触发 Pipeline #{pipeline_id}"
            current_ref = pipeline.get("ref") or state["branch"]
            latest_pipeline_text = f"#{pipeline_id} / {pipeline_status}"
        except Exception as e:
            logger.exception("trigger pipeline failed")
            status = "异常"
            latest_result_text = f"触发失败: {e}"
    elif action_name == "refresh":
        try:
            status = "刷新中"
            latest = gitlab.latest_pipeline(project_id=project["id"], ref=state["branch"])
            if latest:
                pipeline_id = latest.get("id")
                pipeline_status = latest.get("status")
                current_ref = latest.get("ref") or state["branch"]
                latest_pipeline_text = f"#{pipeline_id} / {pipeline_status}"
                latest_result_text = f"最近 Pipeline #{pipeline_id} 状态: {pipeline_status}"
                if str(pipeline_status).lower() == "success":
                    status = "就绪"
                elif str(pipeline_status).lower() in {"failed", "canceled"}:
                    status = "异常"
                else:
                    status = "处理中"
            else:
                status = "就绪"
                latest_result_text = f"分支 {state['branch']} 暂无 Pipeline"
                latest_pipeline_text = "暂无"
        except Exception as e:
            logger.exception("refresh pipeline failed")
            status = "异常"
            latest_result_text = f"刷新失败: {e}"

    card = build_card(
        cfg,
        status=status,
        current_ref=current_ref,
        selected_project=state["project"],
        selected_branch=state["branch"],
        selected_env=state["env"],
        latest_pipeline_text=latest_pipeline_text,
        latest_result_text=latest_result_text,
        show_details=(action_name in {"run", "refresh"}),
    )

    feishu_client = FeishuClient(
        app_id=cfg["feishu"]["app_id"],
        app_secret=cfg["feishu"]["app_secret"],
    )
    updated_in_place = False
    if open_message_id and action_name in {"run", "refresh"}:
        try:
            asyncio.create_task(delayed_update_card(feishu_client, open_message_id, card, 1.0))
            updated_in_place = True
            logger.info("scheduled delayed update open_message_id=%s", open_message_id)
        except Exception:
            logger.exception("schedule delayed update failed open_message_id=%s", open_message_id)

    open_chat_id = context.get("open_chat_id") or context.get("chat_id") or ""
    if (not updated_in_place) and open_chat_id and action_name in {"run", "refresh"}:
        try:
            feishu_client.reply_card(open_chat_id, card)
            logger.info("fallback sent new card to open_chat_id=%s", open_chat_id)
        except Exception:
            logger.exception("fallback send updated card failed open_chat_id=%s", open_chat_id)

    if updated_in_place:
        response_body = {}
    else:
        response_body = {}
    logger.info("/feishu/card response=%s", json.dumps(response_body, ensure_ascii=False)[:3000])
    return JSONResponse(response_body)
