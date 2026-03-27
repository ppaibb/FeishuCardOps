import json
import logging
import time
from typing import Any, Dict

import httpx

logger = logging.getLogger("feishu_gitlab_card_http")


class FeishuClient:
    """飞书开放平台 API 客户端（全异步 + tenant_access_token 自动缓存）"""

    def __init__(self, app_id: str, app_secret: str, base_url: str = "https://open.feishu.cn"):
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_url = base_url.rstrip("/")

    async def tenant_access_token(self) -> str:
        from core.redis_client import get_redis
        r = get_redis()
        token_key = f"feishu_tenant_token:{self.app_id}"
        cached = await r.get(token_key)
        if cached: return str(cached)

        url = f"{self.base_url}/open-apis/auth/v3/tenant_access_token/internal"
        payload = {"app_id": self.app_id, "app_secret": self.app_secret}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"get tenant_access_token failed: {data}")

        token = data["tenant_access_token"]
        expire = data.get("expire", 7200)
        safe_expire = max(expire - 300, 60)
        await r.setex(token_key, safe_expire, token)
        logger.info("tenant_access_token refreshed, expires_in=%ss", expire)
        return token

    async def get_user_name(self, open_id: str) -> str:
        if not open_id: return ""
        from core.redis_client import get_redis
        r = get_redis()
        cache_key = f"feishu_user_name:{open_id}"
        cached_name = await r.get(cache_key)
        if cached_name: return str(cached_name)

        token = await self.tenant_access_token()
        url = f"{self.base_url}/open-apis/contact/v3/users/{open_id}?user_id_type=open_id"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(url, headers={"Authorization": f"Bearer {token}"})
                data = res.json()
                if data.get("code") == 0:
                    name = data.get("data", {}).get("user", {}).get("name", "")
                    if name:
                        await r.setex(cache_key, 7 * 24 * 3600, name)
                        return name
        except Exception as e:
            logger.error("failed to get user name for %s: %s", open_id, e)
        return open_id

    async def send_card(self, chat_id: str, card: Dict[str, Any]) -> Dict[str, Any]:
        token = await self.tenant_access_token()
        url = f"{self.base_url}/open-apis/im/v1/messages?receive_id_type=chat_id"
        headers = {"Authorization": f"Bearer {token}"}
        payload = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                logger.error("Feishu send_card http_error status=%s body=%s", resp.status_code, resp.text[:3000])
            resp.raise_for_status()
            data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"send card failed: {data}")
        return data

    async def send_text(self, chat_id: str, text: str) -> Dict[str, Any]:
        token = await self.tenant_access_token()
        url = f"{self.base_url}/open-apis/im/v1/messages?receive_id_type=chat_id"
        headers = {"Authorization": f"Bearer {token}"}
        payload = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                logger.error("Feishu send_text http_error status=%s body=%s", resp.status_code, resp.text[:3000])
            resp.raise_for_status()
            data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"send text failed: {data}")
        return data

    async def update_card(self, message_id: str, card: Dict[str, Any]) -> Dict[str, Any]:
        token = await self.tenant_access_token()
        url = f"{self.base_url}/open-apis/im/v1/messages/{message_id}"
        headers = {"Authorization": f"Bearer {token}"}
        payload = {
            "content": json.dumps(card, ensure_ascii=False),
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.patch(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                logger.error("Feishu update_card http_error status=%s body=%s", resp.status_code, resp.text[:3000])
            resp.raise_for_status()
            data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"update card failed: {data}")
        return data

    async def reply_card(self, open_chat_id: str, card: Dict[str, Any]) -> Dict[str, Any]:
        return await self.send_card(open_chat_id, card)
