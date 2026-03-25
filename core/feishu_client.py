import json
import logging
import time
from typing import Any, Dict

import httpx

logger = logging.getLogger("feishu_gitlab_card_http")


class FeishuClient:
    """飞书开放平台 API 客户端（全异步 + tenant_access_token 自动缓存）"""

    # 类级别 Token 缓存，相同 app_id 的不同实例共享
    _token_store: Dict[str, str] = {}
    _token_expires: Dict[str, float] = {}

    def __init__(self, app_id: str, app_secret: str, base_url: str = "https://open.feishu.cn"):
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_url = base_url.rstrip("/")

    async def tenant_access_token(self) -> str:
        now = time.time()
        cached = self._token_store.get(self.app_id)
        expires_at = self._token_expires.get(self.app_id, 0)
        if cached and now < expires_at:
            return cached

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
        self._token_store[self.app_id] = token
        self._token_expires[self.app_id] = now + expire - 300  # 提前 5 分钟刷新
        logger.info("tenant_access_token refreshed, expires_in=%ss", expire)
        return token

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
