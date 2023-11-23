## Azure 管理

在原版的基础上 去广告、汉化等。

原版：https://github.com/1injex/azure-manager

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

## 重置管理密码

```bash
docker exec -it az flask admin 用户名 密码
```

### 默认VM账号密码

账号 : defaultuser
密码 : Thisis.yourpassword1

### 增加管理账户

提取API参数：https://www.ydyno.com/archives/1394.html

邮箱：用于多账号区分，可填写你注册azure的邮箱
密码：
```bash
appId|password|tenant|subscriptions
```
