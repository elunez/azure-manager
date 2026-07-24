import base64
import binascii
import os
import re
import secrets
import string
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import click
from azure.core.exceptions import ClientAuthenticationError, HttpResponseError
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, or_, text
from werkzeug.security import check_password_hash, generate_password_hash

import function
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
ACCOUNT_PAGE_SIZE = 8
LOG_PAGE_SIZE = 8
PAGE_SIZE_OPTIONS = (8, 20, 50)
TASK_STATUSES = ("排队中", "执行中", "成功", "失败", "中断")
ACTIVE_TASK_STATUSES = ("排队中", "执行中")
RESOURCE_GROUP_PATTERN = re.compile(r"^[A-Za-z0-9_.()\-]{1,90}$")
VM_USERNAME_ALPHABET = string.ascii_lowercase + string.digits
VM_PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!@#%_-"


master_key, master_key_source, generated_key_path = load_master_key()
credential_cipher = CredentialCipher(master_key)

app = Flask(__name__)
database_uri = os.environ.get(
    "AZURE_MANAGER_DATABASE_URI",
    "sqlite:///{}".format(os.path.join(app.root_path, "database.db")),
)
app.config["SQLALCHEMY_DATABASE_URI"] = database_uri
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = derive_session_key(master_key)
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
task_executor = ThreadPoolExecutor(max_workers=max(1, int(os.environ.get("AZURE_MANAGER_WORKERS", "4"))))


def friendly_error_message(error):
    """将常见 Azure 异常转换为可直接展示的中文提示。"""
    error_text = str(error)
    if "AADSTS7000222" in error_text:
        return "Azure 客户端密钥已过期，请编辑该管理账户并更新凭据"
    if "AADSTS7000215" in error_text:
        return "Azure 客户端密钥无效，请确认填写的是密钥值而不是密钥 ID"
    if "AADSTS700016" in error_text:
        return "Azure 应用不存在或租户不匹配，请检查客户端 ID 和租户 ID"
    if isinstance(error, ClientAuthenticationError):
        return "Azure 身份验证失败，请检查客户端 ID、客户端密钥和租户 ID"
    if isinstance(error, HttpResponseError):
        response = getattr(error, "response", None)
        status_code = getattr(error, "status_code", None) or getattr(response, "status_code", None)
        messages = {
            401: "Azure 身份验证失败，请更新该管理账户的凭据",
            403: "Azure 权限不足，请检查应用权限和订阅授权",
            404: "Azure 资源不存在或已被删除，请刷新后重试",
            409: "Azure 资源当前状态不允许此操作，请稍后重试",
            429: "Azure 请求过于频繁，请稍后重试",
        }
        return messages.get(status_code, "Azure 操作失败，请查看应用日志了解详细原因")
    return str(error) or error.__class__.__name__


def csrf_token():
    token = session.get("_csrf_token")
    if token is None:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


app.jinja_env.globals["csrf_token"] = csrf_token


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
    app.logger.warning("Azure 请求失败: %s", error)
    flash(friendly_error_message(error))
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


@app.errorhandler(500)
def internal_server_error(error):
    return render_template("500.html"), 500


class Credential(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account = db.Column(db.String(120), nullable=False)
    client_id = db.Column(db.String(60), nullable=False)
    client_secret = db.Column(db.Text, nullable=False)
    tenant_id = db.Column(db.String(60), nullable=False)
    subscription_id = db.Column(db.String(60), nullable=False)
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
    name = db.Column(db.String(20))
    username = db.Column(db.String(20))
    password_hash = db.Column(db.String(128))

    def set_password(self, password):
        self.password_hash = generate_password_hash(password, method="pbkdf2:sha256")

    def validate_password(self, password):
        return check_password_hash(self.password_hash, password)


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
            update_operation_log(log_id, "失败", friendly_error_message(error), finished=True)
        else:
            update_operation_log(log_id, "成功", success_detail or "操作已完成", finished=True)
        finally:
            db.session.remove()


def queue_operation(credential_record, action, target, operation, args):
    log_id = create_operation_log(credential_record, action, target)
    try:
        task_executor.submit(run_operation, log_id, credential_record.id, operation, args)
    except Exception as error:
        update_operation_log(log_id, "失败", str(error), finished=True)
        raise
    return log_id


def create_vm_operation(subscription_id, credential, name, location, username, password, size, os_name, custom,
                        accelerated_networking, disk, spot):
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
        accelerated_networking,
        disk,
        spot,
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
        "acc": form.get("acc", "False"),
        "disk": form.get("disk", ""),
        "spot": form.get("spot", "False"),
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
        errors.append("区域选项无效")
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
    if values["acc"] not in ("True", "False"):
        errors.append("加速网络选项无效")
    if values["spot"] not in ("True", "False"):
        errors.append("Spot 选项无效")
    if values["custom"]:
        try:
            custom_data = base64.b64decode(values["custom"], validate=True)
            if len(custom_data) > 65535:
                errors.append("自定义脚本解码后不能超过 64 KB")
        except (binascii.Error, ValueError):
            errors.append("自定义脚本不是有效的 Base64 内容")
    values["count"] = count
    values["disk"] = disk
    values["names"] = names
    return values, errors


def validate_vm_target(form):
    resource_group = form.get("resource_group", "").strip()
    vm_name = form.get("vm_name", "").strip()
    if not RESOURCE_GROUP_PATTERN.fullmatch(resource_group) or not vm_name or len(vm_name) > 64:
        abort(400)
    return resource_group, vm_name


def create_vm_template(credential_record, values=None, errors=None):
    return render_template(
        "createvm.html",
        credential=credential_record,
        values=values or {},
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
        user = User(username=username, name="Admin")
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
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        user = User.query.first()
        if user is not None and username == user.username and user.validate_password(password):
            login_user(user)
            flash("登录成功")
            return redirect(url_for("index"))
        flash("用户名或密码错误")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("已退出登录")
    return redirect(url_for("login"))


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
            errors.append(friendly_error_message(error))
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
            errors.append(friendly_error_message(error))
    if errors:
        modal_state = account_modal_state(
            "edit", page, page_size, credential_record, values=values, errors=errors
        )
        return render_account_index(page, modal_state, page_size=page_size)
    credential_record.account = values["account"]
    credential_record.client_id = values["client_id"]
    credential_record.tenant_id = values["tenant_id"]
    credential_record.subscription_id = values["subscription_id"]
    if values["client_secret"]:
        credential_record.set_client_secret(client_secret)
    db.session.commit()
    flash("管理账户已更新并通过 Azure 身份验证")
    return redirect(url_for("index", page=page, per_page=page_size))


@app.route("/account/<int:credential_id>/delete", methods=["POST"])
@login_required
def account_delete(credential_id):
    credential_record = Credential.query.get_or_404(credential_id)
    db.session.delete(credential_record)
    db.session.commit()
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
                    values["acc"],
                    values["disk"],
                    values["spot"],
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
        credential = azure_credential(credential_record)
        vms = function.list_vms(credential_record.subscription_id, credential)
    except Exception as error:
        app.logger.exception("读取账号 %s 的 VM 列表失败", credential_record.id)
        return jsonify(error=friendly_error_message(error)), 502
    return jsonify(
        html=render_template("_vm_rows.html", vms=vms, credential=credential_record),
        count=len(vms),
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
        credential_record, "删除资源组", "{}/{}".format(resource_group, vm_name), function.delete_vm,
        (resource_group,),
    )
    flash("资源组删除任务已提交")
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
    selected_page_size = parse_page_size(request.args.get("per_page"), LOG_PAGE_SIZE)

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
    app.run(port=8888, host="0.0.0.0")
