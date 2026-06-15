#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Android 自动日志采集 & 录屏工具
===============================
插上安卓手机（USB调试已开启）后运行本脚本，自动完成：
  1. 实时 logcat 日志采集（全程保存为 .log 文件）
  2. 屏幕录制（每段最长 170s，自动循环续录，无缝拼接）
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
RECORD_SEGMENT_SEC = 170          # 每段录屏秒数（adb 上限 180s，留 10s 余量）
RECORD_SIZE = "1280x720"          # 录屏分辨率（横屏格式，降低以减小文件体积）
RECORD_BITRATE = "4M"             # 录屏码率（默认 20Mbps，降到 4Mbps）
DEVICE_POLL_INTERVAL = 3          # 设备检测间隔（秒）

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

    def __init__(self, output_dir: str = None, enable_record: bool = True):
        self.enable_record = enable_record
        self.serial = None
        self.device_model = ""
        self.android_ver = ""
        self._original_show_touches = None  # 录屏前保存原始触摸显示状态
        self._original_auto_rotate = None   # 录屏前保存自动旋转状态
        self._original_user_rotation = None # 录屏前保存屏幕方向

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

    # ---------- 设备检测 ----------
    def _get_connected_devices(self) -> list:
        """获取所有已连接设备的序列号列表"""
        try:
            result = subprocess.run(
                [ADB_PATH, "devices"],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
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
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            return r.stdout.strip()
        except Exception:
            return ""

    def _select_device(self, devices: list) -> str:
        """多设备时让用户选择，返回选中的序列号"""
        print(f"\n📱 检测到 {len(devices)} 台设备，请选择：\n")

        # 先批量查询型号信息
        device_info = []
        for i, serial in enumerate(devices, 1):
            model = self._get_device_prop(serial, "ro.product.model")
            android_ver = self._get_device_prop(serial, "ro.build.version.release")
            label = f"{model} (Android {android_ver})" if model else serial
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
                    print(f"   型号: {self.device_model}  |  Android {self.android_ver}")
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
                        print(f"   型号: {self.device_model}  |  Android {self.android_ver}")
                return True

            self._stop_event.wait(DEVICE_POLL_INTERVAL)
        return False

    def _query_device_info(self):
        """获取当前设备型号和 Android 版本"""
        self.device_model = self._get_device_prop(self.serial, "ro.product.model")
        self.android_ver = self._get_device_prop(self.serial, "ro.build.version.release")

    # ---------- 触摸显示控制 ----------
    def _adb_setting(self, action: str, key: str, value: str = None) -> str:
        """执行 adb settings 命令"""
        cmd = [ADB_PATH, "-s", self.serial, "shell", "settings", action, "system", key]
        if value is not None:
            cmd.append(value)
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
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

    # ---------- 屏幕方向控制 ----------
    def _force_landscape(self):
        """录屏前强制横屏：关闭自动旋转 + 设置为横屏"""
        self._original_auto_rotate = self._adb_setting("get", "accelerometer_rotation")
        self._original_user_rotation = self._adb_setting("get", "user_rotation")
        self._adb_setting("put", "accelerometer_rotation", "0")  # 关闭自动旋转
        self._adb_setting("put", "user_rotation", "1")           # 1=横屏(90°)
        print("🔄 已强制横屏（录屏结束后恢复）")

    def _restore_rotation(self):
        """停止录屏后恢复屏幕方向"""
        if self._original_auto_rotate is not None:
            self._adb_setting("put", "accelerometer_rotation", self._original_auto_rotate)
            self._original_auto_rotate = None
        if self._original_user_rotation is not None:
            self._adb_setting("put", "user_rotation", self._original_user_rotation)
            self._original_user_rotation = None

    def is_device_connected(self) -> bool:
        """检查设备是否仍连接"""
        try:
            r = subprocess.run(
                [ADB_PATH, "-s", self.serial, "get-state"],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
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
        CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
        self._logcat_proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
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

            for raw_line in iter(self._logcat_proc.stdout.readline, b""):
                if self._stop_event.is_set():
                    break
                line = raw_line.decode("utf-8", errors="replace")

                # 只保留今天的日志（logcat 格式: "MM-DD HH:MM:SS.mmm ..."）
                is_date_line = len(line) >= 5 and line[:2].isdigit() and line[2] == '-' and line[3:5].isdigit()
                if is_date_line:
                    prev_kept = line.startswith(today_prefix)
                # 日期行看前缀；续行（多行日志）跟随上一行的去留
                if not prev_kept:
                    continue

                self.log_file.write(line)
                self.log_file.flush()

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

        except Exception:
            pass

    # ---------- 录屏 ----------
    def _start_recording_loop(self):
        """后台线程：循环录屏，每段 170s 自动续录"""
        # 开启触摸显示
        self._enable_show_touches()
        self._record_thread = threading.Thread(target=self._recording_loop, daemon=True)
        self._record_thread.start()
        print("🎥 屏幕录制已启动（每段 170s 自动续录）")

    def _pull_record_file(self, remote_path: str, local_path: Path, filename: str) -> bool:
        """从设备拉取录屏文件到本地，返回是否成功"""
        CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
        try:
            # 等待文件在设备上落盘（screenrecord 结束后可能需要一点时间）
            time.sleep(1)

            # 先确认设备上文件存在
            check = subprocess.run(
                [ADB_PATH, "-s", self.serial, "shell", "ls", "-la", remote_path],
                capture_output=True, text=True, timeout=10,
                creationflags=CREATE_NO_WINDOW,
            )
            if "No such file" in check.stderr or check.returncode != 0:
                print(f"   ⚠️ {filename} 在设备上不存在，跳过")
                return False

            # 拉取到本地
            result = subprocess.run(
                [ADB_PATH, "-s", self.serial, "pull", remote_path, str(local_path)],
                capture_output=True, text=True, timeout=120,
                creationflags=CREATE_NO_WINDOW,
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

            # 设备上保留一份，不删除
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
        CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

        while True:
            segment += 1
            ts = datetime.now().strftime("%H%M%S")
            filename = f"screen_{segment:03d}_{ts}.mp4"
            remote_path = f"/sdcard/screen_{segment:03d}.mp4"
            local_path = self.session_dir / filename

            # 在手机上录屏（--bugreport 在画面左上角叠加时间戳）
            cmd = [
                ADB_PATH, "-s", self.serial, "shell",
                "screenrecord",
                "--bugreport",
                "--size", RECORD_SIZE,
                "--bit-rate", RECORD_BITRATE,
                "--time-limit", str(RECORD_SEGMENT_SEC),
                remote_path,
            ]
            try:
                self._record_proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
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
        CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

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

        # 停止录屏（向手机发停止信号，让 screenrecord 优雅结束并保存文件）
        if self._record_proc and self._record_proc.poll() is None:
            try:
                subprocess.run(
                    [ADB_PATH, "-s", self.serial, "shell",
                     "pkill", "-INT", "screenrecord"],
                    capture_output=True, timeout=10,
                    creationflags=CREATE_NO_WINDOW,
                )
                # 等待录屏进程结束（给时间保存文件）
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

        # 等录屏线程拉取最后一个文件（最多等 120 秒，因为要拉取大文件）
        if self._record_thread and self._record_thread.is_alive():
            print("   ⏳ 等待最后一段录屏拉取完成...")
            self._record_thread.join(timeout=120)

        # 恢复触摸显示设置
        self._restore_show_touches()

        # 关闭文件
        if self.log_file:
            self.log_file.close()
        if self.crash_file:
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

    # ---------- 主入口 ----------
    def run(self):
        """主运行流程"""
        print("=" * 60)
        print("  🤖 Android 自动日志采集 & 录屏工具")
        print("=" * 60)

        # 1. 等待设备
        if not self.wait_for_device():
            print("❌ 未检测到设备，退出")
            return

        # 2. 初始化
        self._init_session()

        # 3. 启动 logcat
        self._start_logcat()

        # 4. 启动录屏（如果启用）
        if self.enable_record:
            self._start_recording_loop()

        print("\n🟢 采集中... 按 Ctrl+C 停止\n")

        # 5. 主循环：正计时 + 每 5 秒检查设备连接
        start_time = time.time()
        try:
            while not self._stop_event.is_set():
                for _ in range(10):  # 每 0.5 秒更新一次计时，共 5 秒
                    if self._stop_event.is_set():
                        break
                    elapsed = int(time.time() - start_time)
                    h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
                    print(f"\r⏱  已采集 {h:02d}:{m:02d}:{s:02d}   ", end="", flush=True)
                    time.sleep(0.5)
                if not self.is_device_connected():
                    print(f"\n\n📴 设备已断开，自动停止采集...")
                    break
        except KeyboardInterrupt:
            print("\n")
            print("⚠️  正在停止采集，请勿关闭窗口...")
            print("   （正在等待最后一段录屏保存并拉取到本地）")

        # 6. 清理 & 摘要
        self._stop_all()
        self._print_summary()


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
    logger.run()


if __name__ == "__main__":
    main()
