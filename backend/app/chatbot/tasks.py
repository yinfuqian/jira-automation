"""tasks 模块定义机器人问答的 Celery 任务。

网关只负责「收消息 / 发消息」，耗时分析与代码检出都在这里执行，
结果通过 Redis 发布回网关，由网关负责按协议回推流式消息。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
from datetime import datetime

from flask import current_app
from celery.signals import worker_ready

import extensions
from extensions import celery, db

from . import answer, fault_analysis, fault_dialog, knowledge, knowledge_answer, repos
from .models.knowledge import ChatbotAnswerSource, KnowledgeSyncRun
from .models.model import ChatbotQuestion
from .settings import DEFAULT_REPLY_CHANNEL, ProjectDefinition, load_settings


logger = logging.getLogger(__name__)

# MySQL TEXT 列上限约 64 KB，留出余量按字节截断，避免长答案导致回写失败。
_DB_TEXT_LIMIT_BYTES = 60000


def _truncate_utf8(value, limit=_DB_TEXT_LIMIT_BYTES):
    """按 UTF-8 字节上限截断文本，保证不会切断多字节字符。"""
    text = str(value or "")
    if len(text.encode("utf-8")) <= limit:
        return text
    parts = []
    size = 0
    for char in text:
        char_size = len(char.encode("utf-8"))
        if size + char_size > limit:
            break
        parts.append(char)
        size += char_size
    return "".join(parts) + "\n\n（内容过长，已截断存储）"


def _split_extra_args(value):
    """把配置中的附加命令行参数拆分为参数列表。"""
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item or "").strip())
    return tuple(item for item in str(value or "").split() if item)


def _workspace_root(config, task_id):
    """返回本次问答的独立工作目录。"""
    root = str(config.get("SKILL_WORKSPACE_DIR") or os.path.join(os.sep, "tmp", "skill-workspace"))
    return os.path.join(root, "chatbot", task_id)


def _codex_options(config, settings, task_id):
    """从 Flask 配置提取 Codex 调用参数，并给每个任务独立的 CODEX_HOME。"""
    codex_home_root = str(config.get("CODEX_HOME") or "").strip()
    task_home = os.path.join(codex_home_root, "runs", f"chatbot-{task_id}") if codex_home_root else ""
    return answer.CodexOptions(
        codex_bin=str(config.get("CODEX_BIN") or "codex"),
        model=str(config.get("CODEX_MODEL") or ""),
        model_provider=str(config.get("CODEX_MODEL_PROVIDER") or ""),
        base_url=str(config.get("CODEX_BASE_URL") or ""),
        wire_api=str(config.get("CODEX_WIRE_API") or "responses"),
        api_key=str(config.get("CODEX_API_KEY") or ""),
        api_key_env=str(config.get("CODEX_API_KEY_ENV") or "CODEX_SKILL_API_KEY"),
        codex_home=task_home,
        sandbox=settings.answer.sandbox,
        ephemeral=bool(config.get("CODEX_EPHEMERAL", True)),
        reasoning_effort=settings.answer.reasoning_effort,
        timeout_seconds=settings.answer.timeout_seconds,
        extra_args=_split_extra_args(config.get("CODEX_EXTRA_ARGS")),
    )


def _upsert_record(payload, status="running"):
    """创建问答记录；同一 task_id 重复投递时复用已有行。"""
    task_id = str(payload.get("task_id") or "").strip()
    record = ChatbotQuestion.query.filter_by(task_id=task_id).first()
    if record is None:
        record = ChatbotQuestion(
            task_id=task_id,
            chatid=str(payload.get("chatid") or ""),
            chattype=str(payload.get("chattype") or ""),
            userid=str(payload.get("userid") or ""),
            project_key=str(payload.get("project_key") or ""),
            project_name=str(payload.get("project_name") or ""),
            question=str(payload.get("question") or ""),
            status=status,
        )
        db.session.add(record)
    else:
        record.status = status
    db.session.commit()
    return record


def _finish_record(record, status, result):
    """回写问答结果与耗时。"""
    record.status = status
    record.answer = _truncate_utf8(result.get("answer")) or None
    record.error = _truncate_utf8(result.get("error")) or None
    record.duration_ms = int(result.get("duration_ms") or 0)
    record.finished_at = datetime.utcnow()
    db.session.commit()


def publish_result(channel, payload):
    """把结果发布到 Redis 频道；网关据此回推流式消息。"""
    client = extensions.redis
    if client is None:
        raise RuntimeError("Redis 未初始化，无法回推问答结果")
    client.publish(channel, json.dumps(payload, ensure_ascii=False))


def _reply_payload(payload, result):
    """把问答结果整理成网关稳定消费的响应结构。"""
    return {
        "task_id": str(payload.get("task_id") or ""),
        "req_id": payload.get("req_id") or "",
        "stream_id": payload.get("stream_id") or "",
        "chatid": payload.get("chatid") or "",
        "chattype": payload.get("chattype") or "",
        "userid": payload.get("userid") or "",
        "project_key": payload.get("project_key") or "",
        "project_name": payload.get("project_name") or "",
        "started_at": payload.get("started_at") or 0,
        "ok": bool(result.get("ok")),
        "answer": result.get("answer") or "",
        "error": result.get("error") or "",
        "duration_ms": int(result.get("duration_ms") or 0),
        "usage": result.get("usage") or {},
    }


def _publish_reply(channel, reply):
    """发布回推消息；返回是否成功，避免回推异常覆盖真正业务错误。"""
    try:
        publish_result(channel, reply)
        return True
    except Exception:  # noqa: BLE001 - 回推失败时用户侧由网关超时兜底
        logger.exception("机器人问答结果回推失败：task_id=%s", reply.get("task_id"))
        return False


def _save_answer_sources(task_id, hits, answer_text):
    """保存本次回答实际引用的 Confluence 切片，便于审计与追责。"""
    if not task_id or not hits:
        return
    cited = {int(item) - 1 for item in re.findall(r"\[S(\d+)\]", str(answer_text or ""))}
    selected = [index for index in sorted(cited) if 0 <= index < len(hits)] or [0]
    ChatbotAnswerSource.query.filter_by(task_id=task_id).delete(synchronize_session=False)
    for index in selected:
        hit = hits[index]
        db.session.add(
            ChatbotAnswerSource(
                task_id=task_id,
                document_id=hit.document.id,
                chunk_id=hit.chunk.id,
                page_version=hit.document.current_version,
                rank=index + 1,
                score=float(hit.score or 0),
                cited=True,
            )
        )
    db.session.commit()


@celery.task(bind=True, name="knowledge.sync_confluence")
def knowledge_sync_task(self, payload=None):
    """同步全部 Confluence 页面或刷新单个页面；手动接口与 Beat 共用。"""
    payload = payload or {}
    try:
        settings = load_settings()
        if not settings.knowledge.enabled:
            return {"ok": False, "error": "知识库未启用"}
        if not settings.confluence.configured:
            return {"ok": False, "error": "Confluence 地址或凭据未配置"}
        page_id = str(payload.get("page_id") or "").strip()
        trigger_source = str(payload.get("trigger_source") or "scheduled")[:32]
        if page_id:
            result = knowledge.sync_page(page_id, settings, trigger_source=trigger_source)
            return {"ok": True, **result}
        return knowledge.sync_confluence(settings, trigger_source=trigger_source)
    except Exception as exc:  # noqa: BLE001 - 任务结果需要可观测，不能只在日志里丢失
        logger.exception("Confluence 知识同步任务失败")
        db.session.rollback()
        return {"ok": False, "error": str(exc)}


@celery.task(bind=True, name="chatbot.knowledge_answer")
def knowledge_answer_task(self, payload):
    """执行一次 Confluence 知识问答，并只回推一个最终结论。"""
    payload = payload or {}
    task_id = str(payload.get("task_id") or "").strip()
    if not task_id:
        logger.error("知识问答任务缺少 task_id，已丢弃：%s", payload)
        return {"ok": False, "error": "missing task_id"}

    config = current_app.config
    channel = DEFAULT_REPLY_CHANNEL
    try:
        settings = load_settings()
        channel = settings.gateway.reply_channel
    except Exception as exc:  # noqa: BLE001 - 配置异常也必须回推用户
        logger.exception("机器人知识库配置不可用：task_id=%s", task_id)
        result = {"ok": False, "answer": "", "error": f"机器人配置不可用：{exc}", "duration_ms": 0, "usage": {}}
        reply = _reply_payload(payload, result)
        _publish_reply(channel, reply)
        return {"task_id": task_id, "ok": False, "error": reply["error"]}

    record = None
    try:
        record = _upsert_record(payload)
    except Exception:  # noqa: BLE001 - 审计写不进去时仍然要回答用户
        logger.exception("知识问答记录创建失败：task_id=%s", task_id)
        db.session.rollback()

    project = settings.project_by_key(payload.get("project_key"))
    result = {"ok": False, "answer": "", "error": "", "duration_ms": 0, "usage": {}}
    workspace_root = _workspace_root(config, task_id)
    try:
        if not settings.knowledge.enabled:
            result["error"] = "知识库未启用"
        elif not settings.confluence.configured:
            result["error"] = "Confluence 地址或凭据未配置"
        else:
            question = payload.get("question")
            hits = knowledge.retrieve_hits(question, project, settings)
            imported_ids = []
            try:
                # 本地索引可能包含同产品但不解决问题的旧页面。在线搜索每次都
                # 补查一次，再与本地结果一起重排，避免“有命中就停止搜索”。
                imported_ids = knowledge.direct_confluence_search(question, settings)
            except Exception:  # noqa: BLE001 - 在线补查失败时仍可使用本地索引
                logger.warning("Confluence 在线补查失败，继续使用本地索引", exc_info=True)
                db.session.rollback()
            if imported_ids:
                online_hits = knowledge.retrieve_hits(
                    question,
                    project,
                    settings,
                    document_ids=imported_ids,
                )
                merged_by_document = {}
                for hit in list(online_hits or []) + list(hits or []):
                    document = getattr(hit, "document", None)
                    key = getattr(document, "id", id(document))
                    previous = merged_by_document.get(key)
                    hit_score = float(getattr(hit, "score", 0) or 0)
                    previous_score = float(getattr(previous, "score", 0) or 0)
                    if previous is None or hit_score > previous_score:
                        merged_by_document[key] = hit
                merged = sorted(
                    merged_by_document.values(),
                    key=lambda item: (
                        -float(getattr(item, "score", 0) or 0),
                        getattr(getattr(item, "document", None), "id", 0),
                    ),
                )
                max_hits = max(1, int(getattr(settings.knowledge, "max_chunks", 6) or 6))
                hits = merged[:max_hits]
            if hits:
                refreshed_ids = knowledge.refresh_hits_live(hits, settings)
                refreshed_hits = knowledge.retrieve_hits(
                    question,
                    project,
                    settings,
                    document_ids=refreshed_ids,
                )
                hits = refreshed_hits or hits
                answer_result = knowledge_answer.run_knowledge_answer(
                    question,
                    project,
                    hits,
                    _codex_options(config, settings, task_id),
                    workspace_root,
                    max_answer_chars=min(settings.answer.max_answer_chars, 800),
                    max_total_chars=settings.knowledge.max_total_chars,
                    require_citation=bool(payload.get("strict_citation")),
                )
                result = {
                    "ok": answer_result.ok,
                    "answer": answer_result.answer,
                    "error": answer_result.error,
                    "duration_ms": answer_result.duration_ms,
                    "usage": answer_result.usage,
                }
                if answer_result.ok:
                    try:
                        _save_answer_sources(task_id, hits, answer_result.answer)
                    except Exception:  # noqa: BLE001 - 来源审计失败不能吞掉正确回答
                        logger.exception("知识问答来源保存失败：task_id=%s", task_id)
                        db.session.rollback()
            else:
                result["error"] = "Confluence 中未找到能够回答该问题的依据"
    except Exception as exc:  # noqa: BLE001 - 任务失败也要回推给用户
        logger.exception("知识问答执行异常：task_id=%s", task_id)
        db.session.rollback()
        result = {
            "ok": False,
            "answer": "",
            "error": f"知识问答过程异常：{exc}",
            "duration_ms": result.get("duration_ms") or 0,
            "usage": {},
        }
    finally:
        try:
            shutil.rmtree(workspace_root, ignore_errors=True)
        except OSError:
            logger.debug("清理知识问答工作目录失败：%s", task_id, exc_info=True)

    if record is not None:
        try:
            _finish_record(record, "succeeded" if result.get("ok") else "failed", result)
        except Exception:  # noqa: BLE001 - 审计写入失败不能阻断用户拿到答案
            logger.exception("知识问答记录回写失败：task_id=%s", task_id)
            db.session.rollback()

    reply = _reply_payload(payload, result)
    _publish_reply(channel, reply)
    return {"task_id": task_id, "ok": reply["ok"], "error": reply["error"]}


def install_knowledge_startup_sync(app, celery_app):
    """知识库首次启动时投递一次同步；已有成功批次时保持安静。"""
    if getattr(celery_app, "_knowledge_startup_sync_installed", False):
        return False

    def on_worker_ready(**_kwargs):
        try:
            with app.app_context():
                settings = load_settings()
                if not settings.knowledge.enabled or not settings.confluence.configured:
                    return
                if KnowledgeSyncRun.query.filter_by(status="succeeded").first() is not None:
                    return
                client = extensions.redis
                if client is not None and not client.set(
                    "chatbot:knowledge:startup-sync", "1", nx=True, ex=3600
                ):
                    return
                knowledge_sync_task.apply_async(
                    args=[{"trigger_source": "startup"}],
                    queue="knowledge-sync",
                )
        except Exception:  # noqa: BLE001 - 启动同步不能阻止 worker 提供服务
            logger.warning("worker 启动时提交 Confluence 同步任务失败", exc_info=True)

    if worker_ready is not None:
        worker_ready.connect(on_worker_ready, weak=False)
    celery_app._knowledge_startup_sync_installed = True
    return True


def _normalize_code_targets(value):
    """校验网关传入的「模块 + 分支」列表，忽略缺项并去重。"""
    if not isinstance(value, (list, tuple)):
        return []
    targets = []
    seen = set()
    for item in value:
        if isinstance(item, dict):
            module = str(item.get("module") or "").strip()
            branch = str(item.get("branch") or "").strip()
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            module = str(item[0] or "").strip()
            branch = str(item[1] or "").strip()
        else:
            continue
        key = (module, branch)
        if not module or not branch or key in seen:
            continue
        seen.add(key)
        targets.append((module, branch))
    return targets


def _fault_background(question, project, settings):
    """为故障分析检索少量 Confluence 背景资料；失败时降级为纯代码分析。"""
    config = settings.fault_analysis
    if not config.include_knowledge:
        return ""
    if not settings.knowledge.enabled or not settings.confluence.configured:
        return ""
    try:
        hits = knowledge.retrieve_hits(
            question,
            project,
            settings,
            limit=config.knowledge_chunks,
        )
        if not hits:
            document_ids = knowledge.direct_confluence_search(question, settings)
            if document_ids:
                hits = knowledge.retrieve_hits(
                    question,
                    project,
                    settings,
                    document_ids=document_ids,
                    limit=config.knowledge_chunks,
                )
        return knowledge_answer.build_evidence(hits, max_total_chars=6000)
    except Exception:  # noqa: BLE001 - 背景资料失败不能阻断代码分析
        logger.warning("故障分析检索 Confluence 背景资料失败", exc_info=True)
        db.session.rollback()
        return ""


@celery.task(bind=True, name="chatbot.answer")
def answer_task(self, payload):
    """执行一次代码问答：检出代码 → Codex 分析 → 回写记录 → 通知网关。"""
    payload = payload or {}
    task_id = str(payload.get("task_id") or "").strip()
    if not task_id:
        logger.error("机器人问答任务缺少 task_id，已丢弃：%s", payload)
        return {"ok": False, "error": "missing task_id"}

    config = current_app.config
    try:
        settings = load_settings()
        channel = settings.gateway.reply_channel
    except Exception as exc:  # noqa: BLE001 - 配置读取的任何异常都要回推给用户
        # 配置异常无法分析，但仍要告诉用户原因，不能让流式消息一直悬着。
        logger.exception("机器人配置不可用：task_id=%s", task_id)
        reply = {
            "task_id": task_id,
            "req_id": payload.get("req_id") or "",
            "stream_id": payload.get("stream_id") or "",
            "chatid": payload.get("chatid") or "",
            "chattype": payload.get("chattype") or "",
            "started_at": payload.get("started_at") or 0,
            "ok": False,
            "answer": "",
            "error": f"机器人配置不可用：{exc}",
            "duration_ms": 0,
            "usage": {},
        }
        try:
            publish_result("chatbot:reply", reply)
        except Exception:  # noqa: BLE001 - 回推失败时用户侧由网关超时兜底
            logger.exception("机器人配置错误结果回推失败：task_id=%s", task_id)
        return {"task_id": task_id, "ok": False, "error": reply["error"]}

    record = None
    try:
        record = _upsert_record(payload)
    except Exception:  # noqa: BLE001 - 审计写不进去时仍然要回答用户
        logger.exception("机器人问答记录创建失败：task_id=%s", task_id)
        db.session.rollback()

    project = settings.project_by_key(payload.get("project_key"))
    code_targets = _normalize_code_targets(payload.get("code_targets"))
    if project is None and code_targets:
        # 没有项目/客户现场也能分析：仓库地址按模块全局解析，分支由用户给出。
        project = ProjectDefinition(
            key="module-targets",
            name="指定模块",
            modules={module: branch for module, branch in code_targets},
            description="用户直接指定的模块与分支",
        )
    result = {"ok": False, "answer": "", "error": "", "duration_ms": 0, "usage": {}}
    workspace = None
    try:
        if project is None:
            result["error"] = (
                "缺少可用的模块/分支信息，请补充模块名和代码分支后再分析"
            )
        elif not project.module_items():
            result["error"] = f"项目「{project.name}」没有配置任何模块分支"
        else:
            workspace_root = _workspace_root(config, task_id)
            lock_client = extensions.redis
            mode = str(payload.get("answer_mode") or "code").strip().lower()
            modules = None
            background = ""
            fault_context = payload.get("fault_context")
            fault_context_text = ""
            search_question = payload.get("question")
            if mode == "fault":
                if isinstance(fault_context, dict) and fault_context:
                    fault_context_text = fault_dialog.format_context(fault_context)
                    search_question = fault_dialog.analysis_query(fault_context)
                modules = code_targets or fault_analysis.select_modules(
                    project,
                    search_question,
                    settings.fault_analysis.max_modules,
                )
                background = _fault_background(search_question, project, settings)
                logger.info(
                    "故障分析范围：task_id=%s project=%s modules=%s turns=%s",
                    task_id,
                    project.key,
                    ",".join(module for module, _branch in modules),
                    (fault_context or {}).get("turns", 0)
                    if isinstance(fault_context, dict)
                    else 0,
                )
            elif mode == "code" and code_targets:
                # 没有项目时也按用户指定的「模块 + 分支」直接检出。
                modules = code_targets
            workspace = answer.prepare_workspace(
                project,
                workspace_root,
                settings,
                lock_client,
                modules=modules,
            )
            result = answer.run_answer(
                payload.get("question"),
                project,
                workspace,
                _codex_options(config, settings, task_id),
                max_answer_chars=settings.answer.max_answer_chars,
                mode=mode,
                background=background,
                fault_context=fault_context_text,
            )
    except Exception as exc:  # noqa: BLE001 - 任务失败也要回推给用户
        logger.exception("机器人问答执行异常：task_id=%s", task_id)
        result = {
            "ok": False,
            "answer": "",
            "error": f"分析过程异常：{exc}",
            "duration_ms": result.get("duration_ms") or 0,
            "usage": {},
        }
    finally:
        if workspace is not None:
            workspace.cleanup(settings)

    if record is not None:
        try:
            _finish_record(record, "succeeded" if result.get("ok") else "failed", result)
        except Exception:  # noqa: BLE001 - 审计写入失败不能阻断用户拿到答案
            logger.exception("机器人问答记录回写失败：task_id=%s", task_id)
            db.session.rollback()

    reply = {
        "task_id": task_id,
        "req_id": payload.get("req_id") or "",
        "stream_id": payload.get("stream_id") or "",
        "chatid": payload.get("chatid") or "",
        "chattype": payload.get("chattype") or "",
        "userid": payload.get("userid") or "",
        "project_key": payload.get("project_key") or "",
        "project_name": payload.get("project_name") or "",
        "started_at": payload.get("started_at") or 0,
        "ok": bool(result.get("ok")),
        "answer": result.get("answer") or "",
        "error": result.get("error") or "",
        "duration_ms": int(result.get("duration_ms") or 0),
        "usage": result.get("usage") or {},
    }
    try:
        publish_result(channel, reply)
    except Exception:  # noqa: BLE001 - 回推失败时用户侧由网关超时兜底
        logger.exception("机器人问答结果回推失败：task_id=%s", task_id)

    # 工作树已清理，这里再删任务级目录，避免磁盘无界增长。
    try:
        shutil.rmtree(_workspace_root(config, task_id), ignore_errors=True)
    except OSError:
        logger.debug("清理问答工作目录失败：%s", task_id, exc_info=True)

    return {"task_id": task_id, "ok": reply["ok"], "error": reply["error"]}
