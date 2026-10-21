"""image ocr 模块负责本文件相关的业务流程、数据转换与依赖协作。"""
import hashlib
import json
import logging
import os
import sys
import threading
import tempfile
from pathlib import Path


_ENGINE = None
_ENGINE_ERROR = None
_ENGINE_LOCK = threading.Lock()
DEFAULT_OCR_MAX_SIDE_LIMIT = 2400
_OCR_MEMORY_ERROR_MARKERS = (
    "resourceexhaustederror",
    "fail to alloc memory",
    "cannot allocate memory",
    "out of memory",
    "std::bad_alloc",
)


def ensure_supported_runtime(version_info=None):
    """校验 ensure_supported_runtime 对应的业务数据，保持现有调用约定。"""
    version = version_info or sys.version_info
    if tuple(version[:2]) >= (3, 13):
        raise RuntimeError(
            "本地 PaddleOCR 在 Python 3.13 环境存在原生预测阻塞风险；"
            "请使用项目 Docker/Python 3.10 环境，当前任务将保留原图继续多模态分析"
        )


def _create_paddle_engine():
    """创建并返回 _create_paddle_engine 对应的业务数据，保持现有调用约定。"""
    ensure_supported_runtime()
    from paddleocr import PaddleOCR

    try:
        return PaddleOCR(
            lang="ch",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=True,
        )
    except TypeError:
        return PaddleOCR(lang="ch", use_angle_cls=True, show_log=False)


def get_ocr_engine(engine_factory=None):
    """读取并返回 get_ocr_engine 对应的业务数据，保持现有调用约定。"""
    global _ENGINE, _ENGINE_ERROR
    if engine_factory is not None:
        return engine_factory()
    if _ENGINE is not None:
        return _ENGINE
    if _ENGINE_ERROR is not None:
        raise _ENGINE_ERROR
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            return _ENGINE
        if _ENGINE_ERROR is not None:
            raise _ENGINE_ERROR
        try:
            _ENGINE = _create_paddle_engine()
        except Exception as exc:
            _ENGINE_ERROR = exc
            raise
    return _ENGINE


def _configured_ocr_max_side_limit():
    try:
        configured = int(os.getenv("OCR_MAX_SIDE_LIMIT", DEFAULT_OCR_MAX_SIDE_LIMIT))
    except (TypeError, ValueError):
        return DEFAULT_OCR_MAX_SIDE_LIMIT
    return configured if configured > 0 else DEFAULT_OCR_MAX_SIDE_LIMIT


def preprocess_screenshot(image_path, max_side_limit=None):
    """处理 preprocess_screenshot 对应的业务步骤，并向调用方返回所需结果。"""
    try:
        import cv2
        import numpy as np

        encoded = np.fromfile(image_path, dtype=np.uint8)
        image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        if image is None:
            return image_path
        height, width = image.shape[:2]
        scale = 1.0
        if width and width < 1600:
            scale = min(2.5, 1600.0 / width)
        limit = int(max_side_limit or _configured_ocr_max_side_limit())
        if limit > 0 and max(width, height) * scale > limit:
            scale *= float(limit) / (max(width, height) * scale)
        if scale != 1.0:
            image = cv2.resize(
                image,
                (int(width * scale), int(height * scale)),
                interpolation=cv2.INTER_CUBIC if scale > 1.0 else cv2.INTER_AREA,
            )
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        enhanced = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
        return cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)
    except MemoryError:
        raise
    except Exception:
        logging.debug("OCR image preprocessing unavailable: %s", image_path, exc_info=True)
        return image_path


def _unwrap_result(result):
    """处理 _unwrap_result 对应的业务步骤，并向调用方返回所需结果。"""
    if hasattr(result, "json"):
        value = result.json
        result = value() if callable(value) else value
    if isinstance(result, dict) and isinstance(result.get("res"), dict):
        return result["res"]
    return result


def _polygon_position(polygon, fallback_index):
    """处理 _polygon_position 对应的业务步骤，并向调用方返回所需结果。"""
    try:
        points = list(polygon or [])
        return (
            min(float(point[1]) for point in points),
            min(float(point[0]) for point in points),
        )
    except (TypeError, ValueError, IndexError):
        return float(fallback_index), 0.0


def _normalize_prediction(prediction):
    """规范化并返回 _normalize_prediction 对应的业务数据，保持现有调用约定。"""
    items = prediction if isinstance(prediction, (list, tuple)) else [prediction]
    lines = []
    for result in items:
        data = _unwrap_result(result)
        if isinstance(data, dict):
            texts = data.get("rec_texts") or data.get("texts") or []
            scores = data.get("rec_scores") or data.get("scores") or []
            polygons = data.get("rec_polys") or data.get("dt_polys") or data.get("polys") or []
            for index, text in enumerate(texts):
                score = scores[index] if index < len(scores) else 1.0
                polygon = polygons[index] if index < len(polygons) else None
                lines.append({
                    "text": str(text or "").strip(),
                    "confidence": float(score or 0.0),
                    "position": _polygon_position(polygon, index),
                })
            continue
        if not isinstance(data, (list, tuple)):
            continue
        for index, row in enumerate(data):
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            value = row[1]
            if not isinstance(value, (list, tuple)) or not value:
                continue
            lines.append({
                "text": str(value[0] or "").strip(),
                "confidence": float(value[1] if len(value) > 1 else 1.0),
                "position": _polygon_position(row[0], index),
            })
    return sorted(lines, key=lambda item: item["position"])


def _predict(engine, image):
    """处理 _predict 对应的业务步骤，并向调用方返回所需结果。"""
    if hasattr(engine, "predict"):
        return engine.predict(image)
    if hasattr(engine, "ocr"):
        return engine.ocr(image, cls=True)
    raise TypeError("OCR engine does not provide predict() or ocr()")


def _same_prediction_input(left, right):
    if left is right:
        return True
    if isinstance(left, (str, bytes, os.PathLike)) and isinstance(right, (str, bytes, os.PathLike)):
        return os.fspath(left) == os.fspath(right)
    return False


def _default_cache_dir():
    configured = os.getenv("OCR_RESULT_CACHE_DIR")
    if configured:
        return configured
    local_storage_dir = os.getenv("LOCAL_STORAGE_DIR", "/data/upload")
    return os.path.join(local_storage_dir, ".ocr-cache")


def _file_content_hash(image_path):
    hasher = hashlib.sha256()
    with open(image_path, "rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _callable_signature(callable_obj):
    if callable_obj is None:
        return ""
    parts = [
        str(getattr(callable_obj, "__module__", "")),
        str(getattr(callable_obj, "__qualname__", getattr(callable_obj, "__name__", ""))),
        str(getattr(callable_obj, "__name__", "")),
    ]
    code = getattr(callable_obj, "__code__", None)
    if code is not None:
        parts.extend([
            code.co_code.hex(),
            repr(code.co_consts),
            repr(code.co_names),
            repr(code.co_varnames),
        ])
    parts.append(repr(getattr(callable_obj, "__defaults__", None)))
    parts.append(repr(getattr(callable_obj, "__kwdefaults__", None)))
    closure = getattr(callable_obj, "__closure__", None)
    if closure:
        parts.append(repr([cell.cell_contents for cell in closure]))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _cache_signature(image_path, preprocess, min_confidence):
    raw_signature = "|".join([
        _file_content_hash(image_path),
        f"{float(min_confidence):.6f}",
        _callable_signature(preprocess),
    ])
    return hashlib.sha256(raw_signature.encode("utf-8")).hexdigest()


def _read_cached_result(cache_dir, cache_key):
    if not cache_dir:
        return None
    cache_path = Path(cache_dir) / f"{cache_key}.json"
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        logging.debug("OCR cache read failed: %s", cache_path, exc_info=True)
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _write_cached_result(cache_dir, cache_key, payload):
    if not cache_dir:
        return
    try:
        cache_root = Path(cache_dir)
        cache_root.mkdir(parents=True, exist_ok=True)
        cache_path = cache_root / f"{cache_key}.json"
        temp_path = cache_root / f"{cache_key}.json.tmp"
        temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temp_path, cache_path)
    except Exception:
        logging.debug("OCR cache write failed: %s", cache_key, exc_info=True)


def _is_ocr_memory_error(exc):
    pending = [exc]
    seen = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, MemoryError):
            return True
        text = f"{type(current).__name__}: {current}".lower()
        if any(marker in text for marker in _OCR_MEMORY_ERROR_MARKERS):
            return True
        pending.extend((getattr(current, "__cause__", None), getattr(current, "__context__", None)))
    return False


def _predict_with_fallback(engine, image_path, preprocess, warnings):
    preprocessed = preprocess(image_path)
    try:
        return _predict(engine, preprocessed)
    except Exception as exc:
        if _is_ocr_memory_error(exc):
            warnings.append(
                f"preprocessed OCR failed for {os.path.basename(image_path)} because of memory exhaustion; "
                f"skipped original image retry: {exc}"
            )
            logging.warning(
                "Local OCR memory exhausted; skipping original image retry: %s: %s",
                image_path,
                exc,
            )
            raise
        if _same_prediction_input(preprocessed, image_path):
            raise
        warnings.append(
            f"preprocessed OCR failed for {os.path.basename(image_path)}, retried original image: {exc}"
        )
        logging.warning("Preprocessed OCR failed, retrying original image: %s: %s", image_path, exc)
        return _predict(engine, image_path)


def extract_text_from_images(
    image_paths,
    engine=None,
    engine_factory=None,
    min_confidence=0.45,
    image_preprocessor=None,
    cache_dir=None,
):
    """解析或提取并返回 extract_text_from_images 对应的业务数据，保持现有调用约定。"""
    warnings = []
    try:
        active_engine = engine or get_ocr_engine(engine_factory=engine_factory)
    except Exception as exc:
        return {
            "available": False,
            "engine": "paddleocr",
            "extracted_text": "",
            "lines": [],
            "average_confidence": 0.0,
            "warnings": [str(exc)],
        }

    preprocess = image_preprocessor or preprocess_screenshot
    cache_root = cache_dir or _default_cache_dir()
    accepted_lines = []
    attempted_predictions = 0
    successful_predictions = 0
    for image_index, image_path in enumerate(image_paths or []):
        if not image_path or not os.path.exists(image_path):
            warnings.append(f"image file not found: {image_path}")
            continue
        try:
            attempted_predictions += 1
            cache_key = _cache_signature(image_path, preprocess, min_confidence)
            cached_result = _read_cached_result(cache_root, cache_key)
            if cached_result is not None:
                successful_predictions += 1
                for cached_line in cached_result.get("lines") or []:
                    if not isinstance(cached_line, dict):
                        continue
                    accepted_lines.append({
                        "text": str(cached_line.get("text") or "").strip(),
                        "confidence": float(cached_line.get("confidence") or 0.0),
                        "image_index": image_index,
                        "line_index": int(cached_line.get("line_index") or 0),
                    })
                continue
            prediction = _predict_with_fallback(active_engine, image_path, preprocess, warnings)
            image_lines = _normalize_prediction(prediction)
            successful_predictions += 1
            cache_lines = []
        except Exception as exc:
            warnings.append(f"OCR failed for {os.path.basename(image_path)}: {exc}")
            logging.exception("Local OCR failed: %s", image_path)
            continue
        for line_index, line in enumerate(image_lines):
            if not line["text"] or line["confidence"] < float(min_confidence):
                continue
            cache_lines.append({
                "text": line["text"],
                "confidence": line["confidence"],
                "line_index": line_index,
            })
            accepted_lines.append({
                "text": line["text"],
                "confidence": line["confidence"],
                "image_index": image_index,
                "line_index": line_index,
            })
        _write_cached_result(cache_root, cache_key, {
            "available": True,
            "engine": "paddleocr",
            "extracted_text": "\n".join(line["text"] for line in cache_lines),
            "lines": cache_lines,
            "average_confidence": round(
                sum(line["confidence"] for line in cache_lines) / len(cache_lines), 4
            ) if cache_lines else 0.0,
        })

    average_confidence = 0.0
    if accepted_lines:
        average_confidence = sum(line["confidence"] for line in accepted_lines) / len(accepted_lines)
    return {
        "available": attempted_predictions > 0 and successful_predictions > 0,
        "engine": "paddleocr",
        "extracted_text": "\n".join(line["text"] for line in accepted_lines),
        "lines": accepted_lines,
        "average_confidence": round(average_confidence, 4),
        "warnings": warnings,
    }


def run_ocr_smoke_check():
    """生成测试图片并执行真实 OCR 预测，用于镜像构建和健康验证。"""
    try:
        import cv2
        import numpy as np

        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = os.path.join(temp_dir, "ocr-smoke.png")
            image = np.full((180, 720, 3), 255, dtype=np.uint8)
            cv2.putText(
                image,
                "ERROR service unavailable",
                (20, 105),
                cv2.FONT_HERSHEY_SIMPLEX,
                1.2,
                (0, 0, 0),
                2,
            )
            if not cv2.imwrite(image_path, image):
                raise RuntimeError("无法生成 OCR 测试图片")
            return extract_text_from_images([image_path], min_confidence=0.0)
    except Exception as exc:
        return {
            "available": False,
            "engine": "paddleocr",
            "extracted_text": "",
            "lines": [],
            "average_confidence": 0.0,
            "warnings": [str(exc)],
        }
