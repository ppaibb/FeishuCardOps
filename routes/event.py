import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.card_builder import build_card, normalize_selection
from core.config import load_config
from core.feishu_client import FeishuClient
from core.gitlab_client import GitLabClient
from core.state import is_repo_locked

logger = logging.getLogger("feishu_gitlab_card_http")

router = APIRouter()


@router.post("/feishu/event")
async def feishu_event(request: Request):
    payload = await request.json()
    logger.info("/feishu/event payload=%s", json.dumps(payload, ensure_ascii=False))

    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge", "")})

    event = payload.get("event", {})
    message = event.get("message", {})

    if message.get("message_type") == "text":
        content_str = message.get("content", "")
        chat_id = message.get("chat_id", "")
        try:
            content_dict = json.loads(content_str)
            text = content_dict.get("text", "")
            logger.info("Received text callback chat_id=%s text=%s", chat_id, text)

            if "发版" in text or "发布" in text:
                cfg = load_config()
                gitlab = GitLabClient(cfg["gitlab"]["base_url"], cfg["gitlab"]["access_token"])
                state = await normalize_selection(cfg, gitlab)

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

    return JSONResponse({"code": 0, "msg": "success"})
