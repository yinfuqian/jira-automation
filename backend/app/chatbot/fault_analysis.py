"""故障分析问答的项目识别、模块筛选与意图判断。

机器人收到问题后需要决定走「知识问答」还是「代码分析」：
- 只有明确的故障/异常类问题才进入代码分析，避免普通文档问答触发全量拉码；
- 项目优先沿用会话里已选中的项目，其次从问题中的项目名、模块名自动匹配；
- 代码分析只检出与问题相关的模块，控制拉取范围和响应时间。
"""
from __future__ import annotations

import re


# 明确表示“需要排查代码”的词。单独的“问题”“怎么办”不触发，避免把文档问答带进代码链路。
_STRONG_FAULT_TERMS = (
    "故障分析",
    "根因",
    "根本原因",
    "排查",
    "定位问题",
    "故障",
    "崩溃",
    "闪退",
    "卡死",
    "无响应",
    "不可用",
    "无法使用",
    "无法访问",
    "无法登录",
    "报错",
    "异常",
    "失败",
    "超时",
    "错误码",
    "堆栈",
    "traceback",
    "exception",
    "timeout",
    "deadlock",
    "oom",
    "bug",
)

# 需要与“现象词”组合才触发的弱信号，减少误判。
_SOFT_FAULT_TERMS = (
    "不生效",
    "没反应",
    "空白",
    "丢失",
    "重复",
    "乱码",
    "卡住",
    "为什么",
    "怎么回事",
    "怎么解决",
)

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._-]*|[㐀-鿿]{2,}", re.IGNORECASE)


def _normalize(value):
    """统一大小写和空白，便于中英文关键词匹配。"""
    return re.sub(r"\s+", "", str(value or "").lower())


def is_fault_analysis_question(question) -> bool:
    """判断问题是否需要进入代码故障分析链路。"""
    text = _normalize(question)
    if not text:
        return False
    if any(term in text for term in _STRONG_FAULT_TERMS):
        return True
    return any(term in text for term in _SOFT_FAULT_TERMS)


def _project_score(project, text):
    """按项目名、别名、描述和模块名计算匹配分。"""
    score = 0
    name = _normalize(project.name)
    key = _normalize(project.key)
    if name and name in text:
        score += 12
    if key and key in text:
        score += 10
    for alias in getattr(project, "aliases", ()) or ():
        value = _normalize(alias)
        if value and value in text:
            score += 9
    for module, _branch in project.module_items():
        value = _normalize(module)
        if value and value in text:
            score += 8
        elif len(value) >= 4:
            # 模块名里的英文片段也可作为弱信号，例如 aiforce-web → aiforce。
            for token in _TOKEN_RE.findall(value):
                if len(token) >= 4 and token in text:
                    score += 3
                    break
    description = _normalize(getattr(project, "description", ""))
    if description and description in text:
        score += 2
    return score


def rank_projects(settings, question):
    """返回按匹配度排序的项目列表；分数相同时保持配置顺序。"""
    text = _normalize(question)
    scored = []
    for position, project in enumerate(settings.projects):
        score = _project_score(project, text)
        if score > 0:
            scored.append((score, position, project))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [(score, project) for score, _position, project in scored]


def match_project(settings, question):
    """从问题中唯一匹配项目；没有匹配或最高分并列时返回 None。"""
    ranked = rank_projects(settings, question)
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[1][0] == ranked[0][0]:
        return None
    return ranked[0][1]


def select_modules(project, question, max_modules=4):
    """按问题中的模块/产品关键词选出需要检出的模块，保持配置顺序。"""
    items = project.module_items()
    if not items:
        return []
    text = _normalize(question)
    scored = []
    for position, (module, branch) in enumerate(items):
        value = _normalize(module)
        score = 0
        if value and value in text:
            score = 10
        else:
            tokens = [token for token in _TOKEN_RE.findall(value) if len(token) >= 4]
            if any(token in text for token in tokens):
                score = 6
        if score:
            scored.append((score, position, module, branch))

    if not scored:
        scored = [(0, position, module, branch) for position, (module, branch) in enumerate(items)]

    limit = max(1, int(max_modules or 4))
    scored.sort(key=lambda item: (-item[0], item[1]))
    selected = scored[: min(limit, len(scored))]
    selected.sort(key=lambda item: item[1])
    return [(module, branch) for _score, _position, module, branch in selected]
