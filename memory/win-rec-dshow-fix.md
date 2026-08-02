---
name: win-rec-dshow-fix
description: recorder_win.py 从 WASAPI 迁移到 dshow 的修复，以及 CI Release 自动发布
metadata:
  type: reference
---

## dshow 迁移（已应用）
`recorder_win.py` 的 `_ffmpeg_start()` 从 WASAPI 改为 dshow：
- 新增 `_get_default_mic()` — 通过 `ffmpeg -list_devices true -f dshow -i dummy` 自动发现第一个可用的 dshow 音频设备
- ffmpeg 参数从 `-f wasapi -i default` 改为 `-f dshow -i audio=<mic_name>`
- 原因：打包版 ffmpeg 静态编译不支持 WASAPI

## CI Workflow 修改（`.github/workflows/build-windows.yml`）
1. ffmpeg 验证增加了 dshow 检查
2. 每次构建自动创建/更新 GitHub Release（`softprops/action-gh-release@v2`）
   - tag: `latest`（强制更新）
   - prerelease: true
   - 支持 HTTP Range 续传下载
3. 添加 `permissions: contents: write` 解决 release 写入权限
