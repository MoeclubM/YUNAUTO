# -*- coding: utf-8 -*-
"""
移动云电脑虚拟机自动开机监控
==============================
持续监测 https://yun.6ka.cn/ 账号下"移动云云电脑"的所有虚拟机，
发现关机的自动开机。纯 HTTP 接口实现，无需浏览器，适合后台长期运行。

用法：
    python monitor.py            # 持续监控（默认每 60 秒一轮）
    python monitor.py --once     # 只检查一轮后退出（用于测试）

日志同时输出到控制台和 monitor.log。
依赖：pip install requests
"""
import argparse
import html
import json
import logging
import os
import re
import sys
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Windows 控制台默认 GBK，强制 UTF-8 输出，避免中文/符号乱码
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ===================== 配置 =====================
BASE = os.getenv("YUN_BASE", "https://yun.6ka.cn")
PRODUCT_PATH = os.getenv("YUN_PRODUCT_PATH", "/yundiannao/")   # 移动云云电脑实例列表
USERNAME = os.getenv("YUN_USERNAME", "")       # 账号：通过环境变量传入
PASSWORD = os.getenv("YUN_PASSWORD", "")       # 密码：通过环境变量传入

CHECK_INTERVAL = int(os.getenv("YUN_INTERVAL", "60"))      # 每轮检查间隔（秒）
START_COOLDOWN = int(os.getenv("YUN_COOLDOWN", "180"))     # 同一台机器两次"开机"指令的最小间隔（秒）
REQUEST_TIMEOUT = int(os.getenv("YUN_TIMEOUT", "25"))
RUNNING_MACHINE_STATUS = "available"   # 该状态视为"已开机/正常"
# 过渡态：正在关机/开机/重启/重装等，此时服务器不允许操作，需等待而非开机
# 经实测，过渡态的 machine_status 形如 onShutdown/onStartup/...（以 on 开头），
# 且 label 含"中"字（关机中/开机中/重启中/重装中/同步中）。两者任一命中即视为过渡态。
LOG_FILE = os.getenv("YUN_LOG_FILE", "")   # 留空则只输出到控制台（Docker 用 docker logs 查看）
# ===============================================

_handlers = [logging.StreamHandler(sys.stdout)]
if LOG_FILE:
    _handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=_handlers,
)
log = logging.getLogger("yunauto")


class CloudPCMonitor:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        })
        # 网络抖动/代理瞬断自动重试
        retry = Retry(total=3, backoff_factor=1.5,
                      status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods=["GET", "POST"])
        adapter = HTTPAdapter(max_retries=retry)
        self.s.mount("https://", adapter)
        self.s.mount("http://", adapter)
        self._last_start = {}   # card_id -> 上次发送开机指令的时间戳

    # ---------- 登录 ----------
    def login(self):
        """标准表单登录，成功后会话(cookie)保存在 self.s 中。"""
        login_url = f"{BASE}/login.php"
        return_to = PRODUCT_PATH
        g = self.s.get(login_url, params={"return_to": return_to}, timeout=REQUEST_TIMEOUT)
        m = re.search(r'name=["\']form_token["\']\s+value=["\']([0-9a-f]+)', g.text)
        if not m:
            raise RuntimeError("登录页未找到 form_token，页面结构可能已变化")
        form_token = m.group(1)
        r = self.s.post(login_url, data={
            "login_type": "account",
            "return_to": return_to,
            "form_token": form_token,
            "login_honeypot": "",
            "username": USERNAME,
            "password": PASSWORD,
            "account_sms_code": "",
        }, allow_redirects=False, timeout=REQUEST_TIMEOUT)
        if r.status_code == 302 and "login.php" not in (r.headers.get("location") or ""):
            log.info("登录成功，会话已建立")
            return True
        # 登录失败：可能账号密码错误，或开启了登录保护(需短信验证码)
        snippet = ""
        try:
            j = r.json()
            snippet = j.get("message", "")
        except Exception:
            snippet = r.text[:200]
        raise RuntimeError(f"登录失败 (HTTP {r.status_code})：{snippet}")

    # ---------- 拉取实例列表 + CSRF ----------
    def fetch_console(self):
        """
        GET 实例列表页。返回 (csrf_token, cards)。
        cards: [{card_id, spec, system}]
        若会话失效会自动重新登录后重试一次。
        """
        for attempt in range(2):
            r = self.s.get(f"{BASE}{PRODUCT_PATH}", timeout=REQUEST_TIMEOUT)
            h = r.text
            if 'name="password"' in h or "/login.php" in r.url:
                log.warning("会话已失效，重新登录…")
                self.login()
                continue
            tok = re.search(r'cloudConsoleCsrfToken\s*=\s*"([0-9a-f]+)"', h)
            if not tok:
                if attempt == 0:
                    self.login()
                    continue
                raise RuntimeError("未找到 cloudConsoleCsrfToken，可能未登录或页面结构变化")
            cards = self._parse_cards(h)
            return tok.group(1), cards
        raise RuntimeError("无法获取控制台页面")

    @staticmethod
    def _parse_cards(h):
        cards = []
        for meta_raw in re.findall(r'data-card-meta="([^"]+)"\s+data-auto-status-card="1"', h):
            try:
                meta = json.loads(html.unescape(meta_raw))
                cards.append({
                    "card_id": int(meta.get("card_id")),
                    "spec": meta.get("spec_label", ""),
                })
            except Exception:
                continue
        # 兜底：若 meta 解析不到，直接抓 data-card-id
        if not cards:
            for cid in sorted(set(re.findall(r'data-card-id="(\d+)"', h))):
                cards.append({"card_id": int(cid), "spec": ""})
        return cards

    # ---------- 查询状态 ----------
    def sync_status(self, csrf_token, card_ids):
        """返回 {card_id: state_dict}。state 含 machine_status/label/status_key/action_locked。"""
        r = self.s.post(f"{BASE}{PRODUCT_PATH}", data={
            "action": "auto_sync_mycloud_status",
            "card_ids": ",".join(str(c) for c in card_ids),
            "csrf_token": csrf_token,
        }, headers={"X-Requested-With": "XMLHttpRequest"}, timeout=REQUEST_TIMEOUT)
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"状态查询失败：{data}")
        return {c["card_id"]: c for c in data.get("cards", [])}

    # ---------- 开机 ----------
    def start_cloud(self, csrf_token, card_id):
        r = self.s.post(f"{BASE}{PRODUCT_PATH}", data={
            "action": "start_cloud",
            "card_id": str(card_id),
            "csrf_token": csrf_token,
        }, headers={"X-Requested-With": "XMLHttpRequest"}, timeout=REQUEST_TIMEOUT)
        data = r.json()
        return bool(data.get("success")), data.get("message", "")

    # ---------- 一轮检查 ----------
    def check_once(self):
        csrf_token, cards = self.fetch_console()
        if not cards:
            log.warning("未发现任何云电脑实例")
            return
        ids = [c["card_id"] for c in cards]
        spec_of = {c["card_id"]: c["spec"] for c in cards}
        states = self.sync_status(csrf_token, ids)

        for cid in ids:
            st = states.get(cid, {})
            label = st.get("label", "?")
            mstatus = st.get("machine_status", "?")
            tag = f"[{cid} {spec_of.get(cid, '')}]".strip()

            if st.get("removed"):
                log.info(f"{tag} 已被移除，跳过")
                continue
            if st.get("action_locked"):
                log.info(f"{tag} 状态={label}（已锁定/到期/退订），跳过")
                continue
            if mstatus == RUNNING_MACHINE_STATUS:
                log.info(f"{tag} 状态={label} (machine_status={mstatus}) [运行中]")
                continue

            # 过渡态（关机中/开机中/重启中/重装中…）：服务器不允许操作，等待下一轮
            if str(mstatus).startswith("on") or "中" in str(label):
                log.info(f"{tag} 状态={label} (machine_status={mstatus}) 处理中，等待…")
                continue

            # 需要开机
            now = time.time()
            if now - self._last_start.get(cid, 0) < START_COOLDOWN:
                log.info(f"{tag} 状态={label} 关机，但在冷却期内（刚下过开机指令），等待…")
                continue
            log.warning(f"{tag} 状态={label} (machine_status={mstatus}) → 检测到关机，发送开机指令")
            self._last_start[cid] = now
            ok, msg = self.start_cloud(csrf_token, cid)
            if ok:
                log.info(f"{tag} 开机指令已发送成功，状态将自动同步刷新")
            else:
                log.error(f"{tag} 开机失败：{msg}")

    def run(self, once=False):
        if not USERNAME or not PASSWORD:
            log.error("未设置账号密码，请通过环境变量 YUN_USERNAME / YUN_PASSWORD 传入")
            sys.exit(2)
        log.info("=" * 60)
        log.info(f"移动云电脑自动开机监控启动  间隔={CHECK_INTERVAL}s  目标={BASE}{PRODUCT_PATH}")
        self.login()
        while True:
            try:
                self.check_once()
            except Exception as e:
                log.error(f"本轮检查出错：{e}")
                # 出错时尝试重建会话，下一轮恢复
                try:
                    self.login()
                except Exception as le:
                    log.error(f"重新登录失败：{le}")
            if once:
                break
            time.sleep(CHECK_INTERVAL)


def main():
    ap = argparse.ArgumentParser(description="移动云电脑虚拟机自动开机监控")
    ap.add_argument("--once", action="store_true", help="只检查一轮后退出")
    args = ap.parse_args()
    try:
        CloudPCMonitor().run(once=args.once)
    except KeyboardInterrupt:
        log.info("已手动停止监控")


if __name__ == "__main__":
    main()
