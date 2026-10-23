"""tasks 模块负责技能执行的 Celery 异步任务编排与状态回写。"""
from __future__ import annotations

import logging
import os
import time

from flask import current_app
from sqlalchemy import text

from extensions import celery, db

from . import codex_runner, env_lock, mcp_config, store, usage
from .models.model import SkillRunRecord
from .registry import SkillError, resolve_skill


# 进度回写节流间隔，避免长任务频繁写库。
PROGRESS_FLUSH_SECONDS = 2.0
# 进度里保留的最近事件条数。
PROGRESS_EVENT_LIMIT = 30

# 事件类型到任务阶段的中文标签映射。
STAGE_LABELS = {
    "session": ("initializing", "已启动 Codex 会话"),
    "turn": ("reasoning", "模型推理中"),
    "command": ("executing", "执行技能脚本"),
    "tool": ("executing", "调用工具"),
    "file": ("writing", "写入文件"),
    "message": ("summarizing", "输出阶段结论"),
    "error": ("error", "Codex 返回错误"),
    "raw": ("running", "执行中"),
}


class _ProgressWriter:
    """在事件读取线程中按节流频率把 Codex 进度写回数据库与 Celery 状态。"""

    def __init__(self, engine, record_id: int, task, interval: float = PROGRESS_FLUSH_SECONDS):
        """记录写库所需的引擎、记录标识与 Celery 任务句柄。"""
        self._engine = engine
        self._record_id = record_id
        self._task = task
        self._interval = interval
        self._last_flush_at = 0.0
        self._recent = []

    def __call__(self, event) -> None:
        """处理单条 Codex 事件，必要时落库并刷新 Celery 进度。"""
        stage, label = STAGE_LABELS.get(getattr(event, "kind", "raw"), ("running", "执行中"))
        self._recent.append(event.to_dict())
        self._recent = self._recent[-PROGRESS_EVENT_LIMIT:]

        now = time.time()
        if getattr(event, "kind", "") != "error" and now - self._last_flush_at < self._interval:
            return
        self._last_flush_at = now
        self._persist(stage, label)

    def _persist(self, stage: str, label: str) -> None:
        """把当前阶段与最近事件写入数据库，并同步 Celery 任务状态。"""
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE skill_run_records SET stage = :stage, progress = :progress "
                        "WHERE id = :record_id"
                    ),
                    {
                        "stage": stage,
                        "progress": store.dump_json({"label": label, "events": self._recent}),
                        "record_id": self._record_id,
                    },
                )
        except Exception:  # noqa: BLE001 - 进度写入失败不能影响技能执行
            logging.warning("写入技能任务进度失败：record_id=%s", self._record_id, exc_info=True)

        try:
            self._task.update_state(
                state="PROGRESS",
                meta={"stage": stage, "label": label, "events": self._recent[-5:]},
            )
        except Exception:  # noqa: BLE001 - Celery 状态刷新失败同样不影响执行
            logging.debug("刷新 Celery 任务进度失败：record_id=%s", self._record_id, exc_info=True)


def _split_extra_args(value) -> list:
    """把配置中的附加命令行参数拆分为参数列表。"""
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item or "").strip()]
    return [item for item in str(value or "").split() if item]


@celery.task(bind=True, name="skillrun.run_skill")
def run_skill_task(self, record_id: int):
    """执行一次技能任务：准备独立工作目录、调用 Codex Agent，并回写状态与结果。"""
    record = db.session.get(SkillRunRecord, record_id)
    if record is None:
        logging.warning("技能任务记录不存在：record_id=%s", record_id)
        return {"record_id": record_id, "status": "missing"}

    config = current_app.config
    # 技能运行参数必须以服务端配置为唯一来源：缺失时直接失败，不做任何回落。
    missing_env = env_lock.missing_env_keys(config)
    if missing_env:
        env_lock.log_missing_env(missing_env)
        message = env_lock.build_missing_env_message(missing_env)
        store.mark_finished(
            record,
            SkillRunRecord.STATUS_FAILED,
            stage="env_not_configured",
            failure_kind=usage.FAILURE_ENV_MISSING,
            error_message=message,
        )
        return {
            "task_id": record.task_id,
            "skill_id": record.skill_id,
            "status": "failed",
            "error": message,
        }
    skills_dir = config.get("SKILLS_DIR")
    try:
        skill = resolve_skill(skills_dir, record.skill_id)
    except SkillError as exc:
        store.mark_finished(
            record,
            SkillRunRecord.STATUS_FAILED,
            stage="skill_unavailable",
            failure_kind=usage.FAILURE_SKILL_NOT_FOUND,
            error_message=str(exc),
        )
        return {"task_id": record.task_id, "skill_id": record.skill_id, "status": "failed", "error": str(exc)}

    workspace_root = config.get("SKILL_WORKSPACE_DIR") or os.path.join(os.path.sep, "tmp", "skill-workspace")
    workspace_dir = os.path.join(str(workspace_root), skill.skill_id, record.task_id)
    os.makedirs(workspace_dir, exist_ok=True)
    store.mark_running(record, workspace_dir)

    inputs = store.load_json(record.inputs, {})
    prompt = codex_runner.build_skill_prompt(skill, inputs, record.jira_url or "")
    last_message_path = os.path.join(workspace_dir, "last-message.md")

    api_key = str(config.get("CODEX_API_KEY") or "")
    api_key_env = str(config.get("CODEX_API_KEY_ENV") or codex_runner.DEFAULT_API_KEY_ENV)
    extra_args = _split_extra_args(config.get("CODEX_EXTRA_ARGS"))
    # 技能目录也交给 agent，便于技能脚本在自身目录内放置临时文件。
    extra_args += ["--add-dir", skill.directory]

    command = codex_runner.build_codex_command(
        codex_bin=str(config.get("CODEX_BIN") or "codex"),
        workspace_dir=workspace_dir,
        prompt=prompt,
        last_message_path=last_message_path,
        sandbox=str(config.get("CODEX_SANDBOX") or codex_runner.DEFAULT_SANDBOX),
        model=str(config.get("CODEX_MODEL") or ""),
        model_provider=str(config.get("CODEX_MODEL_PROVIDER") or ""),
        provider_base_url=str(config.get("CODEX_BASE_URL") or ""),
        provider_wire_api=str(config.get("CODEX_WIRE_API") or "responses"),
        provider_env_key=api_key_env,
        reasoning_effort=str(config.get("CODEX_REASONING_EFFORT") or ""),
        ephemeral=bool(config.get("CODEX_EPHEMERAL", True)),
        extra_args=extra_args,
    )
    codex_home = str(config.get("CODEX_HOME") or "")
    # 每个任务使用独立的 CODEX_HOME，避免共享目录里的 skills/ 被 Codex 自动发现。
    # 否则模型可能看到其它技能（jira-code、devops-mcp-invoker 等）而误调用；
    # 隔离后本任务只能使用提示词指定并挂载的技能目录。
    task_codex_home = os.path.join(codex_home, "runs", record.task_id) if codex_home else ""
    if task_codex_home:
        # Codex CLI 要求 CODEX_HOME 必须已存在且不会自动创建：容器内由命名卷提供，本地运行在这里兜底。
        try:
            os.makedirs(task_codex_home, exist_ok=True)
        except OSError as exc:
            logging.warning("无法创建 CODEX_HOME 目录 %s：%s", task_codex_home, exc)
    # 流水线 MCP 只注册给真正需要它的技能，避免缺陷门禁等任务看到并误用流水线工具。
    # 非流水线技能即使复用了任务目录，也会用空令牌清理掉可能残留的同名 server。
    allow_devops_mcp = mcp_config.supports_devops_mcp(skill.skill_id)
    if allow_devops_mcp:
        mcp_config.ensure_devops_mcp_config(
            task_codex_home,
            url=str(config.get("DEVOPS_MCP_URL") or ""),
            token=str(config.get("DEVOPS_MCP_TOKEN") or ""),
        )
    else:
        mcp_config.ensure_devops_mcp_config(task_codex_home, url="", token="")
    env = codex_runner.build_codex_env(
        os.environ,
        api_key=api_key,
        api_key_env=api_key_env,
        codex_home=task_codex_home or codex_home,
        locked_env=env_lock.collect_locked_env(config, include_devops_mcp=allow_devops_mcp),
    )

    # 未显式配置 CODEX_SKILL_TIMEOUT 时按技能自身 runtime.json 声明的超时执行（缺省 1800 秒）。
    timeout_seconds = int(config.get("CODEX_SKILL_TIMEOUT") or skill.timeout_seconds)
    network_retry_limit = int(
        config.get("CODEX_NETWORK_RETRY_LIMIT") or codex_runner.DEFAULT_NETWORK_RETRY_LIMIT
    )
    progress_writer = _ProgressWriter(db.engine, record.id, self)
    logging.info("开始执行技能任务：task_id=%s skill=%s", record.task_id, skill.skill_id)

    try:
        run_result = codex_runner.run_codex_skill(
            command,
            cwd=workspace_dir,
            env=env,
            timeout_seconds=timeout_seconds,
            last_message_path=last_message_path,
            on_event=progress_writer,
            secrets=[api_key],
            network_retry_limit=network_retry_limit,
        )
    except Exception as exc:  # noqa: BLE001 - 任务失败需要落库而不是静默丢失
        logging.exception("技能任务执行异常：task_id=%s", record.task_id)
        store.mark_finished(
            record,
            SkillRunRecord.STATUS_FAILED,
            stage="error",
            failure_kind=usage.FAILURE_CODEX_EXCEPTION,
            error_message=f"技能执行异常：{exc}",
        )
        return {"task_id": record.task_id, "skill_id": skill.skill_id, "status": "failed", "error": str(exc)}

    # 网络不可达优先于退出码判断：它说明 agent 根本没跑起来，而不是业务判定失败。
    failure_kind = None
    if run_result.network_failed:
        status, stage = SkillRunRecord.STATUS_FAILED, "network_unreachable"
        failure_kind = usage.FAILURE_NETWORK
    elif run_result.timed_out:
        status, stage = SkillRunRecord.STATUS_TIMEOUT, "timeout"
        failure_kind = usage.FAILURE_TIMEOUT
    elif run_result.exit_code == 0 and run_result.last_message:
        status, stage = SkillRunRecord.STATUS_SUCCEEDED, "finished"
    elif run_result.exit_code == 0:
        status, stage = SkillRunRecord.STATUS_FAILED, "failed"
        failure_kind = usage.FAILURE_NO_FINAL_MESSAGE
        run_result.error = run_result.error or "Codex 未返回最终结论"
    else:
        status, stage = SkillRunRecord.STATUS_FAILED, "failed"
        failure_kind = usage.FAILURE_NON_ZERO_EXIT

    store.mark_finished(
        record,
        status,
        failure_kind=failure_kind,
        stage=stage,
        result_text=run_result.last_message or None,
        error_message=run_result.error,
        codex_session_id=run_result.session_id,
        exit_code=run_result.exit_code if isinstance(run_result.exit_code, int) else None,
        duration_ms=run_result.duration_ms,
        progress=store.dump_json(
            {
                "label": "任务结束",
                "events": run_result.events[-PROGRESS_EVENT_LIMIT:],
                "usage": run_result.usage,
            }
        ),
    )
    logging.info(
        "技能任务结束：task_id=%s status=%s exit_code=%s duration_ms=%s",
        record.task_id,
        status,
        run_result.exit_code,
        run_result.duration_ms,
    )
    return {
        "task_id": record.task_id,
        "skill_id": skill.skill_id,
        "status": status,
        "result": run_result.last_message,
        "error": run_result.error,
        "exit_code": run_result.exit_code,
        "timed_out": run_result.timed_out,
        "network_failed": run_result.network_failed,
        "duration_ms": run_result.duration_ms,
        "workspace_dir": workspace_dir,
        "command": run_result.command,
    }
