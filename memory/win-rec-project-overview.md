---
name: win-rec-project-overview
description: win-rec 项目概况、架构和工作流
metadata:
  type: project
---

## 项目定位
Windows 会议录音、转写与纪要生成 CLI 工具。录麦克风 → Whisper 转写 → LLM 精炼 → 生成中文纪要。

## 仓库
- GitHub: `luoqimin-cn/win-rec`
- Release 下载: `https://github.com/luoqimin-cn/win-rec/releases/tag/latest`
- 每次构建自动更新 latest release（支持 HTTP Range 续传）

## 核心模块
- `cli.py` — Typer CLI 命令入口
- `recorder.py` — 录音控制层，启动/停止子进程
- `recorder_win.py` — Windows 录音子进程（ffmpeg dshow 采 mic）
- `store.py` — Session 数据模型 + filelock 并发控制
- `transcribe.py` — faster-whisper 本地转写 + VAD 切片 + 增量 checkpoint
- `refine.py` — LLM 批量修正 ASR 错误，5 线程并发，递归拆分重试
- `summarize.py` — LLM 生成结构化中文纪要（Anthropic/DeepSeek/OpenAI）
- `glossary.py` — 专有名词词汇表，注入 LLM prompt
- `diarize.py` — 双轨合并 + AssemblyAI 说话人分离
- `tray.py` — 系统托盘图标
- `config.py` — 环境变量配置

## 数据目录
`~/AI_Rec_Data/recordings/<session_id>/` 下有 `meta.json`, `mic.m4a`, `transcript.json`, `transcript.md`, `summary.md`
