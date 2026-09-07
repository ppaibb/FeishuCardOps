import unittest
from core.card_builder import (
    format_variable_display_label,
    build_sub_card,
    build_approval_card,
)


class TestCardBuilderVariableLabel(unittest.TestCase):
    def test_format_variable_display_label(self):
        # 常见形式测试
        self.assertEqual(format_variable_display_label("🧩 选服务", "TARGET_MODULE"), "服务")
        self.assertEqual(format_variable_display_label("选服务", "TARGET_MODULE"), "服务")
        self.assertEqual(format_variable_display_label("🧩 选微服务 (Module)", "TARGET_MODULE"), "服务")
        self.assertEqual(format_variable_display_label("🧩 选服务"), "服务")
        self.assertEqual(format_variable_display_label("选服务"), "服务")
        self.assertEqual(format_variable_display_label("服务"), "服务")
        self.assertEqual(format_variable_display_label("", "TARGET_MODULE"), "服务")

        # 其他变量清洗测试
        self.assertEqual(format_variable_display_label("🎨 构建配置", "TARGET_PROFILE"), "构建配置")
        self.assertEqual(format_variable_display_label("选配置", "TARGET_PROFILE"), "配置")

    def test_build_sub_card_displays_service(self):
        state = {
            "project": "一网统飞",
            "repo": "一网统飞-后端服务",
            "repo_id": 101,
            "branch": "develop",
            "env": "test",
            "variables_def": [
                {
                    "key": "TARGET_MODULE",
                    "label": "🧩 选服务",
                    "options": ["snowy-biz-app"],
                }
            ],
            "variables": {
                "TARGET_MODULE": "snowy-biz-app"
            },
            "access_urls": ["http://unifly.kitegogo.net"],
            "access_note": "注意保护视力",
        }

        card = build_sub_card(
            state=state,
            operator_open_id="ou_test_user",
            pipeline_id=2742,
            p_status="success",
            active_job_name="done",
            commit_info="[df4e81f7](http://192.168.3.222:30080/UniSky/backend/-/pipelines/2742)\n- docs(deployment): 添加浪潮云环境部署说明文档 (Rhy)",
        )

        content = card["elements"][0]["content"]
        # 确保包含 **服务**：`snowy-biz-app`
        self.assertIn("**服务**：`snowy-biz-app`", content)
        # 确保不再包含 选服务 或 🧩 选服务
        self.assertNotIn("选服务", content)
        self.assertNotIn("🧩", content)

    def test_build_approval_card_displays_service(self):
        state = {
            "project": "一网统飞",
            "repo": "一网统飞-后端服务",
            "repo_id": 101,
            "branch": "main",
            "env": "prod",
            "variables_def": [
                {
                    "key": "TARGET_MODULE",
                    "label": "🧩 选服务",
                    "options": ["snowy-biz-app"],
                }
            ],
            "variables": {
                "TARGET_MODULE": "snowy-biz-app"
            },
        }

        card = build_approval_card(
            state=state,
            requester_open_id="ou_requester",
            approval_id="appr_001",
            approvers=["ou_approver"],
        )

        content = card["elements"][0]["content"]
        self.assertIn("**服务**：snowy-biz-app", content)
        self.assertNotIn("选服务", content)


if __name__ == "__main__":
    unittest.main()
