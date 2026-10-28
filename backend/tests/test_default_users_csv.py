from pathlib import Path
import unittest

from app.auth.users import UserStore


class DefaultUsersCsvTests(unittest.TestCase):
    def test_default_admin_user_is_enabled(self):
        project_dir = Path(__file__).resolve().parents[2]
        users_path = project_dir / "users.csv"
        store = UserStore(users_path)

        user = store.verify_password("admin", "admin123")

        self.assertIsNotNone(user)
        self.assertEqual(user.status, 1)

        compose_text = (project_dir / "docker-compose-dev.yaml").read_text(encoding="utf-8")
        # API、两个 Worker、Beat 与迁移容器都要读取同一个用户表。
        self.assertEqual(compose_text.count("./data/users-dev.csv:/data/users.csv:ro"), 5)


if __name__ == "__main__":
    unittest.main()
