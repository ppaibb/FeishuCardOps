import asyncio
import io
import json
import tarfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from core.card_builder import build_card, build_wiki_card, format_duration
from core.wiki_engine import (
    generate_wiki_bundle,
    git_blob_sha1,
    parse_json_result,
    scan_archive_bytes,
    validate_catalogue,
)
from routes.card import feishu_card
from routes.event import process_text_message
from services.intent_parser import resolve_wiki_target
from services.repo_wiki import (
    _commit_wiki_with_fallback,
    _compute_commit_actions,
    _running_wiki_tasks,
    background_run_wiki,
    cancel_running_wiki,
)


def _make_tar_gz(entries: dict, root_dir: str = "demo-main-abc123") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for rel_path, content in entries.items():
            raw = content if isinstance(content, bytes) else content.encode("utf-8")
            full_name = f"{root_dir}/{rel_path}" if root_dir else rel_path
            info = tarfile.TarInfo(name=full_name)
            info.size = len(raw)
            tf.addfile(info, io.BytesIO(raw))
    return buf.getvalue()


class TestWikiScannerAndEngine(unittest.IsolatedAsyncioTestCase):
    def test_scan_archive_filters_stacks_and_excludes_secrets(self):
        archive = _make_tar_gz(
            {
                "README.md": "# Demo Repo\nLine 2\n",
                "src/App.vue": "<template><div>Hello</div></template>\n",
                "service-api/pom.xml": "<project><artifactId>api</artifactId></project>\n",
                "service-api/src/Main.java": "public class Main {}\n",
                "node_modules/pkg/index.js": "console.log('ignored');\n",
                ".env.production": "SECRET_KEY=123456\n",
                "package-lock.json": '{"lockfileVersion": 3}',
                "docs/repowiki/README.md": "# Old generated wiki\n",
                "assets/logo.png": b"\x89PNG\r\n\x1a\n\x00binary",
            }
        )
        source = scan_archive_bytes(archive, branch="main", commit_sha="112233445566")
        paths = set(source["files"].keys())
        self.assertEqual(
            paths,
            {"README.md", "src/App.vue", "service-api/pom.xml", "service-api/src/Main.java"},
        )
        self.assertGreaterEqual(len(source["omitted"]), 4)

    def test_scan_archive_subpath_keeps_repo_relative_paths(self):
        archive = _make_tar_gz(
            {
                "README.md": "# Root\n",
                "kitefly-auth/pom.xml": "<project/>\n",
                "kitefly-auth/src/Auth.java": "class Auth {}\n",
                "kitefly-gateway/src/Gw.java": "class Gw {}\n",
            }
        )
        source = scan_archive_bytes(archive, branch="main", commit_sha="aa11", subpath="kitefly-auth")
        self.assertEqual(set(source["files"].keys()), {"kitefly-auth/pom.xml", "kitefly-auth/src/Auth.java"})

    async def test_generate_wiki_bundle_retries_and_produces_markdown_only(self):
        archive = _make_tar_gz(
            {
                "README.md": "# Demo Service\nProvides user APIs.\n",
                "src/app.py": "def run():\n    return 'ok'\n",
                "src/views/Home.vue": "<template><div>Home</div></template>\n",
            }
        )
        source = scan_archive_bytes(archive, branch="main", commit_sha="deadbeef1234")

        call_log = []
        catalogue_attempt = {"n": 0}

        async def fake_model_call(purpose, instructions, ctx):
            call_log.append(purpose)
            if purpose == "wiki.overview":
                return (
                    "<think>internal reasoning</think>\n"
                    "```markdown\n"
                    "# 仓库总览\n\n"
                    "该仓库提供用户服务与前端页面入口。\n"
                    "- 核心入口：`src/app.py:1`\n"
                    "- 前端视图：`src/views/Home.vue:1`\n"
                    "- 未知项：未直接运行端到端集成测试。\n"
                    + ("补充上下文说明与证据索引内容。\n" * 6)
                    + "\n```"
                )
            if purpose == "wiki.catalogue":
                catalogue_attempt["n"] += 1
                if catalogue_attempt["n"] == 1:
                    # 首次故意返回不存在的文件路径，验证带 validation_feedback 的自动重试
                    return json.dumps(
                        {
                            "sections": [
                                {
                                    "id": "backend_core",
                                    "title": "后端核心",
                                    "goal": "说明后端入口",
                                    "files": ["nonexistent/file.py"],
                                }
                            ]
                        }
                    )
                return json.dumps(
                    {
                        "sections": [
                            {
                                "id": "Backend_Core",
                                "title": "后端与接口层",
                                "goal": "说明 Python 服务端入口逻辑",
                                "files": ["src/app.py", "README.md"],
                            }
                        ]
                    }
                )
            if purpose == "wiki.page":
                return (
                    "# 后端与接口层\n\n"
                    "由 `src/app.py:1` 定义核心 `run()` 函数，项目说明见 `README.md:1`。\n\n"
                    "## 未知与边界\n"
                    "- 代码未直接运行；配置外部依赖待结合部署文件进一步确认。\n"
                    + ("详细分析条目补充证据引用。\n" * 8)
                )
            if purpose == "cards.module":
                return json.dumps(
                    {
                        "cards": [
                            {
                                "type": "overview",
                                "title": "模块概览",
                                "content": "# 模块概览\n\n入口位于 `src/app.py:1`，说明文档位于 `README.md:1`，核心逻辑简洁清晰，当前无复杂异步状态切换。",
                            },
                            {
                                "type": "tech_stack",
                                "title": "技术栈说明",
                                "content": "# 技术栈说明\n\n基于 Python 实现，核心模块集中在 `src/app.py` 与 `README.md`，整体轻量无重型外部框架耦合。",
                            },
                        ]
                    },
                    ensure_ascii=False,
                )
            raise AssertionError(f"unexpected purpose {purpose}")

        bundle = await generate_wiki_bundle(
            source=source,
            repo_name="foundation/demo",
            model_call=fake_model_call,
            wiki_dir="docs/repowiki",
            model_name="MiniMax-M2.7",
            max_pages=4,
        )

        # 验证 retries 发生过（catalogue 重试了第 2 次）
        self.assertEqual(catalogue_attempt["n"], 2)
        self.assertIn("wiki.catalogue", call_log)

        files = bundle["files"]
        self.assertIn("docs/repowiki/README.md", files)
        self.assertIn("docs/repowiki/sections/backend-core.md", files)
        self.assertIn("docs/repowiki/knowledge/backend-core/overview.md", files)
        self.assertIn("docs/repowiki/knowledge/backend-core/tech_stack.md", files)
        self.assertIn("docs/repowiki/repowiki-metadata.json", files)

        # 确认产物中绝不包含二进制 .db 或未请求的 index.html
        for path in files:
            self.assertFalse(path.endswith(".db"))
            self.assertFalse(path.endswith("index.html"))

        meta = json.loads(files["docs/repowiki/repowiki-metadata.json"])
        self.assertEqual(meta["repo"]["commit_sha"], "deadbeef1234")
        self.assertEqual(meta["catalogs"][0]["id"], "backend-core")


class TestGitLabWritebackAndFallback(unittest.IsolatedAsyncioTestCase):
    def test_compute_commit_actions_respects_blob_sha_and_deletes_stale(self):
        same_content = "# Same README\n"
        changed_content = "# Updated Section\n"
        new_content = "# New Card\n"

        existing_tree = [
            {"type": "blob", "path": "docs/repowiki/README.md", "id": git_blob_sha1(same_content)},
            {"type": "blob", "path": "docs/repowiki/sections/old.md", "id": "deadbeef"},
            {"type": "blob", "path": "docs/repowiki/sections/topic-1.md", "id": git_blob_sha1("# Old Content\n")},
        ]
        bundle_files = {
            "docs/repowiki/README.md": same_content,
            "docs/repowiki/sections/topic-1.md": changed_content,
            "docs/repowiki/knowledge/topic-1/overview.md": new_content,
        }
        actions = _compute_commit_actions(bundle_files, existing_tree, "docs/repowiki")
        action_map = {a["file_path"]: a["action"] for a in actions}
        self.assertNotIn("docs/repowiki/README.md", action_map)
        self.assertEqual(action_map["docs/repowiki/sections/topic-1.md"], "update")
        self.assertEqual(action_map["docs/repowiki/knowledge/topic-1/overview.md"], "create")
        self.assertEqual(action_map["docs/repowiki/sections/old.md"], "delete")

    async def test_protected_branch_403_falls_back_to_mr_branch(self):
        gitlab = AsyncMock()
        gitlab.get_project.return_value = {"web_url": "https://gitlab.example.com/group/demo"}
        gitlab.list_repository_tree.return_value = []

        req = httpx.Request("POST", "https://gitlab.example.com/api/v4/projects/18/repository/commits")
        resp_403 = httpx.Response(403, request=req, json={"message": "403 Forbidden - Protected branch"})
        http_err = httpx.HTTPStatusError("403 Forbidden", request=req, response=resp_403)

        commit_calls = {"n": 0}

        async def fake_commit(project_id, branch, commit_message, actions, start_branch=None):
            commit_calls["n"] += 1
            if branch == "main":
                raise http_err
            self.assertIn("[skip ci]", commit_message)
            self.assertEqual(branch, "docs/repowiki-main")
            return {"id": "newsha9999", "web_url": "https://gitlab.example.com/group/demo/-/commit/newsha9999"}

        gitlab.create_commit.side_effect = fake_commit
        gitlab.find_open_merge_request.return_value = None
        gitlab.get_branch.return_value = None
        gitlab.create_branch.return_value = {"name": "docs/repowiki-main"}
        gitlab.create_merge_request.return_value = {
            "iid": 42,
            "web_url": "https://gitlab.example.com/group/demo/-/merge_requests/42",
        }

        bundle = {
            "files": {"docs/repowiki/README.md": "# Wiki\n"},
            "topics": [{"id": "core", "title": "核心模块", "path": "docs/repowiki/sections/core.md"}],
        }
        res = await _commit_wiki_with_fallback(
            gitlab_client=gitlab,
            project_id=18,
            target_branch="main",
            commit_sha="basesha111",
            wiki_dir="docs/repowiki",
            bundle=bundle,
        )

        self.assertEqual(commit_calls["n"], 2)
        self.assertEqual(res["write_mode"], "mr")
        self.assertEqual(res["write_branch"], "docs/repowiki-main")
        self.assertEqual(res["mr_iid"], 42)
        self.assertEqual(res["mr_url"], "https://gitlab.example.com/group/demo/-/merge_requests/42")
        self.assertFalse(res["mr_reused"])


class TestWikiSkipCacheAndFeishuUI(unittest.IsolatedAsyncioTestCase):
    def test_main_card_has_wiki_button_and_wiki_card_renders_mr_links(self):
        cfg = {
            "projects": [
                {
                    "name": "Kitefly",
                    "environments": ["test", "prod"],
                    "repos": [{"name": "前端仓库", "id": 22, "repo": "foundation/kitefly/ui"}],
                }
            ]
        }
        state = {
            "project": "Kitefly",
            "repo": "前端仓库",
            "repo_id": 22,
            "branch": "main",
            "env": "test",
            "branches": ["main"],
            "variables_def": [],
            "variables": {},
        }
        card = build_card(cfg, "就绪", state)
        actions = []
        for elem in card.get("elements", []):
            if elem.get("tag") == "action":
                actions.extend(elem.get("actions", []))
        self.assertEqual(len(actions), 6)
        wiki_btn = [b for b in actions if b.get("value", {}).get("action") == "repo_wiki"]
        self.assertEqual(len(wiki_btn), 1)
        self.assertIn("Wiki", wiki_btn[0]["text"]["content"])
        self.assertIn("confirm", wiki_btn[0])
        confirm_text = wiki_btn[0]["confirm"]["text"]["content"]
        self.assertIn("活跃 MR", confirm_text)
        self.assertIn("source_branch", confirm_text)
        self.assertIn("一起评审并合入", confirm_text)

        mr_card = build_wiki_card(
            state=state,
            operator_open_id="ou_dev",
            w_status="success",
            commit_sha="1234567890",
            result_info={
                "wiki_dir": "docs/repowiki",
                "eligible_files": 120,
                "omitted_files": 15,
                "pages_count": 5,
                "cards_count": 8,
                "model_calls": 10,
                "elapsed_seconds": 68.5,
                "write_mode": "mr",
                "write_branch": "docs/repowiki-main",
                "mr_iid": 18,
                "mr_url": "https://gitlab.example.com/mr/18",
                "doc_url": "https://gitlab.example.com/docs/README.md",
                "topics": [{"id": "ui-router", "title": "路由体系", "path": "docs/repowiki/sections/ui-router.md"}],
            },
        )
        self.assertEqual(mr_card["header"]["template"], "green")
        self.assertIn("Merge Request", mr_card["header"]["title"]["content"])
        mr_actions = []
        for elem in mr_card.get("elements", []):
            if elem.get("tag") == "action":
                mr_actions.extend(elem.get("actions", []))
        urls = [b.get("url") for b in mr_actions]
        self.assertIn("https://gitlab.example.com/mr/18", urls)
        self.assertIn("https://gitlab.example.com/docs/README.md", urls)

    @patch("services.repo_wiki.unlock_wiki", new_callable=AsyncMock)
    @patch("core.state.get_redis")
    @patch("services.repo_wiki.get_redis")
    async def test_background_run_wiki_skips_when_baseline_matches_write_commit(
        self, mock_get_redis, mock_state_redis, mock_unlock
    ):
        mock_redis = AsyncMock()
        cached_baseline = {
            "commit_sha": "source_sha_1",
            "write_commit_sha": "wiki_commit_sha_2",
            "write_branch": "main",
            "config_hash": "",  # Will overwrite below
            "wiki_dir": "docs/repowiki",
            "pages_count": 5,
            "cards_count": 8,
            "doc_url": "https://gitlab.example.com/docs/README.md",
        }
        from core.wiki_engine import WIKI_VERSION, stable_digest

        cached_baseline["config_hash"] = stable_digest(
            {
                "version": WIKI_VERSION,
                "locale": "zh-CN",
                "max_pages": 4,
                "context_chars": 24000,
                "model": "MiniMax-M2.7",
                "wiki_dir": "docs/repowiki",
                "subpath": "",
            }
        )
        mock_redis.get.return_value = json.dumps(cached_baseline)
        mock_get_redis.return_value = mock_redis
        mock_state_redis.return_value = mock_redis

        feishu = AsyncMock()
        gitlab = AsyncMock()
        gitlab.get_project.return_value = {"web_url": "https://gitlab.example.com/group/app"}
        gitlab.get_branch.return_value = {"commit": {"id": "wiki_commit_sha_2"}}
        gitlab.list_repository_tree.return_value = [{"name": "README.md", "type": "blob"}]

        cfg = {
            "ai": {"api_key": "sk-test", "base_url": "https://api.example.com", "model": "MiniMax-M2.7"},
            "feishu": {"audit_chat_id": "oc_audit"},
            "projects": [],
        }
        state = {"project": "Demo", "repo": "App", "repo_id": 10, "branch": "main", "env": "test"}

        await background_run_wiki(
            feishu_client=feishu,
            gitlab_client=gitlab,
            cfg=cfg,
            state=state,
            operator_open_id="ou_1",
            open_chat_id="oc_chat",
            lock_scope="10:main:all",
            force=False,
        )

        # 未下载 Tarball、直接发送跳过卡片到触发群（不打扰审计群）
        gitlab.download_repository_archive.assert_not_called()
        feishu.send_card.assert_awaited_once()
        sent_chat_id, sent_card = feishu.send_card.call_args[0]
        self.assertEqual(sent_chat_id, "oc_chat")
        self.assertIn("已是最新版本", sent_card["header"]["title"]["content"])
        mock_unlock.assert_awaited_once_with("10:main:all")


class TestWikiRoutesAndIntent(unittest.IsolatedAsyncioTestCase):
    def test_resolve_wiki_target_detects_ambiguity_and_force(self):
        projects = [
            {
                "name": "Kitefly",
                "environments": ["test"],
                "repos": [
                    {"name": "前端服务", "id": 1, "repo": "foundation/kitefly/ui"},
                    {"name": "后端服务", "id": 2, "repo": "foundation/kitefly/api"},
                ],
            },
            {
                "name": "SingleProj",
                "environments": ["test"],
                "repos": [{"name": "主仓", "id": 3, "repo": "group/single"}],
            },
        ]

        # 精确命中 repo path + 强制标识
        res1 = resolve_wiki_target("强制生成 kitefly/ui repowiki", {}, projects)
        self.assertTrue(res1["unambiguous"])
        self.assertEqual(res1["project"], "Kitefly")
        self.assertEqual(res1["repo"], "前端服务")
        self.assertTrue(res1["force"])

        # 只说项目名但下面有多个仓库 -> 标记为需要弹窗确认
        res2 = resolve_wiki_target("Kitefly 生成wiki", {}, projects)
        self.assertFalse(res2["unambiguous"])
        self.assertEqual(res2["project"], "Kitefly")
        self.assertIsNone(res2["repo"])

        # 单仓库项目只说项目名 -> 自动锁定单仓
        res3 = resolve_wiki_target("SingleProj 生成wiki", {}, projects)
        self.assertTrue(res3["unambiguous"])
        self.assertEqual(res3["project"], "SingleProj")
        self.assertEqual(res3["repo"], "主仓")

    @patch("services.notification.check_and_send_release_note", new_callable=AsyncMock)
    @patch("routes.card.background_run_wiki", new_callable=AsyncMock)
    @patch("routes.card.try_lock_wiki", new_callable=AsyncMock)
    @patch("routes.card.check_action_dedup", new_callable=AsyncMock)
    @patch("routes.card.save_card_state", new_callable=AsyncMock)
    @patch("routes.card.get_card_state", new_callable=AsyncMock)
    @patch("routes.card.get_all_projects", new_callable=AsyncMock)
    @patch("routes.card.load_config")
    async def test_card_button_repo_wiki_triggers_background_task(
        self,
        mock_load_cfg,
        mock_get_projects,
        mock_get_card_state,
        mock_save_card_state,
        mock_dedup,
        mock_try_lock,
        mock_bg_wiki,
        mock_release_note,
    ):
        projects = [
            {
                "name": "Kitefly",
                "environments": ["test"],
                "repos": [{"name": "前端", "id": 18, "repo": "foundation/kitefly/ui"}],
            }
        ]
        mock_load_cfg.return_value = {
            "feishu": {"app_id": "cli_a", "app_secret": "sec"},
            "gitlab": {"base_url": "https://gitlab.example.com", "access_token": "tok"},
            "ai": {"api_key": "sk-test", "base_url": "https://api.example.com", "model": "MiniMax-M2.7"},
            "projects": projects,
        }
        mock_get_projects.return_value = projects
        mock_get_card_state.return_value = {
            "project": "Kitefly",
            "repo": "前端",
            "repo_id": 18,
            "branch": "main",
            "env": "test",
            "branches": ["main"],
            "variables_def": [],
            "variables": {},
        }
        mock_dedup.return_value = True
        mock_try_lock.return_value = True

        payload = {
            "event": {
                "operator": {"open_id": "ou_dev_1", "operator_id": {"open_id": "ou_dev_1"}},
                "action": {
                    "tag": "button",
                    "value": {
                        "action": "repo_wiki",
                        "project": "Kitefly",
                        "repo": "前端",
                        "branch": "main",
                        "env": "test",
                    },
                },
                "context": {"open_message_id": "om_card_1", "open_chat_id": "oc_group_1"},
            }
        }
        req = MagicMock()
        req.json = AsyncMock(return_value=payload)
        resp = await feishu_card(req)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(data["toast"]["type"], "info")
        self.assertIn("RepoWiki", data["toast"]["content"])
        self.assertIn("活跃 MR", data["toast"]["content"])
        mock_try_lock.assert_awaited_once_with("18:main:all")

    @patch("services.notification.check_and_send_release_note", new_callable=AsyncMock)
    @patch("routes.event.is_repo_locked", new_callable=AsyncMock)
    @patch("routes.event.normalize_selection", new_callable=AsyncMock)
    @patch("services.intent_parser.extract_wiki_intent", new_callable=AsyncMock)
    @patch("services.repo_wiki.background_run_wiki", new_callable=AsyncMock)
    @patch("routes.event.try_lock_wiki", new_callable=AsyncMock)
    @patch("routes.event.FeishuClient")
    @patch("routes.event.get_all_projects", new_callable=AsyncMock)
    @patch("routes.event.load_config")
    async def test_event_text_trigger_launches_wiki_or_prompts_when_ambiguous(
        self,
        mock_load_cfg,
        mock_get_projects,
        mock_feishu_cls,
        mock_try_lock,
        mock_bg_wiki,
        mock_extract_wiki,
        mock_norm_sel,
        mock_is_repo_locked,
        mock_release_note,
    ):
        projects = [
            {
                "name": "Kitefly",
                "environments": ["test"],
                "repos": [
                    {"name": "前端", "id": 18, "repo": "foundation/kitefly/ui"},
                    {"name": "后端", "id": 19, "repo": "foundation/kitefly/api"},
                ],
            }
        ]
        mock_load_cfg.return_value = {
            "feishu": {"app_id": "cli_a", "app_secret": "sec"},
            "gitlab": {"base_url": "https://gitlab.example.com", "access_token": "tok"},
            "ai": {"api_key": "sk-test", "base_url": "https://api.example.com", "model": "MiniMax-M2.7"},
            "projects": projects,
        }
        mock_get_projects.return_value = projects
        mock_feishu = AsyncMock()
        mock_feishu_cls.return_value = mock_feishu
        mock_is_repo_locked.return_value = False
        mock_norm_sel.return_value = {
            "project": "Kitefly",
            "repo": "前端",
            "repo_id": 18,
            "repo_path": "foundation/kitefly/ui",
            "gitlab_instance": "default",
            "env_display": {},
            "branch": "main",
            "env": "test",
            "branches": ["main"],
            "variables_def": [],
            "variables": {},
        }

        # 情况 A：明确目标 -> 直接锁定并触发后台任务
        mock_extract_wiki.return_value = {
            "project": "Kitefly",
            "repo": "前端",
            "branch": "main",
            "force": True,
            "unambiguous": True,
        }
        mock_try_lock.return_value = True
        await process_text_message("强制刷新 kitefly 前端 wiki", "oc_1", "ou_1")
        await asyncio.sleep(0)
        mock_try_lock.assert_awaited_once_with("18:main:all")
        mock_bg_wiki.assert_awaited_once()
        self.assertTrue(mock_bg_wiki.call_args.kwargs.get("force"))

        # 情况 B：多仓未指定 -> 发控制台卡片引导确认，不直接猜仓库扫描
        mock_bg_wiki.reset_mock()
        mock_extract_wiki.return_value = {
            "project": "Kitefly",
            "repo": None,
            "branch": None,
            "force": False,
            "unambiguous": False,
        }
        await process_text_message("kitefly 生成wiki", "oc_1", "ou_1")
        mock_bg_wiki.assert_not_called()
        mock_feishu.send_card.assert_awaited_once()

    @patch("services.repo_wiki.unlock_wiki", new_callable=AsyncMock)
    @patch("core.state.get_redis")
    @patch("services.repo_wiki.get_redis")
    @patch("services.repo_wiki.AnthropicWikiModel")
    async def test_background_run_wiki_full_flow_and_unlock_on_success(
        self, mock_model_cls, mock_get_redis, mock_state_redis, mock_unlock
    ):
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None
        mock_get_redis.return_value = mock_redis
        mock_state_redis.return_value = mock_redis

        async def fake_llm(purpose, instructions, ctx):
            if purpose == "wiki.overview":
                return "# 仓库总览\n\n核心代码见 `src/main.py:1`。" + ("补充总览描述信息。\n" * 8)
            if purpose == "wiki.catalogue":
                return json.dumps(
                    {
                        "sections": [
                            {
                                "id": "core",
                                "title": "核心模块",
                                "goal": "解析主入口逻辑",
                                "files": ["src/main.py"],
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            if purpose == "wiki.page":
                return "# 核心模块\n\n入口定义在 `src/main.py:1`。" + ("补充模块流程解析信息。\n" * 10)
            if purpose == "cards.module":
                return json.dumps(
                    {
                        "cards": [
                            {
                                "type": "overview",
                                "title": "模块概览",
                                "content": "# 模块概览\n\n入口位于 `src/main.py:1`，主要承担服务启动与初始化配置职责，依赖关系清晰简洁。",
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            raise AssertionError(purpose)

        fake_model_instance = MagicMock()
        fake_model_instance.calls = 4
        fake_model_instance.tokens = 1234
        fake_model_instance.call = AsyncMock(side_effect=fake_llm)
        mock_model_cls.return_value = fake_model_instance

        feishu = AsyncMock()
        feishu.send_card.side_effect = [{"data": {"message_id": "om_main_msg"}}, {"data": {"message_id": "om_audit_msg"}}]
        feishu.update_card.return_value = {"code": 0}

        gitlab = AsyncMock()
        gitlab.get_project.return_value = {"web_url": "https://gitlab.example.com/group/demo"}
        gitlab.get_branch.return_value = {"commit": {"id": "sha_head_001"}}
        gitlab.download_repository_archive.return_value = _make_tar_gz(
            {"README.md": "# Demo\n", "src/main.py": "print('hello')\n"}
        )
        gitlab.list_repository_tree.return_value = []
        gitlab.create_commit.return_value = {
            "id": "sha_wiki_commit",
            "web_url": "https://gitlab.example.com/group/demo/-/commit/sha_wiki_commit",
        }

        cfg = {
            "ai": {"api_key": "sk-test", "base_url": "https://api.example.com", "model": "MiniMax-M2.7"},
            "feishu": {"audit_chat_id": "oc_audit"},
            "projects": [],
        }
        state = {"project": "Demo", "repo": "后端", "repo_id": 99, "branch": "develop", "env": "test"}

        await background_run_wiki(
            feishu_client=feishu,
            gitlab_client=gitlab,
            cfg=cfg,
            state=state,
            operator_open_id="ou_dev",
            open_chat_id="oc_dev",
            lock_scope="99:develop:all",
            force=False,
        )

        # 验证写入目标分支且包含 [skip ci]
        gitlab.create_commit.assert_awaited_once()
        commit_kwargs = gitlab.create_commit.call_args.kwargs
        self.assertEqual(commit_kwargs["branch"], "develop")
        self.assertIn("[skip ci]", commit_kwargs["commit_message"])

        # 验证发送了群聊 + 审计群卡片，且最终态更新到成功卡片
        self.assertEqual(feishu.send_card.await_count, 2)
        last_update_card = feishu.update_card.call_args_list[-1][0][1]
        self.assertIn("写回成功", last_update_card["header"]["title"]["content"])

        # 验证 Redis 基线保存且并发锁必然释放
        baseline_set_calls = [c for c in mock_redis.set.call_args_list if "wiki_baseline" in c[0][0]]
        self.assertEqual(len(baseline_set_calls), 1)
        mock_unlock.assert_awaited_once_with("99:develop:all")

    def test_format_duration_and_wiki_card_states(self):
        self.assertEqual(format_duration(0.34), "0.3秒")
        self.assertEqual(format_duration(15.2), "15秒")
        self.assertEqual(format_duration(65.0), "1分05秒")
        self.assertEqual(format_duration(3665.0), "1小时01分05秒")

        state = {"project": "Kitefly", "repo": "前端", "repo_id": 22, "branch": "main", "env": "test"}

        # 1. 运行中卡片：包含使用时间 + 带确认弹窗的「🛑 取消生成」按钮
        running_card = build_wiki_card(
            state=state,
            operator_open_id="ou_dev",
            w_status="running",
            stage_text="正在撰写专题...",
            commit_sha="1234567890",
            lock_scope="22:main:all",
            result_info={"elapsed_seconds": 8.3},
        )
        running_md = "\n".join(e.get("content", "") for e in running_card["elements"] if e.get("tag") == "markdown")
        self.assertIn("使用时间", running_md)
        self.assertIn("8.3秒", running_md)
        running_actions = [a for e in running_card["elements"] if e.get("tag") == "action" for a in e.get("actions", [])]
        cancel_btns = [a for a in running_actions if a.get("value", {}).get("action") == "cancel_wiki"]
        self.assertEqual(len(cancel_btns), 1)
        self.assertEqual(cancel_btns[0]["type"], "danger")
        self.assertEqual(cancel_btns[0]["value"]["lock_scope"], "22:main:all")
        self.assertIn("confirm", cancel_btns[0])

        # 2. 已取消卡片：橙色卡片，展示取消人、使用时间且无操作按钮
        canceled_card = build_wiki_card(
            state=state,
            operator_open_id="ou_dev",
            w_status="canceled",
            stage_text="正在生成专题页...",
            commit_sha="1234567890",
            result_info={"elapsed_seconds": 65.0, "canceled_by": "ou_mgr", "canceled_by_name": "李四"},
        )
        self.assertEqual(canceled_card["header"]["template"], "orange")
        self.assertIn("已取消", canceled_card["header"]["title"]["content"])
        canceled_md = "\n".join(e.get("content", "") for e in canceled_card["elements"] if e.get("tag") == "markdown")
        self.assertIn("1分05秒", canceled_md)
        self.assertIn('<at id="ou_mgr"></at>', canceled_md)
        self.assertIn("正在生成专题页", canceled_md)
        self.assertEqual([e for e in canceled_card["elements"] if e.get("tag") == "action"], [])

        # 3. 失败与成功卡片均展示使用时间
        failed_card = build_wiki_card(
            state=state,
            operator_open_id="ou_dev",
            w_status="failed",
            result_info={"elapsed_seconds": 12.0},
            error_info="API timeout",
        )
        failed_md = "\n".join(e.get("content", "") for e in failed_card["elements"] if e.get("tag") == "markdown")
        self.assertIn("使用时间", failed_md)
        self.assertIn("12秒", failed_md)

    @patch("services.repo_wiki.unlock_wiki", new_callable=AsyncMock)
    @patch("core.state.get_redis")
    @patch("services.repo_wiki.get_redis")
    @patch("services.repo_wiki.AnthropicWikiModel")
    async def test_cancel_running_wiki_aborts_live_task_without_committing(
        self, mock_model_cls, mock_get_redis, mock_state_redis, mock_unlock
    ):
        kv_store = {}

        async def redis_get(key):
            return kv_store.get(key)

        async def redis_set(key, val, ex=None):
            kv_store[key] = val
            return True

        async def redis_setex(key, ttl, val):
            kv_store[key] = val
            return True

        async def redis_delete(*keys):
            for k in keys:
                kv_store.pop(k, None)
            return 1

        mock_redis = AsyncMock()
        mock_redis.get.side_effect = redis_get
        mock_redis.set.side_effect = redis_set
        mock_redis.setex.side_effect = redis_setex
        mock_redis.delete.side_effect = redis_delete
        mock_get_redis.return_value = mock_redis
        mock_state_redis.return_value = mock_redis

        entered_model = asyncio.Event()

        async def blocking_llm(purpose, instructions, ctx):
            entered_model.set()
            await asyncio.sleep(30)
            return "never returned"

        fake_model_instance = MagicMock()
        fake_model_instance.calls = 1
        fake_model_instance.tokens = 0
        fake_model_instance.call = AsyncMock(side_effect=blocking_llm)
        mock_model_cls.return_value = fake_model_instance

        feishu = AsyncMock()
        feishu.send_card.return_value = {"data": {"message_id": "om_wiki_run"}}
        feishu.update_card.return_value = {"code": 0}

        gitlab = AsyncMock()
        gitlab.get_project.return_value = {"web_url": "https://gitlab.example.com/group/demo"}
        gitlab.get_branch.return_value = {"commit": {"id": "sha_head_002"}}
        gitlab.download_repository_archive.return_value = _make_tar_gz(
            {"README.md": "# Demo\n", "src/main.py": "print('hi')\n"}
        )

        cfg = {
            "ai": {"api_key": "sk-test", "base_url": "https://api.example.com", "model": "MiniMax-M2.7"},
            "feishu": {},
            "projects": [],
        }
        state = {"project": "Demo", "repo": "后端", "repo_id": 88, "branch": "main", "env": "test"}
        lock_scope = "88:main:all"
        kv_store[f"wiki_lock:{lock_scope}"] = "1"

        task = asyncio.create_task(
            background_run_wiki(
                feishu_client=feishu,
                gitlab_client=gitlab,
                cfg=cfg,
                state=state,
                operator_open_id="ou_starter",
                open_chat_id="oc_dev",
                lock_scope=lock_scope,
                force=False,
            )
        )
        await asyncio.wait_for(entered_model.wait(), timeout=5)

        cancel_res = await cancel_running_wiki(
            lock_scope, canceler_open_id="ou_canceler", canceler_name="王五"
        )
        self.assertTrue(cancel_res["ok"])
        self.assertFalse(cancel_res["stale_unlocked"])
        self.assertIn("取消", cancel_res["msg"])

        await asyncio.wait_for(task, timeout=5)

        gitlab.create_commit.assert_not_called()
        mock_unlock.assert_awaited_once_with(lock_scope)
        self.assertNotIn(lock_scope, _running_wiki_tasks)
        self.assertNotIn(f"wiki_cancel:{lock_scope}", kv_store)

        last_card = feishu.update_card.call_args_list[-1][0][1]
        self.assertEqual(last_card["header"]["template"], "orange")
        self.assertIn("已取消", last_card["header"]["title"]["content"])
        card_md = "\n".join(e.get("content", "") for e in last_card["elements"] if e.get("tag") == "markdown")
        self.assertIn("使用时间", card_md)
        self.assertIn('<at id="ou_canceler"></at>', card_md)

    @patch("services.repo_wiki.clear_wiki_cancel", new_callable=AsyncMock)
    @patch("services.repo_wiki.unlock_wiki", new_callable=AsyncMock)
    @patch("services.repo_wiki.request_wiki_cancel", new_callable=AsyncMock)
    @patch("services.repo_wiki.get_wiki_cancel_info", new_callable=AsyncMock)
    @patch("services.repo_wiki.is_wiki_locked", new_callable=AsyncMock)
    async def test_cancel_running_wiki_cleans_up_stale_lock(
        self, mock_is_locked, mock_get_cancel, mock_req_cancel, mock_unlock, mock_clear_cancel
    ):
        _running_wiki_tasks.pop("77:main:all", None)
        mock_is_locked.return_value = True
        mock_get_cancel.return_value = None

        res = await cancel_running_wiki("77:main:all", canceler_open_id="ou_1", canceler_name="测试")
        self.assertTrue(res["ok"])
        self.assertTrue(res["stale_unlocked"])
        mock_unlock.assert_awaited_once_with("77:main:all")
        mock_clear_cancel.assert_awaited_once_with("77:main:all")

    @patch("services.notification.check_and_send_release_note", new_callable=AsyncMock)
    @patch("routes.card.FeishuClient")
    @patch("routes.card.cancel_running_wiki", new_callable=AsyncMock)
    @patch("routes.card.check_action_dedup", new_callable=AsyncMock)
    @patch("routes.card.get_card_state", new_callable=AsyncMock)
    @patch("routes.card.get_all_projects", new_callable=AsyncMock)
    @patch("routes.card.load_config")
    async def test_route_cancel_wiki_handles_active_and_stale_lock(
        self,
        mock_load_cfg,
        mock_get_projects,
        mock_get_card_state,
        mock_dedup,
        mock_cancel_wiki,
        mock_feishu_cls,
        mock_release_note,
    ):
        mock_load_cfg.return_value = {"feishu": {"app_id": "cli_a", "app_secret": "sec"}, "projects": []}
        mock_get_projects.return_value = []
        mock_get_card_state.return_value = None
        mock_dedup.return_value = True

        mock_feishu = AsyncMock()
        mock_feishu.get_user_name.return_value = "赵六"
        mock_feishu.update_card.return_value = {"code": 0}
        mock_feishu_cls.return_value = mock_feishu

        # 情况 A：正常取消活跃任务
        mock_cancel_wiki.return_value = {"ok": True, "stale_unlocked": False, "msg": "🛑 已发送取消指令，正在停止生成..."}
        req = MagicMock()
        req.json = AsyncMock(
            return_value={
                "event": {
                    "operator": {"open_id": "ou_zhao", "operator_id": {"open_id": "ou_zhao"}},
                    "action": {
                        "tag": "button",
                        "value": {
                            "action": "cancel_wiki",
                            "lock_scope": "18:main:all",
                            "operator_open_id": "ou_dev_1",
                            "project": "Kitefly",
                            "repo": "前端",
                            "repo_id": 18,
                            "branch": "main",
                        },
                    },
                    "context": {"open_message_id": "om_wiki_1", "open_chat_id": "oc_group_1"},
                }
            }
        )
        resp = await feishu_card(req)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(data["toast"]["type"], "success")
        mock_cancel_wiki.assert_awaited_once_with("18:main:all", canceler_open_id="ou_zhao", canceler_name="赵六")
        mock_feishu.update_card.assert_not_called()

        # 情况 B：遗留僵尸锁 -> 自动更新旧进度卡片为已取消状态
        mock_cancel_wiki.reset_mock()
        mock_cancel_wiki.return_value = {"ok": True, "stale_unlocked": True, "msg": "🛑 原后台任务已结束，已释放并发锁"}
        resp2 = await feishu_card(req)
        data2 = json.loads(resp2.body.decode("utf-8"))
        self.assertEqual(data2["toast"]["type"], "success")
        mock_feishu.update_card.assert_awaited_once()
        msg_id, updated_card = mock_feishu.update_card.call_args[0]
        self.assertEqual(msg_id, "om_wiki_1")
        self.assertEqual(updated_card["header"]["template"], "orange")

    @patch("services.repo_wiki.unlock_wiki", new_callable=AsyncMock)
    @patch("core.state.get_redis")
    @patch("services.repo_wiki.get_redis")
    @patch("services.repo_wiki.AnthropicWikiModel")
    async def test_background_run_wiki_active_mr_commits_to_source_branch_and_links_mr(
        self, mock_model_cls, mock_get_redis, mock_state_redis, mock_unlock
    ):
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None
        mock_get_redis.return_value = mock_redis
        mock_state_redis.return_value = mock_redis

        async def fake_llm(purpose, instructions, ctx):
            if purpose == "wiki.overview":
                return "# 仓库总览\n\n核心代码见 `src/auth.py:1`。" + ("补充总览描述信息。\n" * 8)
            if purpose == "wiki.catalogue":
                return json.dumps(
                    {
                        "sections": [
                            {
                                "id": "auth",
                                "title": "认证模块",
                                "goal": "解析登录认证逻辑",
                                "files": ["src/auth.py"],
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            if purpose == "wiki.page":
                return "# 认证模块\n\n入口定义在 `src/auth.py:1`。" + ("补充认证模块说明信息。\n" * 10)
            if purpose == "cards.module":
                return json.dumps(
                    {
                        "cards": [
                            {
                                "type": "overview",
                                "title": "模块概览",
                                "content": "# 模块概览\n\n入口位于 `src/auth.py:1`，主要承担认证校验与会话分发职责，依赖关系清晰简洁。",
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            raise AssertionError(purpose)

        fake_model_instance = MagicMock()
        fake_model_instance.calls = 4
        fake_model_instance.tokens = 888
        fake_model_instance.call = AsyncMock(side_effect=fake_llm)
        mock_model_cls.return_value = fake_model_instance

        feishu = AsyncMock()
        feishu.send_card.return_value = {"data": {"message_id": "om_mr_wiki"}}
        feishu.update_card.return_value = {"code": 0}

        gitlab = AsyncMock()
        gitlab.get_project.return_value = {"web_url": "https://gitlab.example.com/group/demo", "default_branch": "main"}
        gitlab.get_branch.return_value = {"commit": {"id": "sha_feat_001"}}
        gitlab.get_latest_open_mr.return_value = {
            "iid": 45,
            "title": "feat: 优化登录认证",
            "state": "opened",
            "source_branch": "feat/auth",
            "target_branch": "main",
            "web_url": "https://gitlab.example.com/group/demo/-/merge_requests/45",
            "author": {"name": "张三"},
        }
        gitlab.get_mr_changes.return_value = {"changes": [{"new_path": "src/auth.py"}, {"new_path": "README.md"}]}
        gitlab.download_repository_archive.return_value = _make_tar_gz(
            {"README.md": "# Demo\n", "src/auth.py": "def login():\n    return True\n"}
        )
        gitlab.list_repository_tree.return_value = []
        gitlab.create_commit.return_value = {
            "id": "sha_wiki_mr_commit",
            "web_url": "https://gitlab.example.com/group/demo/-/commit/sha_wiki_mr_commit",
        }

        cfg = {
            "ai": {"api_key": "sk-test", "base_url": "https://api.example.com", "model": "MiniMax-M2.7"},
            "feishu": {},
            "projects": [],
        }
        state = {"project": "Demo", "repo": "后端", "repo_id": 99, "branch": "feat/auth", "env": "test"}

        await background_run_wiki(
            feishu_client=feishu,
            gitlab_client=gitlab,
            cfg=cfg,
            state=state,
            operator_open_id="ou_dev",
            open_chat_id="oc_dev",
            lock_scope="99:feat/auth:all",
            force=False,
        )

        # 验证直接提交至 MR 的 source_branch，且提交信息标注 MR !45 与 [skip ci]
        gitlab.create_commit.assert_awaited_once()
        commit_kwargs = gitlab.create_commit.call_args.kwargs
        self.assertEqual(commit_kwargs["branch"], "feat/auth")
        self.assertIn("MR !45", commit_kwargs["commit_message"])
        self.assertIn("[skip ci]", commit_kwargs["commit_message"])

        # 验证初始运行卡片展示 MR 联动信息与变更文件数
        first_card = feishu.send_card.call_args[0][1]
        first_md = "\n".join(e.get("content", "") for e in first_card["elements"] if e.get("tag") == "markdown")
        self.assertIn("MR !45", first_md)
        self.assertIn("变更 `2` 个文件", first_md)

        # 验证最终结果卡片标题、正文说明与直达活跃 MR 按钮
        last_card = feishu.update_card.call_args_list[-1][0][1]
        self.assertIn("写入活跃 MR", last_card["header"]["title"]["content"])
        last_md = "\n".join(e.get("content", "") for e in last_card["elements"] if e.get("tag") == "markdown")
        self.assertIn("feat/auth", last_md)
        self.assertIn("一起评审并合入", last_md)
        action_btns = [a for e in last_card["elements"] if e.get("tag") == "action" for a in e.get("actions", [])]
        urls = [b.get("url") for b in action_btns]
        self.assertIn("https://gitlab.example.com/group/demo/-/merge_requests/45", urls)
        self.assertTrue(any("查看 MR !45" in (b.get("text") or {}).get("content", "") for b in action_btns))
        mock_unlock.assert_awaited_once_with("99:feat/auth:all")


if __name__ == "__main__":
    unittest.main()
