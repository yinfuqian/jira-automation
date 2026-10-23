import tempfile
import unittest
from pathlib import Path

from flask import Flask, jsonify

from app.auth.middleware import install_authentication
from app.auth.account_requests import AccountRequestService, MockAccountRequestProvider
from app.auth.routes import auth_bp
from app.auth.sessions import SessionService
from app.auth.users import UserStore
from app.health.routes import health_bp


class FakeRedis:
    def __init__(self):
        self.values = {}

    def set(self, key, value):
        self.values[key] = str(value)

    def get(self, key):
        return self.values.get(key)

    def delete(self, key):
        self.values.pop(key, None)

    def incr(self, key):
        value = int(self.values.get(key, "0")) + 1
        self.values[key] = str(value)
        return value

    def expire(self, key, seconds):
        return True

    def ping(self):
        return True


class AuthRoutesTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.users_path = Path(self.temp_dir.name) / "users.csv"
        self.write_user()
        self.users = UserStore(self.users_path)
        self.redis = FakeRedis()
        self.sessions = SessionService(self.redis, self.users, max_failures=2, failure_window_seconds=60)

        app = Flask(__name__)
        app.config.update(TESTING=True)
        app.extensions["auth_sessions"] = self.sessions
        app.extensions["account_request_service"] = AccountRequestService(MockAccountRequestProvider())
        app.register_blueprint(auth_bp, url_prefix="/auth")
        app.register_blueprint(health_bp, url_prefix="/health")

        @app.get("/private")
        def private_route():
            return jsonify({"ok": True})

        install_authentication(app, self.sessions)
        self.client = app.test_client()

    def tearDown(self):
        application = getattr(self, "application", None)
        if application is not None:
            for handler in list(application.logger.handlers):
                handler.close()
                application.logger.removeHandler(handler)
        self.temp_dir.cleanup()

    def write_user(self, password="secret", status=1):
        self.users_path.write_text(
            f"username,password,status\nalice,{password},{status}\n",
            encoding="utf-8",
        )

    def login(self):
        response = self.client.post("/auth/login", json={"username": "alice", "password": "secret"})
        self.assertEqual(response.status_code, 200)
        return response.get_json()["token"]

    def test_business_route_requires_authentication(self):
        response = self.client.get("/private")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["code"], "authentication_required")

    def test_login_me_and_logout(self):
        token = self.login()
        headers = {"Authorization": f"Bearer {token}"}

        self.assertEqual(self.client.get("/private", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/auth/me", headers=headers).get_json()["username"], "alice")
        self.assertEqual(self.client.post("/auth/logout", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/private", headers=headers).status_code, 401)

    def test_invalid_credentials_do_not_reveal_username_existence(self):
        unknown = self.client.post("/auth/login", json={"username": "unknown", "password": "wrong"})
        wrong = self.client.post("/auth/login", json={"username": "alice", "password": "wrong"})

        self.assertEqual(unknown.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(unknown.get_json(), wrong.get_json())

    def test_credential_version_change_invalidates_existing_token(self):
        token = self.login()
        self.write_user("new-secret")

        response = self.client.get("/private", headers={"Authorization": f"Bearer {token}"})

        self.assertEqual(response.status_code, 401)

    def test_health_and_preflight_are_anonymous(self):
        self.assertEqual(self.client.get("/health/live").status_code, 200)
        self.assertEqual(self.client.open("/private", method="OPTIONS").status_code, 200)

    def test_account_request_is_anonymous(self):
        response = self.client.post("/auth/account-requests", json={
            "username": "new-user",
            "password": "secret",
            "applicant_name": "张三",
        })

        self.assertEqual(response.status_code, 202)

    def test_application_factory_protects_registered_business_blueprints(self):
        from app import create_app

        app = create_app(
            config_overrides={
                "TESTING": True,
                "AUTH_USERS_FILE": str(self.users_path),
                "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
                "LOG_DIR": self.temp_dir.name,
                "LOGGING_FILE": "NUL",
            },
            redis_client=self.redis,
        )
        self.application = app
        client = app.test_client()

        self.assertEqual(client.get("/health/live").status_code, 200)
        self.assertEqual(
            client.get("/product/get", environ_base={"REMOTE_ADDR": "10.10.14.228"}).status_code,
            401,
        )

    def test_application_starts_fail_closed_when_user_file_is_unavailable(self):
        from app import create_app

        app = create_app(
            config_overrides={
                "TESTING": True,
                "AUTH_USERS_FILE": str(Path(self.temp_dir.name) / "missing-users.csv"),
                "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
                "LOG_DIR": self.temp_dir.name,
                "LOGGING_FILE": "NUL",
            },
            redis_client=self.redis,
        )
        self.application = app
        client = app.test_client()

        self.assertEqual(client.get("/health/live").status_code, 200)
        self.assertEqual(client.get("/health/ready").status_code, 503)
        self.assertEqual(
            client.get("/product/get", environ_base={"REMOTE_ADDR": "10.10.14.228"}).status_code,
            401,
        )
        login = client.post("/auth/login", json={"username": "alice", "password": "secret"})
        self.assertEqual(login.status_code, 503)
        self.assertEqual(login.get_json()["code"], "auth_configuration_unavailable")


if __name__ == "__main__":
    unittest.main()
