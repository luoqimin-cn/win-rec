# win-rec

Windows 会议录音、转写与纪要生成工具（命令行）。

Record your meetings, auto-transcribe with Whisper, and generate structured summaries via Claude — all from the terminal on Windows.

---

## 功能 Features

- 录制麦克风音频（mic-only），适合面对面或耳机通话会议
- 本地 Whisper 转写（CPU 或 GPU），无需上传音频，隐私安全
- LLM 精炼：用 Claude 修正 ASR 术语错误
- 会议纪要：自动生成结构化 Markdown 摘要
- 词汇表：预定义正确拼写，引导 LLM 纠正专有名词
- 暂停/继续：支持中途暂停录音，暂停时间不计入时长
- 安全删除：移入回收站而非直接删除
- 系统托盘图标：可在任务栏托盘操作录音（需安装 `pystray`）

---

## 安装方式 Installation

### 方式一：直接运行打包版（推荐给普通用户）

> **无需安装 Python、ffmpeg 或任何依赖。** 下载 `win-rec.exe`，放到任意文件夹，直接用命令提示符（cmd）运行即可。

1. 从发布页面下载 `win-rec.exe`
2. 打开命令提示符（Win + R，输入 `cmd`，回车）
3. 切换到 exe 所在目录：`cd C:\Users\你的名字\Downloads`
4. 运行：`win-rec.exe start`

打包版内含 ffmpeg，开箱即用。

---

### 方式二：从源码安装（开发者）

#### 1. 安装 Python

从 [python.org](https://www.python.org/downloads/) 下载 Python 3.11 或 3.12。
安装时勾选 **"Add Python to PATH"**。

验证：
```cmd
python --version
```

#### 2. 安装 ffmpeg

**方法 A（推荐）：**
```cmd
winget install ffmpeg
```

**方法 B（手动）：**
1. 从 [ffmpeg.org/download.html](https://ffmpeg.org/download.html) 下载 Windows 构建
2. 解压到 `C:\ffmpeg\`
3. 将 `C:\ffmpeg\bin` 添加到系统 PATH

验证：
```cmd
ffmpeg -version
```

#### 3. 安装 win-rec

```cmd
python -m venv venv
venv\Scripts\activate
pip install win-rec
```

或从源码：
```cmd
git clone https://github.com/your-org/win-rec.git
cd win-rec
python -m venv venv
venv\Scripts\activate
pip install -e .
```

#### 4. 安装 faster-whisper

**CPU 模式（无 GPU）：**
```cmd
pip install faster-whisper
```

**GPU 模式（NVIDIA，需 CUDA 11.8 或 12.x）：**
```cmd
pip install torch --index-url https://download.pytorch.org/whl/cu121
pip install faster-whisper
```

---

## 配置 AI 纪要功能 AI Summary Configuration

win-rec 使用大语言模型（LLM）自动生成会议纪要，支持多种 AI 服务。**推荐使用 DeepSeek，价格低廉且国内访问稳定。**

### 方案一：使用 DeepSeek（推荐）

**第一步：获取 DeepSeek API Key**

1. 访问 [platform.deepseek.com](https://platform.deepseek.com)
2. 注册或登录账号
3. 点击左侧"API Keys"→"创建 API Key"
4. 复制生成的密钥（格式为 `sk-...`）

**第二步：设置环境变量（需要设置以下全部 4 个）**

打开命令提示符，逐行输入（把 `sk-你的密钥` 替换为真实密钥）：

```cmd
setx OPENAI_API_KEY "sk-你的DeepSeek密钥"
setx OPENAI_BASE_URL "https://api.deepseek.com"
setx AI_REC_SUMMARY_MODEL "deepseek-chat"
setx AI_REC_SUMMARY_BACKEND "openai_chat"
```

执行后**关闭并重新打开**命令提示符，设置生效。

> **说明：** DeepSeek 的 API 与 OpenAI 格式兼容，因此密钥填在 `OPENAI_API_KEY` 里。这是正常的，不是错误。

---

### 方案二：使用 Anthropic Claude

**第一步：获取 Anthropic API Key**

1. 访问 [console.anthropic.com](https://console.anthropic.com)
2. 注册或登录，进入"API Keys"页面
3. 点击"Create Key"，复制密钥（格式为 `sk-ant-...`）

**第二步：设置环境变量**

```cmd
setx ANTHROPIC_API_KEY "sk-ant-api03-你的密钥"
```

执行后关闭并重新打开命令提示符。

---

### 如何通过 Windows 图形界面设置（不想用命令行）

1. 右键"此电脑"→ 属性 → 高级系统设置 → 环境变量
2. 在"用户变量"区域点击"新建"
3. 逐个添加上面的变量名和变量值
4. 全部添加完后点击确定，重启程序

---

### 不想使用 AI 纪要功能？

只做语音转写，跳过纪要生成（无需任何 API key）：
```cmd
win-rec process latest --no-summary
```

---

## 其他配置项 Other Configuration

所有配置通过环境变量设置（`setx` 永久设置，`set` 临时设置）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `AI_REC_DATA_ROOT` | `~/AI_Rec_Data` | 录音和转写文件的根目录 |
| `AI_REC_WHISPER_MODEL` | `medium` | Whisper 模型大小（`tiny` / `base` / `small` / `medium` / `large-v3`） |
| `AI_REC_WHISPER_DEVICE` | `auto` | `cpu` / `cuda` / `auto` |
| `AI_REC_WHISPER_COMPUTE_TYPE` | `auto` | `int8` / `float16` / `auto` |
| `AI_REC_REFINE` | `1` | 设为 `0` 跳过 LLM 精炼步骤 |
| `AI_REC_SUMMARY_BACKEND` | `auto` | `openai_chat`（DeepSeek/OpenAI）/ `api`（Anthropic） |
| `AI_REC_SUMMARY_MODEL` | `claude-sonnet-4-6` | 纪要使用的模型名（DeepSeek 时填 `deepseek-chat`） |
| `OPENAI_API_KEY` | — | DeepSeek 或 OpenAI 的 API Key |
| `OPENAI_BASE_URL` | — | DeepSeek 填 `https://api.deepseek.com` |
| `ANTHROPIC_API_KEY` | — | Anthropic Claude 的 API Key |

---

## 使用 Usage

### 基本工作流

```cmd
:: 1. 开始录音（录制麦克风）
win-rec start

:: 2. 开会 ...

:: 3. 停止录音
win-rec stop

:: 4. 转写并生成纪要
win-rec process latest
```

停止时直接处理（一步完成）：
```cmd
win-rec stop --process
```

---

### 所有命令

#### `win-rec start` — 开始录音

```
win-rec start [--name "会议名称"]
```

| 选项 | 说明 |
|------|------|
| `--name "会议名称"` | 为本次会议命名，方便后续查找 |

---

#### `win-rec stop` — 停止录音

```
win-rec stop [--process] [--refine/--no-refine]
```

| 选项 | 说明 |
|------|------|
| `--process` | 停止后立即运行转写和纪要生成 |
| `--refine` / `--no-refine` | 覆盖是否进行 LLM 精炼 |

---

#### `win-rec process` — 转写和生成纪要

```
win-rec process [session_id | latest] [--no-summary] [--redo-transcribe] [--refine/--no-refine]
```

| 选项 | 说明 |
|------|------|
| `session_id` | 会话 ID（如 `2024-01-15_10-30-00`），默认为 `latest` |
| `--no-summary` | 只转写，不生成纪要（不需要 API key） |
| `--redo-transcribe` | 强制重新运行 Whisper（忽略缓存） |

---

#### `win-rec pause` / `win-rec resume` — 暂停/继续

```
win-rec pause
win-rec resume
```

暂停时正在录制的片段会被保留；继续时开启新片段。暂停时间不计入会议时长。

---

#### `win-rec status` — 查看当前状态

```
win-rec status
```

显示是否正在录音、麦克风状态，以及当前会话 ID。

---

#### `win-rec list` — 查看所有会话

```
win-rec list
```

列出所有录音会话，显示名称、时长、转写和纪要状态。

---

#### `win-rec delete` — 删除会话

```
win-rec delete <session_id | latest> [--force] [--yes]
```

将会话目录移入回收站（可从文件资源管理器恢复）。

| 选项 | 说明 |
|------|------|
| `--force` | 强制删除超过 3 分钟的录音 |
| `--yes` / `-y` | 跳过交互式确认 |

---

#### `win-rec run-daily` — 批量处理未完成的会话

```
win-rec run-daily [--dry-run]
```

处理所有有音频但缺少 `summary.md` 的会话。可配置为计划任务每日自动运行。

---

#### `win-rec glossary` — 管理词汇表

```
win-rec glossary           # 显示当前词汇表
win-rec glossary --edit    # 在编辑器中打开（Windows 下默认 Notepad）
win-rec glossary --path    # 显示文件路径
```

词汇表文件：`~/AI_Rec_Data/glossary.yaml`

格式示例：
```yaml
people:
  张三: [zhang san, 张三丰]
  Alice: [爱丽丝, ali]
companies:
  Anthropic: []
products:
  Claude: [claud, 克劳德]
```

---

## 系统托盘图标（Windows 任务栏）

win-rec 可在 Windows 系统托盘（右下角图标区）显示录音状态，支持右键菜单操作：

- 开始/停止录音
- 暂停/继续
- 查看状态

安装托盘支持（源码安装时）：
```cmd
pip install pystray pillow
```

打包版已内含此功能，无需额外安装。

---

## 自动化：配置 Windows 计划任务

让 `run-daily` 每天自动处理当天的录音：

1. 打开"任务计划程序"→ 创建基本任务
2. 触发器：每天定时（如晚上 22:00）
3. 操作：启动程序
   - 程序：`C:\path\to\win-rec.exe`（打包版）或 `C:\path\to\venv\Scripts\win-rec.exe`（源码版）
   - 参数：`run-daily`
4. 勾选"无论用户是否登录都要运行"

---

## 文件结构 File Layout

录音数据存储在 `~/AI_Rec_Data/`（可通过 `AI_REC_DATA_ROOT` 修改）：

```
AI_Rec_Data/
├── recordings/
│   └── 2024-01-15_10-30-00/      ← 会话目录
│       ├── meta.json              ← 会话元数据（开始时间、名称、暂停记录等）
│       ├── mic.m4a                ← 麦克风录音
│       ├── transcript.json        ← 转写结果（JSON，包含时间戳）
│       ├── transcript.md          ← 转写结果（Markdown，人类可读）
│       └── summary.md             ← 会议纪要（Markdown）
├── glossary.yaml                  ← 专有名词词汇表
└── current.json                   ← 当前活跃会话指针
```

---

## 常见问题 FAQ

**Q: `win-rec start` 后没有声音录进去？**

确认 Windows 默认麦克风正常工作：在系统声音设置中检查麦克风是否启用，并在"录音设备"中设置为默认设备。

**Q: 提示找不到 ffmpeg？**

- 打包版（.exe）：ffmpeg 已内置，无需额外安装
- 源码版：运行 `ffmpeg -version` 确认已安装，如未安装参考上方安装步骤

**Q: Whisper 转写速度很慢？**

- 切换到更小的模型：`setx AI_REC_WHISPER_MODEL small`
- 如有 NVIDIA GPU，确认 CUDA 正确安装并设置 `setx AI_REC_WHISPER_DEVICE cuda`
- CPU 下推荐 `medium` 或更小的模型

**Q: 提示找不到 API Key 或无法生成纪要？**

参考上方"配置 AI 纪要功能"章节配置 DeepSeek 或 Anthropic 密钥，设置后重新打开命令提示符。
如果不需要 AI 纪要功能，运行 `win-rec process latest --no-summary` 跳过。

**Q: `win-rec start` 提示录音已在运行？**

可能上次录音异常退出。运行 `win-rec status` 查看状态。如果确认没有活跃录音，可手动删除 `AI_Rec_Data\current.json`。

**Q: 打包后的 .exe 文件还需要安装 Python 吗？**

不需要。打包版（PyInstaller 构建的 .exe）已将 Python 解释器、所有依赖和 ffmpeg 一起打包，用户下载后可直接运行，无需任何预先安装。

---

## 许可证 License

MIT
