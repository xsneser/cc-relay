#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CC Relay - Windows 独立打包与后台静默启动主入口 (main_launcher.py)
用于 PyInstaller 编译为 cc-relay.exe 或由 pythonw 直接静默双击运行。
功能:
  1. 自动定位执行目录，将 config.json / records.jsonl / prompts.json 锚定在 exe 同级目录
  2. 若初次运行，自动从预置模板初始化 config.json 与 ui.html
  3. 单实例检测: 若服务已在运行，不重复拉起，直接唤起默认浏览器打开控制台
  4. 后台静默运行 8400 (API) 与 8610 (UI) 服务
  5. 自动拉起默认浏览器展示 Matrix 深色控制台
"""
import os
import sys
import time
import socket
import shutil
import threading
import webbrowser
from types import SimpleNamespace

# 1. 确定运行路径 (兼容 PyInstaller 打包与源码运行)
if getattr(sys, 'frozen', False):
    EXE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    BUNDLE_DIR = getattr(sys, '_MEIPASS', EXE_DIR)
else:
    EXE_DIR = os.path.dirname(os.path.abspath(__file__))
    BUNDLE_DIR = EXE_DIR

# 确保中转服务的所有数据文件持久化在 exe 同级目录
os.environ["CC_RELAY_DIR"] = EXE_DIR
try:
    os.chdir(EXE_DIR)
except Exception:
    pass

# 2. 静默模式标准输出重定向 (防止 Windows GUI 无控制台时写 stdout/stderr 报错)
LOG_FILE = os.path.join(EXE_DIR, "relay_launch.log")
class SafeWriter:
    def __init__(self, filename):
        self.filename = filename
    def write(self, s):
        try:
            with open(self.filename, "a", encoding="utf-8", errors="ignore") as f:
                f.write(s)
        except Exception:
            pass
    def flush(self):
        pass

if sys.stdout is None:
    sys.stdout = SafeWriter(LOG_FILE)
if sys.stderr is None:
    sys.stderr = SafeWriter(LOG_FILE)

def show_error(title, msg):
    """Windows 弹窗报错，确保用户在无终端时也能看到关键错误"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, str(msg), str(title), 0x10)
    except Exception:
        print(f"[{title}] {msg}", file=sys.stderr)

# 3. 初始资源文件释放/补全
def ensure_essential_files():
    conf_path = os.path.join(EXE_DIR, "config.json")
    if not os.path.isfile(conf_path):
        # 优先使用同目录或打包内的 config.example.json
        ex_paths = [
            os.path.join(EXE_DIR, "config.example.json"),
            os.path.join(BUNDLE_DIR, "config.example.json"),
            os.path.join(BUNDLE_DIR, "config.json")
        ]
        copied = False
        for ep in ex_paths:
            if os.path.isfile(ep):
                try:
                    shutil.copy2(ep, conf_path)
                    copied = True
                    break
                except Exception:
                    pass
        if not copied:
            # 内置最小默认配置
            default_conf = {
                "listen_host": "127.0.0.1",
                "listen_port": 8400,
                "ui_port": 8610,
                "fake_api_key": "sk-relay-local-0000",
                "router": {
                    "route": "hybrid",
                    "deepseek_model": "deepseek-flash",
                    "codex_model": "gpt-5.6-sol",
                    "gemini_model": "gemini-3.7-flash-low",
                    "modifier_mode": "original",
                    "tier_modifier": {
                        "main": "original",
                        "opus": "original",
                        "sonnet": "original",
                        "fast": "original",
                        "agent": "original"
                    },
                    "gemini_modifier": "builtin",
                    "tiers": {
                        "main": "deepseek-flash",
                        "opus": "gpt-5.6-sol",
                        "sonnet": "deepseek-flash",
                        "fast": "deepseek-flash",
                        "agent": "deepseek-flash"
                    },
                    "effort": "medium",
                    "tier_efforts": {
                        "main": "medium",
                        "opus": "medium",
                        "sonnet": "medium",
                        "fast": "medium",
                        "agent": "medium"
                    }
                },
                "upstreams": {
                    "deepseek": {
                        "base": "https://api.deepseek.com/anthropic",
                        "key_env": "DEEPSEEK_API_KEY",
                        "proxy_url": ""
                    },
                    "codex": {
                        "base": "http://127.0.0.1:8317/v1",
                        "key_env": "CODEX_API_KEY",
                        "proxy_url": ""
                    },
                    "gemini": {
                        "base": "http://127.0.0.1:8045/v1",
                        "key_env": "GEMINI_API_KEY",
                        "proxy_url": ""
                    }
                }
            }
            try:
                import json
                with open(conf_path, "w", encoding="utf-8") as f:
                    json.dump(default_conf, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"写入 config.json 失败: {e}", file=sys.stderr)

    # 释放 ui.html (如果磁盘没有，从打包目录复制出来供用户自定义修改)
    ui_path = os.path.join(EXE_DIR, "ui.html")
    if not os.path.isfile(ui_path):
        bundled_ui = os.path.join(BUNDLE_DIR, "ui.html")
        if os.path.isfile(bundled_ui) and bundled_ui != ui_path:
            try:
                shutil.copy2(bundled_ui, ui_path)
            except Exception:
                pass

# 4. 单实例与端口检测
def is_port_busy(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, int(port)), timeout=0.6):
            return True
    except Exception:
        return False

def main():
    ensure_essential_files()

    # 读取配置确定监听端口
    ui_port = 8610
    relay_port = 8400
    host = "127.0.0.1"
    try:
        import json
        conf_path = os.path.join(EXE_DIR, "config.json")
        if os.path.isfile(conf_path):
            with open(conf_path, "r", encoding="utf-8") as f:
                c = json.load(f)
                ui_port = c.get("ui_port", 8610)
                relay_port = c.get("listen_port", 8400)
                host = c.get("listen_host", "127.0.0.1")
    except Exception:
        pass

    ui_url = f"http://{host}:{ui_port}"

    # 单实例检查: 如果 8400 或 8610 已经在跑，直接唤醒浏览器
    if is_port_busy(relay_port, host) or is_port_busy(ui_port, host):
        webbrowser.open(ui_url)
        sys.exit(0)

    # 导入 cc_relay 模块
    try:
        import cc_relay
    except Exception as e:
        show_error("CC Relay 启动失败", f"无法加载中转服务模块 cc_relay:\n{e}")
        sys.exit(1)

    # 异步拉起浏览器检测线程
    def open_browser_when_ready():
        # 等待服务端口就绪，最多尝试 15 次 (共 3 秒)
        for _ in range(15):
            time.sleep(0.2)
            if is_port_busy(ui_port, host):
                time.sleep(0.1)
                webbrowser.open(ui_url)
                return
        # 超时保底打开
        webbrowser.open(ui_url)

    threading.Thread(target=open_browser_when_ready, daemon=True).start()

    # 启动中转主服务 (启动 8400 中转与 8610 UI)
    args = SimpleNamespace(cmd="serve", no_ui=False)
    try:
        cc_relay.serve(args)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        show_error("CC Relay 运行异常", f"服务运行中遇到未捕获异常:\n{e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
