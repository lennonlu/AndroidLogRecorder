# Android 自动日志采集 & 录屏工具

插上手机即开始采集，无需任何配置。

## 功能

| 功能 | 说明 |
|------|------|
| 日志采集 | 自动保存当天 Logcat，过滤历史日志 |
| 崩溃归档 | 自动识别 FATAL/ANR/Native crash，含堆栈上下文写入 `crashes.log` |
| 自动录屏 | 每段 170s 自动续录，横屏 1280×720 / 竖屏 720×1280 |
| 时间戳+触摸 | 录屏叠加实时时间 + 触摸圆点，方便回放定位 |
| 存储预警 | 设备空间 <500MB 自动告警 |
| 即时停止 | Ctrl+C 一次响应，回车确认退出 |
| 跨午夜采集 | 长时间运行穿越 00:00 自动切换日志日期 |

## 环境要求

- **Windows** + **USB 调试** 已开启的安卓手机
- ADB 已内置，无需额外安装
- exe 版无需 Python；源码版需 Python 3.8+

## 文件说明

```
├── AndroidLogger.exe     ← 独立可执行（推荐）
├── android_logger.py     ← Python 源码
├── 启动采集.bat           ← 双击：日志 + 录屏
├── 仅采集日志.bat         ← 双击：仅日志
├── adb.exe / AdbWin*.dll ← ADB 工具（已内置）
```

## 使用方法

**双击启动**：`启动采集.bat`（全功能）或 `仅采集日志.bat`（仅日志）。多设备时自动弹出选择菜单。

```bash
# 命令行
AndroidLogger.exe                  # 全功能
AndroidLogger.exe --no-record      # 仅日志
AndroidLogger.exe -s 设备序列号     # 指定设备
AndroidLogger.exe -o D:\captures   # 指定输出目录
```

## 输出结构

```
captures/session_型号_日期_时间/
├── logcat_full.log      # 当天日志
├── crashes.log          # 崩溃/ANR 归档 + 堆栈
├── screen_001.mp4       # 录屏片段
└── ...
```

## 设计要点

- **当天日志过滤**：按 MM-DD 前缀匹配，忽略缓冲区旧日志
- **录屏分段**：170s 一段自动续录，`--bugreport` 叠加时间戳
- **崩溃检测**：匹配 `FATAL EXCEPTION` / `Fatal signal` / `ANR in` 等关键词，自动写入 crashes.log（含 3-5 行上下文堆栈）
- **精确停止**：pkill 按文件名匹配，不误杀其他 screenrecord
- **触摸恢复**：停止流程优先恢复设备设置，避免 USB 断连后残留
- **批量 flush**：每 100 行写入一次，降低 IO 开销

## 停止

- **Ctrl+C** — 一次即停，自动拉取最后一段录屏，回车退出
- **拔 USB** — 自动检测断开，停止保存
