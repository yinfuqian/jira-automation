"""mcp_config 模块负责把外部 MCP server 写进 Codex 的 config.toml，供热更新类技能调用流水线接口。

背景：`devops-mcp-invoker` 技能通过远端 MCP server（pipeline-integration-mcp）做模块热更新。
交互式会话里靠 `codex mcp add` 注册后重启会话生效；本服务是无头 `codex exec`，每次执行都是新进程，
因此由后端在启动技能前把配置写进 CODEX_HOME/config.toml，Codex 每次都会重新读取，无需“重启会话”。

写入范围被标记块限定：只替换本模块生成的块，文件里其它内容（用户或其他工具写的配置）保持原样。
未配置令牌时把托管块整段移除，避免留下半截配置导致 Codex 启动报错。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path


# 托管块的起止标记：块内内容全部由本模块生成，可以整段替换。
BLOCK_BEGIN = "# >>> skillrun devops-mcp（由后端生成，请勿手工编辑） >>>"
BLOCK_END = "# <<< skillrun devops-mcp <<<"
# 流水线 MCP server 的名称，与 devops-mcp-invoker 技能文档保持一致。
DEVOPS_MCP_SERVER_NAME = "pipeline-integration-mcp"
# 流水线平台约定的鉴权请求头。
DEVOPS_MCP_TOKEN_HEADER = "X-User-Tokens"
# 只有确实需要流水线工具的技能才注入 MCP；缺陷门禁等技能不应看到流水线工具。
DEVOPS_MCP_SKILL_IDS = frozenset({"jira-code", "devops-mcp-invoker"})


def supports_devops_mcp(skill_id: str) -> bool:
    """判断当前技能是否需要注册流水线 MCP server。"""
    return str(skill_id or "").strip() in DEVOPS_MCP_SKILL_IDS


def _toml_string(value: str) -> str:
    """把值渲染成 TOML 基本字符串，转义反斜杠与双引号。"""
    # 换行会破坏基本字符串，先去掉；令牌本身也不该包含空白。
    text = str(value or "").replace("\r", "").replace("\n", "")
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_mcp_block(server_name: str, url: str, token: str, header: str = DEVOPS_MCP_TOKEN_HEADER) -> str:
    """生成 MCP server 的托管配置块；token 为空时返回空串。"""
    if not str(token or "").strip():
        return ""
    lines = [
        BLOCK_BEGIN,
        f"[mcp_servers.{server_name}]",
        f"url = {_toml_string(url)}",
        # 用子表写请求头，与流水线平台文档给的手工配置写法一致，便于运维对照核对。
        f"[mcp_servers.{server_name}.http_headers]",
        # 令牌只写进 CODEX_HOME 卷内的配置文件，不进命令行、不进仓库、不出现在日志里。
        f"{header} = {_toml_string(token)}",
        BLOCK_END,
    ]
    return "\n".join(lines) + "\n"


def _strip_server_tables(text: str, server_name: str) -> str:
    """删掉文件里已存在的同名 MCP server 表（含子表）。

    平台文档允许用户手工往 config.toml 里加 `[mcp_servers.<name>.http_headers]`；如果不去重，
    我们追加的同名表会让整份配置变成非法 TOML（duplicate key），Codex 直接起不来。
    这里连同该表到下一个表头之间的内容一起移除——server 配置由本模块托管，不保留用户的同名表。
    """
    header_exact = f"[mcp_servers.{server_name}]"
    header_prefix = f"[mcp_servers.{server_name}."
    key_prefix = f"mcp_servers.{server_name}"
    kept: list = []
    dropping = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            # 表头行决定后续行的归属：命中同名表就整段丢弃，直到下一个表头。
            dropping = stripped == header_exact or stripped.startswith(header_prefix)
            if dropping:
                continue
        elif not dropping and stripped and stripped.split("=")[0].strip() == key_prefix:
            # 不带表头的点号赋值 mcp_servers.<name>.url = "..." 同样会造成重复键。
            continue
        if not dropping:
            kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    return ("\n".join(kept) + "\n") if kept else ""


def _strip_managed_block(text: str) -> str:
    """移除上一轮生成的托管块，保留文件里的其它内容。"""
    lines = str(text or "").splitlines()
    kept: list = []
    inside = False
    for line in lines:
        if line.strip() == BLOCK_BEGIN:
            inside = True
            continue
        if line.strip() == BLOCK_END:
            inside = False
            continue
        if not inside:
            kept.append(line)
    # 去掉块移除后遗留的多余空行，保持文件整洁。
    while kept and not kept[-1].strip():
        kept.pop()
    return ("\n".join(kept) + "\n") if kept else ""


def ensure_devops_mcp_config(codex_home, url: str = "", token: str = "", server_name: str = DEVOPS_MCP_SERVER_NAME) -> dict:
    """把流水线 MCP server 写进 CODEX_HOME/config.toml，返回本次处理结果（便于测试与排查）。

    返回值：{written, config_path, server_name, reason}
    - 未配置 codex_home 或令牌为空：不写入（令牌为空时同时清掉历史托管块与同名 server 表）。
    - 写入失败不抛出：热更新只是技能的一个阶段，不该因为配置文件问题让整个任务崩掉。
    """
    home = str(codex_home or "").strip()
    if not home:
        return {"written": False, "config_path": None, "server_name": server_name, "reason": "codex-home-missing"}

    config_path = Path(home) / "config.toml"
    try:
        existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    except OSError as exc:
        logging.warning("读取 Codex 配置失败，跳过 MCP 注册：%s", exc)
        return {"written": False, "config_path": str(config_path), "server_name": server_name, "reason": "read-failed"}

    # 先摘掉托管块，再清掉同名表（含用户按平台文档手工加的），避免出现重复表导致配置非法。
    body = _strip_server_tables(_strip_managed_block(existing), server_name)
    block = build_mcp_block(server_name, url, token)
    if not block:
        # 令牌被撤掉时清理托管块与同名 server 表，避免残留一个连不通的 server 拖慢 Codex 启动。
        if body != existing:
            try:
                config_path.write_text(body, encoding="utf-8")
            except OSError as exc:
                logging.warning("清理 Codex 配置中的 MCP 块失败：%s", exc)
                return {"written": False, "config_path": str(config_path), "server_name": server_name, "reason": "write-failed"}
            return {"written": False, "config_path": str(config_path), "server_name": server_name, "reason": "token-missing-block-removed"}
        return {"written": False, "config_path": str(config_path), "server_name": server_name, "reason": "token-missing"}

    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(body + block, encoding="utf-8")
    except OSError as exc:
        logging.warning("写入 Codex MCP 配置失败：%s", exc)
        return {"written": False, "config_path": str(config_path), "server_name": server_name, "reason": "write-failed"}

    logging.info("已注册流水线 MCP server：%s -> %s", server_name, url)
    return {"written": True, "config_path": str(config_path), "server_name": server_name, "reason": "ok", "url": url}
