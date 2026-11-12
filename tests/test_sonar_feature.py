import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from core.card_builder import build_card, build_sonar_card, sonar_report_needs_ack
from core.sonar_client import (
    SonarClient,
    parse_sonar_context_from_trace,
    parse_sonar_report_from_text,
)
from routes.card import feishu_card
from services.sonar_scan import _collect_sonar_report, background_run_sonar, build_sonar_lock_scope


SAMPLE_TRACE_LOG = """
\x1b[32;1m17:25:47.865 INFO  QUALITY GATE STATUS: PASSED - View details on http://192.168.3.216:59000/dashboard?id=foundation-kitefly_ui\x1b[0;m
17:25:46.727 INFO  SCM revision ID '98ffdbe9dd76c6d89f5825a5d795d577f7d52c20'
17:25:47.800 INFO  More about the report processing at http://192.168.3.216:59000/api/ce/task?id=AZab1234
============================================================
📊 SonarQube 代码质量扫描报告
============================================================
📦 Sonar 项目 : foundation/kitefly/ui (projectKey: foundation-kitefly_ui)
🔖 扫描版本   : 98ffdbe9
🛡️ 质量门禁   : ✅ 通过 (OK)
🔗 报告详情   : http://192.168.3.216:59000/dashboard?id=foundation-kitefly_ui
------------------------------------------------------------
📈 质量门禁明细:
  ✅ 新增问题总数 (new_violations): 实际 0 [失败阈值 > 0]
------------------------------------------------------------
📊 全量代码指标 (有效代码行: 83539 | 重复率: 8.8% | 覆盖率: 0.0%):
  🔓 安全性问题 (Security)       : 19 (评级 E)
  🐛 可靠性问题 (Reliability)    : 412 (评级 E)
  🔧 可维护性问题 (Maintainability): 1379 (评级 A)
  🔥 安全热点 (Security Hotspots): 0
------------------------------------------------------------
🚨 未解决重点问题 (共 1726 个，展示前 10 条):
  1. [BLOCKER/RELIABILITY] kitefly-ui/src/components/Wechat/wx-news/index.vue:7 — Unknown value "120" for property "height" (css:S9424)
  2. [BLOCKER/SECURITY] kitefly-ui/src/components/Earth/src/Earth.vue:9 — "defaultAccessToken" detected (call helper) (typescript:S6418)
  3. [HIGH/MAINTAINABILITY] kitefly-ui/src/utils/other.ts:196 — Replace "var" with "let" or "const" <script> test. (typescript:S3504)
============================================================
"""

SAMPLE_MARKDOWN_REPORT = """# 📊 SonarQube 代码质量扫描报告

- **Sonar 项目**: `foundation/kitefly/api` (`foundation-kitefly_api`)
- **扫描版本**: `1122aabb`
- **质量门禁**: ❌ 未通过 (ERROR)
- **报告详情**: http://192.168.3.216:59000/dashboard?id=foundation-kitefly_api

## 📈 质量门禁明细

| 状态 | 指标 | 实际值 | 阈值条件 |
| --- | --- | --- | --- |
| ❌ ERROR | 新增漏洞 (`new_vulnerabilities`) | 2 | > 0 |
| ✅ OK | 新增覆盖率 (`new_coverage`) | 85.0 | < 80 |

## 📊 全量代码指标

- 有效代码行：`45000` | 重复率：`3.2%` | 覆盖率：`61.5%`
- 🔓 安全性问题：**3**（评级 D）
- 🐛 可靠性问题：**12**（评级 B）
- 🔧 可维护性问题：**88**（评级 A）
- 🔥 安全热点：**1**

## 🚨 未解决重点问题（共 103 个，展示前 1 条）

1. **[BLOCKER/SECURITY]** `kitefly-auth/src/AuthController.java:42` — Hard-coded secret detected (`java:S6418`)

## 🔥 待审查安全热点（共 1 个，展示前 1 条）

1. **[HIGH]** `kitefly-gateway/src/Filter.java:18` — Make sure permissive CORS policy is safe
"""


def _collect_actions(card):
    actions = []
    for elem in card.get("elements", []):
        if elem.get("tag") == "action":
            actions.extend(elem.get("actions", []))
    return actions


def _all_markdown(card):
    return "\n".join(elem.get("content", "") for elem in card.get("elements", []) if elem.get("tag") == "markdown")


class TestSonarClientAndParsers(unittest.TestCase):
    def test_parse_context_from_trace(self):
        ctx = parse_sonar_context_from_trace(SAMPLE_TRACE_LOG)
        self.assertEqual(ctx["server_url"], "http://192.168.3.216:59000")
        self.assertEqual(ctx["project_key"], "foundation-kitefly_ui")
        self.assertEqual(ctx["project_name"], "foundation/kitefly/ui")
        self.assertEqual(ctx["ce_task_id"], "AZab1234")
        self.assertEqual(ctx["scan_version"], "98ffdbe9")
        self.assertEqual(ctx["qg_status"], "OK")
        self.assertEqual(ctx["dashboard_url"], "http://192.168.3.216:59000/dashboard?id=foundation-kitefly_ui")

    def test_parse_report_from_console_trace(self):
        report = parse_sonar_report_from_text(SAMPLE_TRACE_LOG, top_issues=5)
        self.assertIsNotNone(report)
        self.assertEqual(report["project_key"], "foundation-kitefly_ui")
        self.assertEqual(report["qg_status"], "OK")
        self.assertEqual(len(report["qg_conditions"]), 1)
        self.assertEqual(report["qg_conditions"][0]["metric_key"], "new_violations")
        self.assertEqual(report["measures"]["ncloc"], "83539")
        self.assertEqual(report["measures"]["security_issues"], "19")
        self.assertEqual(report["measures"]["security_rating"], "E")
        self.assertEqual(report["issue_total"], 1726)
        self.assertEqual(len(report["issues"]), 3)
        self.assertEqual(report["issues"][1]["rule"], "typescript:S6418")
        self.assertIn("(call helper)", report["issues"][1]["message"])

    def test_parse_report_from_markdown_artifact(self):
        report = parse_sonar_report_from_text(SAMPLE_MARKDOWN_REPORT, top_issues=5)
        self.assertIsNotNone(report)
        self.assertEqual(report["source"], "markdown")
        self.assertEqual(report["project_key"], "foundation-kitefly_api")
        self.assertEqual(report["qg_status"], "ERROR")
        self.assertEqual(len(report["qg_conditions"]), 2)
        self.assertEqual(report["qg_conditions"][0]["status"], "ERROR")
        self.assertEqual(report["measures"]["coverage"], "61.5%")
        self.assertEqual(report["measures"]["security_rating"], "D")
        self.assertEqual(report["issue_total"], 103)
        self.assertEqual(report["hotspot_total"], 1)
        self.assertEqual(report["hotspots"][0]["probability"], "HIGH")


class TestSonarClientAPI(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_report_with_mqr_and_400_fallback(self):
        client = SonarClient("http://192.168.3.216:59000", token="squ_test_token")
        ce_calls = {"count": 0}

        def make_resp(code, payload):
            r = MagicMock()
            r.status_code = code
            r.json.return_value = payload
            return r

        async def fake_get(path, params=None):
            if path == "/api/ce/task":
                ce_calls["count"] += 1
                if ce_calls["count"] == 1:
                    return make_resp(200, {"task": {"status": "PENDING", "componentKey": "foundation-kitefly_ui"}})
                return make_resp(
                    200,
                    {
                        "task": {
                            "status": "SUCCESS",
                            "analysisId": "ANA_99",
                            "componentKey": "foundation-kitefly_ui",
                            "componentName": "foundation/kitefly/ui",
                        }
                    },
                )
            if path == "/api/qualitygates/project_status":
                return make_resp(
                    200,
                    {
                        "projectStatus": {
                            "status": "OK",
                            "conditions": [
                                {
                                    "status": "OK",
                                    "metricKey": "new_violations",
                                    "comparator": "GT",
                                    "errorThreshold": "0",
                                    "actualValue": "0",
                                }
                            ],
                        }
                    },
                )
            if path == "/api/measures/component":
                return make_resp(
                    200,
                    {
                        "component": {
                            "name": "foundation/kitefly/ui",
                            "measures": [
                                {"metric": "ncloc", "value": "83539"},
                                {"metric": "duplicated_lines_density", "value": "8.8"},
                                {"metric": "coverage", "value": "0.0"},
                                {"metric": "software_quality_security_issues", "value": "19"},
                                {"metric": "software_quality_reliability_issues", "value": "412"},
                                {"metric": "software_quality_maintainability_issues", "value": "1379"},
                                {"metric": "security_hotspots", "value": "0"},
                                {"metric": "security_rating", "value": "5.0"},
                                {"metric": "reliability_rating", "value": "5.0"},
                                {"metric": "sqale_rating", "value": "1.0"},
                            ],
                        }
                    },
                )
            if path == "/api/issues/search":
                if (params or {}).get("s") == "SEVERITY":
                    return make_resp(400, {"errors": [{"msg": "Unsupported sort"}]})
                return make_resp(
                    200,
                    {
                        "paging": {"total": 2},
                        "issues": [
                            {
                                "severity": "MAJOR",
                                "type": "BUG",
                                "impacts": [{"softwareQuality": "RELIABILITY", "severity": "BLOCKER"}],
                                "component": "foundation-kitefly_ui:src/App.vue",
                                "line": 12,
                                "message": "Fix height property",
                                "rule": "css:S9424",
                            }
                        ],
                    },
                )
            if path == "/api/hotspots/search":
                return make_resp(200, {"paging": {"total": 0}, "hotspots": []})
            return make_resp(404, {})

        with patch.object(client, "_get", side_effect=fake_get), patch("asyncio.sleep", new_callable=AsyncMock):
            report = await client.fetch_report(ce_task_id="CE_1", scan_version="98ffdbe9", top_issues=5)

        self.assertEqual(ce_calls["count"], 2)
        self.assertEqual(report["qg_status"], "OK")
        self.assertEqual(report["measures"]["security_rating"], "E")
        self.assertEqual(report["measures"]["maintainability_rating"], "A")
        self.assertEqual(report["issue_total"], 2)
        self.assertEqual(report["issues"][0]["severity"], "BLOCKER")
        self.assertEqual(report["issues"][0]["quality"], "RELIABILITY")
        self.assertEqual(report["issues"][0]["location"], "src/App.vue:12")

    async def test_fetch_report_handles_403(self):
        client = SonarClient("http://192.168.3.216:59000", token="sqa_only_analysis")
        resp = MagicMock()
        resp.status_code = 403
        with patch.object(client, "_get", new_callable=AsyncMock, return_value=resp):
            report = await client.fetch_report(project_key="foundation-kitefly_ui")
        self.assertTrue(report["auth_denied"])


class TestBuildSonarCardAndMainButton(unittest.TestCase):
    def setUp(self):
        self.state = {
            "project": "Kitefly",
            "repo": "主仓库",
            "repo_id": 18,
            "branch": "main",
            "env": "test",
            "branches": ["main", "develop"],
            "variables_def": [
                {"key": "TARGET_MODULE", "label": "🧩 选微服务", "options": ["kitefly-ui", "kitefly-gateway"]}
            ],
            "variables": {"TARGET_MODULE": "kitefly-ui"},
        }

    def test_main_console_card_has_sonar_button_when_unlocked(self):
        cfg = {"projects": [{"name": "Kitefly", "environments": ["test"], "repos": [{"name": "主仓库", "id": 18, "repo": "foundation/kitefly"}]}]}
        card = build_card(cfg, "就绪", self.state, is_locked=False)
        actions = _collect_actions(card)
        sonar_btns = [a for a in actions if a.get("value", {}).get("action") == "sonar_scan"]
        self.assertEqual(len(sonar_btns), 1)
        self.assertIn("Sonar", sonar_btns[0]["text"]["content"])
        review_btns = [a for a in actions if a.get("value", {}).get("action") == "review"]
        self.assertEqual(len(review_btns), 0)
        run_btns = [a for a in actions if a.get("value", {}).get("action") == "run"]
        self.assertEqual(run_btns[0]["text"]["content"], "✅ 执行发布")

    def test_ok_with_zero_issues_has_no_ack_button(self):
        report = {
            "project_key": "foundation-kitefly_ui",
            "project_name": "foundation/kitefly/ui",
            "dashboard_url": "http://192.168.3.216:59000/dashboard?id=foundation-kitefly_ui",
            "qg_status": "OK",
            "qg_conditions": [],
            "measures": {"ncloc": "1000", "duplicated_lines_density": "1.0%", "coverage": "82.0%"},
            "issue_total": 0,
            "issues": [],
            "hotspot_total": 0,
            "hotspots": [],
        }
        card = build_sonar_card(
            self.state, "ou_user", pipeline_id=301, p_status="success", report=report, pipeline_url="http://gitlab/p/301"
        )
        self.assertEqual(card["header"]["template"], "green")
        actions = _collect_actions(card)
        ack_btns = [a for a in actions if a.get("value", {}).get("action") == "acknowledge_sub_card"]
        self.assertEqual(len(ack_btns), 0)
        urls = [a.get("url") for a in actions if a.get("url")]
        self.assertEqual(len(urls), 2)

    def test_ok_with_blocker_shows_ack_and_sanitizes_markdown(self):
        report = parse_sonar_report_from_text(SAMPLE_TRACE_LOG, top_issues=5)
        card = build_sonar_card(
            self.state, "ou_user", pipeline_id=302, p_status="success", report=report, pipeline_url="http://gitlab/p/302"
        )
        self.assertEqual(card["header"]["template"], "green")
        actions = _collect_actions(card)
        ack_btns = [a for a in actions if a.get("value", {}).get("action") == "acknowledge_sub_card"]
        self.assertEqual(len(ack_btns), 1)
        self.assertEqual(ack_btns[0]["value"]["card_kind"], "sonar")
        md = _all_markdown(card)
        self.assertIn("＜script＞", md)
        self.assertNotIn("<script>", md)
        self.assertIn("1726", md)

    def test_ack_info_removes_ack_button_but_preserves_urls(self):
        report = parse_sonar_report_from_text(SAMPLE_MARKDOWN_REPORT, top_issues=5)
        card = build_sonar_card(
            self.state,
            "ou_user",
            pipeline_id=303,
            p_status="failed",
            report=report,
            pipeline_url="http://gitlab/p/303",
            ack_info={"ack_by": "ou_user", "ack_by_name": "张三", "ack_at": "17:45"},
        )
        self.assertEqual(card["header"]["template"], "red")
        actions = _collect_actions(card)
        ack_btns = [a for a in actions if a.get("value", {}).get("action") == "acknowledge_sub_card"]
        self.assertEqual(len(ack_btns), 0)
        urls = [a.get("url") for a in actions if a.get("url")]
        self.assertEqual(len(urls), 2)
        self.assertIn("张三 已于 17:45 确认知晓", _all_markdown(card))

    def test_trigger_error_with_zero_pipeline_id_has_no_ack_button(self):
        card = build_sonar_card(
            self.state,
            "ou_user",
            pipeline_id=0,
            p_status="failed",
            failed_job_info="触发 Sonar 扫描流水线失败 (HTTP 400)",
        )
        actions = _collect_actions(card)
        ack_btns = [a for a in actions if a.get("value", {}).get("action") == "acknowledge_sub_card"]
        self.assertEqual(len(ack_btns), 0)
        self.assertFalse(sonar_report_needs_ack("success", {"qg_status": "OK", "issues": []}))


class TestSonarServiceAndRoutes(unittest.IsolatedAsyncioTestCase):
    async def test_collect_sonar_report_falls_back_to_artifact_on_403(self):
        gitlab = AsyncMock()
        gitlab.get_job_trace.return_value = "http://sonar:9000/dashboard?id=proj_api"
        gitlab.get_job_artifact_file.return_value = SAMPLE_MARKDOWN_REPORT
        cfg = {"sonarqube": {"base_url": "http://sonar:9000", "token": "sqa_only"}}
        jobs = [{"id": 77, "name": "sonar-kitefly-api", "stage": "sonar", "status": "failed"}]
        state = {"project": "Kitefly", "repo": "主仓库", "repo_id": 18}

        with patch("services.sonar_scan.SonarClient.fetch_report", new_callable=AsyncMock, return_value={"auth_denied": True}):
            report, failed_info = await _collect_sonar_report(cfg, gitlab, 18, jobs, state, "failed")

        self.assertEqual(failed_info, "")
        self.assertIsNotNone(report)
        self.assertEqual(report["project_key"], "foundation-kitefly_api")
        self.assertEqual(report["qg_status"], "ERROR")
        self.assertEqual(report["issue_total"], 103)

    @patch("services.sonar_scan.unlock_sonar", new_callable=AsyncMock)
    async def test_background_run_sonar_unlocks_on_trigger_http_400(self, mock_unlock):
        feishu = AsyncMock()
        gitlab = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = '{"message":"Reference not found or workflow filtered out"}'
        gitlab.trigger_pipeline.side_effect = __import__("httpx").HTTPStatusError("400", request=MagicMock(), response=mock_resp)

        state = {
            "project": "Kitefly",
            "repo": "主仓库",
            "repo_id": 18,
            "branch": "main",
            "env": "test",
            "variables": {"TARGET_MODULE": "kitefly-ui"},
        }
        await background_run_sonar(feishu, gitlab, {"feishu": {}}, state, "ou_user", "oc_chat_1", "18:main:kitefly-ui")

        mock_unlock.assert_awaited_once_with("18:main:kitefly-ui")
        feishu.send_card.assert_awaited_once()
        sent_card = feishu.send_card.call_args.args[1]
        self.assertEqual(sent_card["header"]["template"], "red")
        self.assertIn("ACTION", _all_markdown(sent_card))

    @patch("services.notification.check_and_send_release_note", new_callable=AsyncMock)
    @patch("routes.card.background_run_sonar", new_callable=AsyncMock)
    @patch("routes.card.try_lock_repo", new_callable=AsyncMock)
    @patch("routes.card.try_lock_sonar", new_callable=AsyncMock)
    @patch("routes.card.check_action_dedup", new_callable=AsyncMock)
    @patch("routes.card.save_card_state", new_callable=AsyncMock)
    @patch("routes.card.get_card_state", new_callable=AsyncMock)
    @patch("routes.card.normalize_selection", new_callable=AsyncMock)
    @patch("routes.card.get_all_projects", new_callable=AsyncMock)
    @patch("routes.card.load_config")
    async def test_route_sonar_scan_does_not_touch_repo_lock(
        self,
        mock_cfg,
        mock_projects,
        mock_norm,
        mock_get_state,
        mock_save_state,
        mock_dedup,
        mock_try_sonar,
        mock_try_repo,
        mock_bg_sonar,
        mock_release_note,
    ):
        mock_cfg.return_value = {
            "feishu": {"app_id": "a", "app_secret": "s"},
            "gitlab": {"base_url": "http://gl", "access_token": "t"},
            "permissions": {"admin_users": ["ou_admin"]},
            "projects": [],
        }
        mock_projects.return_value = []
        state = {
            "project": "Kitefly",
            "repo": "主仓库",
            "repo_id": 18,
            "branch": "main",
            "env": "test",
            "gitlab_instance": "default",
            "variables": {"TARGET_MODULE": "kitefly-ui"},
        }
        mock_norm.return_value = state
        mock_get_state.return_value = state
        mock_dedup.return_value = True
        mock_try_sonar.return_value = True

        # 非管理员点击 Sonar 扫描 -> 拒绝拦截，不加锁也不触发后台任务
        non_admin_req = MagicMock()
        non_admin_req.json = AsyncMock(return_value={
            "event": {
                "operator": {"open_id": "ou_tester"},
                "action": {"tag": "button", "value": {"action": "sonar_scan"}},
                "context": {"open_message_id": "om_main", "open_chat_id": "oc_group"},
            }
        })
        deny_resp = await feishu_card(non_admin_req)
        deny_data = json.loads(deny_resp.body.decode("utf-8"))
        self.assertEqual(deny_data["toast"]["type"], "error")
        self.assertIn("权限不足", deny_data["toast"]["content"])
        mock_try_sonar.assert_not_called()
        mock_try_repo.assert_not_called()
        mock_bg_sonar.assert_not_called()

        # 管理员点击 Sonar 扫描 -> 正常加锁并触发扫描（不触碰发版锁）
        req = MagicMock()
        req.json = AsyncMock(return_value={
            "event": {
                "operator": {"open_id": "ou_admin"},
                "action": {"tag": "button", "value": {"action": "sonar_scan"}},
                "context": {"open_message_id": "om_main", "open_chat_id": "oc_group"},
            }
        })
        resp = await feishu_card(req)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(data["toast"]["type"], "info")
        self.assertIn("SonarQube", data["toast"]["content"])
        mock_try_sonar.assert_awaited_once_with(build_sonar_lock_scope(state))
        mock_try_repo.assert_not_called()
        mock_bg_sonar.assert_called_once()

    @patch("services.notification.check_and_send_release_note", new_callable=AsyncMock)
    @patch("routes.card.get_card_state", new_callable=AsyncMock)
    @patch("routes.card.get_all_projects", new_callable=AsyncMock)
    @patch("routes.card.FeishuClient")
    @patch("routes.card.get_redis")
    @patch("routes.card.load_config")
    async def test_route_ack_sonar_card_syncs_audit_chat(
        self, mock_cfg, mock_get_redis, mock_feishu_cls, mock_projects, mock_get_state, mock_release_note
    ):
        mock_cfg.return_value = {"projects": [], "feishu": {"app_id": "a", "app_secret": "s"}}
        mock_projects.return_value = []
        mock_get_state.return_value = {}

        sonar_meta = {
            "card_kind": "sonar",
            "state": {"project": "Kitefly", "repo": "主仓库", "repo_id": 18, "branch": "main", "env": "test"},
            "operator_open_id": "ou_owner_1",
            "pipeline_id": 555,
            "p_status": "failed",
            "report": parse_sonar_report_from_text(SAMPLE_MARKDOWN_REPORT, top_issues=5),
            "pipeline_url": "http://gl/p/555",
            "sub_message_ids": ["om_card_chat", "om_card_audit"],
        }

        async def redis_get(key):
            if key == "cardops:pipeline_ack:555":
                return None
            if key == "cardops:sonar_card_meta:555":
                return json.dumps(sonar_meta, ensure_ascii=False)
            return None

        mock_redis = AsyncMock()
        mock_redis.get.side_effect = redis_get
        mock_get_redis.return_value = mock_redis

        mock_feishu = AsyncMock()
        mock_feishu.get_user_name.return_value = "李四"
        mock_feishu.update_card.return_value = {"code": 0}
        mock_feishu_cls.return_value = mock_feishu

        req = MagicMock()
        req.json = AsyncMock(return_value={
            "event": {
                "operator": {"open_id": "ou_owner_1"},
                "action": {
                    "tag": "button",
                    "value": {
                        "action": "acknowledge_sub_card",
                        "card_kind": "sonar",
                        "operator_open_id": "ou_owner_1",
                        "pipeline_id": 555,
                    },
                },
                "context": {"open_message_id": "om_card_chat"},
            }
        })
        resp = await feishu_card(req)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(data["toast"]["type"], "success")
        self.assertEqual(mock_feishu.update_card.await_count, 2)
        updated_card = mock_feishu.update_card.call_args_list[0].args[1]
        self.assertEqual(updated_card["header"]["template"], "red")
        self.assertIn("李四", _all_markdown(updated_card))
        self.assertIn("foundation/kitefly/api", _all_markdown(updated_card))


if __name__ == "__main__":
    unittest.main()

