## Azure 管理

在原版的基础上 去广告、汉化等。

原版：https://github.com/1injex/azure-manager

## 预览图片
<img width="631" alt="image" src="https://github.com/user-attachments/assets/87ad8994-6caf-472d-8039-483c1c5d3f8b">
<img width="644" alt="image" src="https://github.com/user-attachments/assets/1e67af5d-e868-4907-b3f9-7c617a1f2a58">
<img width="668" alt="image" src="https://github.com/user-attachments/assets/3ce54dc8-9069-4f97-9454-80233a606f5f">

## 使用方法

```bash
docker run -itd --name az \
--restart always \
-p 8888:8888 \
dqjdda/azure-manager:latest
```

**ARM机器用户请使用** 

```bash
docker run -itd --name az \
--restart always \
-p 8888:8888 \
dqjdda/azure-manager:arm
```

## 挂载数据库文件

```bash
-v /path/to/your:/root/azure
```

## 重置管理密码

```bash
docker exec -it az flask admin 用户名 密码
```

### 默认VM账号密码

账号 : defaultuser
密码 : Thisis.yourpassword1

### 创建 VM 的兼容性说明

- 公网 IP 使用 Azure Standard SKU；Basic SKU 已由 Azure 退役。
- 新建 VM 不指定可用性区域，由 Azure 自动放置。
- 默认 Linux 镜像为 Debian 12 Bookworm x64 Gen2；同时提供 Debian 12 ARM64 和 Ubuntu Server 24.04 LTS。ARM 机型仅可使用 ARM64 镜像。
- 更换 IP 会自动识别地址分配方式：Dynamic IP 通过停机再启动更换；Static IP 会创建替换 IP、更新 NIC 绑定，再删除旧 IP。
- 创建失败时会在应用日志保留 Azure 原始异常，并自动删除该次创建的资源组，避免残留资源计费。
- 页面导航中的“任务日志”会保留最近 200 条创建、删除、启动、停止和换 IP 的任务结果。
- 应用启动时会自动补建缺失的数据表，不会删除已有账号或日志数据。

### 增加管理账户

提取API教程：https://www.ydyno.com/archives/1394.html

**添加账户**
```bash
邮箱：可填写你注册azure的邮箱
密码：appId|password|tenant|subscriptions
```
