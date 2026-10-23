"""codex_runner 模块负责以子进程方式调用 Codex CLI 执行技能，并解析事件流用于进度展示。"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field


# 容器内以非 root 会话运行 agent，需要放开沙箱才能执行 shell 与网络访问，隔离边界由容器提供。
DEFAULT_SANDBOX = "danger-full-access"
DEFAULT_API_KEY_ENV = "CODEX_SKILL_API_KEY"
DEFAULT_PROVIDER_NAME = "skillrun"
# 事件流与原始输出只保留最近若干条，避免长任务占用过多内存。
MAX_EVENTS = 200
MAX_TAIL_LINES = 200

# 网络类错误的文本特征：命中说明 Codex 连不上模型中转或 Jira。
NETWORK_ERROR_MARKERS = (
    "waiting for network",
    "connection failed",
    "error sending request",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "failed to connect",
    "connect timeout",
    "dns error",
    "dns resolution",
    "tls handshake",
)
# 连续出现多少次网络错误后判定为不可达。Codex 采用指数退避重连，
# 10 次约 15 分钟，届时仍未恢复基本可判定为网络不可达，
# 继续等待只会空转到 CODEX_SKILL_TIMEOUT，因此提前终止并给出可读原因。
DEFAULT_NETWORK_RETRY_LIMIT = 10
# 出现这些事件说明 Codex 仍在正常推进，可用于重置连续网络错误计数。
PROGRESS_EVENT_KINDS = ("session", "turn", "command", "tool", "file", "message")

DEFAULT_PROMPT_TEMPLATE = """请使用 {skill_id} 技能完成本次任务。

任务输入：
- jira 链接：{jira_url}
{inputs_lines}

执行要求：
1. 本次必须且只能使用技能 {skill_id}；禁止调用、切换到其它技能。即使运行环境里能看到其它技能，也不属于本任务。
2. 先完整阅读 {skill_dir}/SKILL.md（技能 {skill_id}）及其引用的参考资料，严格按其中的标准执行，不要自行发明判定规则。
3. 技能目录位于 {skill_dir}；当前运行环境是服务器容器，没有桌面工具，请直接通过 shell 执行技能自带脚本。
4. 技能要求的操作（例如写入 Jira 评论）必须真实完成，不要在本地生成报告文档代替。
5. 运行环境已预装技能所需工具与依赖（含 bsdtar / 7z / unzip 等解压命令、Python 与 Node 依赖），
   不要执行 apt-get、yum、apk、pip、npm 等安装命令；确实缺少工具时可以自行联网安装。
6. 技能要求的交付物（Jira 评论、状态流转、附件上传）都要真实完成；完成后用简洁中文总结：
   是否达标、难度等级、实际写入的内容、上传的附件名、关键依据。
7. 运行参数已由服务端锁定：JIRA_BASE_URL、JIRA_TOKEN、CODEX_MODEL、CODEX_MODEL_PROVIDER、
   CODEX_BASE_URL 一律直接使用当前进程的环境变量；仅流水线技能还会注入 DEVOPS_MCP_URL 与
   DEVOPS_MCP_TOKEN。不要读取技能目录或 ~/.codex 下的令牌文件，不要用 --base-url/--token 覆盖，
   也不要导出新的同名变量，或在工作区创建 .codex/config.toml 覆盖模型与中转设置
   （技能脚本检测到锁定标记后会直接拒绝这些来源）。"""


# 顶层技能统一的机器可读结果契约，避免各技能提示词遗漏统计入库所需的结论文件。
RESULT_FILE_INSTRUCTION = (
    "\n\n【机器可读结果】完成全部交付动作后、清理工作区之前，必须按 SKILL.md 的"
    "「写机器可读结果」章节，在当前任务根目录（即当前工作目录）写入 .skill-result.json。"
    "内容必须是 JSON 对象，至少包含：schemaVersion=1、skillId、issueKey、conclusion、summary、"
    "blockingCount；conclusion 使用技能文档规定的最终业务结论，无法判断时写「无法判定」，"
    "技能不适用时写「不适用」。不要伪造「通过」。该文件只用于调用统计，不写入 Jira，也不要放进 work/ 子目录。"
    "如果本技能调用了子技能，必须明确要求子技能不要写任务根目录的 .skill-result.json。"
)


@dataclass
class CodexEvent:
    """归一化后的 Codex 事件，用于任务进度与问题排查。"""

    kind: str
    text: str
    at: float

    def to_dict(self) -> dict:
        """转换为可序列化为 JSON 的字典。"""
        return {"kind": self.kind, "text": self.text, "at": self.at}


@dataclass
class CodexRunResult:
    """一次 Codex 技能执行的完整结果。"""

    exit_code: object = None
    timed_out: bool = False
    network_failed: bool = False
    last_message: str = ""
    session_id: object = None
    events: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    duration_ms: int = 0
    command: list = field(default_factory=list)
    tail_output: str = ""
    error: object = None

    def to_dict(self) -> dict:
        """转换为接口返回用的字典结构。"""
        return {
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "network_failed": self.network_failed,
            "last_message": self.last_message,
            "session_id": self.session_id,
            "events": self.events,
            "usage": self.usage,
            "duration_ms": self.duration_ms,
            "command": self.command,
            "error": self.error,
        }


class _SafeFormatDict(dict):
    """格式化提示词模板时容忍缺失占位符，缺失键保留原样而不是抛错。"""

    def __missing__(self, key):
        """返回未提供占位符的原始写法，避免模板缺参导致任务失败。"""
        return "{" + key + "}"


def normalize_base_url(url: str, default: str = "") -> str:
    """规范化模型中转地址，自动补齐 /v1 路径并去掉多余斜杠。"""
    text = str(url or "").strip() or str(default or "").strip()
    if not text:
        return ""
    text = text.rstrip("/")
    if not text.endswith("/v1"):
        text = f"{text}/v1"
    return text


def is_network_error(text) -> bool:
    """判断错误文本是否属于网络类问题，用于区分短暂抖动与真正的网络不可达。"""
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in NETWORK_ERROR_MARKERS)


def build_network_failure_message(count: int, sample: str = "") -> str:
    """构造网络不可达时的可读错误说明，直接给出排查方向。"""
    detail = ""
    if str(sample or "").strip():
        detail = f"；最近一次报错：{str(sample).strip()[:200]}"
    return (
        f"Codex 连续 {int(count)} 次网络错误，无法访问模型中转或 Jira，已提前终止本次任务{detail}。"
        "排查方向：确认 CODEX_BASE_URL 可达、运行进程可出网、代理与 DNS 正常；"
        "若从沙箱或离线环境启动，请改用正常网络环境重启 worker 后重试。"
    )


def build_skill_prompt(skill, inputs: dict, jira_url: str = "") -> str:
    """按技能自定义模板或默认模板生成交给 Codex 的任务提示词。"""
    normalized_inputs = {str(key): value for key, value in (inputs or {}).items()}
    if jira_url:
        normalized_inputs.setdefault("jira_url", jira_url)

    inputs_lines = "\n".join(
        f"- {key}：{value}" for key, value in normalized_inputs.items() if str(value or "").strip()
    )
    template = getattr(skill, "prompt_template", None) or DEFAULT_PROMPT_TEMPLATE
    values = _SafeFormatDict(
        skill_id=getattr(skill, "skill_id", ""),
        skill_name=getattr(skill, "name", ""),
        skill_dir=getattr(skill, "directory", ""),
        jira_url=jira_url or "",
        inputs_json=json.dumps(normalized_inputs, ensure_ascii=False),
        inputs_lines=inputs_lines,
    )
    try:
        prompt = template.format_map(values).strip()
    except (IndexError, ValueError):
        # 模板本身有语法问题时退回默认模板，保证任务仍可执行。
        prompt = DEFAULT_PROMPT_TEMPLATE.format_map(values).strip()
    if ".skill-result.json" not in prompt:
        prompt = f"{prompt}{RESULT_FILE_INSTRUCTION}"
    return prompt


def build_codex_command(
    *,
    codex_bin: str,
    workspace_dir: str,
    prompt: str,
    last_message_path: str,
    sandbox: str = DEFAULT_SANDBOX,
    model: str = "",
    model_provider: str = "",
    provider_base_url: str = "",
    provider_wire_api: str = "responses",
    provider_env_key: str = DEFAULT_API_KEY_ENV,
    reasoning_effort: str = "",
    ephemeral: bool = True,
    extra_args=(),
) -> list:
    """构造 codex exec 命令行参数，密钥通过环境变量注入而不出现在命令行中。"""
    command = [
        str(codex_bin or "codex"),
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        str(sandbox or DEFAULT_SANDBOX),
        "-C",
        str(workspace_dir),
        "--json",
        "-o",
        str(last_message_path),
    ]
    if ephemeral:
        command.append("--ephemeral")
    if model:
        command += ["-c", f"model={model}"]
    if reasoning_effort:
        command += ["-c", f"model_reasoning_effort={reasoning_effort}"]
    if model_provider:
        command += ["-c", f"model_provider={model_provider}"]
    if model_provider and provider_base_url:
        prefix = f"model_providers.{model_provider}"
        command += [
            "-c",
            f"{prefix}.name={model_provider}",
            "-c",
            f"{prefix}.base_url={provider_base_url}",
            "-c",
            f"{prefix}.wire_api={provider_wire_api}",
            "-c",
            f"{prefix}.env_key={provider_env_key}",
            "-c",
            f"{prefix}.requires_openai_auth=false",
        ]
    command += [str(item) for item in (extra_args or []) if str(item or "").strip()]
    command.append(prompt)
    return command


def build_codex_env(
    base_env: dict,
    api_key: str = "",
    api_key_env: str = DEFAULT_API_KEY_ENV,
    codex_home: str = "",
    locked_env: dict = None,
) -> dict:
    """基于当前进程环境构造 Codex 子进程环境变量。

    locked_env 是服务端配置解析出的权威运行参数（见 skillrun/env_lock.py）：最后写入，
    用于覆盖父进程或宿主环境里可能存在的同名变量，并带上锁定标记让技能脚本拒绝其它来源。
    """
    env = dict(base_env or os.environ)
    if codex_home:
        env["CODEX_HOME"] = codex_home
    if api_key:
        env[str(api_key_env or DEFAULT_API_KEY_ENV)] = api_key
    if locked_env:
        env.update({str(name): str(value) for name, value in locked_env.items()})
    return env


def redact_command(command, secrets=()) -> list:
    """把命令行参数中的密钥替换为占位符，便于日志与接口安全回显。"""
    hidden = [str(item) for item in (secrets or []) if str(item or "").strip()]
    redacted = []
    for item in command or []:
        text = str(item)
        for secret in hidden:
            if secret and secret in text:
                text = text.replace(secret, "***")
        redacted.append(text)
    return redacted


def summarize_event(payload: dict, at: float = None) -> CodexEvent:
    """把 Codex 的单条 JSONL 事件归一化为可读的进度事件。"""
    timestamp = time.time() if at is None else at
    event_type = str((payload or {}).get("type") or "")
    item = (payload or {}).get("item") if isinstance((payload or {}).get("item"), dict) else {}
    item_type = str(item.get("type") or "")

    if event_type == "thread.started":
        return CodexEvent("session", f"会话已创建：{payload.get('thread_id')}", timestamp)
    if event_type == "turn.started":
        return CodexEvent("turn", "模型开始处理任务", timestamp)
    if event_type == "error":
        return CodexEvent("error", str(payload.get("message") or "Codex 返回错误事件"), timestamp)
    if event_type == "turn.completed":
        return CodexEvent("turn", "本轮处理结束", timestamp)

    if item_type == "agent_message":
        text = str(item.get("text") or "").strip()
        return CodexEvent("message", text[:2000], timestamp)
    if item_type == "command_execution":
        command_text = str(item.get("command") or "").strip()
        status = str(item.get("status") or "")
        if event_type == "item.completed":
            exit_code = item.get("exit_code")
            return CodexEvent("command", f"命令结束（exit={exit_code}）：{command_text[:300]}", timestamp)
        return CodexEvent("command", f"执行命令{('：' + status) if status else ''}：{command_text[:300]}", timestamp)
    if item_type == "file_change":
        return CodexEvent("file", f"修改文件：{json.dumps(item.get('changes'), ensure_ascii=False)[:300]}", timestamp)
    if item_type == "mcp_tool_call":
        return CodexEvent("tool", f"调用工具：{item.get('tool') or item.get('server') or ''}", timestamp)
    if item_type == "error":
        # item 级错误（例如模型元数据缺失、上游报错）也要显式暴露，避免只留在 raw 事件里。
        return CodexEvent("error", str(item.get("message") or "Codex 返回错误事件")[:2000], timestamp)

    return CodexEvent("raw", json.dumps(payload, ensure_ascii=False)[:500], timestamp)


def _terminate_process(process) -> None:
    """强制结束 Codex 进程及其子进程，避免超时后残留执行体。"""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    # taskkill 或进程组信号被环境限制时，退回直接结束主进程。
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def run_codex_skill(
    command,
    *,
    cwd: str,
    env: dict,
    timeout_seconds: int,
    last_message_path: str,
    on_event=None,
    secrets=(),
    network_retry_limit: int = DEFAULT_NETWORK_RETRY_LIMIT,
) -> CodexRunResult:
    """执行 codex exec 并实时解析 JSONL 事件，返回归一化的执行结果。"""
    result = CodexRunResult(command=redact_command(command, secrets))
    started_at = time.time()
    events = deque(maxlen=MAX_EVENTS)
    tail_lines = deque(maxlen=MAX_TAIL_LINES)
    lock = threading.Lock()
    # 网络错误状态：连续计数达到阈值说明模型/Jira 不可达，继续等待只会空转。
    try:
        retry_limit = max(1, int(network_retry_limit))
    except (TypeError, ValueError):
        retry_limit = DEFAULT_NETWORK_RETRY_LIMIT
    network_state = {"count": 0, "sample": "", "aborted": False}

    popen_kwargs = {
        "cwd": cwd,
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    try:
        process = subprocess.Popen(command, **popen_kwargs)
    except (OSError, ValueError) as exc:
        result.duration_ms = int((time.time() - started_at) * 1000)
        result.error = f"无法启动 Codex 进程：{exc}"
        return result

    def consume_output():
        """在后台线程持续读取 Codex 的 JSONL 事件流。"""
        for raw_line in process.stdout:
            line = str(raw_line or "").strip()
            if not line:
                continue
            with lock:
                tail_lines.append(line)
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            event = summarize_event(payload)
            with lock:
                events.append(event)
                if event.kind == "session" and not result.session_id:
                    result.session_id = payload.get("thread_id")
                if payload.get("type") == "turn.completed" and isinstance(payload.get("usage"), dict):
                    result.usage = payload["usage"]
                should_abort = False
                if event.kind == "error" and is_network_error(event.text):
                    network_state["count"] += 1
                    network_state["sample"] = event.text
                    if network_state["count"] >= retry_limit and not network_state["aborted"]:
                        network_state["aborted"] = True
                        should_abort = True
                elif event.kind in PROGRESS_EVENT_KINDS or event.kind == "error":
                    network_state["count"] = 0
            if should_abort:
                _terminate_process(process)
            if on_event is not None:
                try:
                    on_event(event)
                except Exception:  # noqa: BLE001 - 进度回调异常不能中断任务执行
                    pass

    reader = threading.Thread(target=consume_output, name="codex-event-reader", daemon=True)
    reader.start()

    try:
        result.exit_code = process.wait(timeout=max(1, int(timeout_seconds)))
    except subprocess.TimeoutExpired:
        result.timed_out = True
        _terminate_process(process)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        result.error = f"技能执行超过 {int(timeout_seconds)} 秒，已强制终止"
    finally:
        # 读取线程是守护线程：残留的管道读取会随进程结束自动退出，这里只做有限等待。
        reader.join(timeout=5)
        # 仅在子进程已经结束时关闭管道，避免在进程仍存活时阻塞主线程。
        if process.poll() is not None and process.stdout:
            try:
                process.stdout.close()
            except OSError:
                pass

    result.duration_ms = int((time.time() - started_at) * 1000)
    with lock:
        result.events = [item.to_dict() for item in events]
        result.tail_output = "\n".join(tail_lines)
        network_aborted = network_state["aborted"]
        network_count = network_state["count"]
        network_sample = network_state["sample"]

    result.last_message = _read_last_message(last_message_path) or _last_agent_message(result.events)
    if network_aborted and not result.timed_out:
        result.network_failed = True
        result.error = build_network_failure_message(network_count, network_sample)
    if result.exit_code not in (0, None) and not result.error:
        result.error = f"Codex 退出码为 {result.exit_code}"
    return result


def _read_last_message(path: str) -> str:
    """读取 codex exec 写入的最终回复文件，读取失败时返回空字符串。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _last_agent_message(events) -> str:
    """在最终回复文件缺失时，退回事件流中的最后一条模型消息。"""
    for event in reversed(events or []):
        if isinstance(event, dict) and event.get("kind") == "message":
            return str(event.get("text") or "").strip()
    return ""
