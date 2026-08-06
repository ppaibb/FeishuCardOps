<template>
  <div class="dark-layout">
    <el-container style="min-height: 100vh;">
      <el-header height="64px" class="header-nav">
        <div class="nav-brand">
          <div class="logo-box">
            <el-icon :size="20"><Platform /></el-icon>
          </div>
          <div class="brand-text">
            <span class="title">FeishuCardOps</span>
            <span class="subtitle">GitLab CI/CD 智能发版控制台</span>
          </div>
        </div>
        <div class="nav-extra">
          <div class="status-indicator">
            <span class="status-dot"></span>
            <span class="status-text">{{ serviceStatus }}</span>
          </div>
        </div>
      </el-header>

      <el-main class="app-main">
        <el-row :gutter="24">
          <el-col :span="10" :xs="24">
            <el-card class="glass-card deploy-card" shadow="always">
              <template #header>
                <div class="card-title">
                  <el-icon class="icon-blue"><UploadFilled /></el-icon>
                  <span>发版部署配置</span>
                  <el-tag size="small" type="info" effect="plain" class="header-tag">Step-by-step</el-tag>
                </div>
              </template>

              <el-form label-position="top" class="custom-form">
                <el-form-item label="1. 选择服务项目">
                  <el-select v-model="form.project" placeholder="请选择发版项目" style="width: 100%" @change="onProjectChange" filterable>
                    <el-option v-for="p in projects" :key="p.name" :label="p.name" :value="p.name"></el-option>
                  </el-select>
                </el-form-item>

                <el-form-item label="2. 选择目标仓库">
                  <el-select v-model="form.repo" placeholder="请选择目标仓库" style="width: 100%" :disabled="!form.project" @change="onRepoChange" filterable>
                    <el-option v-for="r in currentRepos" :key="r.name" :label="r.name + ' (' + r.repo + ')'" :value="r.name"></el-option>
                  </el-select>
                </el-form-item>

                <el-form-item label="3. 选择部署环境">
                  <el-radio-group v-model="form.env" style="width: 100%" class="env-radio-group">
                    <el-radio-button value="test">TEST 测试环境</el-radio-button>
                    <el-radio-button value="prod">PROD 生产环境</el-radio-button>
                  </el-radio-group>
                </el-form-item>

                <el-form-item label="4. 选择分支 / 标签 (Ref)">
                  <el-select v-model="form.ref" placeholder="搜索选择分支或 Tag" style="width: 100%" :disabled="!form.repo || loadingBranches" filterable>
                    <el-option-group label="🌿 分支列表 (Branches)">
                      <el-option v-for="b in branches" :key="b.name" :label="b.name" :value="b.name"></el-option>
                    </el-option-group>
                    <el-option-group v-if="tags.length > 0" label="🏷️ 标签列表 (Tags)">
                      <el-option v-for="t in tags" :key="t.name" :label="t.name" :value="t.name"></el-option>
                    </el-option-group>
                  </el-select>
                </el-form-item>

                <template v-if="currentVariables.length > 0">
                  <el-divider content-position="left"><span style="font-size: 12px; color: #8c8c8c;">⚙️ 仓库参数配置</span></el-divider>
                  <el-form-item v-for="v in currentVariables" :key="v.key" :label="v.label || v.key">
                    <el-select v-model="form.variables[v.key]" placeholder="请选择" style="width: 100%">
                      <el-option v-for="opt in v.options" :key="opt" :label="opt" :value="opt"></el-option>
                    </el-select>
                  </el-form-item>
                </template>

                <el-form-item label="执行操作人">
                  <el-input v-model="form.operator" placeholder="请输入操作人" prefix-icon="User" />
                </el-form-item>

                <el-button type="primary" size="large" class="btn-deploy-main" :loading="deploying" :disabled="!form.ref" @click="triggerDeploy">
                  一键触发 CI/CD 部署流水线
                </el-button>
              </el-form>
            </el-card>
          </el-col>

          <el-col :span="14" :xs="24">
            <el-card class="glass-card terminal-card" shadow="always" style="margin-bottom: 24px;">
              <template #header>
                <div class="terminal-mac-header">
                  <div class="mac-dots">
                    <span class="dot red"></span>
                    <span class="dot yellow"></span>
                    <span class="dot green"></span>
                  </div>
                  <div class="terminal-title">
                    <el-icon><Monitor /></el-icon>
                    Pipeline Log Output
                  </div>
                  <div>
                    <el-tag :type="statusTagType" effect="dark" size="small">{{ currentStatus }}</el-tag>
                  </div>
                </div>
              </template>

              <div class="terminal-content" id="terminal-body">
                <pre class="log-text">{{ terminalLog || '等待触发发布流水线...' }}</pre>
              </div>
            </el-card>

            <el-card class="glass-card history-card" shadow="always">
              <template #header>
                <div class="card-title-flex">
                  <div class="card-title">
                    <el-icon class="icon-blue"><List /></el-icon>
                    <span>发版审计历史记录</span>
                  </div>
                  <el-button size="small" type="primary" plain @click="fetchHistory" icon="Refresh">
                    刷新记录
                  </el-button>
                </div>
              </template>

              <el-table :data="history" stripe style="width: 100%" size="default" class="custom-table">
                <el-table-column prop="triggered_at" label="时间" width="160" />
                <el-table-column label="项目 / 仓库" min-width="180">
                  <template #default="scope">
                    <span class="project-name">{{ scope.row.project }}</span>
                    <span class="repo-divider">/</span>
                    <span class="repo-name">{{ scope.row.repo }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="环境" width="90" align="center">
                  <template #default="scope">
                    <el-tag :type="scope.row.env === 'prod' ? 'danger' : 'warning'" size="small" effect="dark">
                      {{ scope.row.env ? scope.row.env.toUpperCase() : '' }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="branch" label="分支/Ref" min-width="120">
                  <template #default="scope">
                    <code class="code-badge">{{ scope.row.branch }}</code>
                  </template>
                </el-table-column>
                <el-table-column prop="operator_name" label="执行人" width="100" />
                <el-table-column label="状态" width="100" align="center">
                  <template #default="scope">
                    <el-tag :type="getHistoryStatusType(scope.row.status)" size="small" effect="light">
                      {{ scope.row.status ? scope.row.status.toUpperCase() : 'RUNNING' }}
                    </el-tag>
                  </template>
                </el-table-column>
              </el-table>
            </el-card>
          </el-col>
        </el-row>
      </el-main>
    </el-container>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import axios from 'axios'

const serviceStatus = ref('FeishuCardOps Standalone')
const projects = ref([])
const branches = ref([])
const tags = ref([])
const history = ref([])

const loadingBranches = ref(false)
const deploying = ref(false)

const currentStatus = ref('IDLE')
const statusTagType = ref('info')
const terminalLog = ref('等待触发发布流水线...')
let pollTimer = null

const form = reactive({
  project: '',
  repo: '',
  env: 'test',
  ref: '',
  operator: 'WebUser',
  variables: {}
})

const currentRepos = computed(() => {
  const p = projects.value.find(item => item.name === form.project)
  return p ? p.repos || [] : []
})

const currentVariables = computed(() => {
  const r = currentRepos.value.find(item => item.name === form.repo)
  return r ? r.variables || [] : []
})

onMounted(async () => {
  document.documentElement.classList.add('dark')
  await fetchConfig()
  await fetchProjects()
  await fetchHistory()
})

async function fetchConfig() {
  try {
    const res = await axios.get('/api/v1/config')
    if (res.data.ok) {
      serviceStatus.value = `FeishuCardOps ${res.data.version}`
    }
  } catch (e) {
    console.error(e)
  }
}

async function fetchProjects() {
  try {
    const res = await axios.get('/api/v1/projects')
    if (res.data.ok) {
      projects.value = res.data.projects || []
    }
  } catch (e) {
    ElMessage.error('获取项目列表失败')
  }
}

function onProjectChange() {
  form.repo = ''
  form.ref = ''
  branches.value = []
  tags.value = []
  form.variables = {}
}

async function onRepoChange() {
  form.ref = ''
  branches.value = []
  tags.value = []
  form.variables = {}

  if (!form.project || !form.repo) return

  currentVariables.value.forEach(v => {
    if (v.options && v.options.length > 0) {
      form.variables[v.key] = v.options[0]
    }
  })

  loadingBranches.value = true
  try {
    const res = await axios.get(`/api/v1/repos/${encodeURIComponent(form.project)}/${encodeURIComponent(form.repo)}/branches`)
    if (res.data.ok) {
      branches.value = res.data.branches || []
      tags.value = res.data.tags || []
      const defBranch = branches.value.find(b => b.default) || branches.value[0]
      if (defBranch) form.ref = defBranch.name
    }
  } catch (e) {
    ElMessage.warning('拉取分支超时，已恢复默认分支')
    branches.value = [{ name: 'main', default: true }]
    form.ref = 'main'
  } finally {
    loadingBranches.value = false
  }
}

async function triggerDeploy() {
  if (!form.project || !form.repo || !form.ref) return

  deploying.value = true
  currentStatus.value = 'SUBMITTING'
  statusTagType.value = 'warning'

  terminalLog.value = `[${new Date().toLocaleTimeString()}] 提交发版部署请求...\n项目: ${form.project}\n仓库: ${form.repo}\n环境: ${form.env.toUpperCase()}\nRef: ${form.ref}\n`

  try {
    const res = await axios.post('/api/v1/pipeline/deploy', {
      project_name: form.project,
      repo_name: form.repo,
      env: form.env,
      ref: form.ref,
      variables: form.variables,
      operator_name: form.operator
    })

    if (res.data.ok) {
      if (res.data.status === 'approval_required') {
        ElMessage.warning(`触发生产审批流程，已生成审批单 #${res.data.approval_id}`)
        terminalLog.value += `\n⚠️ 【生产环境审批拦截】已自动生成审批单 #${res.data.approval_id}\n审批人: ${res.data.approvers.join(', ')}`
        currentStatus.value = 'PENDING APPROVAL'
        statusTagType.value = 'warning'
      } else {
        ElMessage.success(`成功触发 CI/CD 流水线 #${res.data.pipeline_id}`)
        terminalLog.value += `\n✅ 部署流水线触发成功！Pipeline ID: #${res.data.pipeline_id}\n`
        startWatch(form.project, form.repo, res.data.pipeline_id)
      }
    } else {
      ElMessage.error(res.data.error || '部署提交失败')
    }
  } catch (e) {
    ElMessage.error('提交发版异常: ' + e.message)
  } finally {
    deploying.value = false
    fetchHistory()
  }
}

function startWatch(projectName, repoName, pipelineId) {
  if (pollTimer) clearInterval(pollTimer)
  currentStatus.value = 'RUNNING'
  statusTagType.value = 'primary'

  pollTimer = setInterval(async () => {
    try {
      const res = await axios.get(`/api/v1/pipeline/${encodeURIComponent(projectName)}/${encodeURIComponent(repoName)}/${pipelineId}/status`)
      if (res.data.ok) {
        let logText = `=== 流水线 #${res.data.pipeline_id} 状态: ${res.data.status.toUpperCase()} ===\n`
        logText += `Ref: ${res.data.ref} | SHA: ${res.data.sha}\n\n`

        if (res.data.job_logs && res.data.job_logs.length > 0) {
          res.data.job_logs.forEach(j => {
            logText += `------------- Stage: ${j.stage} | Job: ${j.job_name} (${j.status}) -------------\n`
            logText += j.log_tail || '(无日志)'
            logText += '\n\n'
          })
        }
        terminalLog.value = logText

        const termBox = document.getElementById('terminal-body')
        if (termBox) termBox.scrollTop = termBox.scrollHeight

        if (['success', 'failed', 'canceled'].includes(res.data.status)) {
          clearInterval(pollTimer)
          currentStatus.value = res.data.status.toUpperCase()
          statusTagType.value = res.data.status === 'success' ? 'success' : 'danger'
          fetchHistory()
        }
      }
    } catch (e) {
      console.error(e)
    }
  }, 3000)
}

async function fetchHistory() {
  try {
    const res = await axios.get('/api/v1/history?limit=20')
    if (res.data.ok) {
      history.value = res.data.history || []
    }
  } catch (e) {
    console.error(e)
  }
}

function getHistoryStatusType(status) {
  if (!status) return 'info'
  switch (status.toLowerCase()) {
    case 'success': return 'success'
    case 'failed': return 'danger'
    case 'pending': return 'warning'
    case 'running': return 'primary'
    default: return 'info'
  }
}
</script>

<style>
/* Modern Dark Theme Overrides */
html.dark {
  background-color: #0f172a !important;
  color: #f8fafc;
}
.dark-layout {
  min-height: 100vh;
  background-color: #0f172a;
  color: #f8fafc;
}
.header-nav {
  background-color: #1e293b;
  border-bottom: 1px solid #334155;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
}
.nav-brand {
  display: flex;
  align-items: center;
  gap: 12px;
}
.logo-box {
  background: #3b82f6;
  color: #fff;
  width: 36px;
  height: 36px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.brand-text {
  display: flex;
  flex-direction: column;
}
.brand-text .title {
  font-size: 16px;
  font-weight: 700;
  color: #f8fafc;
}
.brand-text .subtitle {
  font-size: 11px;
  color: #94a3b8;
}
.status-indicator {
  display: flex;
  align-items: center;
  gap: 8px;
  background: #1e293b;
  border: 1px solid #334155;
  padding: 6px 14px;
  border-radius: 20px;
  font-size: 12px;
}
.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background-color: #10b981;
  box-shadow: 0 0 10px #10b981;
}
.app-main {
  padding: 24px;
  max-width: 1440px;
  margin: 0 auto;
}
.glass-card {
  background-color: #1e293b !important;
  border: 1px solid #334155 !important;
  border-radius: 12px !important;
}
.card-title {
  font-size: 15px;
  font-weight: 600;
  color: #f8fafc;
  display: flex;
  align-items: center;
  gap: 8px;
}
.card-title-flex {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.icon-blue {
  color: #3b82f6;
}
.header-tag {
  margin-left: 8px;
}
.env-radio-group {
  display: flex;
}
.env-radio-group .el-radio-button {
  flex: 1;
}
.btn-deploy-main {
  background: linear-gradient(135deg, #2563eb, #1d4ed8) !important;
  border: none !important;
  font-weight: 600 !important;
  box-shadow: 0 4px 14px rgba(37, 99, 235, 0.4);
}
.terminal-card .el-card__header {
  padding: 10px 16px;
  background-color: #0f172a;
  border-bottom: 1px solid #334155;
}
.terminal-mac-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.mac-dots {
  display: flex;
  gap: 6px;
}
.mac-dots .dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
}
.mac-dots .dot.red { background-color: #ef4444; }
.mac-dots .dot.yellow { background-color: #f59e0b; }
.mac-dots .dot.green { background-color: #10b981; }
.terminal-title {
  font-size: 13px;
  color: #94a3b8;
  display: flex;
  align-items: center;
  gap: 6px;
  font-family: monospace;
}
.terminal-content {
  background-color: #020617;
  padding: 16px;
  border-radius: 8px;
  height: 380px;
  overflow-y: auto;
}
.log-text {
  font-family: 'JetBrains Mono', 'Fira Code', Consolas, monospace;
  font-size: 12px;
  color: #38bdf8;
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
  line-height: 1.6;
}
.custom-table {
  background-color: transparent !important;
}
.project-name {
  font-weight: 600;
  color: #f8fafc;
}
.repo-divider {
  margin: 0 4px;
  color: #64748b;
}
.repo-name {
  color: #cbd5e1;
}
.code-badge {
  background-color: #0f172a;
  border: 1px solid #334155;
  padding: 2px 6px;
  border-radius: 4px;
  color: #38bdf8;
  font-size: 12px;
}
</style>
