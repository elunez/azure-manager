import base64
import binascii
import os
import re
import secrets
import string
import time
import uuid
from math import ceil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Lock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import click
from azure.core.exceptions import ClientAuthenticationError, HttpResponseError
from flask import Flask, abort, flash, has_request_context, jsonify, redirect, render_template, request, session, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, or_, text
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

import function
import cost_management
from credential_security import (
    CredentialCipher,
    derive_session_key,
    load_master_key,
)


VM_LOCATIONS = [
    ("eastasia", "香港"),
    ("southeastasia", "新加坡"),
    ("japanwest", "日本大阪"),
    ("japaneast", "日本东京"),
    ("koreacentral", "韩国中部"),
    ("koreasouth", "韩国南部"),
    ("northcentralus", "美国中北部"),
    ("westus2", "美国西部 2"),
    ("westus3", "美国西部 3"),
    ("centralus", "美国中部"),
    ("westcentralus", "美国中西部"),
    ("eastus", "美国东部"),
    ("eastus2", "美国东部 2"),
    ("southcentralus", "美国中南部"),
    ("australiaeast", "澳大利亚东部"),
    ("northeurope", "欧洲北部"),
    ("uksouth", "英国南部"),
    ("southafricanorth", "南非北部"),
    ("canadacentral", "加拿大中部"),
    ("francecentral", "法国中部"),
    ("germanywestcentral", "德国中西部"),
    ("norwayeast", "挪威东部"),
    ("switzerlandnorth", "瑞士北部"),
    ("uaenorth", "阿联酋北部"),
    ("qatarcentral", "卡塔尔中部"),
    ("brazilsouth", "巴西南部"),
    ("australiacentral", "澳大利亚中部"),
    ("australiasoutheast", "澳大利亚东南部"),
    ("southindia", "印度南部"),
    ("westindia", "印度西部"),
    ("ukwest", "英国西部"),
    ("canadaeast", "加拿大东部"),
]
VM_SIZES = [
    ("Standard_B2ats_v2", "B2s（2 核 1 GB，AMD）"),
    ("Standard_B2pts_v2", "B2s（2 核 1 GB，ARM）"),
    ("Standard_B1s", "B1s（1 核 1 GB，AMD）"),
]
DISK_SIZES = (64, 128, 256)
ACCOUNT_PAGE_SIZE = 10
PAGE_SIZE_OPTIONS = (10, 20, 50)
DEFAULT_VM_CACHE_DAYS = 1
MIN_VM_CACHE_DAYS = 1
MAX_VM_CACHE_DAYS = 30
SECONDS_PER_DAY = 24 * 60 * 60
MAX_REQUEST_BYTES = 256 * 1024
SESSION_LIFETIME_HOURS = 8
LOGIN_IP_FAILURE_LIMIT = 5
LOGIN_IP_WINDOW_SECONDS = 5 * 60
LOGIN_ACCOUNT_FAILURE_LIMIT = 10
LOGIN_ACCOUNT_WINDOW_SECONDS = 15 * 60
LOGIN_LOCK_SECONDS = 15 * 60
LOGIN_ATTEMPT_MAX_KEYS = 5000
LOGIN_AUDIT_MAX_RECORDS = 2000
TASK_STATUSES = ("排队中", "执行中", "成功", "失败", "中断")
ACTIVE_TASK_STATUSES = ("排队中", "执行中")
RESOURCE_GROUP_PATTERN = re.compile(r"^[A-Za-z0-9_.()\-]{1,90}$")
VM_USERNAME_ALPHABET = string.ascii_lowercase + string.digits
VM_PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!@#%_-"


master_key, master_key_source, generated_key_path = load_master_key()
credential_cipher = CredentialCipher(master_key)


def environment_flag(name, default):
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
database_uri = os.environ.get(
    "AZURE_MANAGER_DATABASE_URI",
    "sqlite:///{}".format(os.path.join(app.root_path, "database.db")),
)
app.config["SQLALCHEMY_DATABASE_URI"] = database_uri
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = derive_session_key(master_key)
app.config["SESSION_COOKIE_SECURE"] = environment_flag(
    "AZURE_MANAGER_SECURE_COOKIE",
    True,
)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=SESSION_LIFETIME_HOURS)
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "请先登录后再访问此页面"
login_manager.needs_refresh_message = "请重新登录后再访问此页面"
task_executor = ThreadPoolExecutor(max_workers=max(1, int(os.environ.get("AZURE_MANAGER_WORKERS", "4"))))
cost_cache = {}
cost_rate_limit_cooldowns = {}
cost_query_locks = {}
cost_cache_lock = Lock()
vm_cache = {}
vm_query_locks = {}
vm_cache_lock = Lock()
login_ip_attempts = {}
login_account_attempts = {}
login_attempts_lock = Lock()
COST_CACHE_SECONDS = 1800
COST_RATE_LIMIT_COOLDOWN_SECONDS = 60
COST_API_SUPPORTED = "supported"
COST_API_UNSUPPORTED = "unsupported"
DEFAULT_TIMEZONE = "Asia/Shanghai"
TIMEZONE_OPTIONS = (
    ("Asia/Shanghai", "上海（Asia/Shanghai）"),
    ("Asia/Hong_Kong", "香港（Asia/Hong_Kong）"),
    ("Asia/Tokyo", "东京（Asia/Tokyo）"),
    ("Asia/Seoul", "首尔（Asia/Seoul）"),
    ("Asia/Singapore", "新加坡（Asia/Singapore）"),
    ("Australia/Sydney", "悉尼（Australia/Sydney）"),
    ("Europe/London", "伦敦（Europe/London）"),
    ("Europe/Paris", "巴黎（Europe/Paris）"),
    ("America/New_York", "纽约（America/New_York）"),
    ("America/Los_Angeles", "洛杉矶（America/Los_Angeles）"),
    ("UTC", "协调世界时（UTC）"),
)


class ErrorReferenceException(RuntimeError):
    """携带已记录的公开错误编号，避免异常上抛时重复生成编号。"""


def friendly_error_message(error, context="操作失败"):
    """将常见 Azure 异常转换为可直接展示的中文提示。"""
    if isinstance(error, ErrorReferenceException):
        return str(error)

    error_text = str(error)
    message = None
    if "AADSTS7000222" in error_text:
        message = "Azure 客户端密钥已过期，请编辑该管理账户并更新凭据"
    elif "AADSTS7000215" in error_text:
        message = "Azure 客户端密钥无效，请确认填写的是密钥值而不是密钥 ID"
    elif "AADSTS700016" in error_text:
        message = "Azure 应用不存在或租户不匹配，请检查客户端 ID 和租户 ID"
    elif isinstance(error, ClientAuthenticationError):
        message = "Azure 身份验证失败，请检查客户端 ID、客户端密钥和租户 ID"
    elif isinstance(error, HttpResponseError):
        response = getattr(error, "response", None)
        status_code = getattr(error, "status_code", None) or getattr(response, "status_code", None)
        messages = {
            401: "Azure 身份验证失败，请更新该管理账户的凭据",
            403: "Azure 权限不足，请检查应用权限和订阅授权",
            404: "Azure 资源不存在或已被删除，请刷新后重试",
            409: "Azure 资源当前状态不允许此操作，请稍后重试",
            429: "Azure 请求过于频繁，请稍后重试",
        }
        message = messages.get(status_code, "Azure 操作失败，请查看应用日志了解详细原因")

    if message is not None:
        app.logger.warning("%s: %s", context, error)
        return message

    error_reference = uuid.uuid4().hex[:10].upper()
    app.logger.error(
        "%s，错误编号=%s",
        context,
        error_reference,
        exc_info=(type(error), error, error.__traceback__),
    )
    return "操作失败，请联系管理员并提供错误编号：{}".format(error_reference)


def csrf_token():
    token = session.get("_csrf_token")
    if token is None:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


app.jinja_env.globals["csrf_token"] = csrf_token


def user_timezone_name(user=None):
    if user is None:
        if not has_request_context() or not current_user.is_authenticated:
            return DEFAULT_TIMEZONE
        user = current_user
    timezone_name = getattr(user, "timezone", None)
    try:
        ZoneInfo(timezone_name or DEFAULT_TIMEZONE)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return DEFAULT_TIMEZONE
    return timezone_name or DEFAULT_TIMEZONE


def format_local_datetime(value, timezone_name=None, date_format="%Y-%m-%d %H:%M:%S"):
    if value is None:
        return ""
    if not isinstance(value, datetime):
        return str(value)
    source = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    target_timezone = user_timezone_name() if timezone_name is None else timezone_name
    try:
        return source.astimezone(ZoneInfo(target_timezone)).strftime(date_format)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return source.astimezone(ZoneInfo(DEFAULT_TIMEZONE)).strftime(date_format)


app.jinja_env.filters["local_datetime"] = format_local_datetime


@app.before_request
def validate_csrf_token():
    if request.method != "POST":
        return None
    expected = session.get("_csrf_token", "")
    provided = request.form.get("csrf_token", "")
    if expected and provided and secrets.compare_digest(expected, provided):
        return None
    flash("页面已过期，请刷新后重新提交")
    return redirect(url_for("index" if current_user.is_authenticated else "login"))


@app.errorhandler(ClientAuthenticationError)
@app.errorhandler(HttpResponseError)
def azure_request_failed(error):
    flash(friendly_error_message(error, context="Azure 请求失败"))
    return redirect(url_for("index"))


@app.errorhandler(400)
def bad_request(error):
    return render_template("500.html", message="请求参数不正确"), 400


@app.errorhandler(401)
def unauthorized(error):
    return redirect(url_for("login"))


@app.errorhandler(404)
def page_not_found(error):
    return render_template("404.html"), 404


@app.errorhandler(413)
def request_too_large(error):
    return render_template(
        "500.html",
        message="请求内容过大，不能超过 256 KB",
    ), 413


@app.errorhandler(500)
def internal_server_error(error):
    original_error = getattr(error, "original_exception", None) or error
    message = friendly_error_message(original_error, context="未处理请求异常")
    return render_template("500.html", message=message), 500


class Credential(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account = db.Column(db.String(120), nullable=False)
    client_id = db.Column(db.String(60), nullable=False)
    client_secret = db.Column(db.Text, nullable=False)
    tenant_id = db.Column(db.String(60), nullable=False)
    subscription_id = db.Column(db.String(60), nullable=False)
    cost_api_status = db.Column(db.String(20), nullable=True)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    def get_client_secret(self):
        return credential_cipher.decrypt(self.client_secret)

    def set_client_secret(self, plaintext):
        self.client_secret = credential_cipher.encrypt(plaintext)


class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(20))
    password_hash = db.Column(db.String(128))
    timezone = db.Column(db.String(64), nullable=False, default=DEFAULT_TIMEZONE)
    vm_cache_days = db.Column(db.Integer, nullable=False, default=DEFAULT_VM_CACHE_DAYS)
    default_vm_script = db.Column(db.Text, nullable=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password, method="pbkdf2:sha256")

    def validate_password(self, password):
        return check_password_hash(self.password_hash, password)

    def get_default_vm_script(self):
        return credential_cipher.decrypt(self.default_vm_script) if self.default_vm_script else ""

    def set_default_vm_script(self, base64_script):
        self.default_vm_script = (
            credential_cipher.encrypt(base64_script) if base64_script else None
        )


class OperationLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    credential_id = db.Column(db.Integer, nullable=True)
    account = db.Column(db.String(120), nullable=False)
    action = db.Column(db.String(30), nullable=False)
    target = db.Column(db.String(180), nullable=False)
    status = db.Column(db.String(20), nullable=False)
    detail = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    finished_at = db.Column(db.DateTime, nullable=True)

    @property
    def duration_seconds(self):
        end_time = self.finished_at or datetime.utcnow()
        return max(0, int((end_time - self.created_at).total_seconds()))


class LoginAudit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), nullable=False)
    ip_address = db.Column(db.String(45), nullable=False)
    status = db.Column(db.String(20), nullable=False)
    detail = db.Column(db.String(120), nullable=False)
    user_agent = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


def migrate_credential_secrets():
    encrypted_values_present = False
    try:
        for credential in Credential.query.all():
            if credential_cipher.is_encrypted(credential.client_secret):
                encrypted_values_present = True
                credential.get_client_secret()
            else:
                credential.set_client_secret(credential.client_secret)
        db.session.commit()
    except Exception:
        db.session.rollback()
        if master_key_source == "generated" and encrypted_values_present and generated_key_path:
            generated_key_path.unlink(missing_ok=True)
        raise


def migrate_credential_updated_at():
    inspector = inspect(db.engine)
    columns = {column["name"] for column in inspector.get_columns(Credential.__tablename__)}
    if "updated_at" in columns:
        return

    timestamp_type = "DATETIME" if db.engine.dialect.name in {"sqlite", "mysql"} else "TIMESTAMP"
    with db.engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE credential ADD COLUMN updated_at {}".format(timestamp_type)
        ))
        connection.execute(text(
            "UPDATE credential SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL"
        ))


def migrate_credential_cost_api_status():
    inspector = inspect(db.engine)
    columns = {column["name"] for column in inspector.get_columns(Credential.__tablename__)}
    if "cost_api_status" in columns:
        return

    with db.engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE credential ADD COLUMN cost_api_status VARCHAR(20)"
        ))


def migrate_user_timezone():
    inspector = inspect(db.engine)
    columns = {column["name"] for column in inspector.get_columns(User.__tablename__)}
    if "timezone" not in columns:
        with db.engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE user ADD COLUMN timezone VARCHAR(64)"
            ))
    with db.engine.begin() as connection:
        connection.execute(
            text("UPDATE user SET timezone = :timezone WHERE timezone IS NULL OR timezone = ''"),
            {"timezone": DEFAULT_TIMEZONE},
        )


def migrate_user_default_vm_script():
    inspector = inspect(db.engine)
    columns = {column["name"] for column in inspector.get_columns(User.__tablename__)}
    if "default_vm_script" in columns:
        return

    with db.engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE user ADD COLUMN default_vm_script TEXT"
        ))


def migrate_user_vm_cache_days():
    inspector = inspect(db.engine)
    columns = {column["name"] for column in inspector.get_columns(User.__tablename__)}
    if "vm_cache_days" not in columns:
        with db.engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE user ADD COLUMN vm_cache_days INTEGER"
            ))
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE user SET vm_cache_days = :cache_days "
                "WHERE vm_cache_days IS NULL "
                "OR vm_cache_days < :minimum "
                "OR vm_cache_days > :maximum"
            ),
            {
                "cache_days": DEFAULT_VM_CACHE_DAYS,
                "minimum": MIN_VM_CACHE_DAYS,
                "maximum": MAX_VM_CACHE_DAYS,
            },
        )


def mark_interrupted_operations():
    now = datetime.utcnow()
    stale_operations = OperationLog.query.filter(OperationLog.status.in_(ACTIVE_TASK_STATUSES)).all()
    for operation_log in stale_operations:
        operation_log.status = "中断"
        operation_log.detail = "应用重启，任务执行结果未知，请检查 Azure 中的实际状态"
        operation_log.finished_at = now
    if stale_operations:
        db.session.commit()


def ensure_database_schema():
    with app.app_context():
        db.create_all()
        migrate_credential_updated_at()
        migrate_credential_cost_api_status()
        migrate_user_timezone()
        migrate_user_default_vm_script()
        migrate_user_vm_cache_days()
        migrate_credential_secrets()
        mark_interrupted_operations()


ensure_database_schema()


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def azure_credential(credential_record):
    return function.create_credential_object(
        credential_record.tenant_id,
        credential_record.client_id,
        credential_record.get_client_secret(),
    )


def clear_cost_cache(credential_id):
    with cost_cache_lock:
        cost_cache.pop(credential_id, None)
        cost_rate_limit_cooldowns.pop(credential_id, None)


def clear_vm_cache(credential_id):
    with vm_cache_lock:
        vm_cache.pop(credential_id, None)


def vm_query_lock(credential_id):
    with vm_cache_lock:
        return vm_query_locks.setdefault(credential_id, Lock())


def vm_cache_signature(credential_record):
    return (
        credential_record.client_id,
        credential_record.tenant_id,
        credential_record.subscription_id,
    )


def cached_vm_list(credential_record, now, cache_seconds):
    signature = vm_cache_signature(credential_record)
    with vm_cache_lock:
        cached = vm_cache.get(credential_record.id)
        if not cached or cached["signature"] != signature:
            return None
        if now - cached["created_at"] >= cache_seconds:
            vm_cache.pop(credential_record.id, None)
            return None
        return cached["vms"]


def get_cached_vm_list(credential_record, cache_days, force_refresh=False):
    cache_seconds = cache_days * SECONDS_PER_DAY
    if not force_refresh:
        cached = cached_vm_list(credential_record, time.monotonic(), cache_seconds)
        if cached is not None:
            return cached

    with vm_query_lock(credential_record.id):
        if force_refresh:
            clear_vm_cache(credential_record.id)
        else:
            cached = cached_vm_list(credential_record, time.monotonic(), cache_seconds)
            if cached is not None:
                return cached

        credential = azure_credential(credential_record)
        vms = function.list_vms(credential_record.subscription_id, credential)
        with vm_cache_lock:
            vm_cache[credential_record.id] = {
                "signature": vm_cache_signature(credential_record),
                "created_at": time.monotonic(),
                "vms": vms,
            }
        return vms


def cost_query_lock(credential_id):
    with cost_cache_lock:
        return cost_query_locks.setdefault(credential_id, Lock())


def cost_cache_signature(credential_record):
    return (
        credential_record.client_id,
        credential_record.tenant_id,
        credential_record.subscription_id,
    )


def cached_cost_overview(credential_record, now, allow_expired=False):
    signature = cost_cache_signature(credential_record)
    with cost_cache_lock:
        cached = cost_cache.get(credential_record.id)
        if not cached or cached["signature"] != signature:
            return None
        if not allow_expired and now - cached["created_at"] >= COST_CACHE_SECONDS:
            return None
        return cached["overview"]


def rate_limit_remaining(credential_id, now):
    with cost_cache_lock:
        cooldown_until = cost_rate_limit_cooldowns.get(credential_id, 0)
        if cooldown_until <= now:
            cost_rate_limit_cooldowns.pop(credential_id, None)
            return 0
        return max(1, ceil(cooldown_until - now))


def stale_cost_overview(overview, retry_after):
    stale = dict(overview)
    stale["warnings"] = list(overview.get("warnings", [])) + [
        "Azure 费用接口正在限流，当前显示缓存数据，可在 {} 秒后刷新".format(retry_after)
    ]
    stale["is_stale"] = True
    return stale


def get_cached_cost_overview(credential_record, force_refresh=False):
    now = time.monotonic()
    cached = cached_cost_overview(credential_record, now)
    if cached is not None and not force_refresh:
        return cached

    retry_after = rate_limit_remaining(credential_record.id, now)
    if retry_after:
        stale = cached_cost_overview(credential_record, now, allow_expired=True)
        if stale is not None:
            return stale_cost_overview(stale, retry_after)
        raise cost_management.CostManagementError(
            "Azure 费用接口正在限流，请在 {} 秒后重试".format(retry_after),
            status_code=429,
            error_code="429",
            retry_after=retry_after,
        )

    with cost_query_lock(credential_record.id):
        now = time.monotonic()
        cached = cached_cost_overview(credential_record, now)
        if cached is not None and not force_refresh:
            return cached
        retry_after = rate_limit_remaining(credential_record.id, now)
        if retry_after:
            stale = cached_cost_overview(credential_record, now, allow_expired=True)
            if stale is not None:
                return stale_cost_overview(stale, retry_after)
            raise cost_management.CostManagementError(
                "Azure 费用接口正在限流，请在 {} 秒后重试".format(retry_after),
                status_code=429,
                error_code="429",
                retry_after=retry_after,
            )

        credential = azure_credential(credential_record)
        try:
            overview = cost_management.get_cost_overview(
                credential_record.subscription_id,
                credential,
            )
        except cost_management.CostManagementError as error:
            if error.status_code != 429:
                raise
            cooldown = max(
                error.retry_after or COST_RATE_LIMIT_COOLDOWN_SECONDS,
                COST_RATE_LIMIT_COOLDOWN_SECONDS,
            )
            with cost_cache_lock:
                cost_rate_limit_cooldowns[credential_record.id] = time.monotonic() + cooldown
            stale = cached_cost_overview(
                credential_record,
                time.monotonic(),
                allow_expired=True,
            )
            if stale is not None:
                return stale_cost_overview(stale, cooldown)
            raise cost_management.CostManagementError(
                "Azure 费用接口正在限流，请在 {} 秒后重试".format(cooldown),
                status_code=429,
                error_code=error.error_code,
                retry_after=cooldown,
            ) from error

        with cost_cache_lock:
            cost_rate_limit_cooldowns.pop(credential_record.id, None)
            cost_cache[credential_record.id] = {
                "signature": cost_cache_signature(credential_record),
                "created_at": time.monotonic(),
                "overview": overview,
            }
        return overview


def validate_uuid(value, label, errors):
    try:
        return str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError):
        errors.append("{}格式不正确".format(label))
        return value


def credential_form_data(form, require_secret=True):
    values = {
        "account": form.get("account", "").strip(),
        "client_id": form.get("client_id", "").strip(),
        "client_secret": form.get("client_secret", "").strip(),
        "tenant_id": form.get("tenant_id", "").strip(),
        "subscription_id": form.get("subscription_id", "").strip(),
    }
    errors = []
    if not values["account"]:
        errors.append("账号名称不能为空")
    elif len(values["account"]) > 120:
        errors.append("账号名称不能超过 120 个字符")
    values["client_id"] = validate_uuid(values["client_id"], "客户端 ID", errors)
    values["tenant_id"] = validate_uuid(values["tenant_id"], "租户 ID", errors)
    values["subscription_id"] = validate_uuid(values["subscription_id"], "订阅 ID", errors)
    if require_secret and not values["client_secret"]:
        errors.append("客户端密钥不能为空")
    if len(values["client_secret"]) > 512:
        errors.append("客户端密钥长度不正确")
    return values, errors


def create_operation_log(credential_record, action, target):
    operation_log = OperationLog(
        credential_id=credential_record.id,
        account=credential_record.account,
        action=action,
        target=target,
        status="排队中",
        detail="任务等待执行",
    )
    db.session.add(operation_log)
    db.session.commit()
    return operation_log.id


def update_operation_log(log_id, status, detail, finished=False):
    operation_log = OperationLog.query.get(log_id)
    if operation_log is None:
        return
    operation_log.status = status
    operation_log.detail = detail[:2000]
    operation_log.finished_at = datetime.utcnow() if finished else None
    db.session.commit()


def task_error_detail(error):
    """返回仅供登录管理员查看的任务原始错误内容。"""
    return str(error) or error.__class__.__name__


def run_operation(log_id, credential_id, operation, args):
    with app.app_context():
        try:
            update_operation_log(log_id, "执行中", "任务正在执行")
            credential_record = Credential.query.get(credential_id)
            if credential_record is None:
                raise RuntimeError("关联的 Azure 管理账户已被删除")
            credential = azure_credential(credential_record)
            success_detail = operation(credential_record.subscription_id, credential, *args)
        except Exception as error:
            app.logger.exception("任务日志 %s 执行失败", log_id)
            update_operation_log(
                log_id,
                "失败",
                task_error_detail(error),
                finished=True,
            )
        else:
            clear_vm_cache(credential_id)
            update_operation_log(log_id, "成功", success_detail or "操作已完成", finished=True)
        finally:
            db.session.remove()


def queue_operation(credential_record, action, target, operation, args):
    log_id = create_operation_log(credential_record, action, target)
    try:
        task_executor.submit(run_operation, log_id, credential_record.id, operation, args)
    except Exception as error:
        public_message = friendly_error_message(
            error,
            context="任务日志 {} 入队失败".format(log_id),
        )
        update_operation_log(
            log_id,
            "失败",
            task_error_detail(error),
            finished=True,
        )
        raise ErrorReferenceException(public_message) from error
    return log_id


def create_vm_operation(subscription_id, credential, name, location, username, password, size, os_name, custom, disk):
    function.create_resource_group(subscription_id, credential, name, location)
    function.create_or_update_vm(
        subscription_id,
        credential,
        name,
        location,
        username,
        password,
        size,
        os_name,
        custom,
        disk,
    )
    return "VM 登录凭据：用户名 {}，密码 {}".format(username, password)


def generate_vm_credentials():
    username = "vmuser" + "".join(secrets.choice(VM_USERNAME_ALPHABET) for _ in range(8))
    password = "Aa1!" + "".join(secrets.choice(VM_PASSWORD_ALPHABET) for _ in range(24))
    return username, password


def build_vm_names(base_name, count):
    if count == 1:
        return [base_name]
    return ["{}-{}".format(base_name, index) for index in range(1, count + 1)]


def validate_vm_name(name, os_name):
    max_length = 15 if os_name == "WinData_2022" else 64
    return (
        len(name) <= max_length
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", name) is not None
        and not name.endswith("-")
    )


def vm_form_data(form):
    values = {
        "tag": form.get("tag", "").strip(),
        "location": form.get("location", ""),
        "size": form.get("size", ""),
        "count": form.get("count", "1"),
        "os": form.get("os", ""),
        "custom": form.get("custom", "").strip(),
        "disk": form.get("disk", ""),
    }
    errors = []
    try:
        count = int(values["count"])
    except (TypeError, ValueError):
        count = 0
    if count not in range(1, 6):
        errors.append("创建数量必须在 1 到 5 之间")
    names = build_vm_names(values["tag"], count) if count else []
    if not names or any(not validate_vm_name(name, values["os"]) for name in names):
        errors.append("VM 名称必须以字母开头，只能包含字母、数字和连字符，并符合系统长度限制")
    if values["location"] not in dict(VM_LOCATIONS):
        errors.append("位置选项无效")
    if values["size"] not in dict(VM_SIZES):
        errors.append("机型选项无效")
    if values["os"] not in function.IMAGES:
        errors.append("系统镜像选项无效")
    elif not function.is_size_image_compatible(values["size"], values["os"]):
        errors.append("所选机型与镜像架构不兼容")
    try:
        disk = int(values["disk"])
    except (TypeError, ValueError):
        disk = 0
    if disk not in DISK_SIZES:
        errors.append("系统磁盘大小无效")
    validate_base64_script(values["custom"], errors, "自定义脚本")
    values["count"] = count
    values["disk"] = disk
    values["names"] = names
    return values, errors


def validate_base64_script(value, errors, label):
    if not value:
        return
    try:
        decoded = base64.b64decode(value, validate=True)
        if len(decoded) > 65535:
            errors.append("{}解码后不能超过 64 KB".format(label))
    except (binascii.Error, ValueError):
        errors.append("{}不是有效的 Base64 内容".format(label))


def validate_vm_target(form):
    resource_group = form.get("resource_group", "").strip()
    vm_name = form.get("vm_name", "").strip()
    if not RESOURCE_GROUP_PATTERN.fullmatch(resource_group) or not vm_name or len(vm_name) > 64:
        abort(400)
    return resource_group, vm_name


def create_vm_template(credential_record, values=None, errors=None):
    if values is None:
        values = {"custom": current_user.get_default_vm_script()}
    return render_template(
        "createvm.html",
        credential=credential_record,
        values=values,
        errors=errors or [],
        locations=VM_LOCATIONS,
        sizes=VM_SIZES,
        images=function.IMAGES,
        disk_sizes=DISK_SIZES,
    )


def page_number(value):
    try:
        return max(1, int(value or 1))
    except (TypeError, ValueError):
        return 1


def parse_page_size(value, default):
    try:
        page_size = int(value or default)
    except (TypeError, ValueError):
        return default
    return page_size if page_size in PAGE_SIZE_OPTIONS else default


def user_vm_cache_days():
    cache_days = getattr(current_user, "vm_cache_days", DEFAULT_VM_CACHE_DAYS)
    if MIN_VM_CACHE_DAYS <= cache_days <= MAX_VM_CACHE_DAYS:
        return cache_days
    return DEFAULT_VM_CACHE_DAYS


def account_values(credential_record=None):
    if credential_record is None:
        return {}
    return {
        "account": credential_record.account,
        "client_id": credential_record.client_id,
        "tenant_id": credential_record.tenant_id,
        "subscription_id": credential_record.subscription_id,
    }


def account_modal_state(mode, page, page_size, credential_record=None, values=None, errors=None):
    return {
        "mode": mode,
        "page": page,
        "page_size": page_size,
        "credential_id": credential_record.id if credential_record else None,
        "action": url_for(
            "account_edit" if credential_record else "account_add",
            **({"credential_id": credential_record.id} if credential_record else {}),
        ),
        "values": values if values is not None else account_values(credential_record),
        "errors": errors or [],
    }


def render_account_index(page=1, modal_state=None, status_code=200, page_size=ACCOUNT_PAGE_SIZE):
    pagination = Credential.query.order_by(Credential.id).paginate(
        page=page,
        per_page=page_size,
        error_out=False,
    )
    if pagination.pages and page > pagination.pages:
        pagination = Credential.query.order_by(Credential.id).paginate(
            page=pagination.pages,
            per_page=page_size,
            error_out=False,
        )
    return render_template(
        "index.html",
        pagination=pagination,
        account_modal=modal_state,
        page_size_options=PAGE_SIZE_OPTIONS,
        selected_page_size=page_size,
    ), status_code


def login_client_ip():
    return (request.remote_addr or "未知").strip()[:45]


def _login_attempt_state(states, key, now, window_seconds):
    state = states.get(key)
    if state is None:
        if len(states) >= LOGIN_ATTEMPT_MAX_KEYS:
            stale_keys = [
                state_key
                for state_key, existing_state in states.items()
                if existing_state["locked_until"] <= now
                and not any(
                    now - timestamp < window_seconds
                    for timestamp in existing_state["failures"]
                )
            ]
            for stale_key in stale_keys:
                states.pop(stale_key, None)
            while len(states) >= LOGIN_ATTEMPT_MAX_KEYS:
                states.pop(next(iter(states)))
        state = {"failures": [], "locked_until": 0}
        states[key] = state
    state["failures"] = [
        timestamp
        for timestamp in state["failures"]
        if now - timestamp < window_seconds
    ]
    if state["locked_until"] <= now:
        state["locked_until"] = 0
    return state


def login_retry_after(ip_address, account_key, now=None):
    current_time = time.monotonic() if now is None else now
    retry_after = 0
    with login_attempts_lock:
        ip_state = _login_attempt_state(
            login_ip_attempts,
            ip_address,
            current_time,
            LOGIN_IP_WINDOW_SECONDS,
        )
        retry_after = max(retry_after, ceil(ip_state["locked_until"] - current_time))
        if account_key:
            account_state = _login_attempt_state(
                login_account_attempts,
                account_key,
                current_time,
                LOGIN_ACCOUNT_WINDOW_SECONDS,
            )
            retry_after = max(
                retry_after,
                ceil(account_state["locked_until"] - current_time),
            )
    return max(0, retry_after)


def register_login_failure(ip_address, account_key, now=None):
    current_time = time.monotonic() if now is None else now
    retry_after = 0
    with login_attempts_lock:
        ip_state = _login_attempt_state(
            login_ip_attempts,
            ip_address,
            current_time,
            LOGIN_IP_WINDOW_SECONDS,
        )
        ip_state["failures"].append(current_time)
        if len(ip_state["failures"]) >= LOGIN_IP_FAILURE_LIMIT:
            ip_state["locked_until"] = current_time + LOGIN_LOCK_SECONDS
        retry_after = max(retry_after, ceil(ip_state["locked_until"] - current_time))

        if account_key:
            account_state = _login_attempt_state(
                login_account_attempts,
                account_key,
                current_time,
                LOGIN_ACCOUNT_WINDOW_SECONDS,
            )
            account_state["failures"].append(current_time)
            if len(account_state["failures"]) >= LOGIN_ACCOUNT_FAILURE_LIMIT:
                account_state["locked_until"] = current_time + LOGIN_LOCK_SECONDS
            retry_after = max(
                retry_after,
                ceil(account_state["locked_until"] - current_time),
            )
    return max(0, retry_after)


def clear_login_failures(ip_address, account_key):
    with login_attempts_lock:
        login_ip_attempts.pop(ip_address, None)
        if account_key:
            login_account_attempts.pop(account_key, None)


def record_login_audit(username, ip_address, status, detail):
    try:
        db.session.add(LoginAudit(
            username=(username or "未填写")[:120],
            ip_address=ip_address[:45],
            status=status[:20],
            detail=detail[:120],
            user_agent=request.headers.get("User-Agent", "未知")[:255],
        ))
        db.session.commit()
        overflow_ids = [
            audit_id
            for audit_id, in (
                LoginAudit.query
                .with_entities(LoginAudit.id)
                .order_by(LoginAudit.id.desc())
                .offset(LOGIN_AUDIT_MAX_RECORDS)
                .all()
            )
        ]
        if overflow_ids:
            LoginAudit.query.filter(LoginAudit.id.in_(overflow_ids)).delete(
                synchronize_session=False
            )
            db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("写入登录审计失败")


def locked_login_response(retry_after):
    minutes = max(1, ceil(retry_after / 60))
    flash("登录尝试过多，请在 {} 分钟后重试".format(minutes))
    response = app.make_response((render_template("login.html"), 429))
    response.headers["Retry-After"] = str(retry_after)
    return response


@app.cli.command()
@click.option("--drop", is_flag=True, help="Create after drop.")
def initdb(drop):
    """Initialize the database."""
    if drop:
        db.drop_all()
    db.create_all()
    click.echo("Initialized database.")


@app.cli.command()
@click.argument("username")
@click.argument("password")
def admin(username, password):
    """Create or update the administrator."""
    user = User.query.first()
    if user is None:
        user = User(username=username)
        db.session.add(user)
    else:
        user.username = username
    user.set_password(password)
    db.session.commit()
    click.echo("Done.")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.first()
        ip_address = login_client_ip()
        account_key = user.username if user is not None and username == user.username else None
        retry_after = login_retry_after(ip_address, account_key)
        if retry_after:
            record_login_audit(username, ip_address, "已拦截", "登录尝试过多")
            return locked_login_response(retry_after)
        if account_key and user.validate_password(password):
            clear_login_failures(ip_address, account_key)
            record_login_audit(username, ip_address, "成功", "登录成功")
            session.clear()
            login_user(user)
            session.permanent = True
            flash("登录成功")
            return redirect(url_for("index"))
        retry_after = register_login_failure(ip_address, account_key)
        record_login_audit(username, ip_address, "失败", "用户名或密码错误")
        if retry_after:
            return locked_login_response(retry_after)
        flash("用户名或密码错误")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    session.clear()
    flash("已退出登录")
    return redirect(url_for("login"))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def system_settings():
    values = {
        "username": current_user.username or "",
        "timezone": user_timezone_name(current_user),
        "vm_cache_days": user_vm_cache_days(),
        "default_vm_script": current_user.get_default_vm_script(),
    }
    errors = {"general": [], "account": []}
    valid_sections = {"general", "account", "audit"}
    active_section = (
        request.form.get("section", "")
        if request.method == "POST"
        else request.args.get("tab", "general")
    )
    if active_section not in valid_sections:
        active_section = "general"
    if request.method == "POST":
        if active_section == "general":
            values["timezone"] = request.form.get("timezone", "").strip()
            values["vm_cache_days"] = request.form.get(
                "vm_cache_days", str(values["vm_cache_days"])
            ).strip()
            values["default_vm_script"] = request.form.get(
                "default_vm_script", values["default_vm_script"]
            ).strip()
            if values["timezone"] not in dict(TIMEZONE_OPTIONS):
                errors["general"].append("时区选项无效")
            try:
                cache_days = int(values["vm_cache_days"])
            except (TypeError, ValueError):
                cache_days = 0
            if not MIN_VM_CACHE_DAYS <= cache_days <= MAX_VM_CACHE_DAYS:
                errors["general"].append(
                    "VM 列表缓存时间必须在 {} 到 {} 天之间".format(
                        MIN_VM_CACHE_DAYS,
                        MAX_VM_CACHE_DAYS,
                    )
                )
            validate_base64_script(
                values["default_vm_script"],
                errors["general"],
                "默认 VM 脚本",
            )
            if not errors["general"]:
                current_user.timezone = values["timezone"]
                current_user.vm_cache_days = cache_days
                current_user.set_default_vm_script(values["default_vm_script"])
                db.session.commit()
                flash("常规设置已更新")
                return redirect(url_for("system_settings", tab="general"))
        elif active_section == "account":
            values["username"] = request.form.get("username", "").strip()
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            password_confirmation = request.form.get("password_confirmation", "")
            username_changed = values["username"] != current_user.username
            password_changed = bool(new_password or password_confirmation)

            if not values["username"]:
                errors["account"].append("登录账号名不能为空")
            elif len(values["username"]) > 20:
                errors["account"].append("登录账号名不能超过 20 个字符")
            elif User.query.filter(
                User.username == values["username"],
                User.id != current_user.id,
            ).first() is not None:
                errors["account"].append("登录账号名已存在")
            if username_changed or password_changed:
                if not current_password or not current_user.validate_password(current_password):
                    errors["account"].append("当前密码不正确")
            if password_changed:
                if len(new_password) < 8:
                    errors["account"].append("新密码不能少于 8 个字符")
                if new_password != password_confirmation:
                    errors["account"].append("两次输入的新密码不一致")

            if not errors["account"]:
                current_user.username = values["username"]
                if password_changed:
                    current_user.set_password(new_password)
                db.session.commit()
                flash("账号安全设置已更新")
                return redirect(url_for("system_settings", tab="account"))
        else:
            errors["account"].append("设置类型无效，请刷新页面后重试")

    audit_page = page_number(request.args.get("audit_page"))
    audit_pagination = LoginAudit.query.order_by(LoginAudit.id.desc()).paginate(
        page=audit_page,
        per_page=ACCOUNT_PAGE_SIZE,
        error_out=False,
    )
    return render_template(
        "settings.html",
        values=values,
        errors=errors,
        active_tab=active_section,
        timezone_options=TIMEZONE_OPTIONS,
        audit_pagination=audit_pagination,
    )


@app.route("/")
@login_required
def index():
    page = page_number(request.args.get("page"))
    page_size = parse_page_size(request.args.get("per_page"), ACCOUNT_PAGE_SIZE)
    modal_state = None
    modal_mode = request.args.get("modal", "")
    if modal_mode == "add":
        modal_state = account_modal_state("add", page, page_size)
    elif modal_mode == "edit":
        credential_id = request.args.get("credential_id", "")
        if credential_id.isdigit():
            credential_record = Credential.query.get_or_404(int(credential_id))
            modal_state = account_modal_state("edit", page, page_size, credential_record)
    return render_account_index(page, modal_state, page_size=page_size)


@app.route("/account/add", methods=["GET", "POST"])
@login_required
def account_add():
    if request.method == "GET":
        return redirect(url_for(
            "index",
            modal="add",
            page=page_number(request.args.get("page")),
            per_page=parse_page_size(request.args.get("per_page"), ACCOUNT_PAGE_SIZE),
        ))
    values = {}
    errors = []
    values, errors = credential_form_data(request.form)
    if not errors:
        try:
            credential = function.create_credential_object(
                values["tenant_id"], values["client_id"], values["client_secret"]
            )
            function.validate_credential(values["subscription_id"], credential)
        except Exception as error:
            errors.append(friendly_error_message(error, context="验证 Azure 管理账户失败"))
    page = page_number(request.form.get("page"))
    page_size = parse_page_size(request.form.get("per_page"), ACCOUNT_PAGE_SIZE)
    if errors:
        modal_state = account_modal_state(
            "add", page, page_size, values=values, errors=errors
        )
        return render_account_index(page, modal_state, page_size=page_size)
    credential_record = Credential(
        account=values["account"],
        client_id=values["client_id"],
        tenant_id=values["tenant_id"],
        subscription_id=values["subscription_id"],
    )
    credential_record.set_client_secret(values["client_secret"])
    db.session.add(credential_record)
    db.session.commit()
    last_page = max(1, (Credential.query.count() + page_size - 1) // page_size)
    flash("管理账户已添加")
    return redirect(url_for("index", page=last_page, per_page=page_size))


@app.route("/account/<int:credential_id>/edit", methods=["GET", "POST"])
@login_required
def account_edit(credential_id):
    credential_record = Credential.query.get_or_404(credential_id)
    page = page_number(request.values.get("page"))
    page_size = parse_page_size(request.values.get("per_page"), ACCOUNT_PAGE_SIZE)
    if request.method == "GET":
        return redirect(url_for(
            "index",
            modal="edit",
            credential_id=credential_id,
            page=page,
            per_page=page_size,
        ))
    values, errors = credential_form_data(request.form, require_secret=False)
    client_secret = values["client_secret"] or credential_record.get_client_secret()
    if not errors:
        try:
            credential = function.create_credential_object(
                values["tenant_id"], values["client_id"], client_secret
            )
            function.validate_credential(values["subscription_id"], credential)
        except Exception as error:
            errors.append(friendly_error_message(error, context="验证 Azure 管理账户失败"))
    if errors:
        modal_state = account_modal_state(
            "edit", page, page_size, credential_record, values=values, errors=errors
        )
        return render_account_index(page, modal_state, page_size=page_size)
    subscription_changed = values["subscription_id"] != credential_record.subscription_id
    credential_record.account = values["account"]
    credential_record.client_id = values["client_id"]
    credential_record.tenant_id = values["tenant_id"]
    credential_record.subscription_id = values["subscription_id"]
    if subscription_changed:
        credential_record.cost_api_status = None
    if values["client_secret"]:
        credential_record.set_client_secret(client_secret)
    db.session.commit()
    clear_cost_cache(credential_record.id)
    clear_vm_cache(credential_record.id)
    flash("管理账户已更新并通过 Azure 身份验证")
    return redirect(url_for("index", page=page, per_page=page_size))


@app.route("/account/<int:credential_id>/delete", methods=["POST"])
@login_required
def account_delete(credential_id):
    credential_record = Credential.query.get_or_404(credential_id)
    db.session.delete(credential_record)
    db.session.commit()
    clear_cost_cache(credential_id)
    clear_vm_cache(credential_id)
    flash("本地管理账户已删除，Azure 资源不受影响")
    return redirect(url_for(
        "index",
        page=page_number(request.form.get("page")),
        per_page=parse_page_size(request.form.get("per_page"), ACCOUNT_PAGE_SIZE),
    ))


@app.route("/account/<int:credential_id>/vm/create", methods=["GET", "POST"])
@login_required
def create_vm(credential_id):
    credential_record = Credential.query.get_or_404(credential_id)
    if request.method == "POST":
        values, errors = vm_form_data(request.form)
        if errors:
            return create_vm_template(credential_record, values, errors), 400
        for name in values["names"]:
            username, password = generate_vm_credentials()
            queue_operation(
                credential_record,
                "创建 VM",
                name,
                create_vm_operation,
                (
                    name,
                    values["location"],
                    username,
                    password,
                    values["size"],
                    values["os"],
                    values["custom"],
                    values["disk"],
                ),
            )
        flash("已提交 {} 个 VM 创建任务".format(len(values["names"])))
        return redirect(url_for("operation_logs", credential_id=credential_id))
    return create_vm_template(credential_record)


@app.route("/account/<int:credential_id>/vms")
@login_required
def vm_list(credential_id):
    credential_record = Credential.query.get_or_404(credential_id)
    return render_template("list.html", credential=credential_record)


@app.route("/account/<int:credential_id>/vms/data")
@login_required
def vm_list_data(credential_id):
    credential_record = Credential.query.get(credential_id)
    if credential_record is None:
        return jsonify(error="账号不存在或已删除"), 404
    try:
        vms = get_cached_vm_list(
            credential_record,
            user_vm_cache_days(),
            force_refresh=request.args.get("refresh") == "1",
        )
    except Exception as error:
        return jsonify(error=friendly_error_message(
            error,
            context="读取账号 {} 的 VM 列表失败".format(credential_record.id),
        )), 502
    return jsonify(
        html=render_template("_vm_rows.html", vms=vms, credential=credential_record),
        count=len(vms),
    )


@app.route("/costs")
@login_required
def cost_overview():
    credentials = Credential.query.order_by(Credential.id.asc()).all()
    selected_credential = None
    credential_id = request.args.get("credential_id", "").strip()
    if credential_id.isdigit():
        selected_credential = Credential.query.get_or_404(int(credential_id))
    elif credentials:
        selected_credential = credentials[0]
    return render_template(
        "costs.html",
        credentials=credentials,
        selected_credential=selected_credential,
    )


@app.route("/account/<int:credential_id>/costs/data")
@login_required
def cost_overview_data(credential_id):
    credential_record = Credential.query.get(credential_id)
    if credential_record is None:
        return jsonify(error="账号不存在或已删除"), 404
    force_refresh = request.args.get("refresh") == "1"
    if credential_record.cost_api_status == COST_API_UNSUPPORTED and not force_refresh:
        return jsonify(html=render_template("_cost_unavailable.html"))
    try:
        overview = get_cached_cost_overview(
            credential_record,
            force_refresh=force_refresh,
        )
    except cost_management.CostManagementUnsupportedError as error:
        app.logger.info(
            "账号 %s 的订阅报价不支持 Cost Management API，code=%s",
            credential_record.id,
            error.error_code,
        )
        if credential_record.cost_api_status != COST_API_UNSUPPORTED:
            credential_record.cost_api_status = COST_API_UNSUPPORTED
            db.session.commit()
        return jsonify(html=render_template("_cost_unavailable.html"))
    except cost_management.CostManagementError as error:
        app.logger.warning(
            "读取账号 %s 的费用失败，HTTP=%s，code=%s",
            credential_record.id,
            error.status_code,
            error.error_code,
        )
        response_status = 429 if error.status_code == 429 else 502
        return jsonify(error=str(error), retry_after=error.retry_after), response_status
    except Exception as error:
        return jsonify(error=friendly_error_message(
            error,
            context="读取账号 {} 的费用失败".format(credential_record.id),
        )), 502
    if credential_record.cost_api_status != COST_API_SUPPORTED:
        credential_record.cost_api_status = COST_API_SUPPORTED
    if db.session.is_modified(credential_record):
        db.session.commit()
    return jsonify(
        html=render_template(
            "_cost_overview.html",
            overview=overview,
            credential=credential_record,
        )
    )


@app.route("/account/<int:credential_id>/vm/start", methods=["POST"])
@login_required
def start_vm(credential_id):
    credential_record = Credential.query.get_or_404(credential_id)
    resource_group, vm_name = validate_vm_target(request.form)
    queue_operation(
        credential_record, "启动 VM", "{}/{}".format(resource_group, vm_name), function.start_vm,
        (resource_group, vm_name),
    )
    flash("启动任务已提交")
    return redirect(url_for("vm_list", credential_id=credential_id))


@app.route("/account/<int:credential_id>/vm/stop", methods=["POST"])
@login_required
def stop_vm(credential_id):
    credential_record = Credential.query.get_or_404(credential_id)
    resource_group, vm_name = validate_vm_target(request.form)
    queue_operation(
        credential_record, "停止 VM", "{}/{}".format(resource_group, vm_name), function.stop_vm,
        (resource_group, vm_name),
    )
    flash("停止任务已提交")
    return redirect(url_for("vm_list", credential_id=credential_id))


@app.route("/account/<int:credential_id>/vm/change-ip", methods=["POST"])
@login_required
def changeip_vm(credential_id):
    credential_record = Credential.query.get_or_404(credential_id)
    resource_group, vm_name = validate_vm_target(request.form)
    queue_operation(
        credential_record, "更换 IP", "{}/{}".format(resource_group, vm_name), function.change_ip,
        (resource_group, vm_name),
    )
    flash("更换 IP 任务已提交")
    return redirect(url_for("vm_list", credential_id=credential_id))


@app.route("/account/<int:credential_id>/vm/delete", methods=["POST"])
@login_required
def delete_vm(credential_id):
    credential_record = Credential.query.get_or_404(credential_id)
    resource_group, vm_name = validate_vm_target(request.form)
    queue_operation(
        credential_record, "删除 VM", vm_name, function.delete_vm,
        (resource_group,),
    )
    flash("VM 删除任务已提交")
    return redirect(url_for("index"))


@app.route("/logs")
@login_required
def operation_logs():
    status = request.args.get("status", "").strip()
    credential_id = request.args.get("credential_id", "").strip()
    keyword = request.args.get("q", "").strip()
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    selected_page_size = parse_page_size(request.args.get("per_page"), ACCOUNT_PAGE_SIZE)

    query = OperationLog.query
    if status in TASK_STATUSES:
        query = query.filter(OperationLog.status == status)
    if credential_id.isdigit():
        query = query.filter(OperationLog.credential_id == int(credential_id))
    if keyword:
        pattern = "%{}%".format(keyword)
        query = query.filter(or_(
            OperationLog.account.ilike(pattern),
            OperationLog.action.ilike(pattern),
            OperationLog.target.ilike(pattern),
        ))
    pagination = query.order_by(OperationLog.created_at.desc()).paginate(
        page=page,
        per_page=selected_page_size,
        error_out=False,
    )
    if pagination.pages and page > pagination.pages:
        pagination = query.order_by(OperationLog.created_at.desc()).paginate(
            page=pagination.pages,
            per_page=selected_page_size,
            error_out=False,
        )
    has_active_tasks = OperationLog.query.filter(OperationLog.status.in_(ACTIVE_TASK_STATUSES)).first() is not None
    return render_template(
        "logs.html",
        pagination=pagination,
        credentials=Credential.query.order_by(Credential.account).all(),
        statuses=TASK_STATUSES,
        selected_status=status,
        selected_credential_id=credential_id,
        keyword=keyword,
        has_active_tasks=has_active_tasks,
        page_size_options=PAGE_SIZE_OPTIONS,
        selected_page_size=selected_page_size,
    )


if __name__ == "__main__":
    app.run(port=18888, host="127.0.0.1")
