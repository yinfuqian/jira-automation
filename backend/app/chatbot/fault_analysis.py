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
    "生产问题",
    "线上问题",
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

# 明确指向代码实现的问题。知识库可用时，这些问法仍保留原来的代码分析链路。
_CODE_TERMS = (
    "代码",
    "源码",
    "实现",
    "接口",
    "回调",
    "方法",
    "函数",
    "类名",
    "模块",
    "入口",
    "调用",
    "数据库",
    "表结构",
    "字段",
    "sql",
    "api",
    "controller",
    "service",
    "mapper",
    "repository",
    "逻辑",
    "链路",
    "依赖",
)

# 明确在要「资料 / 方案 / 操作步骤」的问法。它们先走 Confluence，
# 不能因为会话里残留故障追问或问题里出现“迁移/部署”就被当成现场故障。
_DOCUMENT_TERMS = (
    "文档",
    "方案",
    "指引",
    "手册",
    "说明",
    "教程",
    "步骤",
    "怎么配置",
    "如何配置",
    "怎么部署",
    "如何部署",
    "怎么迁移",
    "如何迁移",
    "怎么升级",
    "如何升级",
    "怎么做",
    "如何做",
    "迁移方案",
    "部署方案",
    "升级方案",
)
# 这些是现场证据，出现时即使同时提到“方案”，也优先按故障分析处理。
_FAULT_EVIDENCE_TERMS = (
    "报错",
    "异常",
    "失败",
    "超时",
    "崩溃",
    "闪退",
    "卡死",
    "无响应",
    "不可用",
    "错误码",
    "堆栈",
    "traceback",
    "exception",
    "timeout",
)

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._-]*|[㐀-鿿]{2,}", re.IGNORECASE)

# 代码分支既可能是 release/3.1-zszq，也可能是 master 这类短名。
_BRANCH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:release|hotfix|feature|bugfix|develop)/[A-Za-z0-9._/-]+",
    re.IGNORECASE,
)
_PLAIN_BRANCH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:master|main|develop|dev|test|uat|pre)(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)
_VERSION_RE = re.compile(r"(?<![A-Za-z0-9_.-])v?\d+\.\d+(?:\.\d+)*", re.IGNORECASE)


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


def is_code_question(question) -> bool:
    """判断问题是否明确要求查看代码实现。"""
    text = _normalize(question)
    return bool(text) and any(term in text for term in _CODE_TERMS)


def is_document_question(question) -> bool:
    """判断是否在索取文档、方案或操作步骤；此类问题优先查知识库。"""
    text = _normalize(question)
    if not text:
        return False
    return any(term in text for term in _DOCUMENT_TERMS)


def has_fault_evidence(question) -> bool:
    """判断描述里是否包含具体故障证据，用于避免把文档请求误判成故障。"""
    text = _normalize(question)
    return bool(text) and any(term in text for term in _FAULT_EVIDENCE_TERMS)


def classify_question(question, knowledge_ready=False, knowledge_first=True) -> str:
    """把问题分为 fault、knowledge、code 三种路由。

    知识库未就绪时完全沿用旧的代码问答路径；知识库就绪时，明确的代码问题
    仍走代码链路，其余普通问题优先查 Confluence，避免破坏原有代码问答能力。
    """
    document_request = is_document_question(question) and not has_fault_evidence(question)
    if is_fault_analysis_question(question) and not document_request:
        return "fault"
    if knowledge_ready and knowledge_first and (
        document_request or not is_code_question(question)
    ):
        return "knowledge"
    return "code"


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
    # 产品名本身就是最稳定的定位词，例如“Bot3.2”里的 Bot。只配产品别名时，
    # 用户带版本号提问会因为别名不完整而落到产品菜单，这里补上产品级匹配。
    product_name = _normalize(getattr(project, "product_name", ""))
    if product_name and product_name in text:
        score += 9
    product_key = _normalize(getattr(project, "product_key", ""))
    if product_key and product_key in text:
        score += 8
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


def _product_score(product, text):
    """按产品名、标识和别名计算产品级匹配分。"""
    score = 0
    name = _normalize(getattr(product, "name", ""))
    key = _normalize(getattr(product, "key", ""))
    if name and name in text:
        score += 12
    if key and key in text:
        score += 10
    for alias in getattr(product, "aliases", ()) or ():
        value = _normalize(alias)
        if value and value in text:
            score += 9
    return score


def rank_products(settings, question):
    """返回按匹配度排序的产品列表，用于在项目并列时判断所属产品。"""
    text = _normalize(question)
    scored = []
    for position, product in enumerate(getattr(settings, "products", ()) or ()):
        score = _product_score(product, text)
        if score > 0:
            scored.append((score, position, product))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [(score, product) for score, _position, product in scored]


def match_product(settings, question):
    """从问题中唯一匹配产品；没有匹配或最高分并列时返回 None。"""
    ranked = rank_products(settings, question)
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[1][0] == ranked[0][0]:
        return None
    return ranked[0][1]


def _contains_module(text, module):
    """按模块名边界匹配，避免 aiforce2 命中 aiforce22 之类误判。"""
    pattern = r"(?<![A-Za-z0-9_.-])" + re.escape(module) + r"(?![A-Za-z0-9_.-])"
    return bool(re.search(pattern, text, re.IGNORECASE))


def _clean_branch(value):
    """去掉分支后面常见的标点，保留 git 分支本身的斜杠。"""
    return str(value or "").strip().rstrip("./,，。；;、)）]")


def _unique_branch_for_version(settings, module, version):
    """按模块和版本在现有项目分支里反查；唯一时才返回分支名。"""
    wanted = _normalize(version).lstrip("v")
    candidates = set()
    if not wanted:
        return ""
    for project in getattr(settings, "projects", ()) or ():
        branches = dict(project.module_items())
        branch = str(branches.get(module) or "").strip()
        if branch and wanted in _normalize(branch).lstrip("v"):
            candidates.add(branch)
    return next(iter(candidates)) if len(candidates) == 1 else ""


def parse_code_targets(settings, text):
    """从自然语言里解析「模块 + 分支」；无法完整解析的分支留空。

    代码检出只依赖模块名与分支，不依赖项目/客户现场。用户可以直接回复
    `yicall-common release/3.1`，也可以只回复模块名，由网关继续追问分支。
    """
    raw = str(text or "")
    if not raw.strip():
        return []
    modules = [
        name
        for name in getattr(settings.repos, "module_repo_urls", {}) or {}
        if _contains_module(raw, name)
    ]
    if not modules:
        return []
    modules = sorted(set(modules), key=len, reverse=True)

    branches = []
    for match in _BRANCH_RE.findall(raw):
        branch = _clean_branch(match)
        if branch and branch not in branches:
            branches.append(branch)
    if not branches:
        for match in _PLAIN_BRANCH_RE.findall(raw):
            branch = _clean_branch(match)
            if branch and branch not in branches:
                branches.append(branch)

    versions = []
    for match in _VERSION_RE.findall(raw):
        version = _clean_branch(match)
        if version and version not in versions:
            versions.append(version)

    targets = []
    for index, module in enumerate(modules):
        branch = ""
        if len(branches) == 1:
            branch = branches[0]
        elif len(branches) == len(modules):
            branch = branches[index]
        elif len(versions) == 1:
            branch = _unique_branch_for_version(settings, module, versions[0])
        elif len(versions) == len(modules):
            branch = _unique_branch_for_version(settings, module, versions[index])
        targets.append({"module": module, "branch": branch})
    return targets


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
