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
        # 线上 API 使用宿主机 data 目录中的用户表。
        self.assertEqual(compose_text.count("./data/users.csv:/users.csv"), 1)


if __name__ == "__main__":
    unittest.main()
