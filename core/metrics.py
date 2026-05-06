"""
DevOps 洞察指标模块

使用 prometheus_client 暴露标准 Prometheus 格式的 /metrics 接口，
支持 Grafana 拉取后构建可视化大盘。

指标体系：
- Counter: 发版触发次数、发版结果（成功/失败）、AI Review 触发、AI 故障诊断、审批流、动态项目注册
- Histogram: 发版耗时（从触发到流水线结束）
- Gauge: 当前活跃发版锁数量
"""

from prometheus_client import Counter, Histogram, Gauge, Info

# ───────────────────── 系统信息 ─────────────────────
SYSTEM_INFO = Info(
    "feishu_cardops",
    "FeishuCardOps 系统基本信息",
)
SYSTEM_INFO.info({
    "version": "1.2.0",
    "service": "feishu-gitlab-card-http",
})

# ───────────────────── 发版指标 ─────────────────────
PIPELINE_TRIGGERED = Counter(
    "feishu_pipeline_triggered_total",
    "发版流水线触发总次数",
    ["project", "repo", "env"],
)

PIPELINE_COMPLETED = Counter(
    "feishu_pipeline_completed_total",
    "发版流水线完成总次数（按最终状态）",
    ["project", "repo", "env", "status"],
)

PIPELINE_DURATION = Histogram(
    "feishu_pipeline_duration_seconds",
    "发版流水线从触发到结束的耗时（秒）",
    ["project", "repo", "env"],
    buckets=[30, 60, 120, 180, 300, 600, 900, 1200, 1800, 3600],
)

# ───────────────────── AI 指标 ─────────────────────
AI_REVIEW_TRIGGERED = Counter(
    "feishu_ai_review_triggered_total",
    "AI Code Review 触发总次数",
    ["project", "repo"],
)

AI_REVIEW_CACHED = Counter(
    "feishu_ai_review_cache_hit_total",
    "AI Code Review 命中缓存次数",
    ["project", "repo"],
)

AI_DIAGNOSIS_TRIGGERED = Counter(
    "feishu_ai_diagnosis_triggered_total",
    "AI 故障诊断触发总次数",
    ["project", "repo"],
)

# ───────────────────── 卡片交互指标 ─────────────────
CARD_INTERACTION = Counter(
    "feishu_card_interaction_total",
    "飞书卡片交互事件总次数",
    ["action"],
)

# ───────────────────── 审批流指标 ─────────────────────
APPROVAL_TRIGGERED = Counter(
    "feishu_approval_triggered_total",
    "审批流发起总次数",
    ["project", "env"],
)

APPROVAL_RESOLVED = Counter(
    "feishu_approval_resolved_total",
    "审批流处理总次数（按操作类型）",
    ["action"],
)

# ───────────────────── 动态项目指标 ─────────────────────
PROJECT_REGISTERED = Counter(
    "feishu_project_registered_total",
    "通过 AI 注册的动态项目总次数",
)

PROJECT_DELETED = Counter(
    "feishu_project_deleted_total",
    "动态项目删除总次数",
)

# ───────────────────── 实时状态 ─────────────────────
ACTIVE_LOCKS = Gauge(
    "feishu_active_pipeline_locks",
    "当前活跃的发版并发锁数量",
)
