import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

import bootstrap_schema


BACKEND_DIR = Path(__file__).resolve().parents[1]


def load_migration(filename):
    path = BACKEND_DIR / "migrations" / "versions" / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeInspector:
    def __init__(self, tables=(), indexes=None, unique_constraints=None, columns=None):
        self.tables = set(tables)
        self.indexes = indexes or {}
        self.unique_constraints = unique_constraints or {}
        self.columns = columns or {}

    def has_table(self, table_name):
        return table_name in self.tables

    def get_columns(self, table_name):
        return [
            {"name": name, "type": column_type}
            for name, column_type in self.columns.get(table_name, {}).items()
        ]

    def get_indexes(self, table_name):
        return [{"name": name} for name in self.indexes.get(table_name, ())]

    def get_unique_constraints(self, table_name):
        return [{"name": name} for name in self.unique_constraints.get(table_name, ())]


class MigrationIdempotencyTests(unittest.TestCase):
    def test_git_refs_upgrade_preserves_an_existing_table(self):
        migration = load_migration("c2b9e3a1d4f6_git_ref_cache.py")
        inspector = FakeInspector(
            tables={"git_refs"},
            indexes={"git_refs": {"ix_git_refs_repo_url"}},
            unique_constraints={"git_refs": {"uq_git_refs_ref_key"}},
        )

        with patch.object(migration.op, "get_bind", return_value=object()), \
                patch.object(migration.sa, "inspect", return_value=inspector), \
                patch.object(migration.op, "create_table") as create_table, \
                patch.object(migration.op, "create_index") as create_index:
            migration.upgrade()

        create_table.assert_not_called()
        create_index.assert_not_called()

    def test_user_operation_logs_upgrade_preserves_an_existing_table(self):
        migration = load_migration("7c9a4f21d801_user_operation_logs.py")
        inspector = FakeInspector(
            tables={"user_operation_logs"},
            indexes={
                "user_operation_logs": {
                    "ix_user_operation_logs_operator_username",
                    "ix_user_operation_logs_request_path",
                    "ix_user_operation_logs_created_at",
                }
            },
        )

        with patch.object(migration.op, "get_bind", return_value=object()), \
                patch.object(migration.sa, "inspect", return_value=inspector), \
                patch.object(migration.op, "create_table") as create_table, \
                patch.object(migration.op, "create_index") as create_index:
            migration.upgrade()

        create_table.assert_not_called()
        create_index.assert_not_called()

    def test_bootstrap_schema_contains_user_operation_logs_before_stamping_head(self):
        ddl = "\n".join(bootstrap_schema.DDL_STATEMENTS).lower()

        self.assertIn("create table if not exists user_operation_logs", ddl)

    def test_skill_run_records_upgrade_preserves_an_existing_table(self):
        migration = load_migration("d4c8b1e6a927_skill_run_records.py")
        inspector = FakeInspector(
            tables={"skill_run_records"},
            indexes={
                "skill_run_records": {
                    "ix_skill_run_records_task_id",
                    "ix_skill_run_records_skill_id",
                    "ix_skill_run_records_status",
                    "ix_skill_run_records_requested_by",
                    "ix_skill_run_records_created_at",
                }
            },
        )

        with patch.object(migration.op, "get_bind", return_value=object()), \
                patch.object(migration.sa, "inspect", return_value=inspector), \
                patch.object(migration.op, "create_table") as create_table, \
                patch.object(migration.op, "create_index") as create_index:
            migration.upgrade()

        create_table.assert_not_called()
        create_index.assert_not_called()

    def test_skill_run_records_upgrade_creates_unique_task_index(self):
        migration = load_migration("d4c8b1e6a927_skill_run_records.py")
        inspector = FakeInspector(tables=set(), indexes={})

        with patch.object(migration.op, "get_bind", return_value=object()), \
                patch.object(migration.sa, "inspect", return_value=inspector), \
                patch.object(migration.op, "create_table"), \
                patch.object(migration.op, "create_index") as create_index:
            migration.upgrade()

        unique_indexes = [
            call for call in create_index.call_args_list if call.kwargs.get("unique")
        ]
        self.assertEqual(len(unique_indexes), 1)
        self.assertEqual(unique_indexes[0].args[0], "ix_skill_run_records_task_id")

    def test_bootstrap_schema_contains_skill_run_records_before_stamping_head(self):
        ddl = "\n".join(bootstrap_schema.DDL_STATEMENTS).lower()

        self.assertIn("create table if not exists skill_run_records", ddl)

    def test_skill_run_progress_migration_alters_text_column_to_longtext(self):
        migration = load_migration("b7e4c2a9f310_skill_run_progress_longtext.py")
        inspector = FakeInspector(
            tables={"skill_run_records"},
            columns={"skill_run_records": {"progress": sa.Text()}},
        )

        with patch.object(migration.op, "get_bind", return_value=object()), \
                patch.object(migration.sa, "inspect", return_value=inspector), \
                patch.object(migration.op, "alter_column") as alter_column, \
                patch.object(migration.op, "add_column") as add_column:
            migration.upgrade()

        alter_column.assert_called_once()
        self.assertEqual(alter_column.call_args.args[0], "skill_run_records")
        self.assertEqual(alter_column.call_args.args[1], "progress")
        self.assertEqual(add_column.call_args_list, [])

    def test_skill_run_progress_migration_skips_when_already_longtext(self):
        migration = load_migration("b7e4c2a9f310_skill_run_progress_longtext.py")
        inspector = FakeInspector(
            tables={"skill_run_records"},
            columns={"skill_run_records": {"progress": mysql.LONGTEXT()}},
        )

        with patch.object(migration.op, "get_bind", return_value=object()), \
                patch.object(migration.sa, "inspect", return_value=inspector), \
                patch.object(migration.op, "alter_column") as alter_column, \
                patch.object(migration.op, "add_column") as add_column:
            migration.upgrade()

        alter_column.assert_not_called()
        add_column.assert_not_called()

    def test_skill_run_progress_migration_adds_missing_column(self):
        migration = load_migration("b7e4c2a9f310_skill_run_progress_longtext.py")
        inspector = FakeInspector(tables={"skill_run_records"}, columns={"skill_run_records": {}})

        with patch.object(migration.op, "get_bind", return_value=object()), \
                patch.object(migration.sa, "inspect", return_value=inspector), \
                patch.object(migration.op, "alter_column") as alter_column, \
                patch.object(migration.op, "add_column") as add_column:
            migration.upgrade()

        alter_column.assert_not_called()
        add_column.assert_called_once()
        self.assertEqual(add_column.call_args.args[1].name, "progress")

    def test_skill_run_progress_migration_skips_when_table_is_missing(self):
        migration = load_migration("b7e4c2a9f310_skill_run_progress_longtext.py")
        inspector = FakeInspector(tables=set())

        with patch.object(migration.op, "get_bind", return_value=object()), \
                patch.object(migration.sa, "inspect", return_value=inspector), \
                patch.object(migration.op, "alter_column") as alter_column:
            migration.upgrade()

        alter_column.assert_not_called()

    def test_bootstrap_schema_declares_longtext_progress_for_skill_run_records(self):
        ddl = " ".join(bootstrap_schema.DDL_STATEMENTS).lower()

        self.assertIn("create table if not exists skill_run_records", ddl)
        self.assertIn("progress longtext", ddl)

    def test_skill_run_records_ddl_is_shared_with_auto_create_helper(self):
        self.assertIn(
            "create table if not exists skill_run_records",
            bootstrap_schema.SKILL_RUN_RECORDS_DDL.lower(),
        )

    def test_skill_call_records_upgrade_preserves_an_existing_table(self):
        # 新统计表迁移必须幂等：表已存在时不重复建表或建索引。
        migration = load_migration("c4f1a7b9d2e6_skill_call_records.py")
        inspector = FakeInspector(
            tables={"skill_call_records"},
            indexes={
                "skill_call_records": {
                    "ix_skill_call_skill_created",
                    "ix_skill_call_status_created",
                    "ix_skill_call_outcome_created",
                    "ix_skill_call_issue_created",
                    "ix_skill_call_source_created",
                }
            },
        )

        with patch.object(migration.op, "get_bind", return_value=object()), \
                patch.object(migration.sa, "inspect", return_value=inspector), \
                patch.object(migration.op, "create_table") as create_table, \
                patch.object(migration.op, "create_index") as create_index:
            migration.upgrade()

        create_table.assert_not_called()
        create_index.assert_not_called()

    def test_bootstrap_schema_creates_skill_call_records(self):
        # 自动建表流程必须创建统计表，且不修改原任务表。
        self.assertIn(
            "create table if not exists skill_call_records",
            bootstrap_schema.SKILL_CALL_RECORDS_DDL.lower(),
        )


if __name__ == "__main__":
    unittest.main()
