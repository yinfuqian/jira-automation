"""env_lock 模块负责把技能运行参数锁定为服务端配置的唯一来源。

背景：技能脚本（jira.mjs / jira-cli.mjs 等）内置了「命令行入参 → 环境变量 → 令牌文件」的多级回落，
Codex 的 CODEX_HOME/config.toml 里也可能残留上一次写进去的模型与 MCP 配置。来源一多就会出现
「配置写着测试站、实际却把评论写到生产站」这类静默错配，排查成本很高。

因此由服务端在启动技能子进程前完成两件事：
1. 用配置里的权威值覆盖同名环境变量，并写入 SKILLRUN_ENV_LOCKED=1；
2. 技能脚本看到该标记后只认环境变量，不再回落到令牌文件与命令行入参（见各技能 SKILL.md）。

必填配置缺失时直接判定任务失败，而不是让技能拿着一份来源不明的参数继续跑。
"""
from __future__ import annotations

import logging


# 锁定标记环境变量：技能脚本读到 "1" 时只认环境变量，禁止回落到令牌文件与命令行入参。
LOCK_FLAG_ENV = "SKILLRUN_ENV_LOCKED"

# 需要锁定来源的运行参数：环境变量名 -> 配置键名（顺序即报错提示顺序）。
LOCKED_ENV_KEYS = {
    "JIRA_BASE_URL": "JIRA_BASE_URL",
    "JIRA_TOKEN": "JIRA_TOKEN",
    "DEVOPS_MCP_URL": "DEVOPS_MCP_URL",
    "DEVOPS_MCP_TOKEN": "DEVOPS_MCP_TOKEN",
    "CODEX_MODEL": "CODEX_MODEL",
    "CODEX_MODEL_PROVIDER": "CODEX_MODEL_PROVIDER",
    "CODEX_BASE_URL": "CODEX_BASE_URL",
}
# 流水线 MCP 专用参数：只注入明确需要流水线工具的技能，避免缺陷门禁等任务拿到无关凭据。
DEVOPS_MCP_ENV_KEYS = ("DEVOPS_MCP_URL", "DEVOPS_MCP_TOKEN")

# 必填项：缺失时任务直接失败。DEVOPS_MCP_TOKEN 不在其中——留空表示不注册流水线 MCP，
# 此时后端会清掉 config.toml 里的同名 server 表，技能同样拿不到其它来源的令牌。
REQUIRED_ENV_KEYS = (
    "JIRA_BASE_URL",
    "JIRA_TOKEN",
    "DEVOPS_MCP_URL",
    "CODEX_MODEL",
    "CODEX_MODEL_PROVIDER",
    "CODEX_BASE_URL",
)

# 各项配置的中文说明，用于生成可直接照做的报错提示。
ENV_KEY_NOTES = {
    "JIRA_BASE_URL": "技能访问的 Jira 站点地址，例如 https://jira.in.wezhuiyi.com",
    "JIRA_TOKEN": "上址站点的 Jira 个人访问令牌（PAT），必须是该站点签发的令牌",
    "DEVOPS_MCP_URL": "流水线 MCP 服务地址，热更新阶段使用",
    "DEVOPS_MCP_TOKEN": "流水线平台个人中心签发的 API Token；留空表示关闭热更新",
    "CODEX_MODEL": "技能执行使用的模型，例如 codex/deepseek-flash",
    "CODEX_MODEL_PROVIDER": "模型供应方名称，需与 CODEX_BASE_URL 配套",
    "CODEX_BASE_URL": "模型中转地址，例如 https://newapi.in.wezhuiyi.com/v1",
}


def _read(config, key: str) -> str:
    """从 Flask config 或普通字典中读取配置项，统一转成去掉首尾空白的字符串。"""
    if config is None:
        return ""
    getter = getattr(config, "get", None)
    value = getter(key) if callable(getter) else None
    return str(value or "").strip()


def collect_locked_env(config, include_devops_mcp: bool = True) -> dict:
    """收集注入技能子进程的权威运行参数，并附加锁定标记。

    include_devops_mcp=False 时不注入流水线 MCP 地址与令牌，实现按技能最小权限。
    """
    locked = {}
    for name, key in LOCKED_ENV_KEYS.items():
        if not include_devops_mcp and name in DEVOPS_MCP_ENV_KEYS:
            continue
        value = _read(config, key)
        if value:
            locked[name] = value
    locked[LOCK_FLAG_ENV] = "1"
    return locked


def missing_env_keys(config) -> list:
    """返回尚未配置（或配置为空）的必填项名称，全部配置齐全时返回空列表。"""
    return [name for name in REQUIRED_ENV_KEYS if not _read(config, LOCKED_ENV_KEYS[name])]


def build_missing_env_message(missing) -> str:
    """生成中文报错：说明缺哪些配置、去哪填、为什么不让技能自己回落。"""
    lines = ["技能运行参数未配置完整，已终止本次任务（技能不允许从令牌文件或命令行入参自行回落）："]
    for name in [str(item) for item in (missing or [])]:
        note = ENV_KEY_NOTES.get(name, "")
        lines.append(f"- {name}：{note}" if note else f"- {name}")
    lines.append("请在仓库根目录的 .env 补齐后重启 api 与 worker；这些值会作为唯一来源注入技能进程。")
    return "\n".join(lines)


def log_missing_env(missing) -> None:
    """把缺失项写入服务端日志，便于部署后第一时间发现配置问题。"""
    if missing:
        logging.error("技能运行参数缺失：%s", "、".join(str(item) for item in missing))
