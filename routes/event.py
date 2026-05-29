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


async def process_text_message(text: str, chat_id: str, sender_open_id: str = ""):
    """后台异步处理文本并发送卡片，防止阻塞"""
    try:
        cfg = load_config()
        feishu = FeishuClient(cfg["feishu"]["app_id"], cfg["feishu"]["app_secret"])
        
        if sender_open_id:
            from services.notification import check_and_send_release_note
            asyncio.create_task(check_and_send_release_note(cfg, sender_open_id))
        
        if text.startswith("广播公告 "):
            from core.permissions import is_admin
            if not is_admin(cfg, sender_open_id):
                await feishu.send_text(chat_id, "❌ 权限不足：只有管理员可发布广播")
                return
                
            announcement = text.replace("广播公告 ", "").strip()
            if not announcement:
                return
                
            from core.redis_client import get_redis
            redis = get_redis()
            if not redis:
                await feishu.send_text(chat_id, "❌ Redis 不可用，无法获取群聊列表")
                return
                
            active_chats = await redis.smembers("cardops:active_chats")
            success_count = 0
            
            card = {
                "config": {"wide_screen_mode": True},
                "header": {"template": "blue", "title": {"tag": "plain_text", "content": "📢 系统更新公告"}},
                "elements": [{"tag": "markdown", "content": announcement}]
            }
            
            for cid in active_chats:
                try:
                    if isinstance(cid, bytes):
                        cid = cid.decode("utf-8")
                    await feishu.send_card(cid, card)
                    success_count += 1
                except Exception as e:
                    logger.error(f"发送广播到 {cid} 失败: {e}")
                    
            await feishu.send_text(chat_id, f"✅ 广播发送完毕！共推送到 {success_count} 个会话。")
            return

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
            
            # 使用大模型智能提取意图
            from services.intent_parser import extract_deploy_intent
            intent = await extract_deploy_intent(text, cfg["projects"])
            
            selected_project = intent.get("project")
            selected_repo = intent.get("repo")
            selected_branch = intent.get("branch")
            selected_env = intent.get("env")
            
            # 降级处理：如果没有匹配到项目，则退回到原有的模糊匹配
            if not selected_project:
                for p in cfg.get("projects", []):
                    if p["name"] in text:
                        selected_project = p["name"]
                        break
                        
            latest_result_text = "就绪"
            show_details = False
            if any(intent.values()):
                latest_result_text = "🤖 **AI 已为你预填了参数！** 请核对无误后点击执行触发"
                show_details = True
                    
            state = await normalize_selection(
                cfg, gitlab, 
                selected_project=selected_project,
                selected_repo=selected_repo,
                selected_branch=selected_branch,
                selected_env=selected_env
            )

            card = build_card(
                cfg,
                status="就绪",
                state=state,
                latest_result_text=latest_result_text,
                latest_pipeline_text="暂无",
                show_details=show_details,
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
    logger.info("/feishu/event received event_type=%s event_id=%s", 
                payload.get("event", {}).get("event_type", "unknown"),
                payload.get("header", {}).get("event_id", ""))

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
    sender_open_id = event.get("sender", {}).get("sender_id", {}).get("open_id", "")

    # 2. 将耗时的 API 操作（取分支）和网路请求丢进后台任务，立刻响应飞书 200
    if message.get("message_type") == "text":
        content_str = message.get("content", "")
        chat_id = message.get("chat_id", "")
        
        try:
            from core.redis_client import get_redis
            redis = get_redis()
            if redis and chat_id:
                asyncio.create_task(redis.sadd("cardops:active_chats", chat_id))
        except Exception:
            pass
            
        try:
            content_dict = json.loads(content_str)
            text = content_dict.get("text", "")
            logger.info("Received text callback chat_id=%s text=%s", chat_id, text)

            asyncio.create_task(process_text_message(text, chat_id, sender_open_id))
        except Exception as e:
            logger.error("parse text message failed: %s", e)

    return JSONResponse({"code": 0, "msg": "success"})
