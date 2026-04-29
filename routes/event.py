import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.card_builder import build_card, normalize_selection
from services.project_manager import get_all_projects, parse_and_add_project, delete_project
from core.config import load_config
from core.feishu_client import FeishuClient
from core.gitlab_client import GitLabClient
from core.state import check_action_dedup, is_repo_locked

logger = logging.getLogger("feishu_gitlab_card_http")

router = APIRouter()


async def process_text_message(text: str, chat_id: str):
    """后台异步处理文本并发送卡片，防止阻塞"""
    try:
        cfg = load_config()
        feishu = FeishuClient(cfg["feishu"]["app_id"], cfg["feishu"]["app_secret"])
        
        if any(keyword in text for keyword in ["添加项目", "注册项目", "新加项目", "新增项目", "加一个项目"]):
            reply_txt = await parse_and_add_project(text)
            await feishu.send_text(chat_id, reply_txt)
            return
            
        if "删除项目" in text:
            reply_txt = await delete_project(text)
            await feishu.send_text(chat_id, reply_txt)
            return

        if "发版" in text or "发布" in text:
            cfg["projects"] = await get_all_projects()
            gitlab = GitLabClient(cfg["gitlab"]["base_url"], cfg["gitlab"]["access_token"])
            
            # 从用户消息中尝试匹配项目名称
            selected_project = None
            for p in cfg.get("projects", []):
                if p["name"] in text:
                    selected_project = p["name"]
                    break
                    
            state = await normalize_selection(cfg, gitlab, selected_project=selected_project)

            card = build_card(
                cfg,
                status="就绪",
                state=state,
                latest_result_text="就绪",
                latest_pipeline_text="暂无",
                show_details=False,
                is_locked=await is_repo_locked(state.get("repo_id", -1)),
            )
            feishu = FeishuClient(cfg["feishu"]["app_id"], cfg["feishu"]["app_secret"])
            result = await feishu.send_card(chat_id, card)
            logger.info("Card sent by async background chat_id=%s result=%s", chat_id, json.dumps(result, ensure_ascii=False))
    except Exception as e:
        logger.error("process text message failed: %s", e)


@router.post("/feishu/event")
async def feishu_event(request: Request):
    payload = await request.json()
    logger.info("/feishu/event payload=%s", json.dumps(payload, ensure_ascii=False))

    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge", "")})

    # 1. 防止飞书由于超时发起的重试（去重），基于 event_id 拦截
    event_id = payload.get("header", {}).get("event_id")
    if not event_id:
        event_id = payload.get("event", {}).get("message", {}).get("message_id", "")
    
    if event_id:
        if not await check_action_dedup(f"event:{event_id}", 300):
            logger.info("dedup hit event_id=%s, skipping retry", event_id)
            return JSONResponse({"code": 0, "msg": "success"})

    event = payload.get("event", {})
    message = event.get("message", {})

    # 2. 将耗时的 API 操作（取分支）和网路请求丢进后台任务，立刻响应飞书 200
    if message.get("message_type") == "text":
        content_str = message.get("content", "")
        chat_id = message.get("chat_id", "")
        try:
            content_dict = json.loads(content_str)
            text = content_dict.get("text", "")
            logger.info("Received text callback chat_id=%s text=%s", chat_id, text)

            asyncio.create_task(process_text_message(text, chat_id))
        except Exception as e:
            logger.error("parse text message failed: %s", e)

    return JSONResponse({"code": 0, "msg": "success"})
