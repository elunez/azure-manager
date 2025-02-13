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

### 增加管理账户

提取API教程：https://www.ydyno.com/archives/1394.html

**添加账户**
```bash
邮箱：可填写你注册azure的邮箱
密码：appId|password|tenant|subscriptions
```
