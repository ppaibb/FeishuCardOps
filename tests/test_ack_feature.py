import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch, MagicMock

from core.card_builder import build_sub_card
from fastapi.testclient import TestClient
from app import app


class TestAckFeature(unittest.TestCase):
    def setUp(self):
        self.state = {
            "project": "OrderService",
            "repo": "order-api",
            "repo_id": 101,
            "branch": "main",
            "env": "test",
        }
        self.operator_open_id = "ou_owner_123"
        self.pipeline_id = 9999

    def test_running_pipeline_has_no_ack_button(self):
        card = build_sub_card(
            state=self.state,
            operator_open_id=self.operator_open_id,
            pipeline_id=self.pipeline_id,
            p_status="running",
            active_job_name="build-job",
        )
        # 运行中卡片不应该有确认按钮
        actions = []
        for elem in card.get("elements", []):
            if elem.get("tag") == "action":
                actions.extend(elem.get("actions", []))
        ack_buttons = [a for a in actions if a.get("value", {}).get("action") == "acknowledge_sub_card"]
        self.assertEqual(len(ack_buttons), 0)

    def test_success_pipeline_has_ack_button(self):
        card = build_sub_card(
            state=self.state,
            operator_open_id=self.operator_open_id,
            pipeline_id=self.pipeline_id,
            p_status="success",
            active_job_name="done",
        )
        actions = []
        for elem in card.get("elements", []):
            if elem.get("tag") == "action":
                actions.extend(elem.get("actions", []))
        ack_buttons = [a for a in actions if a.get("value", {}).get("action") == "acknowledge_sub_card"]
        self.assertEqual(len(ack_buttons), 1)
        self.assertIn("收到", ack_buttons[0]["text"]["content"])
        self.assertEqual(ack_buttons[0]["value"]["operator_open_id"], self.operator_open_id)
        self.assertEqual(ack_buttons[0]["value"]["pipeline_id"], self.pipeline_id)

    def test_failed_pipeline_has_troubleshoot_button(self):
        card = build_sub_card(
            state=self.state,
            operator_open_id=self.operator_open_id,
            pipeline_id=self.pipeline_id,
            p_status="failed",
            active_job_name="deploy",
            failed_job_info="编译错误",
        )
        actions = []
        for elem in card.get("elements", []):
            if elem.get("tag") == "action":
                actions.extend(elem.get("actions", []))
        ack_buttons = [a for a in actions if a.get("value", {}).get("action") == "acknowledge_sub_card"]
        self.assertEqual(len(ack_buttons), 1)
        self.assertIn("排查", ack_buttons[0]["text"]["content"])
        self.assertEqual(ack_buttons[0]["type"], "danger")

    def test_acknowledged_card_replaces_button_with_text(self):
        ack_info = {
            "ack_by": self.operator_open_id,
            "ack_by_name": "张三",
            "ack_at": "16:30",
        }
        card = build_sub_card(
            state=self.state,
            operator_open_id=self.operator_open_id,
            pipeline_id=self.pipeline_id,
            p_status="success",
            active_job_name="done",
            ack_info=ack_info,
        )
        # 不应再有确认按钮
        actions = []
        for elem in card.get("elements", []):
            if elem.get("tag") == "action":
                actions.extend(elem.get("actions", []))
        ack_buttons = [a for a in actions if a.get("value", {}).get("action") == "acknowledge_sub_card"]
        self.assertEqual(len(ack_buttons), 0)

        # 文本中应该展示确认知晓记录
        content = card["elements"][0]["content"]
        self.assertIn("知晓确认", content)
        self.assertIn("张三", content)
        self.assertIn("16:30", content)


from routes.card import feishu_card


class TestCardAckRoute(unittest.IsolatedAsyncioTestCase):
    @patch("services.notification.check_and_send_release_note", new_callable=AsyncMock)
    @patch("routes.card.get_card_state", new_callable=AsyncMock)
    @patch("routes.card.get_all_projects", new_callable=AsyncMock)
    @patch("routes.card.get_redis")
    @patch("routes.card.load_config")
    async def test_non_owner_click_is_blocked(self, mock_load_config, mock_get_redis, mock_get_all_projects, mock_get_card_state, mock_release_note):
        mock_load_config.return_value = {"projects": [], "feishu": {"app_id": "app", "app_secret": "sec"}}
        mock_get_all_projects.return_value = []
        mock_get_card_state.return_value = None
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None
        mock_get_redis.return_value = mock_redis
        payload = {
            "event": {
                "operator": {"open_id": "ou_stranger_456"},
                "action": {
                    "tag": "button",
                    "value": {
                        "action": "acknowledge_sub_card",
                        "operator_open_id": "ou_owner_123",
                        "pipeline_id": 9999,
                    }
                },
                "context": {"open_message_id": "om_msg_001"}
            }
        }
        mock_request = MagicMock()
        mock_request.json = AsyncMock(return_value=payload)
        resp = await feishu_card(mock_request)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertIn("toast", data)
        self.assertEqual(data["toast"]["type"], "warning")
        self.assertIn("仅限被艾特的发版责任人确认", data["toast"]["content"])

    @patch("services.notification.check_and_send_release_note", new_callable=AsyncMock)
    @patch("routes.card.get_card_state", new_callable=AsyncMock)
    @patch("routes.card.get_all_projects", new_callable=AsyncMock)
    @patch("routes.card.FeishuClient")
    @patch("routes.card.get_redis")
    @patch("routes.card.load_config")
    async def test_owner_click_succeeds(self, mock_load_config, mock_get_redis, mock_feishu_cls, mock_get_all_projects, mock_get_card_state, mock_release_note):
        mock_load_config.return_value = {"projects": [], "feishu": {"app_id": "app", "app_secret": "sec"}}
        mock_get_all_projects.return_value = []
        mock_get_card_state.return_value = None
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None
        mock_get_redis.return_value = mock_redis

        mock_feishu = AsyncMock()
        mock_feishu.get_user_name.return_value = "发版人张三"
        mock_feishu.update_card.return_value = {"code": 0}
        mock_feishu_cls.return_value = mock_feishu

        payload = {
            "event": {
                "operator": {"open_id": "ou_owner_123"},
                "action": {
                    "tag": "button",
                    "value": {
                        "action": "acknowledge_sub_card",
                        "operator_open_id": "ou_owner_123",
                        "pipeline_id": 9999,
                    }
                },
                "context": {"open_message_id": "om_msg_001"}
            }
        }
        mock_request = MagicMock()
        mock_request.json = AsyncMock(return_value=payload)
        resp = await feishu_card(mock_request)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertIn("toast", data)
        self.assertEqual(data["toast"]["type"], "success")
        self.assertIn("已确认收到", data["toast"]["content"])
        mock_redis.set.assert_called()


if __name__ == "__main__":
    unittest.main()
