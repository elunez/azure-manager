import os
import re
import sys
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, patch

from sqlalchemy import inspect, text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "azure"))
os.environ["AZURE_MANAGER_MASTER_KEY"] = "test-master-key-for-isolated-app-tests-only"
os.environ["AZURE_MANAGER_DATABASE_URI"] = "sqlite:///:memory:"

import app as app_module  # noqa: E402
from azure.core.exceptions import ClientAuthenticationError  # noqa: E402


class AppTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config.update(TESTING=True)
        self.context = app_module.app.app_context()
        self.context.push()
        app_module.db.drop_all()
        app_module.db.create_all()
        user = app_module.User(username="admin", name="Admin")
        user.set_password("password")
        app_module.db.session.add(user)
        app_module.db.session.commit()
        self.client = app_module.app.test_client()

    def tearDown(self):
        app_module.db.session.remove()
        app_module.db.drop_all()
        self.context.pop()

    def csrf_token(self):
        with self.client.session_transaction() as client_session:
            return client_session["_csrf_token"]

    def login(self):
        self.client.get("/login")
        return self.client.post("/login", data={
            "csrf_token": self.csrf_token(),
            "username": "admin",
            "password": "password",
        })

    def add_credential(self):
        credential = app_module.Credential(
            account="test@example.com",
            client_id=str(uuid.uuid4()),
            tenant_id=str(uuid.uuid4()),
            subscription_id=str(uuid.uuid4()),
        )
        credential.set_client_secret("plain-secret")
        app_module.db.session.add(credential)
        app_module.db.session.commit()
        return credential

    def test_management_routes_require_login(self):
        response = self.client.get("/account/add")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_account_list_is_paginated(self):
        self.login()
        for index in range(1, 13):
            credential = app_module.Credential(
                account="account{:02d}@example.com".format(index),
                client_id=str(uuid.uuid4()),
                tenant_id=str(uuid.uuid4()),
                subscription_id=str(uuid.uuid4()),
            )
            credential.set_client_secret("secret-{}".format(index))
            app_module.db.session.add(credential)
        app_module.db.session.commit()

        response = self.client.get("/?page=2")

        self.assertEqual(response.status_code, 200)
        self.assertIn("共 12 个账号".encode("utf-8"), response.data)
        self.assertIn(b"account09@example.com", response.data)
        self.assertIn(b"account12@example.com", response.data)
        self.assertNotIn(b"account08@example.com", response.data)
        self.assertNotIn("序号".encode("utf-8"), response.data)
        self.assertIn("更新时间".encode("utf-8"), response.data)

    def test_account_list_uses_manage_vm_label(self):
        self.login()
        self.add_credential()

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("管理 VM".encode("utf-8"), response.data)

    def test_account_page_size_is_validated_and_preserved(self):
        self.login()
        for index in range(1, 26):
            credential = app_module.Credential(
                account="page-size-account{:02d}@example.com".format(index),
                client_id=str(uuid.uuid4()),
                tenant_id=str(uuid.uuid4()),
                subscription_id=str(uuid.uuid4()),
            )
            credential.set_client_secret("secret-{}".format(index))
            app_module.db.session.add(credential)
        app_module.db.session.commit()

        response = self.client.get("/?page=2&per_page=20")
        invalid_response = self.client.get("/?page=2&per_page=999")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.count(b'data-account="page-size-account'), 5)
        self.assertIn(b'<option value="20" selected>20', response.data)
        self.assertIn(b"per_page=20", response.data)
        self.assertEqual(invalid_response.status_code, 200)
        self.assertEqual(invalid_response.data.count(b'data-account="page-size-account'), 8)
        self.assertIn(b'<option value="8" selected>8', invalid_response.data)

    def test_account_form_uses_visible_client_secret_input(self):
        self.login()

        response = self.client.get("/?modal=add")

        self.assertEqual(response.status_code, 200)
        self.assertIn("添加 Azure 账号".encode("utf-8"), response.data)
        self.assertIn(b'id="account-client-secret" type="text"', response.data)
        self.assertNotIn(b'id="account-client-secret" type="password"', response.data)

    def test_operation_logs_are_paginated_eight_per_page(self):
        self.login()
        for index in range(1, 13):
            app_module.db.session.add(app_module.OperationLog(
                account="account@example.com",
                action="创建 VM",
                target="target-{:02d}".format(index),
                status="成功",
                detail="操作已完成",
                created_at=datetime(2026, 1, index),
            ))
        app_module.db.session.commit()

        response = self.client.get("/logs?page=2")

        self.assertEqual(response.status_code, 200)
        self.assertIn("共 12 条".encode("utf-8"), response.data)
        self.assertGreater(
            response.data.index("共 12 条".encode("utf-8")),
            response.data.index(b"</table>"),
        )
        self.assertIn(b'<li class="page-item active" aria-current="page"><span class="page-link">2</span></li>', response.data)
        self.assertEqual(response.data.count(b"target-"), 4)
        self.assertIn(b"target-04", response.data)
        self.assertIn(b"target-02", response.data)
        self.assertIn(b"target-01", response.data)
        self.assertNotIn(b"target-05", response.data)

    def test_operation_log_page_size_is_validated_and_preserved(self):
        self.login()
        for index in range(1, 26):
            app_module.db.session.add(app_module.OperationLog(
                account="account@example.com",
                action="创建 VM",
                target="page-size-target-{:02d}".format(index),
                status="成功",
                detail="操作已完成",
                created_at=datetime(2026, 2, index),
            ))
        app_module.db.session.commit()

        response = self.client.get("/logs?page=2&per_page=20&q=page-size-target")
        invalid_response = self.client.get("/logs?page=2&per_page=999&q=page-size-target")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.count(b"page-size-target-"), 5)
        self.assertIn(b'<option value="20" selected>20', response.data)
        self.assertIn(b"per_page=20", response.data)
        self.assertEqual(invalid_response.status_code, 200)
        self.assertEqual(invalid_response.data.count(b"page-size-target-"), 8)
        self.assertIn(b'<option value="8" selected>8', invalid_response.data)

    def test_operation_logs_show_failure_details_only_for_failed_tasks(self):
        self.login()
        app_module.db.session.add_all([
            app_module.OperationLog(
                account="failed@example.com",
                action="创建 VM",
                target="failed-vm",
                status="失败",
                detail="Azure 权限不足",
            ),
            app_module.OperationLog(
                account="success@example.com",
                action="创建 VM",
                target="success-vm",
                status="成功",
                detail="VM 登录凭据：用户名 user，密码 password",
            ),
        ])
        app_module.db.session.commit()

        response = self.client.get("/logs")

        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8")
        self.assertEqual(
            re.findall(r"<th>(.*?)</th>", html),
            ["账号", "操作", "VM名称", "耗时/秒", "时间", "状态", "失败原因"],
        )
        self.assertEqual(html.count('data-bs-target="#failure-detail-modal"'), 1)
        self.assertIn('data-failure-detail="Azure 权限不足"', html)
        self.assertIn('disabled aria-disabled="true"', html)
        self.assertIn('id="failure-detail-content"', html)

    def test_legacy_credential_table_gets_updated_at_column(self):
        app_module.db.drop_all()
        with app_module.db.engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE credential (
                    id INTEGER PRIMARY KEY,
                    account VARCHAR(120) NOT NULL,
                    client_id VARCHAR(60) NOT NULL,
                    client_secret TEXT NOT NULL,
                    tenant_id VARCHAR(60) NOT NULL,
                    subscription_id VARCHAR(60) NOT NULL
                )
            """))
            connection.execute(text("""
                INSERT INTO credential (
                    account, client_id, client_secret, tenant_id, subscription_id
                ) VALUES (
                    'legacy@example.com', 'client-id', 'secret', 'tenant-id', 'subscription-id'
                )
            """))

        app_module.migrate_credential_updated_at()

        column_names = {
            column["name"] for column in inspect(app_module.db.engine).get_columns("credential")
        }
        updated_at = app_module.db.session.execute(
            text("SELECT updated_at FROM credential WHERE account = 'legacy@example.com'")
        ).scalar()
        self.assertIn("updated_at", column_names)
        self.assertIsNotNone(updated_at)

    def test_account_secret_is_encrypted_before_commit(self):
        self.login()
        form_data = {
            "csrf_token": self.csrf_token(),
            "account": "azure@example.com",
            "client_id": str(uuid.uuid4()),
            "client_secret": "new-secret",
            "tenant_id": str(uuid.uuid4()),
            "subscription_id": str(uuid.uuid4()),
        }
        with patch.object(app_module.function, "create_credential_object", return_value=object()), \
                patch.object(app_module.function, "validate_credential") as validate_credential:
            response = self.client.post("/account/add", data=form_data)

        self.assertEqual(response.status_code, 302)
        credential = app_module.Credential.query.one()
        self.assertTrue(app_module.credential_cipher.is_encrypted(credential.client_secret))
        self.assertEqual(credential.get_client_secret(), "new-secret")
        validate_credential.assert_called_once_with(form_data["subscription_id"], ANY)

    def test_account_get_routes_open_the_shared_modal(self):
        self.login()
        credential = self.add_credential()

        add_response = self.client.get("/account/add?page=2")
        edit_response = self.client.get("/account/{}/edit?page=2".format(credential.id))

        self.assertEqual(add_response.status_code, 302)
        self.assertIn("/?modal=add&page=2", add_response.headers["Location"])
        self.assertEqual(edit_response.status_code, 302)
        self.assertIn(
            "/?modal=edit&credential_id={}&page=2".format(credential.id),
            edit_response.headers["Location"],
        )

    def test_plaintext_secret_migration(self):
        credential = app_module.Credential(
            account="legacy@example.com",
            client_id=str(uuid.uuid4()),
            client_secret="legacy-plaintext-secret",
            tenant_id=str(uuid.uuid4()),
            subscription_id=str(uuid.uuid4()),
        )
        app_module.db.session.add(credential)
        app_module.db.session.commit()

        app_module.migrate_credential_secrets()

        self.assertTrue(app_module.credential_cipher.is_encrypted(credential.client_secret))
        self.assertEqual(credential.get_client_secret(), "legacy-plaintext-secret")

    def test_failed_credential_edit_preserves_existing_values(self):
        self.login()
        credential = self.add_credential()
        original_client_id = credential.client_id
        form_data = {
            "csrf_token": self.csrf_token(),
            "account": "changed@example.com",
            "client_id": str(uuid.uuid4()),
            "client_secret": "replacement-secret",
            "tenant_id": str(uuid.uuid4()),
            "subscription_id": str(uuid.uuid4()),
        }
        with patch.object(
            app_module.function,
            "create_credential_object",
            side_effect=ClientAuthenticationError(
                message="AADSTS7000215: invalid secret"
            ),
        ):
            response = self.client.post(
                "/account/{}/edit".format(credential.id),
                data=form_data,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="account-modal"', response.data)
        self.assertIn(b'data-open="true"', response.data)
        self.assertIn("Azure 客户端密钥无效".encode("utf-8"), response.data)
        app_module.db.session.refresh(credential)
        self.assertEqual(credential.client_id, original_client_id)
        self.assertEqual(credential.get_client_secret(), "plain-secret")

    def test_successful_credential_edit_refreshes_updated_at(self):
        self.login()
        credential = self.add_credential()
        credential.updated_at = datetime(2020, 1, 1)
        app_module.db.session.commit()
        form_data = {
            "csrf_token": self.csrf_token(),
            "account": "updated@example.com",
            "client_id": credential.client_id,
            "client_secret": "",
            "tenant_id": credential.tenant_id,
            "subscription_id": credential.subscription_id,
        }

        with patch.object(app_module.function, "create_credential_object", return_value=object()), \
                patch.object(app_module.function, "validate_credential"):
            response = self.client.post(
                "/account/{}/edit".format(credential.id),
                data=form_data,
            )

        self.assertEqual(response.status_code, 302)
        app_module.db.session.refresh(credential)
        self.assertGreater(credential.updated_at, datetime(2020, 1, 1))

    def test_batch_creation_uses_unique_names(self):
        self.login()
        credential = self.add_credential()
        form_data = {
            "csrf_token": self.csrf_token(),
            "tag": "batch",
            "location": "japaneast",
            "size": "Standard_B1s",
            "count": "3",
            "os": "Debian_12_X64",
            "custom": "",
            "acc": "False",
            "disk": "64",
            "spot": "False",
        }
        generated_credentials = [
            ("vmuser00000001", "Aa1!password-one"),
            ("vmuser00000002", "Aa1!password-two"),
            ("vmuser00000003", "Aa1!password-three"),
        ]
        with patch.object(app_module, "generate_vm_credentials", side_effect=generated_credentials), \
                patch.object(app_module, "queue_operation") as queue_operation:
            response = self.client.post(
                "/account/{}/vm/create".format(credential.id),
                data=form_data,
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            [call.args[2] for call in queue_operation.call_args_list],
            ["batch-1", "batch-2", "batch-3"],
        )
        self.assertEqual(
            [(call.args[4][2], call.args[4][3]) for call in queue_operation.call_args_list],
            generated_credentials,
        )

    def test_create_vm_operation_returns_generated_credentials_for_log(self):
        with patch.object(app_module.function, "create_resource_group"), \
                patch.object(app_module.function, "create_or_update_vm"):
            detail = app_module.create_vm_operation(
                "subscription-id",
                object(),
                "vm-name",
                "japaneast",
                "vmuser12345678",
                "Aa1!generated-password",
                "Standard_B1s",
                "Debian_12_X64",
                "",
                "False",
                64,
                "False",
            )

        self.assertEqual(
            detail,
            "VM 登录凭据：用户名 vmuser12345678，密码 Aa1!generated-password",
        )

    def test_generated_vm_credentials_meet_length_and_complexity_rules(self):
        username, password = app_module.generate_vm_credentials()

        self.assertRegex(username, r"^vmuser[a-z0-9]{8}$")
        self.assertGreaterEqual(len(password), 12)
        self.assertRegex(password, r"[A-Z]")
        self.assertRegex(password, r"[a-z]")
        self.assertRegex(password, r"[0-9]")
        self.assertRegex(password, r"[!@#%_-]")

    def test_operation_success_detail_is_saved_in_log(self):
        credential = self.add_credential()
        operation_log = app_module.OperationLog(
            credential_id=credential.id,
            account=credential.account,
            action="创建 VM",
            target="vm-name",
            status="排队中",
            detail="任务等待执行",
        )
        app_module.db.session.add(operation_log)
        app_module.db.session.commit()
        operation_log_id = operation_log.id
        credential_id = credential.id

        with patch.object(app_module, "azure_credential", return_value=object()):
            app_module.run_operation(
                operation_log_id,
                credential_id,
                lambda subscription_id, azure_credential: "VM 登录凭据：用户名 user，密码 password",
                (),
            )

        operation_log = app_module.OperationLog.query.get(operation_log_id)
        self.assertEqual(operation_log.status, "成功")
        self.assertEqual(operation_log.detail, "VM 登录凭据：用户名 user，密码 password")
        self.assertIsNotNone(operation_log.finished_at)

    def test_invalid_vm_form_returns_specific_error(self):
        self.login()
        credential = self.add_credential()
        response = self.client.post(
            "/account/{}/vm/create".format(credential.id),
            data={"csrf_token": self.csrf_token(), "tag": "-invalid", "count": "99"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("创建数量必须在 1 到 5 之间".encode("utf-8"), response.data)

    def test_vm_page_renders_loading_state_without_calling_azure(self):
        self.login()
        credential = self.add_credential()

        with patch.object(app_module, "azure_credential") as azure_credential, \
                patch.object(app_module.function, "list_vms") as list_vms:
            response = self.client.get("/account/{}/vms".format(credential.id))

        self.assertEqual(response.status_code, 200)
        self.assertIn("正在加载 VM".encode("utf-8"), response.data)
        self.assertIn(
            "/account/{}/vms/data".format(credential.id).encode("utf-8"),
            response.data,
        )
        azure_credential.assert_not_called()
        list_vms.assert_not_called()

    def test_vm_data_endpoint_returns_rendered_rows(self):
        self.login()
        credential = self.add_credential()
        vm = SimpleNamespace(
            name="test-vm",
            public_ips=["203.0.113.10"],
            location="japaneast",
            size="Standard_B1s",
            power_state="运行中",
            details_error=None,
            resource_group="test-rg",
        )

        with patch.object(app_module, "azure_credential", return_value=object()), \
                patch.object(app_module.function, "list_vms", return_value=[vm]):
            response = self.client.get("/account/{}/vms/data".format(credential.id))

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["count"], 1)
        self.assertIn("test-vm", payload["html"])
        self.assertIn("203.0.113.10", payload["html"])

    def test_vm_data_endpoint_returns_friendly_error(self):
        self.login()
        credential = self.add_credential()

        with patch.object(
            app_module,
            "azure_credential",
            side_effect=ClientAuthenticationError(message="authentication failed"),
        ):
            response = self.client.get("/account/{}/vms/data".format(credential.id))

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.get_json()["error"],
            "Azure 身份验证失败，请检查客户端 ID、客户端密钥和租户 ID",
        )

    def test_vm_data_endpoint_returns_json_when_account_is_missing(self):
        self.login()

        response = self.client.get("/account/999/vms/data")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"], "账号不存在或已删除")

    def test_post_without_csrf_is_rejected(self):
        self.login()
        response = self.client.post("/account/add", data={"account": "ignored"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(app_module.Credential.query.count(), 0)

    def test_active_tasks_are_marked_interrupted_after_restart(self):
        credential = self.add_credential()
        for status in app_module.ACTIVE_TASK_STATUSES:
            app_module.db.session.add(app_module.OperationLog(
                credential_id=credential.id,
                account=credential.account,
                action="测试",
                target="vm",
                status=status,
                detail="old",
            ))
        app_module.db.session.commit()

        app_module.mark_interrupted_operations()

        logs = app_module.OperationLog.query.all()
        self.assertEqual({log.status for log in logs}, {"中断"})
        self.assertTrue(all(log.finished_at is not None for log in logs))


if __name__ == "__main__":
    unittest.main()
