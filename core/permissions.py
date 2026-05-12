"""
权限控制模块

根据 config.yaml 中 permissions 配置段，
判断用户是否有权在指定项目/环境下执行发版操作。

配置示例：
  permissions:
    default_policy: "allow"          # 全局默认策略：allow / deny
    rules:
      - project: "示例项目A"
        env: "prod"
        policy: "deny"              # 该规则匹配后的默认策略
        allow_users: ["ou_xxxx"]    # 白名单
      - project: "*"
        env: "test"
        policy: "allow"             # test 环境所有人可操作
    approval_required:               # 需要审批的场景
      - project: "*"
        env: "prod"
        approvers: ["ou_approver1", "ou_approver2"]
"""
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("feishu_gitlab_card_http")


def _match_rule(rule: Dict[str, Any], project: str, env: str, repo: str) -> bool:
    """检查一条规则是否匹配当前项目、环境和仓库"""
    rule_project = rule.get("project", "*")
    rule_env = rule.get("env", "*")
    rule_repo = rule.get("repo", "*")
    
    project_match = rule_project == "*" or rule_project == project
    env_match = rule_env == "*" or rule_env == env
    repo_match = rule_repo == "*" or rule_repo == repo
    
    return project_match and env_match and repo_match


def check_permission(cfg: Dict[str, Any], project: str, env: str, repo: str, operator_open_id: str) -> Tuple[bool, str]:
    """
    检查用户是否有权限执行发版。

    Returns:
        (allowed: bool, reason: str)
    """
    permissions = cfg.get("permissions")
    if not permissions:
        return True, ""

    default_policy = permissions.get("default_policy", "allow").lower()
    rules: List[Dict[str, Any]] = permissions.get("rules", [])

    # 从后往前匹配，最后一条匹配的规则优先（更具体的规则应放在后面）
    matched_rule = None
    for rule in reversed(rules):
        if _match_rule(rule, project, env, repo):
            matched_rule = rule
            break

    if matched_rule is None:
        # 没有匹配的规则，使用全局默认策略
        if default_policy == "deny":
            return False, "默认策略：禁止操作。请联系管理员添加权限。"
        return True, ""

    rule_policy = matched_rule.get("policy", default_policy).lower()
    allow_users: List[str] = matched_rule.get("allow_users", [])
    deny_users: List[str] = matched_rule.get("deny_users", [])

    # 明确在拒绝名单中
    if operator_open_id in deny_users:
        return False, f"您不在 [{project}/{env}] 的发版授权名单中。"

    # 明确在允许名单中
    if operator_open_id in allow_users:
        return True, ""

    # 规则策略为 deny 且不在白名单中
    if rule_policy == "deny":
        return False, f"[{project}/{env}] 需要授权才能发版，您不在允许名单中。"

    # 规则策略为 allow
    return True, ""


def check_approval_required(cfg: Dict[str, Any], project: str, env: str, repo: str) -> Optional[Dict[str, List[str]]]:
    """
    检查是否需要审批。

    Returns:
        如需审批返回 dict 包含授权审批人和通知审批人，否则返回 None。
    """
    permissions = cfg.get("permissions")
    if not permissions:
        return None

    approval_rules: List[Dict[str, Any]] = permissions.get("approval_required", [])
    
    notify_approvers = None
    all_authorized_approvers = set()
    rule_matched = False

    for rule in reversed(approval_rules):
        if _match_rule(rule, project, env, repo):
            if "approvers" in rule:
                rule_approvers = rule.get("approvers", [])
                
                # 第一次匹配到的最具体的规则，决定了是否免审批以及需要 @ 谁
                if not rule_matched:
                    rule_matched = True
                    if not rule_approvers:
                        # 空列表代表完全免审批
                        return None
                    notify_approvers = list(rule_approvers)
                
                # 将所有匹配规则（包括更宽泛的兜底规则）中的审批人加入授权集合
                for a in rule_approvers:
                    all_authorized_approvers.add(a)

    if not rule_matched:
        return None
        
    return {
        "authorized_approvers": list(all_authorized_approvers),
        "notify_approvers": notify_approvers or []
    }


def is_admin(cfg: Dict[str, Any], operator_open_id: str) -> bool:
    """
    检查当前操作人是否是卡片管理员。
    管理员才能在卡片中执行项目管理等特殊操作。
    """
    permissions = cfg.get("permissions")
    if not permissions:
        return False
        
    admin_users = permissions.get("admin_users", [])
    return operator_open_id in admin_users
