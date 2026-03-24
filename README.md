<p align="center">
  <h1 align="center">🚀 FeishuCardOps</h1>
  <p align="center">
    <strong>飞书卡片驱动的 GitLab CI/CD 智能发版控制台</strong>
  </p>
  <p align="center">
    在飞书群聊中通过交互式卡片，一键触发、追踪和管理 GitLab 流水线部署
  </p>
  <p align="center">
    <a href="#-核心功能">功能</a> •
    <a href="#-系统架构">架构</a> •
    <a href="#-快速开始">快速开始</a> •
    <a href="#-配置说明">配置</a> •
    <a href="#-使用方式">使用</a>
  </p>
</p>

---

## 💡 项目简介

**FeishuCardOps** 是一款基于飞书交互卡片的 GitLab CI/CD 流水线管理工具。无需打开浏览器、无需登录 GitLab，在飞书群聊中即可完成从选择项目到触发部署的全部操作。

**一句话发版：** 在群聊中发送 `项目名 + 发版`，即刻唤醒智能发版控制台。

---

## 📸 效果预览

<table>
  <tr>
    <td align="center"><strong>🔒 发版进行中 · 按钮自动锁定</strong></td>
    <td align="center"><strong>✅ 发版完成 · 锁已释放</strong></td>
  </tr>
  <tr>
    <td><img src="docs/images/image-1.png" width="420" /></td>
    <td><img src="docs/images/image-0.png" width="420" /></td>
  </tr>
  <tr>
    <td align="center"><sub>主卡状态变为「处理中」，按钮变为灰色🔒<br/>副卡实时追踪当前流水线阶段</sub></td>
    <td align="center"><sub>流水线执行完毕后，主卡自动解锁恢复就绪<br/>副卡显示最终执行结果 🎉</sub></td>
  </tr>
</table>

---

## ✨ 核心功能

| 功能 | 说明 |
|------|------|
| 🏢 **多项目管理** | 一个机器人管理多个项目，每个项目可包含多个仓库 |
| 🌿 **动态分支发现** | 实时从 GitLab 获取最新分支列表，无需手动维护 |
| 🧩 **微服务模块选择** | 针对单仓多服务架构，动态展示 `TARGET_MODULE` 下拉框 |
| 💬 **自然语言触发** | 群聊发送 `项目名 + 发版` 即可唤醒发版控制台卡片 |
| 🔒 **并发锁保护** | 同一仓库同时只能运行一条流水线，防止重复发版 |
| 📊 **实时进度追踪** | 主卡 + 副卡架构，独立追踪每条流水线的运行状态与阶段 |
| 📢 **全程通知** | 发版开始/结束自动通知群组并 @触发人 |
| ⚡ **极速交互** | 分支缓存 + 全异步架构，下拉框切换毫秒级响应 |

---

## 📐 系统架构

```
用户在飞书群聊发送 "XXX项目发版"
        │
        ▼
┌──────────────────────────┐
│     飞书开放平台           │
│  (事件订阅 + 卡片回调)     │
└──────────┬───────────────┘
           │ Webhook
           ▼
┌──────────────────────────┐       ┌──────────────────┐
│   FeishuCardOps 服务      │──────▶│   GitLab API      │
│   FastAPI · :18789        │◀──────│   分支/流水线/任务  │
└──────────────────────────┘       └──────────────────┘
           │
           │ 异步轮询 Pipeline 状态
           ▼
┌──────────────────────────┐
│      飞书卡片实时更新       │
│  主卡(控制台) + 副卡(进度)  │
└──────────────────────────┘
```

### 卡片交互流程

```
选项目 → 选仓库 → 选分支 → 选环境 → [选微服务] → 执行触发
  │                                                   │
  │◀──────── 下拉框即时刷新（缓存加速）──────────────────│
                                                       │
                                              ┌────────▼────────┐
                                              │ 🔒 按钮上锁      │
                                              │ 📤 副卡弹出追踪   │
                                              │ ⏳ 异步轮询状态   │
                                              │ ✅ 完成后自动解锁  │
                                              └─────────────────┘
```

---

## 🚀 快速开始

### 前置条件

- Docker 20.10+ & Docker Compose v2+
- 飞书开放平台企业自建应用
- GitLab Personal Access Token（`api` 权限）

### 1. 克隆项目

```bash
git clone https://github.com/your-username/FeishuCardOps.git
cd FeishuCardOps
```

### 2. 配置

```bash
cp config.example.yaml config.yaml
vim config.yaml   # 填入飞书凭证、GitLab 地址和 Token
```

### 3. 启动

```bash
docker compose up -d --build
```

### 4. 验证

```bash
curl http://localhost:18789/healthz
# 返回 {"ok": true} 即为正常
```

---

## 📝 配置说明

```yaml
# ---- 飞书应用凭证 ----
feishu:
  app_id: "cli_xxxx"              # 飞书 App ID
  app_secret: "xxxx"              # 飞书 App Secret
  verification_token: "xxxx"      # 事件订阅验证 Token

# ---- GitLab 配置 ----
gitlab:
  base_url: "http://gitlab:port"  # GitLab 实例地址
  access_token: "glpat-xxxx"      # GitLab Access Token

# ---- 项目配置（支持多项目 × 多仓库 × 多环境 × 多模块）----
projects:
  - name: "我的项目"               # 项目名（同时也是触发词前缀）
    environments: ["test", "prod"]
    repos:
      - name: "后端仓库"
        repo: "group/backend"
        id: 10                     # GitLab Project ID
        modules:                   # 【可选】微服务列表
          - "service-user"
          - "service-order"
      - name: "前端仓库"
        repo: "group/frontend"
        id: 11
```

### Pipeline 变量

触发流水线时自动传递以下变量给 GitLab CI：

| 变量名 | 说明 | 示例值 |
|--------|------|--------|
| `ENV` | 部署环境 | `test` |
| `DEPLOY_ENV` | 部署环境（兼容字段） | `prod` |
| `TARGET_MODULE` | 微服务模块（仅配置了 modules 时传递） | `service-user` |

---

## 🔗 飞书开放平台配置

### 1. 创建企业自建应用

登录 [飞书开放平台](https://open.feishu.cn) → 创建应用 → 记录 App ID / App Secret

### 2. 添加机器人能力

应用详情 → 添加能力 → **机器人**

### 3. 配置事件订阅

| 配置项 | 值 |
|--------|-----|
| 请求地址 | `http://你的服务器:18789/feishu/event` |
| 订阅事件 | `接收消息 (im.message.receive_v1)` |

### 4. 配置卡片回调

| 配置项 | 值 |
|--------|-----|
| 卡片请求网址 | `http://你的服务器:18789/feishu/card` |

### 5. 权限配置

- `im:message` — 获取与发送消息
- `im:message:send_as_bot` — 以应用身份发送消息
- `contact:user.id:readonly` — 获取用户 ID

### 6. 发布上线

创建版本 → 提交审核 → 管理员审批 → 上线使用

---

## 🛠️ 运维命令

```bash
# 查看日志
docker compose logs -f --tail=100

# 重启服务（修改 config.yaml 后）
docker compose restart

# 停止服务
docker compose down

# 重新构建（修改代码后）
docker compose up -d --build
```

---

## 📁 项目结构

```
FeishuCardOps/
├── app.py                 # 核心业务逻辑（FastAPI）
├── config.yaml            # 运行时配置（⚠️ 含敏感信息，勿提交 Git）
├── config.example.yaml    # 配置模板
├── requirements.txt       # Python 依赖
├── Dockerfile             # Docker 镜像构建
├── docker-compose.yml     # Docker Compose 编排
├── .dockerignore           # Docker 构建排除
├── .gitignore             # Git 排除
├── LICENSE                # MIT 开源协议
└── README.md              # 本文档
```

---

## ⚠️ 注意事项

- `config.yaml` 包含敏感凭证，已在 `.gitignore` 中排除，**请勿提交到 Git**
- 建议部署在与飞书和 GitLab **同区域的国内服务器**，可获得最佳响应速度
- 状态存储在内存中，服务重启后并发锁会重置（不影响已在运行的 GitLab 流水线）
- 生产环境建议使用 Nginx 反向代理 + HTTPS

---

## 🗺️ Roadmap

- [ ] Redis 持久化并发锁状态
- [ ] RBAC 用户权限控制（限制生产环境发布权限）
- [ ] 审批流（生产发版需领导审批）
- [ ] Pipeline 历史记录面板
- [ ] Prometheus 监控指标

---

## 📄 License

[MIT](LICENSE) © 2025
