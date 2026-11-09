"""
SonarQube Web API 异步客户端 & 日志/报告解析工具

支持：
1. 直接查询 SonarQube Web API (适配 Community Build v26.8+ MQR 模型与旧版模型)
2. 从 GitLab CI Job 日志自动提取 projectKey / ceTaskId / dashboardUrl / serverUrl
3. 当未配置 sonarqube.token 或 API 返回 401/403 时，自动降级解析 sonar-report.md 或 CI 日志报告
"""
import asyncio
import base64
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import httpx

logger = logging.getLogger("feishu_gitlab_card_http")

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

METRIC_LABELS: Dict[str, str] = {
    "new_coverage": "新增覆盖率",
    "new_line_coverage": "新增行覆盖率",
    "new_branch_coverage": "新增分支覆盖率",
    "coverage": "整体覆盖率",
    "new_duplicated_lines_density": "新增重复率",
    "duplicated_lines_density": "整体重复率",
    "new_security_hotspots_reviewed": "新增热点审查率",
    "new_violations": "新增问题总数",
    "new_bugs": "新增 Bug",
    "new_vulnerabilities": "新增漏洞",
    "new_code_smells": "新增异味",
    "new_reliability_violations": "新增可靠性问题",
    "new_security_violations": "新增安全性问题",
    "new_maintainability_violations": "新增可维护性问题",
    "new_reliability_rating": "新增可靠性评级",
    "new_security_rating": "新增安全性评级",
    "new_maintainability_rating": "新增可维护性评级",
    "reliability_rating": "可靠性评级",
    "security_rating": "安全性评级",
    "sqale_rating": "可维护性评级",
}


def strip_ansi(text: str) -> str:
    if not text:
        return ""
    return ANSI_RE.sub("", text).replace("\r", "")


def rating_letter(val: Any) -> str:
    s = str(val or "").strip()
    if not s:
        return ""
    mapping = {"1": "A", "2": "B", "3": "C", "4": "D", "5": "E"}
    for prefix, letter in mapping.items():
        if s.startswith(prefix):
            return letter
    return s


def _num_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return str(val)


def resolve_sonar_config(cfg: Dict[str, Any], repo: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """合并全局 sonarqube 配置与仓库级覆盖配置"""
    global_cfg = cfg.get("sonarqube") or {}
    repo_cfg = (repo or {}).get("sonarqube") or {}
    base_url = str(repo_cfg.get("base_url") or global_cfg.get("base_url") or "").strip().rstrip("/")
    token = str(repo_cfg.get("token") or global_cfg.get("token") or "").strip()
    project_key = str(
        repo_cfg.get("project_key")
        or (repo or {}).get("sonar_project_key")
        or ""
    ).strip()
    raw_top = repo_cfg.get("top_issues", global_cfg.get("top_issues", 5))
    try:
        top_issues = max(1, min(int(raw_top), 10))
    except Exception:
        top_issues = 5
    return {
        "base_url": base_url,
        "token": token,
        "project_key": project_key,
        "top_issues": top_issues,
    }


def _extract_server_url(target_url: str, marker: str) -> str:
    if not target_url or marker not in target_url:
        return ""
    return target_url.split(marker, 1)[0].rstrip("/")


def parse_sonar_context_from_trace(trace: str) -> Dict[str, str]:
    """从 GitLab CI Job 日志中提取 SonarQube serverUrl / projectKey / ceTaskId / dashboardUrl / qg_status"""
    clean = strip_ansi(trace)
    ctx: Dict[str, str] = {
        "server_url": "",
        "project_key": "",
        "project_name": "",
        "dashboard_url": "",
        "ce_task_id": "",
        "ce_task_url": "",
        "scan_version": "",
        "qg_status": "",
    }
    if not clean:
        return ctx

    # 1. Dashboard URL (from common/sonar.yml summary, sonar qualitygate wait, or standard scanner output)
    dash_matches = re.findall(r"(https?://[^\s)\]>\"']+/dashboard\?id=[^\s)\]>\"']+)", clean)
    if dash_matches:
        dashboard_url = dash_matches[-1].rstrip(".,;")
        ctx["dashboard_url"] = dashboard_url
        ctx["server_url"] = _extract_server_url(dashboard_url, "/dashboard")
        parsed = urlparse(dashboard_url)
        qs_id = parse_qs(parsed.query).get("id")
        if qs_id and qs_id[0]:
            ctx["project_key"] = qs_id[0]

    # 2. CE Task URL / ID
    ce_matches = re.findall(r"(https?://[^\s)\]>\"']+/api/ce/task\?id=([A-Za-z0-9_-]+))", clean)
    if ce_matches:
        ce_url, ce_id = ce_matches[-1]
        ctx["ce_task_url"] = ce_url
        ctx["ce_task_id"] = ce_id
        if not ctx["server_url"]:
            ctx["server_url"] = _extract_server_url(ce_url, "/api/ce/task")

    # 3. Project Name / ProjectKey from SonarReport console block
    proj_m = re.search(r"Sonar 项目\s*:\s*(.+?)\s*\(projectKey:\s*([^)\s]+)\)", clean)
    if proj_m:
        ctx["project_name"] = proj_m.group(1).strip()
        ctx["project_key"] = proj_m.group(2).strip()
    elif not ctx["project_key"]:
        pk_m = re.search(r"Project key:\s*([^\s\r\n]+)", clean)
        if pk_m:
            ctx["project_key"] = pk_m.group(1).strip()

    # 4. Scan Version
    ver_m = re.search(r"扫描版本\s*:\s*([^\s\r\n]+)", clean)
    if ver_m:
        ctx["scan_version"] = ver_m.group(1).strip()
    else:
        scm_m = re.search(r"SCM revision ID '([0-9a-fA-F]{7,40})'", clean)
        if scm_m:
            ctx["scan_version"] = scm_m.group(1)[:8]

    # 5. Quality Gate status from scanner wait line
    qg_m = re.search(r"QUALITY GATE STATUS:\s*([A-Z]+)", clean)
    if qg_m:
        raw_qg = qg_m.group(1).upper()
        ctx["qg_status"] = {"PASSED": "OK", "FAILED": "ERROR"}.get(raw_qg, raw_qg)

    return ctx


def _normalize_qg_status(raw: str) -> str:
    upper = (raw or "").strip().upper()
    if upper in {"OK", "PASSED"}:
        return "OK"
    if upper in {"ERROR", "FAILED"}:
        return "ERROR"
    if upper in {"WARN", "WARNING"}:
        return "WARN"
    return upper or "UNKNOWN"


def _parse_rating_paren(text: str) -> str:
    m = re.search(r"评级\s*([A-E])", text)
    return m.group(1) if m else ""


def parse_sonar_report_from_text(text: str, top_issues: int = 5) -> Optional[Dict[str, Any]]:
    """
    解析 sonar-report.md 或 GitLab Job 日志中 common/sonar.yml 打印的报告内容。
    若文本中不包含有效报告片段则返回 None。
    """
    clean = strip_ansi(text)
    if "SonarQube 代码质量扫描报告" not in clean:
        return None

    # 截取最后一次报告正文，避免匹配到 script echo 前半段
    idx = clean.rfind("SonarQube 代码质量扫描报告")
    block = clean[idx:]

    report: Dict[str, Any] = {
        "source": "markdown" if "# 📊 SonarQube 代码质量扫描报告" in block or "## 📊 全量代码指标" in block else "trace",
        "project_key": "",
        "project_name": "",
        "scan_version": "",
        "dashboard_url": "",
        "qg_status": "UNKNOWN",
        "qg_conditions": [],
        "measures": {},
        "issue_total": -1,
        "issues": [],
        "hotspot_total": -1,
        "hotspots": [],
        "auth_denied": False,
        "error": "",
    }

    # 基础头部信息
    proj_md = re.search(r"-\s*\*\*Sonar 项目\*\*:\s*`([^`]+)`\s*\(`([^`]+)`\)", block)
    if proj_md:
        report["project_name"] = proj_md.group(1).strip()
        report["project_key"] = proj_md.group(2).strip()
    else:
        proj_log = re.search(r"Sonar 项目\s*:\s*(.+?)\s*\(projectKey:\s*([^)\s]+)\)", block)
        if proj_log:
            report["project_name"] = proj_log.group(1).strip()
            report["project_key"] = proj_log.group(2).strip()

    ver_m = re.search(r"(?:-\s*\*\*扫描版本\*\*:\s*`([^`]+)`|扫描版本\s*:\s*([^\s\n]+))", block)
    if ver_m:
        report["scan_version"] = (ver_m.group(1) or ver_m.group(2) or "").strip()

    qg_m = re.search(r"(?:-\s*\*\*质量门禁\*\*:|质量门禁\s*:)\s*[^\n]*?\b(OK|ERROR|WARN|UNKNOWN)\b", block)
    if qg_m:
        report["qg_status"] = _normalize_qg_status(qg_m.group(1))

    url_m = re.search(r"(?:-\s*\*\*报告详情\*\*:|报告详情\s*:)\s*(https?://[^\s\n]+)", block)
    if url_m:
        report["dashboard_url"] = url_m.group(1).strip()

    if "HTTP 401/403" in block or "无权查询明细接口" in block:
        report["auth_denied"] = True
        return report

    net_m = re.search(r"⚠️\s*连接 SonarQube API 失败\s*\(([^)]+)\)", block)
    if net_m:
        report["error"] = net_m.group(1).strip()
        return report

    # 质量门禁明细：Markdown 表格 or 控制台行
    for line in block.splitlines():
        s = line.strip()
        # Markdown row: | ✅ OK | 新增问题总数 (`new_violations`) | 0 | > 0 |
        md_cond = re.match(r"^\|\s*[✅❌⚠️]*\s*(OK|ERROR|WARN)\s*\|\s*(.+?)\s*\(`([^`]+)`\)\s*\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|$", s)
        if md_cond:
            thr_parts = md_cond.group(5).strip().split(None, 1)
            report["qg_conditions"].append({
                "status": md_cond.group(1).strip(),
                "label": md_cond.group(2).strip(),
                "metric_key": md_cond.group(3).strip(),
                "actual_value": md_cond.group(4).strip(),
                "comparator": thr_parts[0] if thr_parts else "",
                "threshold": thr_parts[1] if len(thr_parts) > 1 else "",
            })
            continue
        # Log line: ✅ 新增问题总数 (new_violations): 实际 0 [失败阈值 > 0]
        log_cond = re.match(r"^[✅❌⚠️]\s*(.+?)\s*\(([^)]+)\):\s*实际\s+(\S+)\s+\[失败阈值\s+([<>!=]*)\s*([^\]]*)\]$", s)
        if log_cond:
            status_val = "OK" if s.startswith("✅") else ("ERROR" if s.startswith("❌") else "WARN")
            report["qg_conditions"].append({
                "status": status_val,
                "label": log_cond.group(1).strip(),
                "metric_key": log_cond.group(2).strip(),
                "actual_value": log_cond.group(3).strip(),
                "comparator": log_cond.group(4).strip(),
                "threshold": log_cond.group(5).strip(),
            })

    # 全量代码指标
    ov_m = re.search(r"有效代码行[：:]\s*`?([^\s|`]+)`?\s*\|\s*重复率[：:]\s*`?([^\s|`]+)`?\s*\|\s*覆盖率[：:]\s*`?([^\s|`)]+)`?", block)
    if ov_m:
        report["measures"]["ncloc"] = ov_m.group(1).strip()
        report["measures"]["duplicated_lines_density"] = ov_m.group(2).strip()
        report["measures"]["coverage"] = ov_m.group(3).strip()

    for line in block.splitlines():
        s = line.strip()
        if "安全性问题" in s and ":" in s or "安全性问题" in s and "：" in s:
            m = re.search(r"安全性问题[^:：]*[:：]\s*\*{0,2}([^\s（(]+)\*{0,2}(.*)", s)
            if m:
                report["measures"]["security_issues"] = m.group(1).strip()
                r = _parse_rating_paren(m.group(2))
                if r:
                    report["measures"]["security_rating"] = r
        elif "可靠性问题" in s and (":" in s or "：" in s):
            m = re.search(r"可靠性问题[^:：]*[:：]\s*\*{0,2}([^\s（(]+)\*{0,2}(.*)", s)
            if m:
                report["measures"]["reliability_issues"] = m.group(1).strip()
                r = _parse_rating_paren(m.group(2))
                if r:
                    report["measures"]["reliability_rating"] = r
        elif "可维护性问题" in s and (":" in s or "：" in s):
            m = re.search(r"可维护性问题[^:：]*[:：]\s*\*{0,2}([^\s（(]+)\*{0,2}(.*)", s)
            if m:
                report["measures"]["maintainability_issues"] = m.group(1).strip()
                r = _parse_rating_paren(m.group(2))
                if r:
                    report["measures"]["maintainability_rating"] = r
        elif "安全热点 (Security Hotspots)" in s or ("安全热点" in s and "待审查" not in s and "审查率" not in s):
            m = re.search(r"安全热点[^:：]*[:：]\s*\*{0,2}(\d+|-)\*{0,2}", s)
            if m:
                report["measures"]["security_hotspots"] = m.group(1).strip()

    # 未解决重点问题总数
    zero_issue_m = re.search(r"未解决代码问题[^0-9]*:\s*0\s*个|未解决代码问题：\s*0\s*个", block)
    if zero_issue_m:
        report["issue_total"] = 0
    else:
        tot_m = re.search(r"未解决重点问题\s*[（(]共\s*(\d+)\s*个", block)
        if tot_m:
            report["issue_total"] = int(tot_m.group(1))

    # 解析重点问题与安全热点行（分段识别）
    in_issues = False
    in_hotspots = False
    for line in block.splitlines():
        s = line.strip()
        if "未解决重点问题" in s:
            in_issues = True
            in_hotspots = False
            continue
        if "待审查安全热点" in s:
            in_issues = False
            in_hotspots = True
            hs_m = re.search(r"共\s*(\d+)\s*个", s)
            if hs_m:
                report["hotspot_total"] = int(hs_m.group(1))
            continue
        if s.startswith("====") or s.startswith("## "):
            in_issues = False
            in_hotspots = False
            continue

        if in_issues:
            # 1. **[BLOCKER/RELIABILITY]** `path:line` — msg (`rule`)  OR  1. [BLOCKER/RELIABILITY] path:line — msg (rule)
            iss_m = re.match(
                r"^\d+\.\s+(?:\*\*)?\[([^/\]]+)(?:/([^\]]+))?\](?:\*\*)?\s+`?([^`\s]+)`?\s+[—-]\s+(.+?)(?:\s+\(`?([a-zA-Z0-9_.:-]+)`?\))?\s*$",
                s,
            )
            if iss_m and len(report["issues"]) < top_issues:
                report["issues"].append({
                    "severity": iss_m.group(1).strip(),
                    "quality": (iss_m.group(2) or "").strip(),
                    "location": iss_m.group(3).strip(),
                    "message": iss_m.group(4).strip(),
                    "rule": (iss_m.group(5) or "").strip(),
                })
        elif in_hotspots:
            hs_item = re.match(
                r"^\d+\.\s+(?:\*\*)?\[([^\]]+)\](?:\*\*)?\s+`?([^`\s]+)`?\s+[—-]\s+(.+)$",
                s,
            )
            if hs_item and len(report["hotspots"]) < 3:
                report["hotspots"].append({
                    "probability": hs_item.group(1).strip(),
                    "location": hs_item.group(2).strip(),
                    "message": hs_item.group(3).strip(),
                })

    return report


class SonarClient:
    """SonarQube Web API 异步客户端"""

    def __init__(self, base_url: str, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            encoded = base64.b64encode(f"{self.token}:".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
        return headers

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> httpx.Response:
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=15) as client:
            return await client.get(url, headers=self._headers(), params=params)

    async def get_ce_task(self, ce_task_id: str, max_attempts: int = 15, poll_interval: float = 1.5) -> Dict[str, Any]:
        """查询并短暂轮询 Compute Engine 任务，直到拿到 analysisId 或返回状态"""
        if not ce_task_id:
            return {}
        for attempt in range(max_attempts):
            resp = await self._get("/api/ce/task", params={"id": ce_task_id})
            if resp.status_code in {401, 403}:
                return {"_auth_denied": True}
            if resp.status_code != 200:
                return {"_http_status": resp.status_code}
            task = resp.json().get("task") or {}
            status = str(task.get("status") or "").upper()
            if status in {"PENDING", "IN_PROGRESS"} and attempt < max_attempts - 1:
                await asyncio.sleep(poll_interval)
                continue
            return task
        return {}

    async def get_quality_gate_status(self, analysis_id: str = "", project_key: str = "") -> Dict[str, Any]:
        params: Dict[str, str] = {}
        if analysis_id:
            params["analysisId"] = analysis_id
        elif project_key:
            params["projectKey"] = project_key
        else:
            return {}

        resp = await self._get("/api/qualitygates/project_status", params=params)
        if resp.status_code in {401, 403}:
            return {"_auth_denied": True}
        if resp.status_code != 200:
            return {"_http_status": resp.status_code}
        return resp.json().get("projectStatus") or {}

    async def get_measures(self, project_key: str) -> Dict[str, Any]:
        if not project_key:
            return {}
        metrics = ",".join([
            "ncloc",
            "duplicated_lines_density",
            "coverage",
            "software_quality_security_issues",
            "software_quality_reliability_issues",
            "software_quality_maintainability_issues",
            "security_hotspots",
            "bugs",
            "vulnerabilities",
            "code_smells",
            "reliability_rating",
            "security_rating",
            "sqale_rating",
        ])
        resp = await self._get("/api/measures/component", params={"component": project_key, "metricKeys": metrics})
        if resp.status_code in {401, 403}:
            return {"_auth_denied": True}
        if resp.status_code != 200:
            return {}
        return resp.json().get("component") or {}

    async def search_issues(self, project_key: str, limit: int = 5) -> Dict[str, Any]:
        if not project_key:
            return {}
        base_params = {
            "componentKeys": project_key,
            "resolved": "false",
            "ps": max(1, min(int(limit), 10)),
            "asc": "false",
        }
        resp = await self._get("/api/issues/search", params={**base_params, "s": "SEVERITY"})
        if resp.status_code == 400:
            resp = await self._get("/api/issues/search", params={**base_params, "s": "CREATION_DATE"})
        if resp.status_code in {401, 403}:
            return {"_auth_denied": True}
        if resp.status_code != 200:
            return {}
        return resp.json()

    async def search_hotspots(self, project_key: str, limit: int = 3) -> Dict[str, Any]:
        if not project_key:
            return {}
        resp = await self._get(
            "/api/hotspots/search",
            params={"projectKey": project_key, "status": "TO_REVIEW", "ps": max(1, min(int(limit), 5))},
        )
        if resp.status_code in {401, 403}:
            return {"_auth_denied": True}
        if resp.status_code != 200:
            return {}
        return resp.json()

    async def fetch_report(
        self,
        project_key: str = "",
        ce_task_id: str = "",
        dashboard_url: str = "",
        project_name: str = "",
        scan_version: str = "",
        top_issues: int = 5,
    ) -> Dict[str, Any]:
        """聚合拉取 SonarQube 质量门禁、全量指标、重点问题与待审查安全热点"""
        report: Dict[str, Any] = {
            "source": "api",
            "project_key": project_key,
            "project_name": project_name,
            "scan_version": scan_version,
            "dashboard_url": dashboard_url,
            "qg_status": "UNKNOWN",
            "qg_conditions": [],
            "measures": {},
            "issue_total": -1,
            "issues": [],
            "hotspot_total": -1,
            "hotspots": [],
            "auth_denied": False,
            "error": "",
        }

        analysis_id = ""
        if ce_task_id:
            task = await self.get_ce_task(ce_task_id)
            if task.get("_auth_denied"):
                report["auth_denied"] = True
                return report
            analysis_id = str(task.get("analysisId") or "")
            if not report["project_key"] and task.get("componentKey"):
                report["project_key"] = str(task["componentKey"])
            if not report["project_name"] and task.get("componentName"):
                report["project_name"] = str(task["componentName"])

        pk = report["project_key"]
        if not report["dashboard_url"] and pk and self.base_url:
            report["dashboard_url"] = f"{self.base_url}/dashboard?id={pk}"

        # 1. Quality Gate
        qg = await self.get_quality_gate_status(analysis_id=analysis_id, project_key=pk)
        if qg.get("_auth_denied"):
            report["auth_denied"] = True
            return report
        if qg and not qg.get("_http_status"):
            report["qg_status"] = _normalize_qg_status(str(qg.get("status") or ""))
            for cond in qg.get("conditions") or []:
                mk = str(cond.get("metricKey") or "")
                cmp_raw = str(cond.get("comparator") or "")
                cmp_disp = ">" if cmp_raw == "GT" else ("<" if cmp_raw == "LT" else cmp_raw)
                report["qg_conditions"].append({
                    "status": str(cond.get("status") or ""),
                    "metric_key": mk,
                    "label": METRIC_LABELS.get(mk, mk),
                    "actual_value": str(cond.get("actualValue") or ""),
                    "comparator": cmp_disp,
                    "threshold": str(cond.get("errorThreshold") or ""),
                })

        # 2. Measures
        if pk:
            comp = await self.get_measures(pk)
            if comp.get("_auth_denied"):
                report["auth_denied"] = True
                return report
            if comp:
                if not report["project_name"] and comp.get("name"):
                    report["project_name"] = str(comp["name"])
                raw_m: Dict[str, str] = {}
                for m in comp.get("measures") or []:
                    k = str(m.get("metric") or "")
                    v = m.get("value")
                    if v is None:
                        v = (m.get("period") or {}).get("value")
                    if k and v is not None and str(v) != "":
                        raw_m[k] = str(v)

                measures: Dict[str, str] = {}
                if "ncloc" in raw_m:
                    measures["ncloc"] = raw_m["ncloc"]
                if "duplicated_lines_density" in raw_m:
                    measures["duplicated_lines_density"] = f"{raw_m['duplicated_lines_density']}%"
                if "coverage" in raw_m:
                    measures["coverage"] = f"{raw_m['coverage']}%"

                sec_val = raw_m.get("software_quality_security_issues", raw_m.get("vulnerabilities", ""))
                rel_val = raw_m.get("software_quality_reliability_issues", raw_m.get("bugs", ""))
                mnt_val = raw_m.get("software_quality_maintainability_issues", raw_m.get("code_smells", ""))
                if sec_val != "":
                    measures["security_issues"] = sec_val
                if rel_val != "":
                    measures["reliability_issues"] = rel_val
                if mnt_val != "":
                    measures["maintainability_issues"] = mnt_val
                if "security_hotspots" in raw_m:
                    measures["security_hotspots"] = raw_m["security_hotspots"]

                sec_r = rating_letter(raw_m.get("security_rating"))
                rel_r = rating_letter(raw_m.get("reliability_rating"))
                mnt_r = rating_letter(raw_m.get("sqale_rating"))
                if sec_r:
                    measures["security_rating"] = sec_r
                if rel_r:
                    measures["reliability_rating"] = rel_r
                if mnt_r:
                    measures["maintainability_rating"] = mnt_r
                report["measures"] = measures

            # 3. Issues
            iss_data = await self.search_issues(pk, limit=top_issues)
            if iss_data.get("_auth_denied"):
                report["auth_denied"] = True
                return report
            if iss_data:
                paging = iss_data.get("paging") or {}
                if "total" in paging:
                    report["issue_total"] = int(paging["total"])
                for iss in (iss_data.get("issues") or [])[:top_issues]:
                    sev = str(iss.get("severity") or "")
                    qual = str(iss.get("type") or "")
                    imps = iss.get("impacts") or []
                    if imps and isinstance(imps, list) and isinstance(imps[0], dict):
                        sev = str(imps[0].get("severity") or sev)
                        qual = str(imps[0].get("softwareQuality") or qual)
                    comp_str = str(iss.get("component") or "")
                    prefix = f"{pk}:"
                    if pk and comp_str.startswith(prefix):
                        comp_str = comp_str[len(prefix):]
                    line = _num_str(iss.get("line"))
                    loc = f"{comp_str}:{line}" if line else comp_str
                    msg = str(iss.get("message") or "").replace("\n", " ").replace("\r", " ").strip()
                    report["issues"].append({
                        "severity": sev,
                        "quality": qual,
                        "location": loc,
                        "message": msg,
                        "rule": str(iss.get("rule") or ""),
                    })

            # 4. Hotspots
            hs_data = await self.search_hotspots(pk, limit=3)
            if hs_data and not hs_data.get("_auth_denied"):
                paging = hs_data.get("paging") or {}
                if "total" in paging:
                    report["hotspot_total"] = int(paging["total"])
                    if "security_hotspots" not in report["measures"]:
                        report["measures"]["security_hotspots"] = str(report["hotspot_total"])
                for hs in (hs_data.get("hotspots") or [])[:3]:
                    comp_str = str(hs.get("component") or "")
                    prefix = f"{pk}:"
                    if pk and comp_str.startswith(prefix):
                        comp_str = comp_str[len(prefix):]
                    line = _num_str(hs.get("line"))
                    loc = f"{comp_str}:{line}" if line else comp_str
                    msg = str(hs.get("message") or "").replace("\n", " ").replace("\r", " ").strip()
                    report["hotspots"].append({
                        "probability": str(hs.get("vulnerabilityProbability") or ""),
                        "location": loc,
                        "message": msg,
                    })

        return report

