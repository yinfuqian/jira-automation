"""内部接口令牌测试：技能回写故障分析使用的 ANALYSIS_API_TOKEN 只能访问约定路径。"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


ANALYSIS_PATHS = "/analysis,/logfile,/product/get,/module/get,/module/search"


STUBBED_TOP_LEVEL_MODULES = ("app", "flask", "openai", "celery", "extensions")


def purge_stubbed_modules():
    """剔除其他测试模块遗留在 sys.modules 中的桩模块，保证导入真实实现。

    仓库里部分用例用 types.ModuleType 顶替 flask/app 等模块且不做还原，
    unittest discover 到本模块时 sys.modules 已被污染，因此导入前先清理。
    """
    for name in list(sys.modules):
        if name.split(".")[0] not in STUBBED_TOP_LEVEL_MODULES:
            continue
        module = sys.modules.get(name)
        if module is None:
            continue
        if getattr(module, "__file__", None):
            continue
        sys.modules.pop(name, None)



class InternalAnalysisTokenTests(unittest.TestCase):
    """校验内部令牌与外部技能令牌的权限边界互不重叠。"""

    def build_app(self, analysis_token="analysis-secret", trust_loopback=False):
        """构造同时启用外部令牌与内部令牌的最小应用。"""
        purge_stubbed_modules()

        from flask import Flask, g, jsonify

        from app.auth.middleware import install_authentication

        app = Flask(__name__)
        app.config.update(
            SKILL_API_TOKEN="skill-secret",
            SKILL_API_TOKEN_PATHS="/skill",
            SKILL_API_USERNAME="external-api",
            ANALYSIS_API_TOKEN=analysis_token,
            ANALYSIS_API_TOKEN_PATHS=ANALYSIS_PATHS,
            ANALYSIS_API_USERNAME="analysis-skill",
            ANALYSIS_API_TRUST_LOOPBACK=trust_loopback,
        )
        session_service = MagicMock()
        session_service.authenticate.return_value = None
        install_authentication(app, session_service)

        def operator():
            return jsonify({"operator": getattr(g.current_user, "username", None)})

        @app.post("/analysis/submit_async")
        def submit_analysis():
            return operator()

        @app.post("/analysis-admin")
        def analysis_admin():
            return operator()

        @app.post("/logfile/upload")
        def upload_log():
            return operator()

        @app.get("/product/get")
        def list_products():
            return operator()

        @app.post("/product/delete")
        def delete_product():
            return operator()

        @app.post("/skill/run")
        def skill_run():
            return operator()

        @app.get("/business/data")
        def business():
            return jsonify({"ok": True})

        return app

    def test_internal_token_grants_access_to_analysis_and_upload(self):
        """内部令牌可以调用故障分析与上传接口，并记录内部调用方标识。"""
        client = self.build_app().test_client()

        submit = client.post("/analysis/submit_async", headers={"X-API-Token": "analysis-secret"})
        upload = client.post("/logfile/upload", headers={"X-API-Token": "analysis-secret"})
        product = client.get("/product/get", headers={"X-API-Token": "analysis-secret"})

        self.assertEqual(submit.status_code, 200)
        self.assertEqual(submit.get_json()["operator"], "analysis-skill")
        self.assertEqual(upload.status_code, 200)
        self.assertEqual(product.status_code, 200)

    def test_internal_token_is_limited_to_configured_paths(self):
        """内部令牌不能调用未授权的业务接口（例如产品删除）。"""
        client = self.build_app().test_client()

        response = client.post("/product/delete", headers={"X-API-Token": "analysis-secret"})

        self.assertEqual(response.status_code, 401)

    def test_skill_token_cannot_call_analysis_endpoints(self):
        """外部技能令牌仍然只对 /skill 生效，不能借用故障分析接口。"""
        client = self.build_app().test_client()

        skill_ok = client.post("/skill/run", headers={"X-API-Token": "skill-secret"})
        analysis_denied = client.post("/analysis/submit_async", headers={"X-API-Token": "skill-secret"})

        self.assertEqual(skill_ok.status_code, 200)
        self.assertEqual(skill_ok.get_json()["operator"], "external-api")
        self.assertEqual(analysis_denied.status_code, 401)

    def test_internal_token_cannot_call_skill_endpoints(self):
        """内部令牌不能调用外部技能接口。"""
        client = self.build_app().test_client()

        response = client.post("/skill/run", headers={"X-API-Token": "analysis-secret"})

        self.assertEqual(response.status_code, 401)

    def test_internal_token_disabled_when_not_configured(self):
        """未配置内部令牌时，分析接口回落到登录校验。"""
        client = self.build_app(analysis_token="").test_client()

        response = client.post("/analysis/submit_async", headers={"X-API-Token": "analysis-secret"})

        self.assertEqual(response.status_code, 401)

    def test_trusted_loopback_call_can_use_analysis_api_without_token(self):
        """同机技能进程通过回环地址调用时属于内部调用，不需要服务间令牌。"""
        client = self.build_app(analysis_token="", trust_loopback=True).test_client()

        submit = client.post(
            "/analysis/submit_async",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        product = client.get(
            "/product/get",
            environ_base={"REMOTE_ADDR": "::1"},
        )

        self.assertEqual(submit.status_code, 200)
        self.assertEqual(submit.get_json()["operator"], "analysis-loopback")
        self.assertEqual(product.status_code, 200)

    def test_loopback_trust_still_requires_token_from_non_loopback_callers(self):
        """启用回环信任后，来自其它主机的调用仍必须携带内部令牌。"""
        client = self.build_app(analysis_token="analysis-secret", trust_loopback=True).test_client()

        remote = client.post(
            "/analysis/submit_async",
            environ_base={"REMOTE_ADDR": "172.16.55.50"},
        )
        local = client.post(
            "/analysis/submit_async",
            headers={"X-API-Token": "analysis-secret"},
            environ_base={"REMOTE_ADDR": "172.16.55.50"},
        )

        self.assertEqual(remote.status_code, 401)
        self.assertEqual(local.status_code, 200)

    def test_loopback_trust_does_not_expose_unscoped_business_routes(self):
        """回环信任只覆盖分析脚本所需路径，不能扩展到产品删除或外部技能接口。"""
        client = self.build_app(analysis_token="", trust_loopback=True).test_client()

        delete_product = client.post(
            "/product/delete",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        run_skill = client.post(
            "/skill/run",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )

        self.assertEqual(delete_product.status_code, 401)
        self.assertEqual(run_skill.status_code, 401)

    def test_loopback_trust_uses_path_segment_boundaries(self):
        """回环免鉴权不能仅凭字符串前缀放行 /analysis-admin 等相邻路径。"""
        client = self.build_app(analysis_token="", trust_loopback=True).test_client()

        response = client.post(
            "/analysis-admin",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )

        self.assertEqual(response.status_code, 401)

    def test_missing_path_config_falls_back_to_restricted_defaults(self):
        """路径配置缺失时回落到默认允许路径，令牌不会意外获得全接口权限。"""
        purge_stubbed_modules()

        from app.auth.middleware import match_api_token

        app = self.build_app()
        app.config["ANALYSIS_API_TOKEN_PATHS"] = ""
        app.config["SKILL_API_TOKEN_PATHS"] = ""

        self.assertEqual(
            match_api_token(app, "/analysis/submit_async", {"X-API-Token": "analysis-secret"}),
            "analysis-skill",
        )
        self.assertEqual(
            match_api_token(app, "/skill/run", {"X-API-Token": "skill-secret"}),
            "external-api",
        )
        self.assertIsNone(match_api_token(app, "/business/data", {"X-API-Token": "analysis-secret"}))
        self.assertIsNone(match_api_token(app, "/business/data", {"X-API-Token": "skill-secret"}))
        self.assertIsNone(match_api_token(app, "/analysis/submit_async", {"X-API-Token": "wrong"}))


if __name__ == "__main__":
    unittest.main()
