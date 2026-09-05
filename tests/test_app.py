import base64
import os
import re
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, patch

from sqlalchemy import inspect, text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "azure"))
os.environ["AZURE_MANAGER_MASTER_KEY"] = "test-master-key-for-isolated-app-tests-only"
os.environ["AZURE_MANAGER_DATABASE_URI"] = "sqlite:///:memory:"
os.environ["AZURE_MANAGER_SECURE_COOKIE"] = "false"

import app as app_module  # noqa: E402
from azure.core.exceptions import ClientAuthenticationError  # noqa: E402


class AppTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config.update(
            TESTING=True,
            SESSION_COOKIE_SECURE=False,
        )
        app_module.cost_cache.clear()
        app_module.cost_rate_limit_cooldowns.clear()
        app_module.cost_query_locks.clear()
        app_module.vm_cache.clear()
        app_module.vm_query_locks.clear()
        app_module.login_ip_attempts.clear()
        app_module.login_account_attempts.clear()
        self.context = app_module.app.app_context()
        self.context.push()
        app_module.db.drop_all()
        app_module.db.create_all()
        user = app_module.User(username="admin")
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
        response = self.client.post("/login", data={
            "csrf_token": self.csrf_token(),
            "username": "admin",
            "password": "password",
        })
        if response.status_code == 302:
            self.client.get(response.headers["Location"])
        return response

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

        settings_response = self.client.get("/settings")
        self.assertEqual(settings_response.status_code, 302)
        self.assertIn("/login", settings_response.headers["Location"])

    def test_login_required_message_is_chinese(self):
        response = self.client.get("/settings", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("请先登录后再访问此页面".encode("utf-8"), response.data)
        self.assertNotIn(b"Please log in to access this page.", response.data)

    def test_session_cookie_security_and_lifetime(self):
        app_module.app.config["SESSION_COOKIE_SECURE"] = True
        client = app_module.app.test_client()

        response = client.get("/login")
        cookie = response.headers["Set-Cookie"]

        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertEqual(
            app_module.app.config["PERMANENT_SESSION_LIFETIME"].total_seconds(),
            8 * 60 * 60,
        )
        self.assertEqual(
            app_module.app.config["MAX_CONTENT_LENGTH"],
            256 * 1024,
        )
        app_module.app.config["SESSION_COOKIE_SECURE"] = False

    def test_successful_login_is_audited_and_uses_forwarded_ip(self):
        self.client.get("/login")
        response = self.client.post(
            "/login",
            data={
                "csrf_token": self.csrf_token(),
                "username": "admin",
                "password": "password",
            },
            headers={
                "X-Forwarded-For": "203.0.113.7",
                "User-Agent": "Security Test Browser",
            },
        )

        self.assertEqual(response.status_code, 302)
        audit = app_module.LoginAudit.query.one()
        self.assertEqual(audit.username, "admin")
        self.assertEqual(audit.ip_address, "203.0.113.7")
        self.assertEqual(audit.status, "成功")
        self.assertEqual(audit.user_agent, "Security Test Browser")
        with self.client.session_transaction() as client_session:
            self.assertTrue(client_session.permanent)

    def test_ip_is_locked_after_five_failed_logins(self):
        self.client.get("/login")
        token = self.csrf_token()

        for attempt in range(app_module.LOGIN_IP_FAILURE_LIMIT):
            response = self.client.post("/login", data={
                "csrf_token": token,
                "username": "wrong-account",
                "password": "wrong-password",
            })

        self.assertEqual(response.status_code, 429)
        self.assertGreaterEqual(int(response.headers["Retry-After"]), 899)

        blocked = self.client.post("/login", data={
            "csrf_token": token,
            "username": "admin",
            "password": "password",
        })
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(
            app_module.LoginAudit.query.filter_by(status="已拦截").count(),
            1,
        )

    def test_account_is_locked_after_ten_failed_logins_across_ips(self):
        self.client.get("/login")
        token = self.csrf_token()

        for attempt in range(app_module.LOGIN_ACCOUNT_FAILURE_LIMIT):
            response = self.client.post(
                "/login",
                data={
                    "csrf_token": token,
                    "username": "admin",
                    "password": "wrong-password",
                },
                headers={"X-Forwarded-For": "203.0.113.{}".format(attempt + 1)},
            )

        self.assertEqual(response.status_code, 429)
        blocked = self.client.post(
            "/login",
            data={
                "csrf_token": token,
                "username": "admin",
                "password": "password",
            },
            headers={"X-Forwarded-For": "203.0.113.250"},
        )
        self.assertEqual(blocked.status_code, 429)

    def test_successful_login_clears_previous_failure_state(self):
        self.client.get("/login")
        token = self.csrf_token()
        headers = {"X-Forwarded-For": "203.0.113.21"}

        self.client.post(
            "/login",
            data={
                "csrf_token": token,
                "username": "admin",
                "password": "wrong-password",
            },
            headers=headers,
        )
        response = self.client.post(
            "/login",
            data={
                "csrf_token": token,
                "username": "admin",
                "password": "password",
            },
            headers=headers,
        )

        self.assertEqual(response.status_code, 302)
        self.assertNotIn("203.0.113.21", app_module.login_ip_attempts)
        self.assertNotIn("admin", app_module.login_account_attempts)

    def test_login_lock_expires_without_blocked_attempts_extending_it(self):
        start_time = 1000
        for attempt in range(app_module.LOGIN_IP_FAILURE_LIMIT):
            retry_after = app_module.register_login_failure(
                "203.0.113.22",
                None,
                now=start_time + attempt,
            )

        self.assertEqual(retry_after, app_module.LOGIN_LOCK_SECONDS)
        self.assertEqual(
            app_module.login_retry_after(
                "203.0.113.22",
                None,
                now=start_time + app_module.LOGIN_IP_FAILURE_LIMIT + 60,
            ),
            app_module.LOGIN_LOCK_SECONDS - 61,
        )
        self.assertEqual(
            app_module.login_retry_after(
                "203.0.113.22",
                None,
                now=start_time + app_module.LOGIN_IP_FAILURE_LIMIT + app_module.LOGIN_LOCK_SECONDS,
            ),
            0,
        )

    def test_login_audit_retention_is_bounded(self):
        self.client.get("/login")
        token = self.csrf_token()

        with patch.object(app_module, "LOGIN_AUDIT_MAX_RECORDS", 2):
            for attempt in range(3):
                self.client.post("/login", data={
                    "csrf_token": token,
                    "username": "wrong-{}".format(attempt),
                    "password": "wrong-password",
                })

        self.assertEqual(app_module.LoginAudit.query.count(), 2)

    def test_request_body_larger_than_limit_returns_413(self):
        self.client.get("/login")

        response = self.client.post(
            "/login",
            data={"padding": "x" * (app_module.MAX_REQUEST_BYTES + 1)},
        )

        self.assertEqual(response.status_code, 413)
        self.assertIn("请求内容过大".encode("utf-8"), response.data)

    def test_logout_requires_post_and_csrf(self):
        self.login()
        self.client.get("/")
        token = self.csrf_token()

        self.assertEqual(self.client.get("/logout").status_code, 405)

        rejected = self.client.post("/logout")
        self.assertEqual(rejected.status_code, 302)
        self.assertNotIn("/login", rejected.headers["Location"])

        response = self.client.post("/logout", data={"csrf_token": token})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        self.assertIn("/login", self.client.get("/").headers["Location"])

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
        self.assertIn(b"account11@example.com", response.data)
        self.assertIn(b"account12@example.com", response.data)
        self.assertNotIn(b"account10@example.com", response.data)
        self.assertNotIn("序号".encode("utf-8"), response.data)
        self.assertIn("更新时间".encode("utf-8"), response.data)
        self.assertIn(b'class="page-size-form"', response.data)

    def test_account_list_uses_manage_vm_label(self):
        self.login()
        self.add_credential()

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("管理 VM".encode("utf-8"), response.data)

    def test_cost_overview_precedes_operation_logs_in_navigation(self):
        self.login()

        response = self.client.get("/")
        html = response.data.decode("utf-8")

        self.assertLess(html.index("费用概览"), html.index("任务日志"))

    def test_system_settings_precedes_logout_in_navigation(self):
        self.login()

        response = self.client.get("/")
        html = response.data.decode("utf-8")

        self.assertLess(html.index("系统设置"), html.index("退出"))

    def test_default_timezone_is_asia_shanghai(self):
        user = app_module.User.query.one()
        self.assertEqual(user.timezone, "Asia/Shanghai")
        self.assertEqual(user.vm_cache_days, 1)

    def test_utc_time_is_formatted_in_selected_timezone(self):
        value = datetime(2026, 7, 24, 6, 19)

        formatted = app_module.format_local_datetime(value, "Asia/Shanghai")
        default_formatted = app_module.format_local_datetime(value)

        self.assertEqual(formatted, "2026-07-24 14:19:00")
        self.assertEqual(default_formatted, "2026-07-24 14:19:00")

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
        self.assertEqual(invalid_response.data.count(b'data-account="page-size-account'), 10)
        self.assertIn(b'<option value="10" selected>10', invalid_response.data)

    def test_account_form_uses_visible_client_secret_input(self):
        self.login()

        response = self.client.get("/?modal=add")

        self.assertEqual(response.status_code, 200)
        self.assertIn("添加 Azure 账号".encode("utf-8"), response.data)
        self.assertIn(b'id="account-client-secret" type="text"', response.data)
        self.assertNotIn(b'id="account-client-secret" type="password"', response.data)

    def test_operation_logs_are_paginated_ten_per_page(self):
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
        self.assertIn(b'class="app-main flex-grow-1 logs-main"', response.data)
        self.assertIn(b'class="app-shell logs-shell"', response.data)
        self.assertIn(b'class="page-size-form"', response.data)
        self.assertIn(b'<li class="page-item active" aria-current="page"><span class="page-link">2</span></li>', response.data)
        self.assertEqual(response.data.count(b"target-"), 2)
        self.assertIn(b"target-02", response.data)
        self.assertIn(b"target-01", response.data)
        self.assertNotIn(b"target-03", response.data)

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
        self.assertEqual(invalid_response.data.count(b"page-size-target-"), 10)
        self.assertIn(b'<option value="10" selected>10', invalid_response.data)

    def test_operation_logs_show_details_for_successful_and_failed_tasks(self):
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
            ["账号", "操作", "VM名称", "耗时/秒", "时间", "状态", "任务详情"],
        )
        self.assertEqual(html.count('data-bs-target="#task-detail-modal"'), 2)
        self.assertIn('data-task-detail="Azure 权限不足"', html)
        self.assertIn(
            'data-task-detail="VM 登录凭据：用户名 user，密码 password"',
            html,
        )
        self.assertNotIn('disabled aria-disabled="true"', html)
        self.assertIn('id="task-detail-content"', html)

    def test_unknown_error_returns_reference_without_exposing_details(self):
        internal_detail = "database failed at /srv/private/database.db"

        with patch.object(app_module.app.logger, "error") as log_error:
            message = app_module.friendly_error_message(
                RuntimeError(internal_detail),
                context="测试未知异常",
            )

        self.assertNotIn(internal_detail, message)
        self.assertRegex(
            message,
            r"^操作失败，请联系管理员并提供错误编号：[A-F0-9]{10}$",
        )
        error_reference = message.rsplit("：", 1)[1]
        self.assertIn(error_reference, str(log_error.call_args))
        self.assertIn("exc_info", log_error.call_args.kwargs)

    def test_500_page_returns_reference_without_exposing_details(self):
        internal_detail = "unexpected failure at /srv/private/app.py"
        error = SimpleNamespace(original_exception=RuntimeError(internal_detail))

        with app_module.app.test_request_context("/"), \
                patch.object(app_module.app.logger, "error") as log_error:
            response, status_code = app_module.internal_server_error(error)

        self.assertEqual(status_code, 500)
        self.assertNotIn(internal_detail, response)
        self.assertRegex(
            response,
            r"操作失败，请联系管理员并提供错误编号：[A-F0-9]{10}",
        )
        error_reference = re.search(r"错误编号：([A-F0-9]{10})", response).group(1)
        self.assertIn(error_reference, str(log_error.call_args))

    def test_queue_failure_saves_raw_exception_but_page_uses_reference(self):
        credential = self.add_credential()
        internal_detail = "executor failed at /srv/private/worker.py"

        with patch.object(
            app_module.task_executor,
            "submit",
            side_effect=RuntimeError(internal_detail),
        ), patch.object(app_module.app.logger, "error"):
            with self.assertRaises(app_module.ErrorReferenceException) as raised:
                app_module.queue_operation(
                    credential,
                    "创建 VM",
                    "demo-vm",
                    lambda *args: None,
                    (),
                )

        operation_log = app_module.OperationLog.query.one()
        self.assertEqual(operation_log.status, "失败")
        self.assertEqual(operation_log.detail, internal_detail)
        self.assertRegex(
            str(raised.exception),
            r"^操作失败，请联系管理员并提供错误编号：[A-F0-9]{10}$",
        )

        with app_module.app.test_request_context("/"), \
                patch.object(app_module.app.logger, "error") as duplicate_log:
            response, status_code = app_module.internal_server_error(
                SimpleNamespace(original_exception=raised.exception)
            )
        self.assertEqual(status_code, 500)
        self.assertNotIn(operation_log.detail, response)
        self.assertIn(str(raised.exception), response)
        duplicate_log.assert_not_called()

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

    def test_legacy_credential_table_gets_cost_api_status_column(self):
        app_module.db.drop_all()
        with app_module.db.engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE credential (
                    id INTEGER PRIMARY KEY,
                    account VARCHAR(120) NOT NULL,
                    client_id VARCHAR(60) NOT NULL,
                    client_secret TEXT NOT NULL,
                    tenant_id VARCHAR(60) NOT NULL,
                    subscription_id VARCHAR(60) NOT NULL,
                    updated_at DATETIME
                )
            """))

        app_module.migrate_credential_cost_api_status()

        column_names = {
            column["name"] for column in inspect(app_module.db.engine).get_columns("credential")
        }
        self.assertIn("cost_api_status", column_names)

    def test_legacy_user_table_gets_default_timezone(self):
        app_module.db.drop_all()
        with app_module.db.engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE user (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR(20),
                    username VARCHAR(20),
                    password_hash VARCHAR(128)
                )
            """))
            connection.execute(text("""
                INSERT INTO user (name, username, password_hash)
                VALUES ('Admin', 'admin', 'hash')
            """))

        app_module.migrate_user_timezone()

        column_names = {
            column["name"] for column in inspect(app_module.db.engine).get_columns("user")
        }
        timezone_name = app_module.db.session.execute(
            text("SELECT timezone FROM user WHERE username = 'admin'")
        ).scalar()
        self.assertIn("timezone", column_names)
        self.assertEqual(timezone_name, "Asia/Shanghai")

    def test_legacy_user_table_gets_default_vm_script_column(self):
        app_module.db.drop_all()
        with app_module.db.engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE user (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR(20),
                    username VARCHAR(20),
                    password_hash VARCHAR(128),
                    timezone VARCHAR(64)
                )
            """))

        app_module.migrate_user_default_vm_script()

        column_names = {
            column["name"] for column in inspect(app_module.db.engine).get_columns("user")
        }
        self.assertIn("default_vm_script", column_names)

    def test_legacy_user_table_gets_default_vm_cache_days(self):
        app_module.db.drop_all()
        with app_module.db.engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE user (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR(20),
                    username VARCHAR(20),
                    password_hash VARCHAR(128),
                    timezone VARCHAR(64),
                    default_vm_script TEXT
                )
            """))
            connection.execute(text("""
                INSERT INTO user (
                    name, username, password_hash, timezone
                )
                VALUES ('Admin', 'admin', 'hash', 'Asia/Shanghai')
            """))

        app_module.migrate_user_vm_cache_days()

        column_names = {
            column["name"] for column in inspect(app_module.db.engine).get_columns("user")
        }
        cache_days = app_module.db.session.execute(
            text("SELECT vm_cache_days FROM user WHERE username = 'admin'")
        ).scalar()
        self.assertIn("vm_cache_days", column_names)
        self.assertEqual(cache_days, 1)

    def test_system_settings_uses_three_tabs_and_combines_vm_settings(self):
        self.login()

        response = self.client.get("/settings")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.count(b'data-settings-tab='), 3)
        self.assertIn("常规设置".encode("utf-8"), response.data)
        self.assertIn("登录记录".encode("utf-8"), response.data)
        self.assertNotIn("脚本设置".encode("utf-8"), response.data)
        self.assertNotIn("VM 默认设置".encode("utf-8"), response.data)
        self.assertNotIn("显示名称".encode("utf-8"), response.data)
        self.assertNotIn("默认每页条数".encode("utf-8"), response.data)
        self.assertNotIn(b"<h2>", response.data)
        self.assertIn(b'id="settings-vm-cache-days"', response.data)
        self.assertIn(b'min="1" max="30"', response.data)
        self.assertIn(b'id="settings-default-vm-script"', response.data)
        self.assertNotIn(b'id="settings-vm-tab"', response.data)
        self.assertIn(b'id="settings-general-form"', response.data)
        self.assertIn(b'id="settings-account-form"', response.data)
        self.assertNotIn(b'data-settings-save-action=', response.data)
        self.assertEqual(response.data.count(b'class="settings-tab-actions'), 2)
        self.assertIn("保存常规设置".encode("utf-8"), response.data)
        self.assertIn("保存账号安全设置".encode("utf-8"), response.data)

        legacy_response = self.client.get("/settings?tab=vm_defaults")
        self.assertIn(b'class="nav-link active" id="settings-general-tab"', legacy_response.data)

    def test_login_audit_tab_is_paginated(self):
        self.login()
        for index in range(11):
            app_module.db.session.add(app_module.LoginAudit(
                username="audit-user-{}".format(index),
                ip_address="203.0.113.{}".format(index),
                status="失败",
                detail="用户名或密码错误",
                user_agent="Test Browser {}".format(index),
            ))
        app_module.db.session.commit()

        first_page = self.client.get("/settings?tab=audit")
        second_page = self.client.get("/settings?tab=audit&audit_page=2")

        self.assertIn(b'id="settings-audit-tab"', first_page.data)
        self.assertIn(b'id="settings-audit-pane"', first_page.data)
        self.assertNotIn(b"audit-user-0", first_page.data)
        self.assertIn(b"audit-user-0", second_page.data)

    def test_general_settings_updates_timezone_and_vm_cache(self):
        self.login()

        response = self.client.post("/settings", data={
            "csrf_token": self.csrf_token(),
            "section": "general",
            "timezone": "Asia/Tokyo",
            "vm_cache_days": "7",
            "default_vm_script": "",
        })

        self.assertEqual(response.status_code, 302)
        self.assertIn("tab=general", response.headers["Location"])
        user = app_module.User.query.one()
        self.assertEqual(user.timezone, "Asia/Tokyo")
        self.assertEqual(user.vm_cache_days, 7)

    def test_default_vm_script_is_encrypted(self):
        self.login()
        base64_script = "IyEvYmluL2Jhc2gKZWNobyBoZWxsbw=="

        response = self.client.post("/settings", data={
            "csrf_token": self.csrf_token(),
            "section": "general",
            "timezone": "Asia/Shanghai",
            "vm_cache_days": "1",
            "default_vm_script": base64_script,
        })

        self.assertEqual(response.status_code, 302)
        self.assertIn("tab=general", response.headers["Location"])
        user = app_module.User.query.one()
        self.assertNotEqual(user.default_vm_script, base64_script)
        self.assertTrue(app_module.credential_cipher.is_encrypted(user.default_vm_script))
        self.assertEqual(user.get_default_vm_script(), base64_script)

    def test_system_settings_rejects_invalid_default_vm_script(self):
        self.login()

        response = self.client.post("/settings", data={
            "csrf_token": self.csrf_token(),
            "section": "general",
            "timezone": "Asia/Shanghai",
            "vm_cache_days": "1",
            "default_vm_script": "not-base64!",
        })

        self.assertEqual(response.status_code, 200)
        self.assertIn("默认 VM 脚本不是有效的 Base64 内容".encode("utf-8"), response.data)
        self.assertIn(b'class="tab-pane fade show active" id="settings-general-pane"', response.data)
        self.assertIsNone(app_module.User.query.one().default_vm_script)

    def test_system_settings_rejects_oversized_default_vm_script(self):
        self.login()
        oversized_script = base64.b64encode(b"x" * 65536).decode("ascii")

        response = self.client.post("/settings", data={
            "csrf_token": self.csrf_token(),
            "section": "general",
            "timezone": "Asia/Shanghai",
            "vm_cache_days": "1",
            "default_vm_script": oversized_script,
        })

        self.assertEqual(response.status_code, 200)
        self.assertIn("默认 VM 脚本解码后不能超过 64 KB".encode("utf-8"), response.data)
        self.assertIsNone(app_module.User.query.one().default_vm_script)

    def test_system_settings_rejects_username_change_with_wrong_password(self):
        self.login()

        response = self.client.post("/settings", data={
            "csrf_token": self.csrf_token(),
            "section": "account",
            "username": "new-admin",
            "current_password": "wrong-password",
            "new_password": "",
            "password_confirmation": "",
        })

        self.assertEqual(response.status_code, 200)
        self.assertIn("当前密码不正确".encode("utf-8"), response.data)
        self.assertEqual(app_module.User.query.one().username, "admin")

    def test_system_settings_updates_username_and_password(self):
        self.login()

        response = self.client.post("/settings", data={
            "csrf_token": self.csrf_token(),
            "section": "account",
            "username": "new-admin",
            "current_password": "password",
            "new_password": "new-password-123",
            "password_confirmation": "new-password-123",
        })

        self.assertEqual(response.status_code, 302)
        self.assertIn("tab=account", response.headers["Location"])
        user = app_module.User.query.one()
        self.assertEqual(user.username, "new-admin")
        self.assertTrue(user.validate_password("new-password-123"))
        self.assertFalse(user.validate_password("password"))

    def test_system_settings_rejects_invalid_timezone(self):
        self.login()

        response = self.client.post("/settings", data={
            "csrf_token": self.csrf_token(),
            "section": "general",
            "timezone": "Invalid/Timezone",
        })

        self.assertEqual(response.status_code, 200)
        self.assertIn("时区选项无效".encode("utf-8"), response.data)
        self.assertEqual(app_module.User.query.one().timezone, "Asia/Shanghai")

    def test_system_settings_rejects_invalid_vm_cache_days(self):
        self.login()

        response = self.client.post("/settings", data={
            "csrf_token": self.csrf_token(),
            "section": "general",
            "timezone": "Asia/Shanghai",
            "vm_cache_days": "31",
            "default_vm_script": "",
        })

        self.assertEqual(response.status_code, 200)
        self.assertIn("VM 列表缓存时间必须在 1 到 30 天之间".encode("utf-8"), response.data)
        self.assertEqual(app_module.User.query.one().vm_cache_days, 1)

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
        app_module.vm_cache[credential.id] = {"vms": [{"name": "stale-vm"}]}
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
        self.assertNotIn(credential.id, app_module.vm_cache)

    def test_deleting_credential_clears_vm_cache(self):
        self.login()
        credential = self.add_credential()
        credential_id = credential.id
        app_module.vm_cache[credential_id] = {"vms": [{"name": "stale-vm"}]}

        response = self.client.post(
            "/account/{}/delete".format(credential_id),
            data={"csrf_token": self.csrf_token()},
        )

        self.assertEqual(response.status_code, 302)
        self.assertNotIn(credential_id, app_module.vm_cache)
        self.assertIsNone(app_module.Credential.query.get(credential_id))

    def test_changing_subscription_resets_cost_api_status(self):
        self.login()
        credential = self.add_credential()
        credential.cost_api_status = app_module.COST_API_UNSUPPORTED
        app_module.db.session.commit()
        form_data = {
            "csrf_token": self.csrf_token(),
            "account": credential.account,
            "client_id": credential.client_id,
            "client_secret": "",
            "tenant_id": credential.tenant_id,
            "subscription_id": str(uuid.uuid4()),
        }

        with patch.object(app_module.function, "create_credential_object", return_value=object()), \
                patch.object(app_module.function, "validate_credential"):
            response = self.client.post(
                "/account/{}/edit".format(credential.id),
                data=form_data,
            )

        self.assertEqual(response.status_code, 302)
        app_module.db.session.refresh(credential)
        self.assertIsNone(credential.cost_api_status)

    def test_batch_creation_uses_unique_names(self):
        self.login()
        credential = self.add_credential()
        user = app_module.User.query.one()
        user.set_default_vm_script("ZGVmYXVsdA==")
        app_module.db.session.commit()
        form_data = {
            "csrf_token": self.csrf_token(),
            "tag": "batch",
            "location": "japaneast",
            "size": "Standard_B1s",
            "count": "3",
            "os": "Debian_12_X64",
            "custom": "",
            "disk": "64",
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
        self.assertTrue(all(call.args[4][6] == "" for call in queue_operation.call_args_list))
        self.assertTrue(all(len(call.args[4]) == 8 for call in queue_operation.call_args_list))
        self.assertEqual(user.get_default_vm_script(), "ZGVmYXVsdA==")

    def test_create_vm_form_uses_default_base64_script(self):
        self.login()
        credential = self.add_credential()
        base64_script = "IyEvYmluL2Jhc2gKZWNobyBkZWZhdWx0"
        user = app_module.User.query.one()
        user.set_default_vm_script(base64_script)
        app_module.db.session.commit()

        response = self.client.get("/account/{}/vm/create".format(credential.id))

        self.assertEqual(response.status_code, 200)
        self.assertIn(base64_script.encode("utf-8"), response.data)
        self.assertIn("位置".encode("utf-8"), response.data)
        self.assertNotIn(b'name="acc"', response.data)
        self.assertNotIn("加速网络".encode("utf-8"), response.data)
        self.assertNotIn(b'name="spot"', response.data)
        self.assertNotIn("Spot 实例".encode("utf-8"), response.data)

    def test_create_vm_operation_returns_generated_credentials_for_log(self):
        with patch.object(app_module.function, "create_resource_group"), \
                patch.object(app_module.function, "create_or_update_vm") as create_or_update_vm:
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
                64,
            )

        create_or_update_vm.assert_called_once_with(
            "subscription-id",
            ANY,
            "vm-name",
            "japaneast",
            "vmuser12345678",
            "Aa1!generated-password",
            "Standard_B1s",
            "Debian_12_X64",
            "",
            64,
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
        app_module.vm_cache[credential_id] = {"vms": [{"name": "stale-vm"}]}

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
        self.assertNotIn(credential_id, app_module.vm_cache)

    def test_operation_failure_saves_raw_exception_in_log(self):
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
        internal_detail = "Azure request failed\nresource: /subscriptions/private-id"

        def failing_operation(*args):
            raise RuntimeError(internal_detail)

        with patch.object(app_module, "azure_credential", return_value=object()), \
                patch.object(app_module.app.logger, "exception") as log_exception:
            app_module.run_operation(
                operation_log_id,
                credential.id,
                failing_operation,
                (),
            )

        operation_log = app_module.OperationLog.query.get(operation_log_id)
        self.assertEqual(operation_log.status, "失败")
        self.assertEqual(operation_log.detail, internal_detail)
        self.assertIsNotNone(operation_log.finished_at)
        log_exception.assert_called_once_with(
            "任务日志 %s 执行失败",
            operation_log_id,
        )

    def test_invalid_vm_form_returns_specific_error(self):
        self.login()
        credential = self.add_credential()
        response = self.client.post(
            "/account/{}/vm/create".format(credential.id),
            data={"csrf_token": self.csrf_token(), "tag": "-invalid", "count": "99"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("创建数量必须在 1 到 5 之间".encode("utf-8"), response.data)
        self.assertIn("位置选项无效".encode("utf-8"), response.data)

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
        self.assertIn('data-confirm-title="删除 VM"', payload["html"])
        self.assertIn(
            "将删除 test-vm 及其关联的全部资源，且无法恢复。确定继续？",
            payload["html"],
        )
        self.assertNotIn("删除资源组", payload["html"])

    def test_change_ip_redirects_to_vm_list_with_operation_tracking(self):
        self.login()
        credential = self.add_credential()

        with patch.object(app_module, "queue_operation", return_value=42) as queue_operation:
            response = self.client.post(
                "/account/{}/vm/change-ip".format(credential.id),
                data={
                    "csrf_token": self.csrf_token(),
                    "resource_group": "test-rg",
                    "vm_name": "test-vm",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(
            "/account/{}/vms?operation_id=42".format(credential.id)
        ))
        queue_operation.assert_called_once_with(
            credential,
            "更换 IP",
            "test-rg/test-vm",
            app_module.function.change_ip,
            ("test-rg", "test-vm"),
        )

    def test_vm_list_tracks_change_ip_operation_and_reports_completion(self):
        self.login()
        credential = self.add_credential()
        operation_log = app_module.OperationLog(
            credential_id=credential.id,
            account=credential.account,
            action="更换 IP",
            target="test-rg/test-vm",
            status="执行中",
            detail="任务正在执行",
        )
        app_module.db.session.add(operation_log)
        app_module.db.session.commit()
        status_url = "/account/{}/vm/change-ip/{}/status".format(
            credential.id,
            operation_log.id,
        )

        list_response = self.client.get(
            "/account/{}/vms?operation_id={}".format(
                credential.id,
                operation_log.id,
            )
        )
        active_response = self.client.get(status_url)

        self.assertEqual(list_response.status_code, 200)
        self.assertIn(
            'data-vm-operation-url="{}"'.format(status_url).encode("utf-8"),
            list_response.data,
        )
        self.assertEqual(active_response.status_code, 200)
        self.assertEqual(active_response.get_json(), {
            "status": "执行中",
            "finished": False,
        })

        operation_log.status = "成功"
        operation_log.finished_at = app_module.datetime.utcnow()
        app_module.db.session.commit()
        completed_response = self.client.get(status_url)

        self.assertEqual(completed_response.status_code, 200)
        self.assertEqual(completed_response.get_json(), {
            "status": "成功",
            "finished": True,
        })

    def test_vm_operation_status_rejects_unrelated_operation(self):
        self.login()
        credential = self.add_credential()
        operation_log = app_module.OperationLog(
            credential_id=credential.id,
            account=credential.account,
            action="启动 VM",
            target="test-rg/test-vm",
            status="成功",
            detail="操作已完成",
        )
        app_module.db.session.add(operation_log)
        app_module.db.session.commit()

        response = self.client.get(
            "/account/{}/vm/change-ip/{}/status".format(
                credential.id,
                operation_log.id,
            )
        )

        self.assertEqual(response.status_code, 404)

    def test_delete_vm_uses_vm_name_in_user_visible_status(self):
        self.login()
        credential = self.add_credential()

        with patch.object(app_module, "queue_operation") as queue_operation:
            response = self.client.post(
                "/account/{}/vm/delete".format(credential.id),
                data={
                    "csrf_token": self.csrf_token(),
                    "resource_group": "test-rg",
                    "vm_name": "test-vm",
                },
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("VM 删除任务已提交".encode("utf-8"), response.data)
        queue_operation.assert_called_once()
        queued_args = queue_operation.call_args.args
        self.assertEqual(queued_args[1], "删除 VM")
        self.assertEqual(queued_args[2], "test-vm")
        self.assertIs(queued_args[3], app_module.function.delete_vm)
        self.assertEqual(queued_args[4], ("test-rg",))

    def test_vm_data_endpoint_uses_cache_and_refresh_replaces_it(self):
        self.login()
        credential = self.add_credential()
        cached_vm = SimpleNamespace(
            name="cached-vm",
            public_ips=["203.0.113.10"],
            location="japaneast",
            size="Standard_B1s",
            power_state="运行中",
            details_error=None,
            resource_group="cached-rg",
        )
        refreshed_vm = SimpleNamespace(
            name="refreshed-vm",
            public_ips=["203.0.113.11"],
            location="japaneast",
            size="Standard_B1s",
            power_state="已停止",
            details_error=None,
            resource_group="refreshed-rg",
        )
        data_url = "/account/{}/vms/data".format(credential.id)

        with patch.object(app_module, "azure_credential", return_value=object()), \
                patch.object(
                    app_module.function,
                    "list_vms",
                    side_effect=[[cached_vm], [refreshed_vm]],
                ) as list_vms:
            first = self.client.get(data_url)
            second = self.client.get(data_url)
            refreshed = self.client.get("{}?refresh=1".format(data_url))

        self.assertIn("cached-vm", first.get_json()["html"])
        self.assertIn("cached-vm", second.get_json()["html"])
        self.assertIn("refreshed-vm", refreshed.get_json()["html"])
        self.assertEqual(list_vms.call_count, 2)

    def test_expired_vm_cache_is_reloaded(self):
        self.login()
        credential = self.add_credential()
        first_vm = SimpleNamespace(
            name="first-vm",
            public_ips=[],
            location="japaneast",
            size="Standard_B1s",
            power_state="运行中",
            details_error=None,
            resource_group="first-rg",
        )
        current_vm = SimpleNamespace(
            name="current-vm",
            public_ips=[],
            location="japaneast",
            size="Standard_B1s",
            power_state="运行中",
            details_error=None,
            resource_group="current-rg",
        )
        data_url = "/account/{}/vms/data".format(credential.id)

        with patch.object(app_module, "azure_credential", return_value=object()), \
                patch.object(
                    app_module.function,
                    "list_vms",
                    side_effect=[[first_vm], [current_vm]],
                ) as list_vms:
            first = self.client.get(data_url)
            app_module.vm_cache[credential.id]["created_at"] -= (
                app_module.SECONDS_PER_DAY + 1
            )
            second = self.client.get(data_url)

        self.assertIn("first-vm", first.get_json()["html"])
        self.assertIn("current-vm", second.get_json()["html"])
        self.assertEqual(list_vms.call_count, 2)

    def test_vm_cache_is_isolated_by_credential(self):
        self.login()
        first_credential = self.add_credential()
        second_credential = app_module.Credential(
            account="second@example.com",
            client_id=str(uuid.uuid4()),
            tenant_id=str(uuid.uuid4()),
            subscription_id=str(uuid.uuid4()),
        )
        second_credential.set_client_secret("second-secret")
        app_module.db.session.add(second_credential)
        app_module.db.session.commit()
        first_vm = SimpleNamespace(
            name="first-account-vm",
            public_ips=[],
            location="japaneast",
            size="Standard_B1s",
            power_state="运行中",
            details_error=None,
            resource_group="first-rg",
        )
        second_vm = SimpleNamespace(
            name="second-account-vm",
            public_ips=[],
            location="japaneast",
            size="Standard_B1s",
            power_state="运行中",
            details_error=None,
            resource_group="second-rg",
        )

        with patch.object(app_module, "azure_credential", return_value=object()), \
                patch.object(
                    app_module.function,
                    "list_vms",
                    side_effect=[[first_vm], [second_vm]],
                ) as list_vms:
            first = self.client.get(
                "/account/{}/vms/data".format(first_credential.id)
            )
            second = self.client.get(
                "/account/{}/vms/data".format(second_credential.id)
            )
            first_again = self.client.get(
                "/account/{}/vms/data".format(first_credential.id)
            )

        self.assertIn("first-account-vm", first.get_json()["html"])
        self.assertIn("second-account-vm", second.get_json()["html"])
        self.assertIn("first-account-vm", first_again.get_json()["html"])
        self.assertEqual(list_vms.call_count, 2)

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

    def test_cost_page_renders_loading_state_without_calling_azure(self):
        self.login()
        credential = self.add_credential()

        with patch.object(app_module, "get_cached_cost_overview") as get_overview:
            response = self.client.get("/costs?credential_id={}".format(credential.id))

        self.assertEqual(response.status_code, 200)
        self.assertIn("正在加载费用数据".encode("utf-8"), response.data)
        self.assertIn(
            "/account/{}/costs/data".format(credential.id).encode("utf-8"),
            response.data,
        )
        self.assertNotIn("查看学生订阅官方余额".encode("utf-8"), response.data)
        self.assertNotIn(
            "费用数据不等于学生订阅剩余额度".encode("utf-8"),
            response.data,
        )
        self.assertNotIn("预测费用".encode("utf-8"), response.data)
        self.assertIn("每日趋势".encode("utf-8"), response.data)
        get_overview.assert_not_called()

    def test_cost_account_selector_is_ordered_by_id(self):
        self.login()
        first = self.add_credential()
        first.account = "z-last-by-name@example.com"
        second = app_module.Credential(
            account="a-first-by-name@example.com",
            client_id=str(uuid.uuid4()),
            tenant_id=str(uuid.uuid4()),
            subscription_id=str(uuid.uuid4()),
        )
        second.set_client_secret("plain-secret")
        app_module.db.session.add(second)
        app_module.db.session.commit()

        response = self.client.get("/costs")
        html = response.data.decode("utf-8")

        self.assertLess(
            html.index("z-last-by-name@example.com"),
            html.index("a-first-by-name@example.com"),
        )

    def test_cost_data_endpoint_returns_rendered_overview(self):
        self.login()
        credential = self.add_credential()
        overview = {
            "currency": "USD",
            "month_to_date": 4.0,
            "latest_daily_cost": 2.75,
            "data_through": "2026-07-02",
            "peak_daily_cost": 2.75,
            "peak_daily_date": "2026-07-02",
            "chart_min_cost": 0,
            "chart_max_cost": 2.75,
            "chart_zero_ratio": 1,
            "daily": [{
                "date": "2026-07-02",
                "cost": 2.75,
                "chart_ratio": 0,
            }],
            "services": [{"name": "Virtual Machines", "cost": 4.0, "share": 100}],
            "resources": [{
                "name": "test-vm",
                "resource_group": "test-rg",
                "resource_type": "Microsoft.Compute/virtualMachines",
                "cost": 4.0,
                "share": 100,
            }],
            "warnings": [],
            "is_empty": False,
            "fetched_at": datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc),
        }

        with patch.object(app_module, "get_cached_cost_overview", return_value=overview):
            response = self.client.get("/account/{}/costs/data".format(credential.id))

        self.assertEqual(response.status_code, 200)
        html = response.get_json()["html"]
        self.assertIn("本月累计消费", html)
        self.assertIn("本月最高单日消费", html)
        self.assertIn("data-cost-line-chart", html)
        self.assertNotIn("免费层内", html)
        self.assertIn("4.00", html)
        self.assertIn("test-vm", html)

    def test_cost_data_endpoint_returns_friendly_permission_error(self):
        self.login()
        credential = self.add_credential()
        error = app_module.cost_management.CostManagementError(
            "费用读取权限不足，请授予 Cost Management Reader 权限",
            status_code=403,
            error_code="AuthorizationFailed",
        )

        with patch.object(app_module, "get_cached_cost_overview", side_effect=error):
            response = self.client.get("/account/{}/costs/data".format(credential.id))

        self.assertEqual(response.status_code, 502)
        self.assertIn("Cost Management Reader", response.get_json()["error"])

    def test_cost_data_endpoint_returns_rate_limit_retry_after(self):
        self.login()
        credential = self.add_credential()
        error = app_module.cost_management.CostManagementError(
            "Azure 费用接口正在限流，请在 60 秒后重试",
            status_code=429,
            error_code="TooManyRequests",
            retry_after=60,
        )

        with patch.object(app_module, "get_cached_cost_overview", side_effect=error):
            response = self.client.get("/account/{}/costs/data".format(credential.id))

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.get_json()["retry_after"], 60)

    def test_cost_data_endpoint_renders_student_offer_unavailable_state(self):
        self.login()
        credential = self.add_credential()
        error = app_module.cost_management.CostManagementUnsupportedError(
            "当前订阅报价不支持 Azure Cost Management 费用 API",
            status_code=400,
            error_code="BadRequest",
        )

        with patch.object(app_module, "get_cached_cost_overview", side_effect=error):
            response = self.client.get("/account/{}/costs/data".format(credential.id))

        self.assertEqual(response.status_code, 200)
        html = response.get_json()["html"]
        self.assertIn("学生订阅暂不支持费用 API", html)
        self.assertIn("查看官方余额", html)
        app_module.db.session.refresh(credential)
        self.assertEqual(credential.cost_api_status, app_module.COST_API_UNSUPPORTED)

        with patch.object(app_module, "get_cached_cost_overview") as get_overview:
            repeated_response = self.client.get(
                "/account/{}/costs/data".format(credential.id)
            )

        self.assertEqual(repeated_response.status_code, 200)
        self.assertIn(
            "学生订阅暂不支持费用 API",
            repeated_response.get_json()["html"],
        )
        get_overview.assert_not_called()

    def test_cost_refresh_rechecks_unsupported_subscription(self):
        self.login()
        credential = self.add_credential()
        credential.cost_api_status = app_module.COST_API_UNSUPPORTED
        app_module.db.session.commit()
        overview = {
            "currency": "USD",
            "month_to_date": 0,
            "latest_daily_cost": 0,
            "data_through": None,
            "peak_daily_cost": 0,
            "peak_daily_date": None,
            "chart_min_cost": 0,
            "chart_max_cost": 0,
            "chart_zero_ratio": 0.5,
            "daily": [],
            "services": [],
            "resources": [],
            "warnings": [],
            "is_empty": True,
            "fetched_at": datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc),
        }

        with patch.object(
            app_module,
            "get_cached_cost_overview",
            return_value=overview,
        ) as get_overview:
            response = self.client.get(
                "/account/{}/costs/data?refresh=1".format(credential.id)
            )

        self.assertEqual(response.status_code, 200)
        get_overview.assert_called_once_with(credential, force_refresh=True)
        app_module.db.session.refresh(credential)
        self.assertEqual(credential.cost_api_status, app_module.COST_API_SUPPORTED)

    def test_cost_data_endpoint_returns_json_when_account_is_missing(self):
        self.login()

        response = self.client.get("/account/999/costs/data")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"], "账号不存在或已删除")

    def test_cost_overview_uses_cache_and_refresh_can_bypass_it(self):
        credential = self.add_credential()
        overview = {"month_to_date": 1.0}

        with patch.object(app_module, "azure_credential", return_value=object()), \
                patch.object(
                    app_module.cost_management,
                    "get_cost_overview",
                    return_value=overview,
                ) as get_overview:
            first = app_module.get_cached_cost_overview(credential)
            second = app_module.get_cached_cost_overview(credential)
            refreshed = app_module.get_cached_cost_overview(credential, force_refresh=True)

        self.assertIs(first, overview)
        self.assertIs(second, overview)
        self.assertIs(refreshed, overview)
        self.assertEqual(get_overview.call_count, 2)

    def test_cost_rate_limit_uses_expired_cache(self):
        credential = self.add_credential()
        overview = {"month_to_date": 1.0, "warnings": []}
        app_module.cost_cache[credential.id] = {
            "signature": app_module.cost_cache_signature(credential),
            "created_at": -app_module.COST_CACHE_SECONDS - 1,
            "overview": overview,
        }
        error = app_module.cost_management.CostManagementError(
            "请求过于频繁",
            status_code=429,
            error_code="TooManyRequests",
            retry_after=75,
        )

        with patch.object(app_module, "azure_credential", return_value=object()), \
                patch.object(
                    app_module.cost_management,
                    "get_cost_overview",
                    side_effect=error,
                ):
            result = app_module.get_cached_cost_overview(credential)

        self.assertEqual(result["month_to_date"], 1.0)
        self.assertTrue(result["is_stale"])
        self.assertTrue(any("显示缓存数据" in item for item in result["warnings"]))

    def test_cost_rate_limit_cooldown_avoids_repeated_azure_calls(self):
        credential = self.add_credential()
        error = app_module.cost_management.CostManagementError(
            "请求过于频繁",
            status_code=429,
            error_code="TooManyRequests",
            retry_after=45,
        )

        with patch.object(app_module, "azure_credential", return_value=object()), \
                patch.object(
                    app_module.cost_management,
                    "get_cost_overview",
                    side_effect=error,
                ) as get_overview:
            with self.assertRaises(app_module.cost_management.CostManagementError):
                app_module.get_cached_cost_overview(credential)
            with self.assertRaises(app_module.cost_management.CostManagementError) as repeated:
                app_module.get_cached_cost_overview(credential)

        self.assertEqual(get_overview.call_count, 1)
        self.assertEqual(repeated.exception.status_code, 429)
        self.assertGreaterEqual(repeated.exception.retry_after, 59)

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
