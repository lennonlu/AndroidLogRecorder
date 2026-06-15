# Android 自动日志采集 & 录屏工具

## 功能一览

| 功能 | 说明 |
|------|------|
| 自动检测设备 | 插上手机即开始，多设备弹出选择菜单 |
| Logcat 日志 | 全程保存，只记录当天日志，自动过滤历史 |
| 崩溃/ANR 归档 | 自动识别崩溃关键词 + 堆栈上下文，写入 `crashes.log` |
| 自动录屏 | 每段 170s 自动续录，无限时长 |
| 录屏时间戳 | 左上角叠加实时时间，方便对照日志 |
| 触摸圆点 | 录屏自动显示点击位置 |
| 正计时 | 采集中持续显示已用时间，确认脚本在运行 |
| 设备保留 | 录屏文件本地 + 设备各存一份 |
| 优雅停止 | Ctrl+C 后提示等待，防误关 |

## 环境要求

- **Windows** 系统
- **USB 调试** 已开启的安卓手机

> ADB 已内置于文件夹中，无需额外安装。  
> exe 版本无需 Python 环境；Python 源码版需 Python 3.8+。

## 文件清单

```
安卓测试脚本/
├── AndroidLogger.exe     ← 独立可执行（推荐，无需 Python）
├── android_logger.py     ← Python 源码（有 Python 环境也能用）
├── adb.exe               ← ADB 工具（已内置）
├── AdbWinApi.dll
├── AdbWinUsbApi.dll
├── 启动采集.bat           ← 双击启动：日志 + 录屏
├── 仅采集日志.bat         ← 双击启动：仅日志
└── README.md
```

## 使用方法

### 双击启动（推荐）

| 文件 | 功能 |
|------|------|
| `启动采集.bat` | 日志 + 录屏（全功能） |
| `仅采集日志.bat` | 只采集日志，不录屏 |

bat 自动优先使用 exe，没有 exe 则用 python。

### 命令行启动

```bash
# 完整采集（日志 + 录屏）
AndroidLogger.exe

# 仅日志
AndroidLogger.exe --no-record

# 指定设备序列号
AndroidLogger.exe -s 设备序列号

# 指定输出目录
AndroidLogger.exe -o D:\my_captures
```

### 多设备选择

插上多台手机时，自动列出所有设备：
```
📱 检测到 3 台设备，请选择：

  [1] abc12345           —  Pixel 6 (Android 14)
  [2] 192.168.1.100:5555 —  Redmi K60 (Android 13)
  [3] XYZ98765           —  Samsung S23 (Android 14)

请输入编号 (1-3):
```

---

## 运作原理

```
┌─────────────────────────────────────────────────────┐
│                  android_logger.py                   │
│                                                     │
│  主线程                                              │
│  ├─ 1. 轮询 adb devices，等待手机连接                  │
│  ├─ 2. 多设备时弹出选择菜单                            │
│  ├─ 3. 创建输出目录（目录名含型号 + 时间戳）             │
│  ├─ 4. 启动日志线程 + 录屏线程                         │
│  ├─ 5. 正计时显示 + 每 5 秒检查设备连接                 │
│  └─ 6. Ctrl+C 或断开 → 提示正在停止 → 清理 → 摘要      │
│                                                     │
│  日志线程（daemon）                                    │
│  ├─ 启动 adb logcat -v threadtime                    │
│  ├─ 过滤：只保留当天日志（按 MM-DD 前缀匹配）            │
│  ├─ 逐行写入 logcat_full.log                         │
│  └─ 崩溃规则匹配 → 写入 crashes.log + 上下文堆栈       │
│                                                     │
│  录屏线程（daemon）                                    │
│  ├─ 开启 show_touches（显示触摸圆点）                   │
│  └─ 循环：                                            │
│      ├─ adb shell screenrecord --bugreport \          │
│      │    --size 1280x720 --bit-rate 4M \             │
│      │    --time-limit 170 /sdcard/screen_NNN.mp4     │
│      ├─ 等待录屏结束                                   │
│      ├─ adb pull 拉到本地（设备上保留一份）              │
│      └─ 检查停止信号 → 退出循环                         │
│                                                     │
│  停止时（_stop_all）                                   │
│  ├─ pkill -INT screenrecord（优雅停止，保存文件）       │
│  ├─ 等待最后一段录屏拉取完成（最多 120s）                │
│  ├─ 恢复 show_touches 原始状态                         │
│  └─ 关闭文件 → 输出采集摘要                             │
└─────────────────────────────────────────────────────┘
```

### 关键设计

**1. 只记录当天日志**
logcat 缓冲区可能有几天的旧日志，脚本按 `MM-DD` 前缀过滤，只写入当天的行，避免文件过大。

**2. 录屏分段 + 时间戳**
`screenrecord` 单次最长 180 秒，脚本设为 170 秒一段，录完自动拉取再续录。使用 `--bugreport` 在画面左上角叠加实时时间，对照 `logcat_full.log` 的时间戳精确定位问题。

**3. 触摸圆点**
录屏前自动设置 `show_touches=1`，结束后恢复。回放视频能看到点击了哪个位置。

**4. 崩溃检测静默运行**
不刷屏，全部写入 `crashes.log`。分两级：
- 高优先级（含上下文）：`FATAL EXCEPTION`、`Fatal signal`、`ANR in`、`am_crash`、`am_anr`
- 低优先级（仅记录）：`Tombstone`、`Force finishing activity`、`Process has died` 等

**5. 录屏双份保存**
拉取到本地后不删除设备上的文件，手机和电脑各一份。

**6. 正计时**
采集中持续显示 `⏱ 已采集 00:03:27`，确认脚本在正常运行。

---

## 输出目录结构

```
captures/
└── session_SM-G9500_20260615_153000/
    ├── logcat_full.log                # 当天 logcat 日志
    ├── crashes.log                    # 崩溃/ANR 归档 + 堆栈
    ├── screen_001_153022.mp4          # 录屏片段 1（15:30:22 开始）
    ├── screen_002_153312.mp4          # 录屏片段 2
    └── ...
```

## 快速定位崩溃

```bash
# 查看崩溃归档（自动提取的，最直接）
cat captures/session_XXXX/crashes.log

# 在完整日志中搜索
grep -i "FATAL\|ANR\|crash" captures/session_XXXX/logcat_full.log
```

## 崩溃检测规则

**高优先级（含上下文堆栈）：**

| 关键词 | 含义 | 上下文行数 |
|--------|------|-----------|
| `FATAL EXCEPTION` | Java 层未捕获异常 | 5 行 |
| `Fatal signal N` | Native 层崩溃（SIGSEGV/SIGABRT） | 3 行 |
| `ANR in xxx` | 应用无响应 | 5 行 |
| `am_crash` | ActivityManager 崩溃记录 | - |
| `am_anr` | ActivityManager ANR 记录 | - |

**低优先级（仅记录）：**

| 关键词 | 含义 |
|--------|------|
| `Native crash` | Native 崩溃 |
| `Tombstone written to` | 系统崩溃转储 |
| `Force finishing activity` | Activity 强制结束 |
| `Process has died` | 进程死亡 |
| `backtrace:` | 堆栈回溯 |

## 录屏参数

| 参数 | 值 | 说明 |
|------|-----|------|
| 分辨率 | 1280x720 | 横屏 720p |
| 码率 | 4 Mbps | 默认 20Mbps，降低以减小文件 |
| 单段时长 | 170 秒 | adb 上限 180s，留余量 |
| 时间戳 | --bugreport | 左上角叠加实时时间 |
| 触摸圆点 | show_touches | 录屏期间自动开启 |

## 停止方式

- **Ctrl+C** — 终端提示「正在停止采集，请勿关闭窗口」，等待最后一段录屏拉取完成
- **拔掉 USB** — 自动检测到设备断开，停止并保存
