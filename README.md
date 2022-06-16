## Azure 管理

原版：https://github.com/1injex/azure-manager

在原版的基础上 去广告、汉化。

### 创建应用

**使用下面的脚本创建应用**

```bash
docker run -itd --name az \
--restart always \
-p 8888:8888 \
dqjdda/azure-manager:1.0
```

**初始化管理员账号与密码**

```bash
docker exec -it az flask admin 用户名 密码
```

虚拟机默认 `ssh` 端口为 `22`

默认账号与密码：

```bash
账号：defaultuser
密码：Thisis.yourpassword1
```

## 更多帮助

安装教程：https://www.ydyno.com/archives/1404.html