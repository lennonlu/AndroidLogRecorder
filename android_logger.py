#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Android 自动日志采集 & 录屏工具
===============================
插上安卓手机（USB调试已开启）后运行本脚本，自动完成：
  1. 实时 logcat 日志采集（全程保存为 .log 文件）
  2. 屏幕录制（每段最长 60s，自动循环续录，无缝拼接）
  3. 崩溃 / ANR 自动检测（发现即高亮提示并单独归档）
  4. Ctrl+C 优雅停止，输出本次采集摘要

用法：python android_logger.py [--output 输出目录] [--no-record]
"""

import subprocess
import threading
import time
import os
import sys
import signal
import re
import shutil
import socket
import csv
from datetime import datetime
from pathlib import Path

# ========== 配置 ==========
# 优先使用脚本/exe同目录下的 adb，其次系统 PATH，最后 fallback 到固定路径
# PyInstaller --onefile 模式下 __file__ 指向临时目录，需要用 sys.executable 定位
if getattr(sys, 'frozen', False):
    _script_dir = os.path.dirname(sys.executable)
else:
    _script_dir = os.path.dirname(os.path.abspath(__file__))
ADB_PATH = (
    os.path.join(_script_dir, "adb.exe") if os.path.isfile(os.path.join(_script_dir, "adb.exe"))
    else (shutil.which("adb") or r"D:\Android_tools\platform-tools\adb.exe")
)
SCRCPY_DIR = os.path.join(_script_dir, "scrcpy-win64-v4.1")
SCRCPY_PATH = os.path.join(SCRCPY_DIR, "scrcpy.exe")
FFMPEG_PATH = os.path.join(SCRCPY_DIR, "ffmpeg.exe")
RECORD_SEGMENT_SEC = 60           # 每段录屏秒数
RECORD_MAX_SHORT_SIDE = 720       # 普通设备录屏短边上限
RECORD_BITRATE = "4M"             # 录屏码率
DEVICE_POLL_INTERVAL = 3          # 设备检测间隔（秒）

# 常见安卓模拟器的本机 ADB 端口
EMULATOR_CONNECTIONS = [
    ("雷电模拟器", "127.0.0.1:5555", "emulator-5554"),
    ("夜神模拟器", "127.0.0.1:62001", None),
    ("逍遥模拟器", "127.0.0.1:21503", None),
    ("MuMu模拟器", "127.0.0.1:7555", None),
    ("BlueStacks", "127.0.0.1:5555", None),
]

# ---------- 崩溃检测规则 ----------
# 采用精确匹配，减少误报。每条规则格式：(pattern, label, context_lines)
# context_lines: 命中后额外采集后续多少行作为崩溃上下文
CRASH_RULES = [
    # Java 层未捕获异常 — 最常见的崩溃类型
    (re.compile(r"FATAL EXCEPTION"), "Java崩溃", 5),
    # Native 层崩溃（信号 11=SIGSEGV, 6=SIGABRT 等）
    (re.compile(r"Fatal signal \d+"), "Native崩溃", 3),
    # ANR（应用无响应）
    (re.compile(r"ANR in \S+"), "ANR", 5),
    # ActivityManager 记录的崩溃/ANR 事件
    (re.compile(r"am_crash.*:"), "AM崩溃记录", 0),
    (re.compile(r"am_anr.*:"), "AM_ANR记录", 0),
]

# 备选规则（仅写入 crashes.log，不实时打印，避免刷屏）
CRASH_RULES_QUIET = [
    (re.compile(r"Native crash"), "Native崩溃", 3),
    (re.compile(r"Tombstone written to"), "Tombstone", 0),
    (re.compile(r"Build fingerprint:.*revision"), "崩溃指纹", 0),
    (re.compile(r"backtrace:"), "堆栈回溯", 0),
    (re.compile(r"Force finishing activity \S+"), "强退Activity", 0),
    (re.compile(r"Process \S+ \(pid \d+\) has died"), "进程死亡", 0),
    (re.compile(r"has died.*Adj"), "进程回收", 0),
]


class AndroidLogger:
    """主控类：管理 logcat 采集、录屏、崩溃检测"""

    CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
    CREATE_NEW_PROCESS_GROUP = 0x00000200 if sys.platform == "win32" else 0

    def __init__(self, output_dir: str = None, enable_record: bool = True):
        self.enable_record = enable_record
        self.record_size = None  # 普通设备连接后按实际屏幕比例自动计算
        self.serial = None
        self.device_brand = ""
        self.device_model = ""
        self.android_ver = ""
        self.is_emulator = False  # 是否为模拟器（拉取后自动清理远程文件）
        self.is_memu = False      # 逍遥模拟器使用 scrcpy 连续录屏
        self._original_show_touches = None  # 录屏前保存原始触摸显示状态

        # 输出目录：延迟到设备连接后再创建（需要型号信息）
        self._output_base = Path(output_dir) if output_dir else Path(_script_dir) / "captures"
        self.session_dir = None
        self.log_file = None
        self.crash_file = None

        # 进程句柄
        self._logcat_proc = None
        self._record_proc = None
        self._stop_event = threading.Event()
        self._record_thread = None
        self._crash_count = 0
        self._crash_quiet_count = 0
        self._record_files = []
        self._current_segment = 0
        self._scrcpy_raw_path = None
        self._scrcpy_started_at = None
        self._scrcpy_log_handle = None

    # ---------- 设备检测 ----------
    def _is_device_online(self, serial: str) -> bool:
        """检查指定序列号是否处于可用的 device 状态。"""
        try:
            result = subprocess.run(
                [ADB_PATH, "-s", serial, "get-state"],
                capture_output=True, text=True, timeout=5,
                creationflags=self.CREATE_NO_WINDOW,
            )
            return result.returncode == 0 and result.stdout.strip() == "device"
        except Exception:
            return False

    @staticmethod
    def _is_tcp_port_open(address: str) -> bool:
        """快速检测模拟器 ADB 端口，避免 adb connect 长时间等待。"""
        try:
            host, port = address.rsplit(":", 1)
            with socket.create_connection((host, int(port)), timeout=1):
                return True
        except (OSError, ValueError):
            return False

    def _disconnect_address(self, address: str):
        """清理指定 TCP 地址在 ADB 中的离线记录。"""
        try:
            subprocess.run(
                [ADB_PATH, "disconnect", address],
                capture_output=True, text=True, timeout=5,
                creationflags=self.CREATE_NO_WINDOW,
            )
        except Exception:
            pass

    def _wait_for_address(self, address: str, retries: int = 8) -> bool:
        """短暂等待刚连接的 TCP 设备进入 device 状态。"""
        for _ in range(retries):
            if self._is_device_online(address):
                return True
            if self._stop_event.wait(1):
                break
        return False

    def _connect_address(self, address: str, wait_ready: bool = True) -> bool:
        """连接一个模拟器 ADB 地址，返回是否已发起或已在线。"""
        if self._is_device_online(address):
            print(f"✅ {address} 已在线")
            return True

        if not self._is_tcp_port_open(address):
            self._disconnect_address(address)
            print(f"⏭️  {address} 端口未开放，已跳过")
            return False

        self._disconnect_address(address)
        try:
            result = subprocess.run(
                [ADB_PATH, "connect", address],
                capture_output=True, text=True, timeout=8,
                creationflags=self.CREATE_NO_WINDOW,
            )
            message = (result.stdout or result.stderr).strip()
            if message:
                print(f"   {message}")
            if result.returncode != 0:
                return False
        except Exception as exc:
            print(f"⚠️ 连接 {address} 失败: {exc}")
            return False

        if not wait_ready or self._wait_for_address(address):
            return True
        print(f"⚠️ {address} 尚未就绪，将继续扫描当前在线设备")
        return False

    def _connect_emulator(self, name: str, address: str,
                          alternate_serial: str = None) -> bool:
        """连接一类模拟器；雷电优先识别其 emulator-* 本地序列号。"""
        print(f"\n🔌 正在连接{name}...")
        if alternate_serial and self._is_device_online(alternate_serial):
            if not self._is_device_online(address):
                self._disconnect_address(address)
            print(f"✅ 已连接{name}: {alternate_serial}")
            return True
        return self._connect_address(address)

    def prompt_emulator_connection(self):
        """显示常见模拟器快速连接菜单并执行用户选择。"""
        print("\n快速连接模拟器：")
        for index, (name, address, _) in enumerate(EMULATOR_CONNECTIONS, 1):
            suffix = " / emulator-5554" if name == "雷电模拟器" else ""
            print(f"  [{index}] {name} ({address}{suffix})")
        print("  [6] 全部尝试")
        print("  [7] 跳过，直接扫描设备（默认）")

        while True:
            try:
                choice = input("请选择 (1-7，直接回车跳过): ").strip() or "7"
            except (EOFError, KeyboardInterrupt):
                print()
                return

            if choice in {str(i) for i in range(1, 6)}:
                self._connect_emulator(*EMULATOR_CONNECTIONS[int(choice) - 1])
                return
            if choice == "6":
                print("\n🔌 正在尝试连接所有模拟器...")
                seen_addresses = set()
                for _, address, alternate_serial in EMULATOR_CONNECTIONS:
                    if alternate_serial and self._is_device_online(alternate_serial):
                        print(f"✅ {alternate_serial} 已在线")
                    if address not in seen_addresses:
                        self._connect_address(address, wait_ready=False)
                        seen_addresses.add(address)
                self._stop_event.wait(1)
                return
            if choice == "7":
                return
            print("⚠️ 请输入 1 到 7 之间的数字")

    def _get_connected_devices(self) -> list:
        """获取所有已连接设备的序列号列表"""
        try:
            result = subprocess.run(
                [ADB_PATH, "devices"],
                capture_output=True, text=True, timeout=5,
                creationflags=self.CREATE_NO_WINDOW,
            )
            devices = []
            for line in result.stdout.strip().split("\n")[1:]:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "device":
                    devices.append(parts[0])
            return devices
        except Exception:
            return []

    def _get_device_prop(self, serial: str, key: str) -> str:
        """获取指定设备的属性值"""
        try:
            r = subprocess.run(
                [ADB_PATH, "-s", serial, "shell", "getprop", key],
                capture_output=True, text=True, timeout=5,
                creationflags=self.CREATE_NO_WINDOW,
            )
            return r.stdout.strip()
        except Exception:
            return ""

    def _select_device(self, devices: list) -> str:
        """多设备时让用户选择，返回选中的序列号"""
        print(f"\n📱 检测到 {len(devices)} 台设备，请选择：\n")

        # 先批量查询品牌+型号信息
        device_info = []
        for i, serial in enumerate(devices, 1):
            brand = self._get_device_prop(serial, "ro.product.brand")
            model = self._get_device_prop(serial, "ro.product.model")
            android_ver = self._get_device_prop(serial, "ro.build.version.release")
            label = f"{brand} {model} (Android {android_ver})".strip() if model else serial
            device_info.append((serial, label))
            print(f"  [{i}] {serial}  —  {label}")

        print()
        while True:
            try:
                choice = input(f"请输入编号 (1-{len(devices)}): ").strip()
                idx = int(choice)
                if 1 <= idx <= len(devices):
                    chosen_serial, chosen_label = device_info[idx - 1]
                    print(f"✅ 已选择: {chosen_serial}  —  {chosen_label}")
                    return chosen_serial
                else:
                    print(f"⚠️ 请输入 1 到 {len(devices)} 之间的数字")
            except ValueError:
                print("⚠️ 请输入有效的数字")
            except (EOFError, KeyboardInterrupt):
                print()
                return ""

    def wait_for_device(self) -> bool:
        """轮询等待设备连接，多设备时让用户选择，返回 True 表示就绪"""
        # 如果预指定了序列号，直接验证
        if self.serial:
            print(f"\n🔍 检查预指定设备: {self.serial}")
            devices = self._get_connected_devices()
            if self.serial in devices:
                self._query_device_info()
                print(f"✅ 设备就绪: {self.serial}")
                if self.device_model:
                    print(f"   型号: {self.device_brand} {self.device_model}  |  Android {self.android_ver}")
                return True
            else:
                print(f"⚠️ 未找到设备 {self.serial}，进入等待模式...")

        print("\n🔍 等待安卓设备连接（请确保 USB 调试已开启）...")
        while not self._stop_event.is_set():
            devices = self._get_connected_devices()
            if devices:
                if len(devices) == 1:
                    # 单设备直接使用
                    self.serial = devices[0]
                else:
                    # 多设备让用户选择
                    chosen = self._select_device(devices)
                    if not chosen:
                        return False
                    self.serial = chosen

                self._query_device_info()
                if len(devices) == 1:
                    print(f"✅ 检测到设备: {self.serial}")
                    if self.device_model:
                        print(f"   型号: {self.device_brand} {self.device_model}  |  Android {self.android_ver}")
                return True

            self._stop_event.wait(DEVICE_POLL_INTERVAL)
        return False

    def _query_device_info(self):
        """获取当前设备品牌、型号和 Android 版本"""
        self.device_brand = self._get_device_prop(self.serial, "ro.product.brand")
        self.device_model = self._get_device_prop(self.serial, "ro.product.model")
        self.android_ver = self._get_device_prop(self.serial, "ro.build.version.release")
        self.is_emulator = self._is_emulator()
        self.is_memu = self._is_memu()

    def _is_memu(self) -> bool:
        """通过默认 ADB 端口或系统包识别逍遥模拟器。"""
        if self.serial == "127.0.0.1:21503":
            return True
        try:
            result = subprocess.run(
                [ADB_PATH, "-s", self.serial, "shell", "pm", "path",
                 "com.microvirt.launcher2"],
                capture_output=True, text=True, timeout=5,
                creationflags=self.CREATE_NO_WINDOW,
            )
            return result.returncode == 0 and "package:" in result.stdout
        except Exception:
            return False

    def _get_screen_size(self):
        """读取设备当前有效屏幕尺寸，存在 Override 时优先使用。"""
        try:
            result = subprocess.run(
                [ADB_PATH, "-s", self.serial, "shell", "wm", "size"],
                capture_output=True, text=True, timeout=5,
                creationflags=self.CREATE_NO_WINDOW,
            )
            sizes = re.findall(r"(\d+)\s*x\s*(\d+)", result.stdout)
            if sizes:
                width, height = map(int, sizes[-1])
                if width > 0 and height > 0:
                    return width, height
        except Exception:
            pass
        return None

    @staticmethod
    def _fit_record_size(width: int, height: int, max_short_side: int = 720):
        """等比限制短边；仅缩小不放大，缩放结果对齐到 8 像素。"""
        short_side = min(width, height)
        if short_side <= max_short_side:
            return width, height

        scale = max_short_side / short_side
        if width <= height:
            target_width = max_short_side
            target_height = max(8, int(height * scale) // 8 * 8)
        else:
            target_height = max_short_side
            target_width = max(8, int(width * scale) // 8 * 8)
        return target_width, target_height

    def _configure_record_size(self, portrait: bool = False):
        """按设备实际比例和用户选择设置普通 screenrecord 的分辨率。"""
        screen_size = self._get_screen_size()
        if not screen_size:
            # 极少数裁剪系统没有 wm 命令，仍保证短边不超过 720。
            self.record_size = "720x1280" if portrait else "1280x720"
            print(f"⚠️ 未能读取屏幕尺寸，录屏分辨率回退为 {self.record_size}")
            return

        width, height = screen_size
        target_width, target_height = self._fit_record_size(
            width, height, RECORD_MAX_SHORT_SIDE
        )
        long_side = max(target_width, target_height)
        short_side = min(target_width, target_height)
        if portrait:
            target_width, target_height = short_side, long_side
        else:
            target_width, target_height = long_side, short_side
        self.record_size = f"{target_width}x{target_height}"
        orientation = "竖屏" if portrait else "横屏"
        size_note = "原始短边不超过 720，不放大" if min(width, height) <= RECORD_MAX_SHORT_SIDE else "等比限制短边为 720"
        print(
            f"✅ {orientation}录屏: {self.record_size} "
            f"（设备 {width}x{height}，{size_note}）"
        )

    def _is_emulator(self) -> bool:
        """检测当前连接的设备是否为模拟器"""
        # 1. 序列号特征
        if self.serial and ("emulator" in self.serial.lower() or self.serial.startswith("127.0.0.1")):
            return True
        # 2. QEMU 内核标记
        if self._get_device_prop(self.serial, "ro.kernel.qemu") == "1":
            return True
        # 3. 硬件类型（goldfish/ranchu 是 Android Emulator；vbox 是 VirtualBox/逍遥等）
        hw = self._get_device_prop(self.serial, "ro.hardware").lower()
        if hw in ("goldfish", "ranchu", "vbox86", "vbox86p") or "vbox" in hw:
            return True
        # 4. 主板型号
        board = self._get_device_prop(self.serial, "ro.product.board").lower()
        if board in ("goldfish", "ranchu") or "vbox" in board:
            return True
        return False

    # ---------- 触摸显示控制 ----------
    def _adb_setting(self, action: str, key: str, value: str = None) -> str:
        """执行 adb settings 命令"""
        cmd = [ADB_PATH, "-s", self.serial, "shell", "settings", action, "system", key]
        if value is not None:
            cmd.append(value)
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5,
                creationflags=self.CREATE_NO_WINDOW,
            )
            return r.stdout.strip()
        except Exception:
            return ""

    def _enable_show_touches(self):
        """录屏前开启「显示点按操作」，保存原始值以便恢复"""
        self._original_show_touches = self._adb_setting("get", "show_touches")
        self._adb_setting("put", "show_touches", "1")
        print("👆 已开启显示点按操作（录屏中会显示触摸圆点）")

    def _restore_show_touches(self):
        """停止录屏后恢复触摸显示状态"""
        if self._original_show_touches is not None:
            self._adb_setting("put", "show_touches", self._original_show_touches)
            self._original_show_touches = None

    def is_device_connected(self) -> bool:
        """检查设备是否仍连接"""
        try:
            r = subprocess.run(
                [ADB_PATH, "-s", self.serial, "get-state"],
                capture_output=True, text=True, timeout=5,
                creationflags=self.CREATE_NO_WINDOW,
            )
            return "device" in r.stdout
        except Exception:
            return False

    # ---------- 初始化 ----------
    def _init_session(self):
        """创建输出目录和文件"""
        # 目录名格式：session_型号_日期_时间
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        model_tag = self.device_model.replace(" ", "_") if self.device_model else self.serial
        # 移除目录名中不合法的字符
        model_tag = re.sub(r'[<>:"/\\|?*]', '', model_tag)
        self.session_dir = self._output_base / f"session_{model_tag}_{ts}"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.session_dir / "logcat_full.log"
        crash_path = self.session_dir / "crashes.log"
        self.log_file = open(log_path, "w", encoding="utf-8", errors="replace")
        self.crash_file = open(crash_path, "w", encoding="utf-8", errors="replace")

        # 写入 session 信息头
        header = (
            f"# Session: {self.session_dir.name}\n"
            f"# Device: {self.serial} ({self.device_model})\n"
            f"# Android: {self.android_ver}\n"
            f"# Start: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"# {'='*60}\n\n"
        )
        self.log_file.write(header)
        self.log_file.flush()
        self.crash_file.write(header)
        self.crash_file.flush()
        print(f"📁 输出目录: {self.session_dir}")

    # ---------- Logcat 采集 ----------
    def _start_logcat(self):
        """后台线程：持续采集 logcat 输出"""
        cmd = [ADB_PATH, "-s", self.serial, "logcat", "-v", "threadtime"]
        self._logcat_proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=self.CREATE_NO_WINDOW,
        )
        t = threading.Thread(target=self._read_logcat, daemon=True)
        t.start()
        print("📝 Logcat 日志采集已启动")

    def _read_logcat(self):
        """读取 logcat 流并写入文件，同时检测崩溃关键词"""
        try:
            today_prefix = datetime.now().strftime("%m-%d")  # 如 "06-15"
            context_remaining = {}  # {rule_label: 剩余需要采集的上下文行数}
            prev_kept = True        # 上一行是否被保留（用于处理续行）
            lines_since_flush = 0   # 批量 flush 计数器
            last_date_check = time.time()  # 上次检查日期的时间

            for raw_line in iter(self._logcat_proc.stdout.readline, b""):
                if self._stop_event.is_set():
                    break
                line = raw_line.decode("utf-8", errors="replace")

                # 每 60 秒更新一次 today_prefix（处理跨午夜场景）
                now = time.time()
                if now - last_date_check > 60:
                    today_prefix = datetime.now().strftime("%m-%d")
                    last_date_check = now

                # 只保留今天的日志（logcat 格式: "MM-DD HH:MM:SS.mmm ..."）
                is_date_line = len(line) >= 5 and line[:2].isdigit() and line[2] == '-' and line[3:5].isdigit()
                if is_date_line:
                    prev_kept = line.startswith(today_prefix)
                # 日期行看前缀；续行（多行日志）跟随上一行的去留
                if not prev_kept:
                    continue

                self.log_file.write(line)
                lines_since_flush += 1
                if lines_since_flush >= 100:
                    self.log_file.flush()
                    lines_since_flush = 0

                # --- 高优先级规则：写入 crashes.log（不打印，避免干扰） ---
                matched_alert = False
                for pattern, label, ctx_lines in CRASH_RULES:
                    if pattern.search(line):
                        self._crash_count += 1
                        ts = datetime.now().strftime("%H:%M:%S")
                        self.crash_file.write(f"\n{'='*50}\n")
                        self.crash_file.write(f"[{ts}] [{label}] {line}")
                        self.crash_file.flush()
                        if ctx_lines > 0:
                            context_remaining[label] = ctx_lines
                        matched_alert = True
                        break

                if matched_alert:
                    continue

                # --- 采集命中规则的后续上下文行 ---
                done_labels = []
                for label, remaining in context_remaining.items():
                    self.crash_file.write(f"   {line}")
                    self.crash_file.flush()
                    context_remaining[label] = remaining - 1
                    if remaining - 1 <= 0:
                        done_labels.append(label)
                for label in done_labels:
                    del context_remaining[label]

                # --- 低优先级规则：仅写入 crashes.log，不打印 ---
                for pattern, label, ctx_lines in CRASH_RULES_QUIET:
                    if pattern.search(line):
                        self._crash_quiet_count += 1
                        self.crash_file.write(f"[{label}] {line}")
                        self.crash_file.flush()
                        break

        except Exception as e:
            if not self._stop_event.is_set():
                print(f"\n⚠️ Logcat 采集异常: {e}")

    # ---------- 录屏 ----------
    def _start_recording_loop(self):
        """启动与设备匹配的后台录屏流程。"""
        # 开启触摸显示
        self._enable_show_touches()
        target = self._scrcpy_recording_loop if self.is_memu else self._recording_loop
        self._record_thread = threading.Thread(target=target, daemon=True)
        self._record_thread.start()
        if self.is_memu:
            print("🎥 逍遥兼容录屏已启动（scrcpy 连续录制，停止后无损分段）")
        else:
            print("🎥 屏幕录制已启动（每段 60s 自动续录）")

    def _scrcpy_recording_loop(self):
        """逍遥模拟器：用一个 scrcpy 进程连续录制，避免分段空档。"""
        if not Path(SCRCPY_PATH).is_file():
            print(f"   ⚠️ 逍遥录屏组件缺失: {SCRCPY_PATH}")
            self._stop_event.set()
            return

        self._scrcpy_started_at = datetime.now()
        stamp = self._scrcpy_started_at.strftime("%Y%m%d%H%M%S")
        self._scrcpy_raw_path = (self.session_dir / f"screen_raw_{stamp}.mkv").resolve()
        scrcpy_log_path = self.session_dir / "scrcpy_record.log"
        cmd = [
            SCRCPY_PATH,
            "-s", self.serial,
            "--record", str(self._scrcpy_raw_path),
            "--no-window",
            "--no-audio",
            "--max-size", "1280",
            "--max-fps", "30",
            "--video-bit-rate", RECORD_BITRATE,
        ]

        try:
            self._scrcpy_log_handle = open(
                scrcpy_log_path, "w", encoding="utf-8", errors="replace"
            )
            self._record_proc = subprocess.Popen(
                cmd,
                stdout=self._scrcpy_log_handle,
                stderr=subprocess.STDOUT,
                cwd=SCRCPY_DIR,
                # 独立进程组允许 Windows 定向发送 CTRL_BREAK，让 MKV 正常收尾。
                creationflags=self.CREATE_NEW_PROCESS_GROUP,
            )
            if self._stop_event.is_set():
                self._stop_scrcpy_process()
            return_code = self._record_proc.wait()
            expected_stop = self._stop_event.is_set()
            if not expected_stop:
                print(f"\n⚠️ scrcpy 录屏意外结束（退出码: {return_code}），停止本次采集")
                self._stop_event.set()
        except Exception as exc:
            print(f"\n⚠️ scrcpy 录屏启动失败: {exc}")
            self._stop_event.set()
        finally:
            if self._scrcpy_log_handle:
                self._scrcpy_log_handle.flush()
                self._scrcpy_log_handle.close()
                self._scrcpy_log_handle = None

        self._split_scrcpy_recording()

    def _split_scrcpy_recording(self) -> bool:
        """按关键帧将 scrcpy 原片无损切成约 60 秒的 MP4。"""
        raw_path = self._scrcpy_raw_path
        if not raw_path or not raw_path.is_file() or raw_path.stat().st_size == 0:
            print("   ⚠️ scrcpy 未生成有效录像，跳过分段")
            return False
        if not Path(FFMPEG_PATH).is_file():
            print(f"   ⚠️ 找不到 FFmpeg，已保留原始录像: {raw_path.name}")
            return self._keep_scrcpy_raw()

        work_dir = self.session_dir / ".scrcpy_segments"
        work_dir.mkdir(exist_ok=True)
        segment_pattern = work_dir / "segment_%03d.mp4"
        segment_list = work_dir / "segments.csv"
        for stale_segment in work_dir.glob("segment_*.mp4"):
            stale_segment.unlink(missing_ok=True)
        segment_list.unlink(missing_ok=True)
        cmd = [
            FFMPEG_PATH,
            "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(raw_path),
            "-map", "0:v:0",
            "-c", "copy",
            "-f", "segment",
            "-segment_time", str(RECORD_SEGMENT_SEC),
            "-reset_timestamps", "1",
            "-segment_list", str(segment_list),
            "-segment_list_type", "csv",
            str(segment_pattern),
        ]

        print("   ✂️ 正在按关键帧无损切分逍遥录像...")
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=600,
                creationflags=self.CREATE_NO_WINDOW,
            )
            if result.returncode != 0 or not segment_list.is_file():
                detail = (result.stderr or result.stdout).strip()
                print(f"   ⚠️ 录像分段失败，已保留原始 MKV: {detail}")
                return self._keep_scrcpy_raw()

            rows = []
            with open(segment_list, "r", encoding="utf-8", newline="") as handle:
                rows = list(csv.reader(handle))

            segment_specs = []
            base_time = self._scrcpy_started_at or datetime.fromtimestamp(raw_path.stat().st_mtime)
            for index, row in enumerate(rows):
                if len(row) < 3:
                    print("   ⚠️ FFmpeg 分段清单不完整，已保留原始 MKV")
                    return self._keep_scrcpy_raw()
                source = work_dir / Path(row[0]).name
                if not source.is_file() or source.stat().st_size == 0:
                    print("   ⚠️ FFmpeg 分段文件不完整，已保留原始 MKV")
                    return self._keep_scrcpy_raw()
                try:
                    start_offset = float(row[1])
                except ValueError:
                    start_offset = index * RECORD_SEGMENT_SEC
                segment_time = datetime.fromtimestamp(base_time.timestamp() + start_offset)
                segment_specs.append((source, segment_time))

            if not segment_specs:
                print("   ⚠️ FFmpeg 未生成有效分段，已保留原始 MKV")
                return self._keep_scrcpy_raw()

            completed = []
            for source, segment_time in segment_specs:
                destination = self.session_dir / f"screen_{segment_time.strftime('%Y%m%d%H%M%S')}.mp4"
                suffix = 1
                while destination.exists():
                    destination = self.session_dir / (
                        f"screen_{segment_time.strftime('%Y%m%d%H%M%S')}_{suffix}.mp4"
                    )
                    suffix += 1
                source.replace(destination)
                completed.append(destination)

            self._record_files.extend(str(path) for path in completed)
            total_mb = sum(path.stat().st_size for path in completed) / 1024 / 1024
            print(f"   💾 已生成 {len(completed)} 段录像 ({total_mb:.1f} MB)")

            # 全部分段成功后才删除连续原片和临时清单。
            raw_path.unlink()
            segment_list.unlink(missing_ok=True)
            try:
                work_dir.rmdir()
            except OSError:
                pass
            return True
        except subprocess.TimeoutExpired:
            print("   ⚠️ 录像分段超时，已保留原始 MKV")
            return self._keep_scrcpy_raw()
        except Exception as exc:
            print(f"   ⚠️ 录像分段异常，已保留原始 MKV: {exc}")
            return self._keep_scrcpy_raw()

    def _keep_scrcpy_raw(self) -> bool:
        """将有效的连续原片加入摘要，同时保持失败返回值。"""
        raw_path = self._scrcpy_raw_path
        if raw_path and raw_path.is_file() and raw_path.stat().st_size > 0:
            raw_text = str(raw_path)
            if raw_text not in self._record_files:
                self._record_files.append(raw_text)
        return False

    def _stop_scrcpy_process(self):
        """请求 scrcpy 正常停止；失败时才强制结束。"""
        if not self._record_proc or self._record_proc.poll() is not None:
            return
        try:
            if sys.platform == "win32":
                os.kill(self._record_proc.pid, signal.CTRL_BREAK_EVENT)
            else:
                self._record_proc.send_signal(signal.SIGINT)
            self._record_proc.wait(timeout=15)
        except (subprocess.TimeoutExpired, OSError):
            print("   ⚠️ scrcpy 停止超时，正在结束进程（原始 MKV 将保留）")
            try:
                self._record_proc.terminate()
                self._record_proc.wait(timeout=5)
            except Exception:
                try:
                    self._record_proc.kill()
                except Exception:
                    pass
        except Exception:
            try:
                self._record_proc.kill()
            except Exception:
                pass

    def _check_device_storage(self):
        """检查设备存储空间，低于 500MB 时清理已拉取的录屏并警告"""
        try:
            r = subprocess.run(
                [ADB_PATH, "-s", self.serial, "shell", "df", "/sdcard"],
                capture_output=True, text=True, timeout=10,
                creationflags=self.CREATE_NO_WINDOW,
            )
            for line in r.stdout.strip().split("\n")[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[0].startswith("/"):
                    avail_kb = int(parts[3])
                    avail_mb = avail_kb / 1024
                    if avail_mb < 500:
                        print(f"\n⚠️ 设备存储不足: 剩余 {avail_mb:.0f} MB，建议及时清理")
                        return
        except Exception:
            pass

    def _pull_record_file(self, remote_path: str, local_path: Path, filename: str) -> bool:
        """从设备拉取录屏文件到本地，返回是否成功"""
        try:
            # 等待文件在设备上落盘（screenrecord 结束后可能需要一点时间）
            time.sleep(1)

            # 先确认设备上文件存在
            check = subprocess.run(
                [ADB_PATH, "-s", self.serial, "shell", "ls", "-la", remote_path],
                capture_output=True, text=True, timeout=10,
                creationflags=self.CREATE_NO_WINDOW,
            )
            if "No such file" in check.stderr or check.returncode != 0:
                print(f"   ⚠️ {filename} 在设备上不存在，跳过")
                return False

            # 拉取到本地
            result = subprocess.run(
                [ADB_PATH, "-s", self.serial, "pull", remote_path, str(local_path)],
                capture_output=True, text=True, timeout=120,
                creationflags=self.CREATE_NO_WINDOW,
            )

            if result.returncode != 0:
                print(f"   ⚠️ 拉取 {filename} 失败: {result.stderr.strip()}")
                return False

            if local_path.exists() and local_path.stat().st_size > 0:
                self._record_files.append(str(local_path))
                size_mb = local_path.stat().st_size / 1024 / 1024
                print(f"   💾 {filename} 已保存 ({size_mb:.1f} MB)")
            else:
                print(f"   ⚠️ {filename} 拉取后本地文件为空或不存在")
                return False

            # 模拟器：拉取后删除远程临时文件，防止虚拟磁盘膨胀
            if self.is_emulator:
                try:
                    subprocess.run(
                        [ADB_PATH, "-s", self.serial, "shell", "rm", "-f", remote_path],
                        capture_output=True, timeout=10,
                        creationflags=self.CREATE_NO_WINDOW,
                    )
                except Exception:
                    pass  # 删除失败静默处理，不影响主流程
            return True

        except subprocess.TimeoutExpired:
            print(f"   ⚠️ 拉取 {filename} 超时（文件可能过大）")
            return False
        except Exception as e:
            print(f"   ⚠️ 拉取 {filename} 异常: {e}")
            return False

    def _recording_loop(self):
        """录屏主循环：录完一段自动开始下一段"""
        segment = 0

        while True:
            segment += 1
            self._current_segment = segment
            filename = f"screen_{datetime.now().strftime('%Y%m%d%H%M%S')}.mp4"
            remote_path = f"/sdcard/screen_{segment:03d}.mp4"
            local_path = self.session_dir / filename

            # 检查设备存储空间（低于 500MB 时清理之前的录屏并警告）
            if segment > 1:
                self._check_device_storage()

            # 在手机上录屏（--bugreport 在画面左上角叠加时间戳）
            cmd = [
                ADB_PATH, "-s", self.serial, "shell",
                "screenrecord",
                "--bugreport",
                "--size", self.record_size,
                "--bit-rate", RECORD_BITRATE,
                "--time-limit", str(RECORD_SEGMENT_SEC),
                remote_path,
            ]
            try:
                self._record_proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=self.CREATE_NO_WINDOW,
                )
                self._record_proc.wait()
            except Exception as e:
                print(f"   ⚠️ 录屏进程异常: {e}")
                break

            # 拉取到本地（无论是否停止，都要拉取已录制的文件）
            self._pull_record_file(remote_path, local_path, filename)

            # 如果收到停止信号或设备断开，退出循环
            if self._stop_event.is_set():
                break
            if not self.is_device_connected():
                print("\n📴 设备已断开，停止录屏")
                break

    # ---------- 停止 & 清理 ----------
    def _stop_all(self):
        """停止所有采集"""
        self._stop_event.set()

        # 最先恢复触摸显示（此时 ADB 连接最稳定，避免后续断连导致恢复失败）
        self._restore_show_touches()

        # 停止 logcat
        if self._logcat_proc:
            try:
                self._logcat_proc.terminate()
                self._logcat_proc.wait(timeout=5)
            except Exception:
                try:
                    self._logcat_proc.kill()
                except Exception:
                    pass

        # 停止录屏并让当前容器正常收尾。
        if self._record_proc and self._record_proc.poll() is None:
            if self.is_memu:
                self._stop_scrcpy_process()
            else:
                try:
                    # 用当前录屏文件路径精确匹配，避免误杀其他 screenrecord 进程
                    current_file = f"/sdcard/screen_{self._current_segment:03d}.mp4"
                    subprocess.run(
                        [ADB_PATH, "-s", self.serial, "shell",
                         "pkill", "-INT", "-f", current_file],
                        capture_output=True, timeout=10,
                        creationflags=self.CREATE_NO_WINDOW,
                    )
                    self._record_proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    print("   ⚠️ 录屏进程停止超时，强制终止")
                    try:
                        self._record_proc.kill()
                    except Exception:
                        pass
                except Exception:
                    try:
                        self._record_proc.kill()
                    except Exception:
                        pass

        # 等待最后一段拉取，或等待 scrcpy 原片完成无损分段。
        if self._record_thread and self._record_thread.is_alive():
            action = "完成无损分段" if self.is_memu else "拉取完成"
            print(f"   ⏳ 等待最后一段录屏{action}...")
            self._record_thread.join(timeout=600 if self.is_memu else 120)

        # flush + 关闭文件
        if self.log_file:
            self.log_file.flush()
            self.log_file.close()
        if self.crash_file:
            self.crash_file.flush()
            self.crash_file.close()

    def _print_summary(self):
        """输出本次采集摘要"""
        print("\n" + "=" * 60)
        print("📋 本次采集摘要")
        print("=" * 60)
        print(f"  设备: {self.serial} ({self.device_model})")
        print(f"  时长: {self.session_dir.name}")
        print(f"  目录: {self.session_dir}")

        # logcat 文件大小
        log_path = self.session_dir / "logcat_full.log"
        if log_path.exists():
            size_mb = log_path.stat().st_size / 1024 / 1024
            print(f"  📝 日志: logcat_full.log ({size_mb:.1f} MB)")

        # 崩溃数
        print(f"  🚨 严重崩溃/ANR: {self._crash_count} 次")
        if self._crash_count > 0:
            print(f"     详见: crashes.log")
        if self._crash_quiet_count > 0:
            print(f"  ⚠️  其他异常信号: {self._crash_quiet_count} 次（仅记录在 crashes.log）")

        # 录屏
        if self._record_files:
            total_size = sum(Path(f).stat().st_size for f in self._record_files if Path(f).exists())
            print(f"  🎥 录屏: {len(self._record_files)} 段 ({total_size / 1024 / 1024:.1f} MB)")
            for f in self._record_files:
                print(f"     - {Path(f).name}")
        else:
            print(f"  🎥 录屏: 未启用")

        print("=" * 60)
        print("提示：用以下命令快速搜索崩溃日志：")
        print(f'  grep -i "FATAL\\|ANR\\|crash" "{self.session_dir / "logcat_full.log"}"')
        print()

    def _open_session_folder(self):
        """打印并在 Windows 资源管理器中打开本次采集目录。"""
        if not self.session_dir:
            return
        session_path = self.session_dir.resolve()
        print(f"📂 采集文件夹: {session_path}")
        if sys.platform != "win32":
            return
        try:
            subprocess.Popen(
                ["explorer.exe", str(session_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            print(f"⚠️ 无法自动打开文件夹，请复制上面的路径手动打开: {exc}")

    # ---------- 主入口 ----------
    def run(self, offer_emulator_connect: bool = True):
        """主运行流程"""
        print("=" * 60)
        print("  🤖 Android 自动日志采集 & 录屏工具")
        print("=" * 60)

        # 1. 可选快速连接模拟器，然后等待设备
        if offer_emulator_connect:
            self.prompt_emulator_connection()

        if not self.wait_for_device():
            print("❌ 未检测到设备，退出")
            return

        # 2. 根据录屏后端配置画面尺寸
        if self.enable_record:
            if self.is_memu:
                print("\n✅ 逍遥兼容模式：录屏方向自动跟随模拟器画面")
            else:
                print("\n录屏方向：")
                print("  [1] 横屏（默认，保持设备比例，短边最大 720）")
                print("  [2] 竖屏（保持设备比例，短边最大 720）")
                try:
                    orient = input("请选择 (1/2，直接回车默认横屏): ").strip()
                except (EOFError, KeyboardInterrupt):
                    orient = ""
                self._configure_record_size(portrait=orient == "2")

        # 3. 初始化
        self._init_session()

        # 4. 启动 logcat
        self._start_logcat()

        # 5. 启动录屏（如果启用）
        if self.enable_record:
            self._start_recording_loop()

        print("\n🟢 采集中... 按 Ctrl+C 停止\n")

        # 注册信号处理：Ctrl+C 直接设置 stop_event，确保一次按键即响应
        def _on_sigint(signum, frame):
            self._stop_event.set()
        original_handler = signal.signal(signal.SIGINT, _on_sigint)

        # 5. 主循环：正计时 + 每 0.5 秒检查停止信号和设备连接
        start_time = time.time()
        last_conn_check = start_time
        while not self._stop_event.is_set():
            # 更新计时
            elapsed = int(time.time() - start_time)
            h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
            print(f"\r⏱  已采集 {h:02d}:{m:02d}:{s:02d}   ", end="", flush=True)
            # 等待 0.5 秒，Ctrl+C 会通过信号处理器立即设置 stop_event
            self._stop_event.wait(0.5)
            # 每 5 秒检查一次设备连接
            if time.time() - last_conn_check > 5:
                if not self.is_device_connected():
                    print(f"\n\n📴 设备已断开，自动停止采集...")
                    break
                last_conn_check = time.time()

        if self._stop_event.is_set():
            print("\n⚠️  正在停止采集，请勿关闭窗口...")
            if self.is_memu and self.enable_record:
                print("   （正在保存连续录像并按关键帧无损分段）")
            else:
                print("   （正在等待最后一段录屏保存并拉取到本地）")

        # 6. 清理 & 摘要（保持信号处理器活跃，避免清理期间 Ctrl+C 导致崩溃）
        self._stop_all()
        self._print_summary()
        self._open_session_folder()

        # 清理完成后再恢复原始信号处理器
        signal.signal(signal.SIGINT, original_handler)

        # 等待用户确认，防止窗口直接关闭
        try:
            input("按回车键退出...")
        except (EOFError, KeyboardInterrupt):
            pass


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Android 自动日志采集 & 录屏工具")
    parser.add_argument("--output", "-o", default=None,
                        help="输出目录（默认: 脚本所在目录/captures/）")
    parser.add_argument("--no-record", action="store_true",
                        help="只采集日志，不录屏")
    parser.add_argument("--adb", default=None,
                        help="指定 ADB 路径（默认自动查找）")
    parser.add_argument("--serial", "-s", default=None,
                        help="指定设备序列号（跳过交互选择）")
    args = parser.parse_args()

    global ADB_PATH
    if args.adb:
        ADB_PATH = args.adb

    # 验证 ADB 可用
    if not os.path.isfile(ADB_PATH):
        print(f"❌ 找不到 ADB: {ADB_PATH}")
        print("   请安装 Android SDK Platform Tools 或用 --adb 指定路径")
        sys.exit(1)

    logger = AndroidLogger(
        output_dir=args.output,
        enable_record=not args.no_record,
    )
    if args.serial:
        logger.serial = args.serial
    logger.run(offer_emulator_connect=not bool(args.serial))


if __name__ == "__main__":
    main()
