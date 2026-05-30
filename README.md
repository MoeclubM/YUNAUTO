# 移动云电脑虚拟机自动开机监控

持续监测 `https://yun.6ka.cn/` 账号下「移动云云电脑」的所有虚拟机，
**发现关机自动开机**。纯 HTTP 接口实现，无需浏览器，资源占用极低，适合服务器后台长期运行。

账号密码通过**环境变量**传入，代码中不含任何凭据，可安全放在公开仓库。

---

## 🚀 服务器一键部署（Docker）

> 前提：服务器已安装 Docker（含 `docker compose`）。把下面命令里的账号密码换成你自己的。

```bash
git clone https://github.com/MoeclubM/YUNAUTO.git
cd YUNAUTO
printf 'YUN_USERNAME=你的账号\nYUN_PASSWORD=你的密码\n' > .env
docker compose up -d --build
```

完成。常用操作：

```bash
docker logs -f yunauto      # 查看实时日志
docker compose restart      # 重启
docker compose down         # 停止并移除
```

容器已设 `restart: unless-stopped`，服务器重启后会自动拉起。

### 方式二：不克隆仓库，直接 docker run

```bash
docker run -d --name yunauto --restart unless-stopped \
  -e YUN_USERNAME=你的账号 \
  -e YUN_PASSWORD=你的密码 \
  ghcr.io/moeclubm/yunauto:latest
```

> 该镜像由 GitHub Actions 自动构建发布。若首次拉取报无权限，需到 GitHub
> 仓库 → Packages → `yunauto` → Package settings 里把可见性设为 **Public**。

---

## 环境变量

| 变量 | 说明 | 默认 |
|------|------|------|
| `YUN_USERNAME` | 登录账号（必填） | — |
| `YUN_PASSWORD` | 登录密码（必填） | — |
| `YUN_INTERVAL` | 每轮检查间隔（秒） | `60` |
| `YUN_COOLDOWN` | 同一台机器两次开机指令最小间隔（秒） | `180` |
| `YUN_LOG_FILE` | 额外写日志到文件（留空则只输出到控制台） | 空 |

---

## 本地直接运行（不用 Docker）

```bash
pip install -r requirements.txt
export YUN_USERNAME=你的账号
export YUN_PASSWORD=你的密码
python monitor.py          # 持续监控
python monitor.py --once   # 只检查一轮（测试用）
```

Windows PowerShell：`$env:YUN_USERNAME="..."; $env:YUN_PASSWORD="..."; python monitor.py`

---

## 工作原理

1. 表单登录 `/login.php` 建立会话（会话失效自动重登）。
2. 每轮：
   - 拉取 `/yundiannao/` 实例列表，解析各机器 `card_id` 与 CSRF token；
   - POST `action=auto_sync_mycloud_status` 获取每台机器实时状态；
   - `machine_status == available` 视为运行中，跳过；
   - 关机中/开机中等**过渡态**等待，不操作（服务器此时会拒绝操作）；
   - 确认关机（`machine_status=shutdown`）则 POST `action=start_cloud` 开机。

> 经实测：该站点开机操作无需短信验证码。
