import importlib.util
import tempfile
import unittest
import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
IMAGE_OCR_PATH = BACKEND_DIR / "app" / "analysis" / "image_ocr.py"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def load_image_ocr_module():
    spec = importlib.util.spec_from_file_location("image_ocr_under_test", IMAGE_OCR_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["image_ocr_under_test"] = module
    spec.loader.exec_module(module)
    return module


class FakePaddleEngine:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def predict(self, input_path):
        self.calls.append(input_path)
        return [self.results[len(self.calls) - 1]]


class ImageOcrTests(unittest.TestCase):
    def test_python_313_runtime_is_rejected_before_loading_native_ocr(self):
        ensure_supported_runtime = load_image_ocr_module().ensure_supported_runtime

        with self.assertRaisesRegex(RuntimeError, "Python 3.10"):
            ensure_supported_runtime((3, 13))

    def test_returns_unavailable_when_all_existing_images_fail_prediction(self):
        extract_text_from_images = load_image_ocr_module().extract_text_from_images

        class FailingEngine:
            def predict(self, _image):
                raise RuntimeError("std::exception")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "screen.png"
            path.write_bytes(b"image")
            result = extract_text_from_images(
                [str(path)],
                engine=FailingEngine(),
                image_preprocessor=lambda value: value,
            )

        self.assertFalse(result["available"])
        self.assertEqual(result["extracted_text"], "")
        self.assertIn("std::exception", result["warnings"][0])

    def test_reuses_cached_ocr_result_for_same_image_content(self):
        extract_text_from_images = load_image_ocr_module().extract_text_from_images

        class CountingEngine:
            def __init__(self):
                self.calls = 0

            def predict(self, _image):
                self.calls += 1
                return [{
                    "rec_texts": [f"cached line {self.calls}"],
                    "rec_scores": [0.99],
                    "rec_polys": [[[10, 10], [200, 10], [200, 30], [10, 30]]],
                }]

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir) / "cache"
            path = Path(temp_dir) / "screen.png"
            path.write_bytes(b"fake image content")
            engine = CountingEngine()

            first = extract_text_from_images(
                [str(path)],
                engine=engine,
                image_preprocessor=lambda value: value,
                cache_dir=str(cache_dir),
            )
            second = extract_text_from_images(
                [str(path)],
                engine=CountingEngine(),
                image_preprocessor=lambda value: value,
                cache_dir=str(cache_dir),
            )

        self.assertEqual(engine.calls, 1)
        self.assertEqual(first["extracted_text"], "cached line 1")
        self.assertEqual(second["extracted_text"], "cached line 1")

    def test_retries_original_image_when_preprocessed_prediction_fails(self):
        extract_text_from_images = load_image_ocr_module().extract_text_from_images

        class PreprocessedImageFailingEngine:
            def __init__(self):
                self.calls = []

            def predict(self, image):
                self.calls.append(image)
                if image == "preprocessed-image":
                    raise RuntimeError("std::exception")
                return [{
                    "rec_texts": ["java.lang.IllegalStateException: failed"],
                    "rec_scores": [0.97],
                    "rec_polys": [[[10, 10], [400, 10], [400, 30], [10, 30]]],
                }]

        engine = PreprocessedImageFailingEngine()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "screen.jpg"
            path.write_bytes(b"fake image content")
            result = extract_text_from_images(
                [str(path)],
                engine=engine,
                image_preprocessor=lambda _path: "preprocessed-image",
            )

        self.assertTrue(result["available"])
        self.assertEqual(engine.calls, ["preprocessed-image", str(path)])
        self.assertEqual(result["extracted_text"], "java.lang.IllegalStateException: failed")
        self.assertIn("preprocessed OCR failed", result["warnings"][0])

    def test_preprocessed_screenshot_keeps_three_color_channels_for_paddle(self):
        try:
            import cv2
        except ModuleNotFoundError:
            self.skipTest("cv2 is not available in this environment")
        import numpy as np

        preprocess_screenshot = load_image_ocr_module().preprocess_screenshot

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "screen.png"
            image = np.zeros((120, 320, 3), dtype=np.uint8)
            cv2.putText(image, "ERROR", (10, 70), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (255, 255, 255), 2)
            self.assertTrue(cv2.imwrite(str(path), image))

            processed = preprocess_screenshot(str(path))

        self.assertEqual(len(processed.shape), 3)
        self.assertEqual(processed.shape[2], 3)

    def test_preprocessed_screenshot_downscales_large_images_before_paddle(self):
        try:
            import cv2
        except ModuleNotFoundError:
            self.skipTest("cv2 is not available in this environment")
        import numpy as np

        preprocess_screenshot = load_image_ocr_module().preprocess_screenshot

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "large-screen.png"
            image = np.full((600, 1200, 3), 255, dtype=np.uint8)
            self.assertTrue(cv2.imwrite(str(path), image))

            processed = preprocess_screenshot(str(path), max_side_limit=600)

        self.assertEqual(max(processed.shape[:2]), 600)
        self.assertEqual(tuple(processed.shape[:2]), (300, 600))

    def test_memory_error_during_preprocessed_ocr_does_not_retry_original_image(self):
        extract_text_from_images = load_image_ocr_module().extract_text_from_images

        class ResourceExhaustedError(RuntimeError):
            pass

        class MemoryExhaustedEngine:
            def __init__(self):
                self.calls = []

            def predict(self, image):
                self.calls.append(image)
                if image == "preprocessed-image":
                    raise ResourceExhaustedError(
                        "Fail to alloc memory of 1010688000 size, error code is 12."
                    )
                return [{"rec_texts": ["should not be read"], "rec_scores": [0.99]}]

        engine = MemoryExhaustedEngine()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "screen.jpg"
            path.write_bytes(b"fake image content")
            result = extract_text_from_images(
                [str(path)],
                engine=engine,
                image_preprocessor=lambda _path: "preprocessed-image",
            )

        self.assertFalse(result["available"])
        self.assertEqual(engine.calls, ["preprocessed-image"])
        self.assertIn("memory exhaustion", result["warnings"][0])
        self.assertNotIn("should not be read", result["extracted_text"])

    def test_combines_multiple_images_and_filters_low_confidence_lines(self):
        extract_text_from_images = load_image_ocr_module().extract_text_from_images

        engine = FakePaddleEngine([
            {
                "rec_texts": [
                    "2026-07-14 ERROR downstream request failed",
                    "unreliable fragment",
                ],
                "rec_scores": [0.98, 0.20],
                "rec_polys": [
                    [[10, 10], [400, 10], [400, 30], [10, 30]],
                    [[10, 40], [220, 40], [220, 60], [10, 60]],
                ],
            },
            {
                "rec_texts": [
                    "java.lang.NullPointerException: null",
                    "at demo.Service.run(Service.java:42)",
                ],
                "rec_scores": [0.96, 0.94],
                "rec_polys": [
                    [[10, 10], [360, 10], [360, 30], [10, 30]],
                    [[10, 40], [390, 40], [390, 60], [10, 60]],
                ],
            },
        ])

        with tempfile.TemporaryDirectory() as temp_dir:
            paths = []
            for index, name in enumerate(("first.png", "second.png"), start=1):
                path = Path(temp_dir) / name
                path.write_bytes(f"fake image content {index}".encode("utf-8"))
                paths.append(str(path))
            result = extract_text_from_images(
                paths,
                engine=engine,
                min_confidence=0.45,
                image_preprocessor=lambda path: path,
            )

        self.assertTrue(result["available"])
        self.assertEqual(result["engine"], "paddleocr")
        self.assertEqual(len(result["lines"]), 3)
        self.assertNotIn("unreliable fragment", result["extracted_text"])
        self.assertEqual(
            result["extracted_text"].splitlines(),
            [
                "2026-07-14 ERROR downstream request failed",
                "java.lang.NullPointerException: null",
                "at demo.Service.run(Service.java:42)",
            ],
        )
        self.assertEqual([line["image_index"] for line in result["lines"]], [0, 1, 1])
        self.assertAlmostEqual(result["average_confidence"], 0.96, places=2)

    def test_returns_unavailable_result_when_runtime_cannot_initialize(self):
        extract_text_from_images = load_image_ocr_module().extract_text_from_images

        def fail_factory():
            raise ImportError("paddleocr is not installed")

        result = extract_text_from_images(
            ["missing.png"],
            engine_factory=fail_factory,
        )

        self.assertFalse(result["available"])
        self.assertEqual(result["extracted_text"], "")
        self.assertEqual(result["lines"], [])
        self.assertIn("paddleocr is not installed", result["warnings"][0])


if __name__ == "__main__":
    unittest.main()
