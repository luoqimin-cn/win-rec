---
name: win-rec-build-pitfalls
description: 构建过程中反复踩的坑和最终修复方案
metadata:
  type: reference
---

## 反复出现的问题

### 1. SRC 同步覆盖修复
- 现象：dshow 修复推送后，下次 SRC 同步又覆盖回旧的 WASAPI 代码
- 发生过 2+ 次，每次都需重新应用 recorder_win.py + workflow

### 2. CI 中 pwsh 的 `2>&1` 类型陷阱
- `& ffmpeg -devices 2>&1` 在 pwsh 中会把 stderr 转成 ErrorRecord 对象
- `-notmatch` 对 ErrorRecord 直接匹配会失败（不是 string）
- **修复**：`& ffmpeg -devices 2>&1 | Out-String` 先转成 string 再匹配

### 3. ffmpeg 验证：`-formats` vs `-devices`
- WASAPI/dshow 是 device 而非 format，不能用 `ffmpeg -formats` 检查
- **修复**：用 `ffmpeg -devices 2>&1 | Out-String` 检查 dshow

### 4. GitHub Actions Artifacts 不支持续传
- 用户多次下载 282MB zip 接近完成时失败
- **修复**：每次构建自动更新 `latest` Release → `https://github.com/luoqimin-cn/win-rec/releases/tag/latest`
- Release 下载支持 HTTP Range，浏览器自带断点续传

### 5. Release 写入权限
- `softprops/action-gh-release@v2` 更新 release 需要 `permissions: contents: write`

### 6. Git push 网络问题
- HTTPS 连 GitHub 超时（20.205.243.166:443 TCP 不通）
- 改用 SSH push，需先生成 SSH key 并添加到 GitHub Settings

## 最终 stable 的 workflow 关键配置

```yaml
permissions:
  contents: write

# ffmpeg 验证（pwsh）：
$devices = & ffmpeg -devices 2>&1 | Out-String
if ($devices -notmatch "dshow") { throw "ffmpeg 不支持 dshow" }

# 自动 latest release：
- uses: softprops/action-gh-release@v2
  with:
    tag_name: latest
    prerelease: true
    make_latest: true
    files: dist\win-rec-windows.zip
```

## 下载地址

- **latest release（推荐，支持续传）**: `https://github.com/luoqimin-cn/win-rec/releases/tag/latest`
- **Actions artifacts（不支持续传）**: `https://github.com/luoqimin-cn/win-rec/actions`
