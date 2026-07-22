from flask import Flask, render_template, request, url_for, redirect, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from msrest.exceptions import AuthenticationError
from msrestazure.azure_exceptions import CloudError
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import time
import function
import os
import click
import threading

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////' + os.path.join(app.root_path, 'database.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.jinja_env.filters['zip'] = zip
###
###
### for security reason, set secret key to a very random string
app.config['SECRET_KEY'] = '932ngvi3bnru3b4'
###
###
###
db = SQLAlchemy(app)
login_manager = LoginManager(app)


def friendly_error_message(error):
    """将常见 Azure 异常转换为可直接展示的中文提示。"""
    error_text = str(error)
    if 'AADSTS7000222' in error_text:
        return 'Azure 客户端密钥已过期，请更新该管理账户的凭据'
    if 'AADSTS7000215' in error_text:
        return 'Azure 客户端密钥无效，请确认填写的是密钥值而不是密钥 ID'
    if 'AADSTS700016' in error_text:
        return 'Azure 应用不存在或租户不匹配，请检查客户端 ID 和租户 ID'
    if isinstance(error, AuthenticationError):
        return 'Azure 身份验证失败，请检查客户端 ID、客户端密钥和租户 ID'
    if isinstance(error, CloudError):
        response = getattr(error, 'response', None)
        status_code = getattr(response, 'status_code', None)
        if status_code == 401:
            return 'Azure 身份验证失败，请更新该管理账户的凭据'
        if status_code == 403:
            return 'Azure 权限不足，请检查应用权限和订阅授权'
        if status_code == 404:
            return 'Azure 资源不存在或已被删除，请刷新后重试'
        if status_code == 409:
            return 'Azure 资源当前状态不允许此操作，请稍后重试'
        if status_code == 429:
            return 'Azure 请求过于频繁，请稍后重试'
        return 'Azure 操作失败，请查看应用日志了解详细原因'
    return str(error) or error.__class__.__name__


@app.errorhandler(AuthenticationError)
@app.errorhandler(CloudError)
def azure_request_failed(error):
    app.logger.warning('Azure 请求失败: %s', error)
    flash(friendly_error_message(error))
    return redirect(url_for('index'))


@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404


@app.errorhandler(401)
def page_not_found(e):
    return render_template('401.html'), 401


@app.errorhandler(500)
def page_not_found(e):
    return render_template('500.html'), 500


class Credential(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account = db.Column(db.String(60))
    client_id = db.Column(db.String(60))
    client_secret = db.Column(db.String(60))
    tenant_id = db.Column(db.String(60))
    subscription_id = db.Column(db.String(60))


class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(20))
    username = db.Column(db.String(20))
    password_hash = db.Column(db.String(128))

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def validate_password(self, password):
        return check_password_hash(self.password_hash, password)


class OperationLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    credential_id = db.Column(db.Integer, nullable=True)
    account = db.Column(db.String(60), nullable=False)
    action = db.Column(db.String(30), nullable=False)
    target = db.Column(db.String(80), nullable=False)
    status = db.Column(db.String(20), nullable=False)
    detail = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    finished_at = db.Column(db.DateTime, nullable=True)


def ensure_database_schema():
    """补建缺失表，不删除或重建已有数据。"""
    with app.app_context():
        db.create_all()


ensure_database_schema()


@login_manager.user_loader
def load_user(user_id):
    user = User.query.get(int(user_id))
    return user


def create_operation_log(credential, action, target):
    operation_log = OperationLog(
        credential_id=credential.id,
        account=credential.account,
        action=action,
        target=target,
        status='执行中',
        detail='任务已提交'
    )
    db.session.add(operation_log)
    db.session.commit()
    return operation_log.id


def update_operation_log(log_id, status, detail):
    operation_log = OperationLog.query.get(log_id)
    if operation_log is None:
        return
    operation_log.status = status
    operation_log.detail = detail[:2000]
    operation_log.finished_at = datetime.utcnow()
    db.session.commit()


def run_operation(log_id, operation, args):
    with app.app_context():
        try:
            operation(*args)
        except Exception as error:
            app.logger.exception('任务日志 %s 执行失败', log_id)
            update_operation_log(log_id, '失败', friendly_error_message(error))
        else:
            update_operation_log(log_id, '成功', '操作已完成')
        finally:
            db.session.remove()


def queue_operation(credential, action, target, operation, args):
    log_id = create_operation_log(credential, action, target)
    threading.Thread(target=run_operation, args=(log_id, operation, args)).start()


def create_vm_operation(subscription_id, credential, name, location, username, password, size, os_name, custom,
                        acc, disk, spot):
    function.create_resource_group(subscription_id, credential, name, location)
    function.create_or_update_vm(subscription_id, credential, name, location, username, password, size, os_name,
                                 custom, acc, disk, spot)


@app.cli.command()  # registe as command
@click.option('--drop', is_flag=True, help='Create after drop.')
def initdb(drop):
    """Initialize the database."""
    if drop:
        db.drop_all()
    db.create_all()
    click.echo('Initialized database.')


@app.cli.command()
@click.argument("username")
@click.argument("password")
def admin(username, password):
    """Create user."""
    db.create_all()

    user = User.query.first()
    if user is not None:
        click.echo('Updating user...')
        user.username = username
        user.set_password(password)  # set password
    else:
        click.echo('Creating user...')
        user = User(username=username, name='Admin')
        user.set_password(password)  # setpassword
        db.session.add(user)

    db.session.commit()  # commit to database
    click.echo('Done.')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        if not username or not password:
            flash('Invalid input.')
            return redirect(url_for('login'))

        user = User.query.first()
        if username == user.username and user.validate_password(password):
            login_user(user)
            flash('Login success.')
            return redirect(url_for('index'))

        flash('Invalid username or password.')
        return redirect(url_for('login'))

    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Goodbye.')
    return redirect(url_for('index'))


@app.route('/')
def index():
    if not current_user.is_authenticated:
        return redirect(url_for('login'))
    Credentials = Credential.query.all()
    return render_template('index.html', Credentials=Credentials)


@app.route('/account/add', methods=['GET', 'POST'])
def account_add():
    if request.method == 'POST':
        if not current_user.is_authenticated:
            flash('You need login')
            return redirect(url_for('login'))
        account = request.form.get('account')
        client_id = request.form.get('string').split("|")[0]
        client_secret = request.form.get('string').split("|")[1]
        tenant_id = request.form.get('string').split("|")[2]
        subscription_id = request.form.get('string').split("|")[3]
        if not account or not client_id or not client_secret or not tenant_id or not subscription_id:
            flash('Incorrect input')
            return redirect(url_for('index'))
        credential = Credential(account=account, client_id=client_id, client_secret=client_secret, tenant_id=tenant_id,
                                subscription_id=subscription_id)
        db.session.add(credential)
        db.session.commit()
        flash('Create account successful')
        return redirect(url_for('index'))
    Credentials = Credential.query.all()
    return render_template('account.html', Credentials=Credentials)


@app.route('/account/delete/<int:credential_id>', methods=['POST'])
def account_delete(credential_id):
    if not current_user.is_authenticated:
        flash('You need login')
        return redirect(url_for('login'))
    credential = Credential.query.get_or_404(credential_id)
    db.session.delete(credential)
    db.session.commit()
    flash('Delete account successful')
    return redirect(url_for('index'))


@app.route('/account/<int:credential_id>/vm/create', methods=['GET', 'POST'])
def create_vm(credential_id):
    if request.method == 'POST':
        if not current_user.is_authenticated:
            flash('You need login')
            return redirect(url_for('login'))
        credential_record = Credential.query.get_or_404(credential_id)
        client_id = credential_record.client_id
        client_secret = credential_record.client_secret
        tenant_id = credential_record.tenant_id
        subscription_id = credential_record.subscription_id
        tag = request.form.get('tag')
        location = request.form.get('location')
        size = request.form.get('size')
        os = request.form.get('os')
        set = request.form.get('set')
        custom = request.form.get('custom')
        acc = request.form.get('acc')
        disk = request.form.get('disk')
        spot = request.form.get('spot')
        username = "defaultuser"
        password = "Thisis.yourpassword1"
        credential = function.create_credential_object(tenant_id, client_id, client_secret)
        if not function.is_size_image_compatible(size, os):
            flash('所选机型与镜像架构不兼容')
            return redirect(url_for('create_vm', credential_id=credential_id))
        for i in range(int(set)):
            name = tag
            queue_operation(
                credential_record, '创建 VM', name, create_vm_operation,
                (subscription_id, credential, name, location, username, password, size, os, custom, acc, disk, spot)
            )
        flash('Creating VM, Be patient')
    info = Credential.query.all()
    credential = Credential.query.get_or_404(credential_id)
    account = credential.account
    id = credential.id
    return render_template('createvm.html', account=account, id=id, Credentials=info)


@app.route('/logs')
@login_required
def operation_logs():
    logs = OperationLog.query.order_by(OperationLog.created_at.desc()).limit(200).all()
    return render_template('logs.html', logs=logs)


@app.route('/account/<int:credential_id>/vm/delete/<string:tag>', methods=['POST'])
def delete_vm(credential_id, tag):
    if not current_user.is_authenticated:
        flash('You need login')
        return redirect(url_for('login'))
    credential_record = Credential.query.get_or_404(credential_id)
    credential = function.create_credential_object(
        credential_record.tenant_id, credential_record.client_id, credential_record.client_secret)
    queue_operation(credential_record, '删除 VM', tag, function.delete_vm,
                    (credential_record.subscription_id, credential, tag))
    flash("Deleting VM, Be patient")
    return redirect(url_for('index'))


@app.route('/account/<int:credential_id>/vm/start/<string:tag>', methods=['POST'])
def start_vm(credential_id, tag):
    if not current_user.is_authenticated:
        flash('You need login')
        return redirect(url_for('login'))
    credential_record = Credential.query.get_or_404(credential_id)
    credential = function.create_credential_object(
        credential_record.tenant_id, credential_record.client_id, credential_record.client_secret)
    queue_operation(credential_record, '启动 VM', tag, function.start_vm,
                    (credential_record.subscription_id, credential, tag))
    flash("Starting VM, Be patient")
    return redirect(url_for('index'))


@app.route('/account/<int:credential_id>/vm/stop/<string:tag>', methods=['POST'])
def stop_vm(credential_id, tag):
    if not current_user.is_authenticated:
        flash('You need login')
        return redirect(url_for('login'))
    credential_record = Credential.query.get_or_404(credential_id)
    credential = function.create_credential_object(
        credential_record.tenant_id, credential_record.client_id, credential_record.client_secret)
    queue_operation(credential_record, '停止 VM', tag, function.stop_vm,
                    (credential_record.subscription_id, credential, tag))
    flash("Stoping VM, Be patient")
    return redirect(url_for('index'))


@app.route('/account/<int:credential_id>/vm/changeip/<string:tag>', methods=['POST'])
def changeip_vm(credential_id, tag):
    if not current_user.is_authenticated:
        flash('You need login')
        return redirect(url_for('login'))
    credential_record = Credential.query.get_or_404(credential_id)
    credential = function.create_credential_object(
        credential_record.tenant_id, credential_record.client_id, credential_record.client_secret)
    queue_operation(credential_record, '更换 IP', tag, function.change_ip,
                    (credential_record.subscription_id, credential, tag))
    flash("Chaning IP, Be patient")
    return redirect(url_for('index'))


@app.route('/account/<int:credential_id>/list', methods=['GET', 'POST'])
def list(credential_id):
    if request.method == 'POST':
        if not current_user.is_authenticated:
            flash('You need login')
            return redirect(url_for('login'))
        credential = Credential.query.get_or_404(credential_id)
        id = credential.id
        account = credential.account
        client_id = credential.client_id
        client_secret = credential.client_secret
        tenant_id = credential.tenant_id
        subscription_id = credential.subscription_id
        credential = function.create_credential_object(tenant_id, client_id, client_secret)
        dict = function.list(subscription_id, credential)
        return render_template('list.html', dict=dict, id=id, account=account)


if __name__ == '__main__':
    app.run(port=8888, host="0.0.0.0")
