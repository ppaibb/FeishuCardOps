import unittest
from unittest.mock import AsyncMock

from core.card_builder import (
    build_approval_card,
    build_card,
    build_sub_card,
    format_variable_display_label,
    get_env_display,
    normalize_selection,
)
from core.permissions import check_permission, check_approval_required


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

        actions = [a for elem in card["elements"] if elem.get("tag") == "action" for a in elem.get("actions", [])]
        action_keys = [a.get("value", {}).get("action") for a in actions]
        self.assertEqual(action_keys, ["approve", "reject"])
        self.assertNotIn("review", action_keys)


class TestEnvironmentResolutionAndCardBuilder(unittest.IsolatedAsyncioTestCase):
    def test_get_env_display_inheritance_and_semantic_fallbacks(self):
        # 1. 行业标准语义兜底（废除写死本地机房/阿里云）
        self.assertEqual(get_env_display("test"), "测试环境")
        self.assertEqual(get_env_display("testing"), "测试环境")
        self.assertEqual(get_env_display("prod"), "生产环境")
        self.assertEqual(get_env_display("production"), "生产环境")
        self.assertEqual(get_env_display("dev"), "开发环境")
        self.assertEqual(get_env_display("uat"), "UAT环境")
        self.assertEqual(get_env_display("sit"), "SIT环境")
        self.assertEqual(get_env_display("stage"), "预发环境")
        self.assertEqual(get_env_display("gray"), "灰度环境")
        self.assertEqual(get_env_display("pre"), "预发环境")
        self.assertEqual(get_env_display("alpha"), "alpha环境")
        self.assertEqual(get_env_display("测试环境"), "测试环境")

        # 2. 全局映射生效
        global_map = {"test": "阿里云测试", "prod": "阿里云"}
        self.assertEqual(get_env_display("test", global_display_map=global_map), "阿里云测试")
        self.assertEqual(get_env_display("prod", global_display_map=global_map), "阿里云")
        self.assertEqual(get_env_display("dev", global_display_map=global_map), "开发环境")

        # 3. 项目级个性化覆盖优先于全局映射
        proj_map = {"test": "本地专有机房"}
        self.assertEqual(get_env_display("test", display_map=proj_map, global_display_map=global_map), "本地专有机房")
        self.assertEqual(get_env_display("prod", display_map=proj_map, global_display_map=global_map), "阿里云")

    async def test_normalize_selection_inherits_global_environments_and_resolves_aliases(self):
        cfg = {
            "environments": {
                "default": ["dev", "test", "uat", "prod"],
                "display": {"test": "阿里云测试", "prod": "阿里云生产", "uat": "验收环境"},
            },
            "projects": [
                {
                    "name": "AutoInheritProj",
                    # 未声明 environments，自动继承全局 default
                    "repos": [
                        {
                            "name": "核心服务",
                            "id": 88,
                            "repo": "group/core",
                            "access": {"uat": "http://uat.example.com", "prod": "https://prod.example.com"},
                            "access_note": {"uat": "验收专用", "prod": "生产环境回归"},
                        }
                    ],
                },
                {
                    "name": "CustomEnvProj",
                    "environments": ["sit", "gray", "prod"],
                    "env_display": {"gray": "灰度灰阶集群"},
                    "repos": [{"name": "主仓", "id": 89, "repo": "group/custom"}],
                },
            ],
        }

        # 1. 继承全局默认环境列表与别名
        state1 = await normalize_selection(cfg, None, selected_project="AutoInheritProj", cached_branches=["main"])
        self.assertEqual(state1["env"], "dev")
        self.assertEqual(state1["env_display"]["test"], "阿里云测试")
        self.assertEqual(state1["env_display"]["uat"], "验收环境")

        # 2. 传入中文别名或自定义环境反向解析
        state_alias_test = await normalize_selection(
            cfg, None, selected_project="AutoInheritProj", selected_env="阿里云测试", cached_branches=["main"]
        )
        self.assertEqual(state_alias_test["env"], "test")

        state_alias_uat = await normalize_selection(
            cfg, None, selected_project="AutoInheritProj", selected_env="验收环境", cached_branches=["main"]
        )
        self.assertEqual(state_alias_uat["env"], "uat")
        self.assertEqual(state_alias_uat["access_urls"], ["http://uat.example.com"])
        self.assertEqual(state_alias_uat["access_note"], "验收专用")

        # 3. 项目自定义环境与个性化映射
        state2 = await normalize_selection(
            cfg, None, selected_project="CustomEnvProj", selected_env="gray", cached_branches=["main"]
        )
        self.assertEqual(state2["env"], "gray")
        self.assertEqual(state2["env_display"]["gray"], "灰度灰阶集群")
        self.assertEqual(state2["env_display"]["test"], "阿里云测试")  # 全局仍被合并保留

    def test_build_card_renders_multi_environments_dropdown(self):
        cfg = {
            "environments": {
                "default": ["dev", "test", "uat", "prod"],
                "display": {"uat": "UAT集成验收", "test": "测试机房"},
            },
            "projects": [
                {
                    "name": "MultiEnvProj",
                    "environments": ["dev", "test", "uat", "prod"],
                    "repos": [{"name": "服务A", "id": 1, "repo": "group/a"}],
                }
            ],
        }
        state = {
            "project": "MultiEnvProj",
            "repo": "服务A",
            "repo_id": 1,
            "branch": "main",
            "env": "uat",
            "branches": ["main"],
            "variables_def": [],
            "variables": {},
            "env_display": {"uat": "UAT集成验收", "test": "测试机房"},
        }
        card = build_card(cfg, "就绪", state)
        select_elements = [
            elem["columns"][1]["elements"][0]
            for elem in card.get("elements", [])
            if elem.get("tag") == "column_set"
            and len(elem.get("columns", [])) >= 2
            and elem["columns"][1].get("elements")
            and elem["columns"][1]["elements"][0].get("tag") == "select_static"
            and elem["columns"][1]["elements"][0].get("name") == "env"
        ]
        self.assertEqual(len(select_elements), 1)
        env_select = select_elements[0]
        options = env_select.get("options", [])
        self.assertEqual([opt["value"] for opt in options], ["dev", "test", "uat", "prod"])
        self.assertEqual([opt["text"]["content"] for opt in options], ["开发环境", "测试机房", "UAT集成验收", "生产环境"])
        self.assertEqual(env_select["placeholder"]["content"], "UAT集成验收")

    def test_permissions_rule_matching_with_alias_and_key(self):
        cfg = {
            "permissions": {
                "rules": [
                    {"project": "*", "env": "prod", "policy": "deny", "allow_users": ["ou_admin"]},
                    {"project": "*", "env": "生产环境", "policy": "deny", "allow_users": ["ou_admin"]},
                ],
                "approval_required": [
                    {"project": "*", "env": "prod", "approvers": ["ou_approver"]},
                ],
            }
        }
        # 通过 key 匹配
        allowed, _ = check_permission(cfg, "ProjA", "prod", "RepoA", "ou_guest", env_display="生产环境")
        self.assertFalse(allowed)
        allowed_admin, _ = check_permission(cfg, "ProjA", "prod", "RepoA", "ou_admin", env_display="生产环境")
        self.assertTrue(allowed_admin)

        # 通过 display 匹配
        appr = check_approval_required(cfg, "ProjA", "prod", "RepoA", env_display="生产环境")
        self.assertIsNotNone(appr)
        self.assertEqual(appr["authorized_approvers"], ["ou_approver"])


if __name__ == "__main__":
    unittest.main()
