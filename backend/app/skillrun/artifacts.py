"""artifacts 模块负责列出技能工作目录里的交付物，并把请求里的文件名安全地解析为真实文件路径。

技能以任务工作目录为 cwd 运行，交付物固定落在工作目录下的约定子目录里：

- ``outputs/``：打包类交付物，例如 ``jira-gate-1-mock`` 的 ``<KEY>-G1预检包-<时间戳>.zip``；
- ``reports/``：报告类交付物，例如 ``review-jira-songlizhi`` 的 ``review-report-<时间戳>.md``。

工作目录里的 ``work/`` 是解析附件用的临时目录，不是交付物，这里不列也不允许下载。
本模块只读文件系统，不接受任何外部拼接的目录。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from urllib.parse import quote


# 交付物所在的子目录，按查找优先级排列；同名文件以靠前的目录为准。
ARTIFACT_SUBDIRS = ("outputs", "reports")


class ArtifactError(Exception):
    """交付物不存在或请求的文件名不合法时抛出。"""

    def __init__(self, message: str, reason: str = "not-found"):
        """记录错误信息与机器可读原因，供接口据此选择状态码。"""
        super().__init__(message)
        self.message = message
        self.reason = reason


def _artifact_roots(workspace_dir) -> list:
    """返回任务工作目录下实际存在的交付物目录（真实路径，已解析符号链接）。"""
    workspace = str(workspace_dir or "").strip()
    if not workspace or not os.path.isdir(workspace):
        return []
    roots = []
    for name in ARTIFACT_SUBDIRS:
        root = os.path.join(workspace, name)
        if os.path.isdir(root):
            roots.append(os.path.realpath(root))
    return roots


def _format_mtime(timestamp: float) -> str:
    """把文件修改时间格式化为与任务字段一致的 UTC ISO 字符串。"""
    moment = datetime.fromtimestamp(timestamp, tz=timezone.utc).replace(microsecond=0)
    return moment.isoformat().replace("+00:00", "Z")


def list_artifacts(workspace_dir) -> list:
    """列出交付物文件，按名称排序。

    只扫交付物目录的一层，不下钻子目录：``reports/.work/`` 这类中间草稿含需求原文，
    不属于交付物。隐藏文件与符号链接同样跳过。
    """
    items = []
    seen = set()
    for root in _artifact_roots(workspace_dir):
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for entry in sorted(entries, key=lambda item: item.name):
            if entry.name.startswith(".") or entry.name in seen:
                continue
            try:
                if entry.is_symlink() or not entry.is_file():
                    continue
                stat = entry.stat()
            except OSError:
                continue
            seen.add(entry.name)
            items.append(
                {
                    "name": entry.name,
                    "size": stat.st_size,
                    "modified_at": _format_mtime(stat.st_mtime),
                }
            )
    return items


def list_artifacts_with_urls(task_id: str, workspace_dir, ticket_signer=None) -> list:
    """在交付物清单上补出下载地址，供 ``/skill/task/<task_id>`` 直接回给调用方。

    ``ticket_signer`` 传入 ``(task_id, 文件名) -> 票据`` 时，地址会带上短时效签名票据，
    浏览器可以直接打开这个地址下载；未传时地址仍可用 ``X-API-Token`` 请求头下载。
    """
    items = list_artifacts(workspace_dir)
    for item in items:
        # 文件名按 URL 编码：中文原名仍保留在 name 字段里，地址里用编码形式。
        url = f"/skill/task/{quote(str(task_id), safe='')}/artifacts/{quote(item['name'], safe='')}"
        ticket = ticket_signer(str(task_id), item["name"]) if ticket_signer else None
        if ticket:
            url = f"{url}?ticket={quote(str(ticket), safe='')}"
        item["download_url"] = url
    return items


def resolve_artifact(workspace_dir, filename) -> str:
    """把请求里的文件名解析为工作目录内交付物的真实路径。

    只接受交付物目录下已存在的文件名：拒绝任何分隔符、``..``、隐藏文件与符号链接，
    并在解析后复核父目录仍是交付物目录，确保请求无法越出工作区。
    """
    name = str(filename or "").strip()
    if not name or name.startswith(".") or os.path.basename(name) != name:
        raise ArtifactError("文件名不合法：只允许交付物目录下的文件名", "invalid-name")

    available = {item["name"] for item in list_artifacts(workspace_dir)}
    if name not in available:
        raise ArtifactError(f"交付物不存在：{name}", "not-found")

    for root in _artifact_roots(workspace_dir):
        candidate = os.path.join(root, name)
        if os.path.islink(candidate) or not os.path.isfile(candidate):
            continue
        resolved = os.path.realpath(candidate)
        if os.path.dirname(resolved) == root:
            return resolved
    raise ArtifactError(f"交付物不存在：{name}", "not-found")
