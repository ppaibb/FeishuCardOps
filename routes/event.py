import asyncio
import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.card_builder import build_card, normalize_selection
from core.config import load_config
from core.feishu_client import FeishuClient
from core.gitlab_client import GitLabClient

logger = logging.getLogger("feishu_gitlab_card_http")

router = APIRouter()


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


async def _background_send_new_card(cfg: Dict[str, Any], matched_project: str, chat_id: str) -> None:
    try:
        gitlab = GitLabClient(cfg["gitlab"]["base_url"], cfg["gitlab"]["access_token"])
        state = await normalize_selection(cfg, gitlab_client=gitlab, selected_project=matched_project)

        card = build_card(
            cfg,
            state=state,
            status="就绪",
            latest_result_text="就绪",
            latest_pipeline_text="暂无",
            show_details=False,
        )
        feishu = FeishuClient(cfg["feishu"]["app_id"], cfg["feishu"]["app_secret"])
        result = await feishu.send_card(chat_id, card)
        logger.info("Card sent by async background chat_id=%s result=%s", chat_id, json.dumps(result, ensure_ascii=False)[:1000])
    except Exception as e:
        logger.exception("async send card failed chat_id=%s err=%s", chat_id, e)


@router.post("/feishu/event")
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

    matched_project = None
    for p in cfg.get("projects", []):
        proj_name = p["name"]
        trigger_word = f"{proj_name}发版".lower()
        if trigger_word in text or text == "gitlab":
            matched_project = proj_name
            break

    if not matched_project:
        return JSONResponse({"code": 0, "msg": "ignored"})

    asyncio.create_task(_background_send_new_card(cfg, matched_project, chat_id))
    return JSONResponse({"code": 0, "msg": "ok"})
