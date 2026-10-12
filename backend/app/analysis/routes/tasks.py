"""tasks 模块负责本文件相关的业务流程、数据转换与依赖协作。"""
import hashlib
import logging
import os
import shutil

from extensions import celery
from app.config import DEFAULT_REPO_CACHE_DIR


def report_progress(task, stage, stage_label, stage_percent, percent, message=None):
    """处理 report_progress 对应的业务步骤，并向调用方返回所需结果。"""
    meta = {
        "stage": stage,
        "stage_label": stage_label,
        "stage_percent": int(stage_percent),
        "percent": int(percent),
        "message": message or f"{stage_label} {int(stage_percent)}%",
    }
    task.update_state(state="PROGRESS", meta=meta)
    return meta


def build_analysis_evidence(
    error_info,
    resolved_errors,
    code_snippets,
    log_error_events=None,
    grouped_log_errors=None,
    detected_languages=None,
    ai_call_count=0,
    repositories=None,
    common_module_references=None,
    auto_pulled_common_modules=None,
    common_pull_skipped=None,
):
    """构建并返回 build_analysis_evidence 对应的业务数据，保持现有调用约定。"""
    snippet_files = []
    module_counts = {}
    for snippet in code_snippets or []:
        file_path = snippet.get("file") if isinstance(snippet, dict) else None
        if file_path and file_path not in snippet_files:
            snippet_files.append(file_path)
        if not isinstance(snippet, dict):
            continue
        role = str(snippet.get("module_role") or "primary")
        name = str(snippet.get("module_name") or "primary")
        module_counts[(role, name)] = module_counts.get((role, name), 0) + 1

    code_context_modules = [
        {"role": role, "moduleName": name, "snippetCount": count}
        for (role, name), count in module_counts.items()
    ]
    selected_code_repositories = [
        {
            "role": repository.get("role") or "related",
            "moduleId": repository.get("moduleId"),
            "moduleName": repository.get("moduleName"),
            "branchAddress": repository.get("branchAddress"),
            "tagVersion": repository.get("tagVersion"),
            "cloned": bool(repository.get("repo_path")),
            "common": bool(repository.get("common")),
            "metadataOnly": bool(repository.get("metadataOnly")),
            "autoPulled": bool(repository.get("autoPulled")),
        }
        for repository in (repositories or [])
        if isinstance(repository, dict)
    ]
    related_code_modules = [
        item for item in code_context_modules if item.get("role") != "primary"
    ]

    return {
        "used_code_context": bool(code_snippets),
        "used_related_code_context": bool(related_code_modules),
        "related_code_module_count": len(related_code_modules),
        "code_context_modules": code_context_modules,
        "selected_code_repositories": selected_code_repositories,
        "common_module_reference_count": len(common_module_references or []),
        "common_module_references": common_module_references or [],
        "auto_pulled_common_module_count": len(auto_pulled_common_modules or []),
        "auto_pulled_common_modules": auto_pulled_common_modules or [],
        "common_pull_skipped_count": len(common_pull_skipped or []),
        "common_pull_skipped": common_pull_skipped or [],
        "log_error_event_count": len(log_error_events or []),
        "log_error_events": log_error_events or [],
        "grouped_issue_count": len(grouped_log_errors or []),
        "grouped_log_errors": grouped_log_errors or [],
        "detected_languages": list(detected_languages or []),
        "ai_call_count": int(ai_call_count or 0),
        "error_info_count": len(error_info or []),
        "resolved_file_count": len(resolved_errors or []),
        "code_snippet_count": len(code_snippets or []),
        "code_snippet_files": snippet_files,
    }


def merge_component_candidates(*groups):
    """合并整理并返回 merge_component_candidates 对应的业务数据，保持现有调用约定。"""
    items = []
    for group in groups:
        for item in group or []:
            normalized = str(item or "").strip().lower()
            if normalized and normalized not in items:
                items.append(normalized)
    return items


def should_use_knowledge_cache(source_type):
    """判断 should_use_knowledge_cache 对应的业务数据，保持现有调用约定。"""
    return str(source_type or "file").strip().lower() != "image"


def _normalize_task_image_ocr(payload):
    """规范化并返回 _normalize_task_image_ocr 对应的业务数据，保持现有调用约定。"""
    data = payload if isinstance(payload, dict) else {}
    try:
        average_confidence = float(data.get("average_confidence") or 0.0)
    except (TypeError, ValueError):
        average_confidence = 0.0
    return {
        "available": bool(data.get("available")),
        "engine": str(data.get("engine") or "paddleocr"),
        "extracted_text": str(data.get("extracted_text") or "").strip(),
        "lines": [item for item in (data.get("lines") or []) if isinstance(item, dict)],
        "average_confidence": average_confidence,
        "warnings": [str(item) for item in (data.get("warnings") or []) if str(item or "").strip()],
        "summary": str(data.get("summary") or "").strip(),
        "keywords": [str(item).strip() for item in (data.get("keywords") or []) if str(item).strip()],
        "components": [str(item).strip() for item in (data.get("components") or []) if str(item).strip()],
        "analysis_text": str(data.get("analysis_text") or "").strip(),
        "scene_summary": str(data.get("scene_summary") or "").strip(),
        "business_domain": str(data.get("business_domain") or "").strip(),
        "page_name": str(data.get("page_name") or "").strip(),
        "user_action": str(data.get("user_action") or "").strip(),
        "error_message": str(data.get("error_message") or "").strip(),
        "visible_fields": [str(item).strip() for item in (data.get("visible_fields") or []) if str(item).strip()],
        "candidate_apis": [str(item).strip() for item in (data.get("candidate_apis") or []) if str(item).strip()],
        "candidate_code_keywords": [str(item).strip() for item in (data.get("candidate_code_keywords") or []) if str(item).strip()],
        "missing_context": [item for item in (data.get("missing_context") or []) if isinstance(item, dict)],
    }


def _task_gpt_fallback_confidence_threshold():
    configured = os.getenv("OCR_GPT_FALLBACK_CONFIDENCE", "0.6")
    try:
        return float(configured)
    except (TypeError, ValueError):
        return 0.6


def _task_should_use_gpt_vision_fallback(image_ocr):
    data = image_ocr if isinstance(image_ocr, dict) else {}
    if str(data.get("engine") or "").strip().lower() == "gpt-vision":
        return False
    enabled = str(os.getenv("LOCAL_OCR_ENABLED", "false")).strip().lower() in {
        "1", "true", "yes", "on",
    }
    if not enabled:
        return True
    if not data.get("available"):
        return True
    if not str(data.get("extracted_text") or "").strip():
        return True
    try:
        confidence = float(data.get("average_confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return confidence < _task_gpt_fallback_confidence_threshold()


def _task_build_gpt_image_ocr_result(image_paths, image_tag, image_description="", image_fallback_extractor=None):
    image_paths = [path for path in (image_paths or []) if path]
    if not image_paths:
        return {
            "available": False,
            "engine": "gpt-vision",
            "extracted_text": "",
            "lines": [],
            "average_confidence": 0.0,
            "warnings": ["no image files available for GPT vision fallback"],
        }
    try:
        fallback_extractor = image_fallback_extractor
        if fallback_extractor is None:
            import importlib

            routes_module = importlib.import_module("app.analysis.routes.routes")
            fallback_extractor = getattr(routes_module, "analyze" + "_uploaded_image")
        image_analysis = fallback_extractor(image_paths, image_tag, image_description)
        extracted_text = str(image_analysis.get("extracted_text") or image_analysis.get("summary") or "").strip()
        result = {
            "available": bool(extracted_text),
            "engine": "gpt-vision",
            "extracted_text": extracted_text,
            "lines": [],
            "average_confidence": 0.0,
            "warnings": [],
            "summary": str(image_analysis.get("summary") or "").strip(),
            "keywords": [str(item).strip() for item in (image_analysis.get("keywords") or []) if str(item).strip()],
            "components": [str(item).strip() for item in (image_analysis.get("components") or []) if str(item).strip()],
            "analysis_text": str(image_analysis.get("analysis_text") or "").strip(),
        }
        if not result["available"]:
            result["warnings"].append("GPT vision fallback returned no usable text")
        return result
    except Exception as exc:
        logging.exception("GPT vision fallback failed in task pipeline")
        return {
            "available": False,
            "engine": "gpt-vision",
            "extracted_text": "",
            "lines": [],
            "average_confidence": 0.0,
            "warnings": [str(exc)],
        }


def build_image_ocr_log_content(data, input_paths, ocr_extractor=None, image_fallback_extractor=None):
    """构建并返回 build_image_ocr_log_content 对应的业务数据，保持现有调用约定。"""
    image_ocr_source = data.get("image_ocr")
    image_ocr = _normalize_task_image_ocr(image_ocr_source)
    has_supplied_ocr = isinstance(image_ocr_source, dict)
    local_ocr_enabled = str(os.getenv("LOCAL_OCR_ENABLED", "false")).strip().lower() in {
        "1", "true", "yes", "on",
    }

    image_tag = str(data.get("image_tag") or "").strip()
    image_description = str(data.get("image_description") or "").strip()
    if image_ocr.get("engine") == "gpt-vision" and image_ocr.get("extracted_text"):
        pass
    elif has_supplied_ocr and image_ocr.get("extracted_text") and not _task_should_use_gpt_vision_fallback(image_ocr):
        pass
    else:
        if has_supplied_ocr:
            gpt_result = _task_build_gpt_image_ocr_result(
                input_paths,
                image_tag,
                image_description,
                image_fallback_extractor=image_fallback_extractor,
            )
            if gpt_result.get("available"):
                image_ocr = _normalize_task_image_ocr(gpt_result)
            elif image_ocr.get("extracted_text"):
                image_ocr["warnings"].extend(gpt_result.get("warnings") or [])
            else:
                image_ocr = _normalize_task_image_ocr(gpt_result)
        elif not local_ocr_enabled:
            gpt_result = _task_build_gpt_image_ocr_result(
                input_paths,
                image_tag,
                image_description,
                image_fallback_extractor=image_fallback_extractor,
            )
            image_ocr = _normalize_task_image_ocr(gpt_result)
        else:
            try:
                if ocr_extractor is None:
                    from app.analysis.image_ocr import extract_text_from_images
                    ocr_extractor = extract_text_from_images
                image_ocr = _normalize_task_image_ocr(ocr_extractor(
                    input_paths,
                    min_confidence=float(os.getenv("OCR_MIN_CONFIDENCE", "0.45")),
                ))
            except TypeError:
                image_ocr = _normalize_task_image_ocr(ocr_extractor(input_paths))
            except Exception as exc:
                logging.exception("图片来源文字提取失败（本地 OCR 通道，默认已停用）")
                image_ocr["warnings"].append(str(exc))
            if _task_should_use_gpt_vision_fallback(image_ocr):
                gpt_result = _task_build_gpt_image_ocr_result(
                    input_paths,
                    image_tag,
                    image_description,
                    image_fallback_extractor=image_fallback_extractor,
                )
                if gpt_result.get("available"):
                    image_ocr = _normalize_task_image_ocr(gpt_result)
                else:
                    image_ocr["warnings"].extend(gpt_result.get("warnings") or [])

    metadata = [
        f"image_tag: {data.get('image_tag') or ''}",
        f"image_description: {data.get('image_description') or ''}",
        "image_files: " + ", ".join(os.path.basename(path) for path in input_paths or []),
    ]
    log_parts = [image_ocr.get("extracted_text")]
    if image_ocr.get("summary"):
        log_parts.append(f"image_summary: {image_ocr.get('summary')}")
    if image_ocr.get("analysis_text"):
        log_parts.append(image_ocr.get("analysis_text"))
    log_parts.extend(metadata)
    log_content = "\n".join(filter(None, log_parts))
    return log_content, image_ocr


def read_text_file_for_analysis(file_path):
    """读取并返回 read_text_file_for_analysis 对应的业务数据，保持现有调用约定。"""
    if not file_path or not os.path.exists(file_path):
        return ""
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            with open(file_path, "r", encoding=encoding) as file_obj:
                return file_obj.read()
        except UnicodeDecodeError:
            continue
        except OSError:
            return ""
    return ""


def build_related_evidence_context(related_evidence):
    """构建并返回 build_related_evidence_context 对应的业务数据，保持现有调用约定。"""
    sections = []
    related_image_paths = []
    for index, evidence in enumerate(related_evidence or [], start=1):
        label = evidence.get("label") or evidence.get("role") or f"related-{index}"
        source_type = str(evidence.get("source_type") or "file").lower()
        description = evidence.get("description") or ""
        if source_type == "image":
            image_paths = [path for path in (evidence.get("file_paths") or []) if path]
            if evidence.get("file_path") and evidence.get("file_path") not in image_paths:
                image_paths.insert(0, evidence.get("file_path"))
            if not image_paths:
                continue
            try:
                related_image_paths.extend(
                    path for path in image_paths if path not in related_image_paths
                )
                ocr_text, image_ocr = build_image_ocr_log_content(
                    {
                        "image_tag": evidence.get("image_tag") or "related_image",
                        "image_description": description,
                        "image_ocr": evidence.get("image_ocr"),
                    },
                    image_paths,
                )
                image_analysis = {
                    "analysis_text": "\n".join([
                        f"image_tag: {evidence.get('image_tag') or 'related_image'}",
                        f"description: {description}",
                        "image_files: " + ", ".join(os.path.basename(path) for path in image_paths),
                        ocr_text,
                    ])
                }
                text = image_analysis.get("analysis_text") or ""
            except Exception:
                logging.exception("上下游图片证据识别失败：%s", label)
                text = description
        else:
            text = read_text_file_for_analysis(evidence.get("file_path"))
        if text:
            sections.append(f"【上下游补充证据 {index}: {label}】\n{text}")
    return "\n\n".join(sections), related_image_paths


def build_skipped_chain_issue_context(skipped_issues):
    """构建并返回 build_skipped_chain_issue_context 对应的业务数据，保持现有调用约定。"""
    lines = []
    for index, issue in enumerate(skipped_issues or [], start=1):
        summary = str(issue.get("issueSummary") or "").strip()
        reason = str(issue.get("reason") or "").strip()
        modules = issue.get("relatedModules") or []
        module_names = [
            str(module.get("moduleName") or module.get("moduleId") or "").strip()
            for module in modules
            if isinstance(module, dict) and (module.get("moduleName") or module.get("moduleId"))
        ]
        lines.append(f"【跳过上下游补充条件 {index}】")
        if summary:
            lines.append(f"异常摘要: {summary}")
        if module_names:
            lines.append("疑似上下游模块: " + ", ".join(module_names))
        if reason:
            lines.append(f"跳过原因: {reason}")
        lines.append("说明: 仅跳过该类异常的上下游代码/日志补充条件；原始日志中的该异常仍必须参与最终故障分析。")
    return "\n".join(lines)


def _path_is_under(path, directory):
    """处理 _path_is_under 对应的业务步骤，并向调用方返回所需结果。"""
    if not path or not directory:
        return False
    try:
        real_path = os.path.realpath(path)
        real_directory = os.path.realpath(directory)
        return os.path.commonpath([real_path, real_directory]) == real_directory
    except (OSError, ValueError):
        return False


def _default_upload_dir():
    """处理 _default_upload_dir 对应的业务步骤，并向调用方返回所需结果。"""
    try:
        from flask import current_app

        return current_app.config.get("LOCAL_STORAGE_DIR", "/data/upload")
    except RuntimeError:
        return "/data/upload"


def _default_repo_base_dir():
    """返回代码检出缓存根目录：以 REPO_CACHE_DIR 配置为准，脱离应用上下文时用内置默认值。"""
    try:
        from flask import current_app

        return current_app.config.get("REPO_CACHE_DIR") or DEFAULT_REPO_CACHE_DIR
    except RuntimeError:
        return DEFAULT_REPO_CACHE_DIR


def cleanup_analysis_files(data, repo_path=None, task_id=None, upload_dir=None, repo_base_dir=None):
    """清理 cleanup_analysis_files 对应的业务数据，保持现有调用约定。"""
    upload_dir = upload_dir or _default_upload_dir()
    # 未显式传入时按 REPO_CACHE_DIR 配置解析，保证清理范围与克隆仓库的根目录一致。
    repo_base_dir = repo_base_dir or _default_repo_base_dir()
    uploaded_files = []
    if isinstance(data, dict):
        uploaded_files = [path for path in (data.get("file_paths") or []) if path]
        if data.get("file_path") and data.get("file_path") not in uploaded_files:
            uploaded_files.insert(0, data.get("file_path"))

    result = {
        "uploaded_file_removed": False,
        "uploaded_file_removed_count": 0,
        "repo_workspace_removed": False,
        "uploaded_file_path": data.get("file_path") if isinstance(data, dict) else None,
        "uploaded_file_paths": uploaded_files,
        "repo_workspace_path": None,
    }

    for uploaded_file in uploaded_files:
        if not (uploaded_file and _path_is_under(uploaded_file, upload_dir) and os.path.isfile(uploaded_file)):
            continue
        try:
            os.remove(uploaded_file)
            result["uploaded_file_removed"] = True
            result["uploaded_file_removed_count"] += 1
        except OSError:
            logging.exception("?????????%s", uploaded_file)

    if task_id:
        real_repo_path = os.path.realpath(repo_path) if repo_path else None
        real_repo_base = os.path.realpath(repo_base_dir)
        task_workspace = os.path.realpath(os.path.join(real_repo_base, str(task_id)))
        result["repo_workspace_path"] = task_workspace

        if (
            os.path.basename(task_workspace) == str(task_id)
            and _path_is_under(task_workspace, real_repo_base)
            and (not real_repo_path or _path_is_under(real_repo_path, task_workspace))
            and os.path.isdir(task_workspace)
        ):
            try:
                shutil.rmtree(task_workspace)
                result["repo_workspace_removed"] = True
            except OSError:
                logging.exception("?? Git ?????????%s", task_workspace)

    return result


@celery.task(bind=True, name="analysis.analyze_log_task")
def analyze_log_task(self, data):
    """执行故障分析并返回 analyze_log_task 对应的业务数据，保持现有调用约定。"""
    from app.analysis.routes.routes import (
        annotate_code_snippets,
        analyze_code_with_deepseek,
        AiServiceError,
        auto_pull_common_repositories,
        build_analysis_repositories,
        build_backend_log_analysis,
        build_cached_analysis_payload,
        build_code_findings,
        build_common_module_candidates,
        build_error_fingerprint,
        clone_analysis_repositories,
        clone_git_repo,
        detect_error_components,
        detect_log_languages,
        extract_code_snippets,
        extract_error_info_from_log,
        extract_log_error_events,
        find_related_repository_code_usages,
        find_component_code_usages,
        find_knowledge_case,
        GitCloneError,
        group_log_error_events,
        insert_query_record,
        merge_code_snippets,
        normalize_image_analysis,
        parse_issue_conclusion,
        resolve_file_paths,
        upsert_knowledge_case,
    )

    task_id = self.request.id
    repo_path = None
    repo_paths = []
    try:
        report_progress(self, "clone_repo_started", "代码拉取", 0, 25, "开始拉取代码 0%")
        repositories = build_analysis_repositories(data, task_id)
        try:
            cloned_repositories = clone_analysis_repositories(repositories, clone_git_repo, max_workers=4)
        except GitCloneError as exc:
            message = f"主模块代码拉取失败：{exc}"
            report_progress(self, "clone_repo_failed", "代码拉取", 100, 45, message)
            insert_query_record(data, 1, status="clone_repo_failed")
            raise RuntimeError(message) from exc
        for repository in cloned_repositories:
            current_repo_path = repository.get("repo_path")
            if repository.get("role") == "primary":
                repo_path = current_repo_path
            if current_repo_path:
                repo_paths.append(current_repo_path)
        if not repo_path:
            report_progress(self, "clone_repo_failed", "代码拉取", 100, 45, "代码拉取失败")
            insert_query_record(data, 1)
            raise RuntimeError("克隆 Git 仓库失败")
        report_progress(self, "clone_repo_done", "代码拉取", 100, 45, "代码拉取 100%")

        source_type = str(data.get("source_type") or "file").lower()
        input_path = data.get("file_path")
        input_paths = [path for path in (data.get("file_paths") or []) if path]
        if input_path and input_path not in input_paths:
            input_paths.insert(0, input_path)
        image_analysis = None
        components = []

        if source_type == "image":
            report_progress(self, "read_image_started", "图片识别", 0, 45, "开始识别图片 0%")
            if not input_paths or any(not os.path.exists(path) for path in input_paths):
                report_progress(self, "read_image_failed", "图片识别", 100, 75, "图片识别失败")
                insert_query_record(data, 1)
                raise RuntimeError("图片文件路径无效或文件不存在")
            try:
                log_content, image_ocr = build_image_ocr_log_content(data, input_paths)
                extracted_text = image_ocr.get("extracted_text") or ""
                image_analysis = normalize_image_analysis(
                    str(data.get("image_tag") or "").strip(),
                    {
                        "summary": "图片识别模型已提取画面文本" if extracted_text else "图片识别模型未提取到文本，将保留原图继续分析",
                        "extracted_text": extracted_text,
                    },
                    image_description=str(data.get("image_description") or "").strip(),
                )
                image_analysis.update({
                    "ocr_engine": image_ocr.get("engine"),
                    "ocr_available": image_ocr.get("available"),
                    "ocr_average_confidence": image_ocr.get("average_confidence"),
                    "ocr_warnings": image_ocr.get("warnings") or [],
                })
                components = merge_component_candidates(
                    image_analysis.get("components"),
                    image_analysis.get("candidate_apis"),
                    image_analysis.get("candidate_code_keywords"),
                    [
                        image_analysis.get("business_domain"),
                        image_analysis.get("page_name"),
                        image_analysis.get("user_action"),
                        image_analysis.get("error_message"),
                    ],
                    detect_error_components(log_content),
                )
            except Exception as exc:
                report_progress(self, "read_image_failed", "图片识别", 100, 75, "图片识别失败")
                insert_query_record(data, 1)
                raise RuntimeError("图片识别失败") from exc
            report_progress(self, "read_image_done", "图片识别", 100, 75, "图片识别 100%")
        else:
            report_progress(self, "read_log_started", "日志读取", 0, 55, "开始读取日志 0%")
            if not input_path or not os.path.exists(input_path):
                report_progress(self, "read_log_failed", "日志读取", 100, 60, "日志读取失败")
                insert_query_record(data, 1)
                raise RuntimeError("日志文件路径无效或文件不存在")

            try:
                with open(input_path, "r", encoding="utf-8") as file_obj:
                    log_content = file_obj.read()
            except Exception as exc:
                report_progress(self, "read_log_failed", "日志读取", 100, 60, "日志读取失败")
                insert_query_record(data, 1)
                raise RuntimeError("读取日志失败") from exc
            report_progress(self, "read_log_done", "日志读取", 100, 60, "日志读取 100%")
            components = detect_error_components(log_content)

        error_info = extract_error_info_from_log(log_content)
        log_error_events = extract_log_error_events(log_content)
        grouped_log_errors = group_log_error_events(log_error_events)
        related_evidence_context, related_image_paths = build_related_evidence_context(data.get("relatedEvidence"))
        skipped_chain_issue_context = build_skipped_chain_issue_context(data.get("skippedChainIssues"))
        extra_context = "\n\n".join(
            context for context in [related_evidence_context, skipped_chain_issue_context] if context
        )
        analysis_log_content = f"{log_content}\n\n{extra_context}" if extra_context else log_content
        common_candidates = build_common_module_candidates(data)
        common_pull = {"references": [], "repositories": [], "skipped": []}
        auto_pulled_common_modules = []
        if common_candidates:
            report_progress(
                self,
                "common_repo_pull_started",
                "公共组件联动",
                0,
                82,
                "开始识别并拉取公共组件 0%",
            )
            try:
                common_pull = auto_pull_common_repositories(
                    cloned_repositories,
                    common_candidates,
                    clone_git_repo,
                    max_workers=4,
                    source_text=analysis_log_content,
                    task_id=task_id,
                )
            except Exception as exc:
                logging.exception("公共组件自动联动失败，已跳过后继续主分析：%s", exc)
                common_pull = {
                    "references": [],
                    "repositories": [],
                    "skipped": [{"moduleName": "common", "reason": f"公共组件联动异常：{exc}"}],
                }
            linked_common_repositories = common_pull.get("repositories") or []
            auto_pulled_common_modules = [
                repository
                for repository in linked_common_repositories
                if repository.get("autoPulled")
            ]
            for linked_repository in linked_common_repositories:
                metadata_index = next(
                    (
                        index
                        for index, repository in enumerate(cloned_repositories)
                        if repository.get("common")
                        and not repository.get("repo_path")
                        and repository.get("moduleName") == linked_repository.get("moduleName")
                    ),
                    None,
                )
                if metadata_index is None:
                    cloned_repositories.append(linked_repository)
                else:
                    cloned_repositories[metadata_index] = linked_repository
            repo_paths.extend(
                repository.get("repo_path")
                for repository in linked_common_repositories
                if repository.get("repo_path")
            )
            report_progress(
                self,
                "common_repo_pull_done",
                "公共组件联动",
                100,
                83,
                f"公共组件联动 100%，引用 {len(common_pull.get('references') or [])} 个，"
                f"自动拉取 {len(auto_pulled_common_modules)} 个，"
                f"跳过 {len(common_pull.get('skipped') or [])} 个",
            )
        common_module_references = common_pull.get("references") or []
        common_pull_skipped = common_pull.get("skipped") or []
        error_fingerprint = build_error_fingerprint(
            data.get("productId"),
            data.get("moduleId"),
            error_info,
            data.get("branchAddress"),
            data.get("tagVersion"),
            fallback_text=log_content,
            grouped_log_errors=grouped_log_errors,
            related_modules=data.get("relatedModules"),
            common_modules=common_module_references,
        )
        log_hash = hashlib.sha256(log_content.encode("utf-8")).hexdigest()

        knowledge_case = None
        if should_use_knowledge_cache(source_type):
            knowledge_case = find_knowledge_case(
                data.get("productId"), data.get("moduleId"), error_fingerprint
            )
        if knowledge_case:
            report_progress(self, "knowledge_hit", "知识库命中", 100, 95, "命中历史知识库 100%")
            payload = build_cached_analysis_payload(knowledge_case, task_id=task_id, repo_path=repo_path)
            cached_code_evidence = build_analysis_evidence(
                error_info=[],
                resolved_errors=[],
                code_snippets=payload.get("code_snippets") or [],
                repositories=cloned_repositories,
                common_module_references=common_module_references,
                auto_pulled_common_modules=auto_pulled_common_modules,
                common_pull_skipped=common_pull_skipped,
            )
            payload["repositories"] = cloned_repositories
            payload["common_module_references"] = common_module_references
            payload["auto_pulled_common_modules"] = auto_pulled_common_modules
            payload["common_pull_skipped"] = common_pull_skipped
            payload.setdefault("analysis_evidence", {}).update({
                key: cached_code_evidence[key]
                for key in (
                    "used_code_context",
                    "used_related_code_context",
                    "related_code_module_count",
                    "code_context_modules",
                    "selected_code_repositories",
                    "common_module_reference_count",
                    "common_module_references",
                    "auto_pulled_common_module_count",
                    "auto_pulled_common_modules",
                    "common_pull_skipped_count",
                    "common_pull_skipped",
                )
            })
            insert_query_record(
                data,
                0,
                status="cache_hit",
                log_hash=log_hash,
                error_fingerprint=error_fingerprint,
            hit_cache=True,
            knowledge_case_id=knowledge_case.id,
        )
            report_progress(self, "completed", "任务完成", 100, 100, "全部阶段完成 100%")
            return payload

        report_progress(self, "analyze_log_started", "日志分析", 0, 70, "开始分析日志 0%")
        try:
            log_analysis = build_backend_log_analysis(analysis_log_content)
            detected_languages = detect_log_languages(analysis_log_content)
        except AiServiceError as exc:
            report_progress(self, "ai_service_failed", "AI 服务", 100, 75, str(exc))
            insert_query_record(data, 1, status="ai_service_failed", log_hash=log_hash, error_fingerprint=error_fingerprint)
            raise RuntimeError(str(exc)) from exc
        if not log_analysis:
            report_progress(self, "analyze_log_failed", "日志分析", 100, 75, "日志分析失败")
            insert_query_record(data, 1, log_hash=log_hash, error_fingerprint=error_fingerprint)
            raise RuntimeError("日志分析失败")
        report_progress(self, "analyze_log_done", "日志分析", 100, 75, "日志分析 100%")

        report_progress(self, "extract_code_started", "代码定位", 0, 85, "开始定位相关代码 0%")
        resolved_errors = resolve_file_paths(repo_path, error_info) if error_info else []
        primary_repository = next(
            (item for item in cloned_repositories if item.get("role") == "primary"),
            {"role": "primary", "moduleName": "primary"},
        )
        stack_snippets = annotate_code_snippets(extract_code_snippets(resolved_errors), primary_repository)
        component_groups = []
        for repository in cloned_repositories:
            if repository.get("role") == "primary":
                snippets = find_component_code_usages(repository.get("repo_path"), components)
            else:
                snippets = find_related_repository_code_usages(
                    repository.get("repo_path"),
                    analysis_log_content,
                    components,
                )
            component_groups.append(annotate_code_snippets(snippets, repository))
        code_snippets = merge_code_snippets(stack_snippets, *component_groups)
        code_findings = build_code_findings(code_snippets)
        analysis_evidence = build_analysis_evidence(
            error_info,
            resolved_errors,
            code_snippets,
            log_error_events,
            grouped_log_errors=grouped_log_errors,
            detected_languages=detected_languages,
            ai_call_count=1,
            repositories=cloned_repositories,
            common_module_references=common_module_references,
            auto_pulled_common_modules=auto_pulled_common_modules,
            common_pull_skipped=common_pull_skipped,
        )
        report_progress(
            self,
            "extract_code_done",
            "代码定位",
            100,
            89,
            f"代码定位 100%，找到 {analysis_evidence['code_snippet_count']} 个代码片段",
        )

        report_progress(
            self,
            "deep_reasoning_started",
            "深度推理",
            0,
            95,
            "正在使用 gpt-5.6-sol 进行深度故障推理，请耐心等待 0%",
        )
        try:
            code_analysis = analyze_code_with_deepseek(
                analysis_log_content,
                log_analysis,
                code_snippets,
                image_analysis=image_analysis,
                image_paths=(input_paths if source_type == "image" else []) + related_image_paths,
                common_repositories=common_module_references,
            )
        except AiServiceError as exc:
            report_progress(self, "ai_service_failed", "AI 服务", 100, 98, str(exc))
            insert_query_record(data, 1, status="ai_service_failed", log_hash=log_hash, error_fingerprint=error_fingerprint)
            raise RuntimeError(str(exc)) from exc
        report_progress(self, "deep_reasoning_done", "深度推理", 100, 98, "深度故障推理 100%")

        issue_conclusion = parse_issue_conclusion(code_analysis)
        knowledge_case = None
        if code_analysis and should_use_knowledge_cache(source_type):
            knowledge_case = upsert_knowledge_case(
                data,
                error_fingerprint,
                error_info,
                log_content,
                code_snippets,
                code_analysis,
                issue_conclusion,
            )

        insert_query_record(
            data,
            0 if code_analysis else 1,
            log_hash=log_hash,
            error_fingerprint=error_fingerprint,
            hit_cache=False,
            knowledge_case_id=knowledge_case.id if knowledge_case else None,
        )
        logging.info("异步分析任务完成：%s", task_id)
        report_progress(self, "completed", "任务完成", 100, 100, "全部阶段完成 100%")

        return {
            "task_id": task_id,
            "repo_path": repo_path,
            "repositories": cloned_repositories,
            "knowledge_hit": False,
            "knowledge_case_id": knowledge_case.id if knowledge_case else None,
            "error_fingerprint": error_fingerprint,
            "source_type": source_type,
            "image_tag": data.get("image_tag"),
            "file_paths": input_paths if source_type == "image" else None,
            "log_ids": data.get("log_ids"),
            "image_analysis": image_analysis,
            "log_analysis": log_analysis,
            "code_snippets": code_snippets,
            "code_findings": code_findings,
            "analysis_evidence": analysis_evidence,
            "common_module_references": common_module_references,
            "auto_pulled_common_modules": auto_pulled_common_modules,
            "common_pull_skipped": common_pull_skipped,
            "issue_conclusion": issue_conclusion,
            "code_analysis": code_analysis,
        }
    finally:
        cleanup_result = cleanup_analysis_files(data, repo_path=repo_path, task_id=task_id)
        for extra_repo_path in repo_paths:
            if extra_repo_path == repo_path:
                continue
            cleanup_analysis_files(data, repo_path=extra_repo_path, task_id=task_id)
        if cleanup_result.get("uploaded_file_removed") or cleanup_result.get("repo_workspace_removed"):
            logging.info("分析临时文件清理完成：%s", cleanup_result)
