@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

echo ============================================================
echo  win-rec Windows 打包脚本
echo ============================================================
echo.

:: ---- 检查 Python ----
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 找不到 Python，请先安装 Python 3.11 或 3.12
    echo   下载地址：https://www.python.org/downloads/
    pause & exit /b 1
)

:: ---- 检查 ffmpeg ----
where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo [错误] 找不到 ffmpeg，请先安装：
    echo   winget install ffmpeg
    echo   或手动下载并将 bin 目录加入 PATH
    pause & exit /b 1
)
for /f "tokens=*" %%i in ('where ffmpeg') do set FFMPEG_PATH=%%i
echo [OK] ffmpeg: %FFMPEG_PATH%

:: ---- 创建虚拟环境 ----
if not exist "venv_build" (
    echo [1/5] 创建构建虚拟环境...
    python -m venv venv_build
) else (
    echo [1/5] 复用已有构建虚拟环境
)

call venv_build\Scripts\activate.bat

:: ---- 安装依赖 ----
echo [2/5] 安装项目依赖...
pip install -e . --quiet
if errorlevel 1 ( echo [错误] 依赖安装失败 & pause & exit /b 1 )

echo [3/5] 安装 PyInstaller...
pip install pyinstaller --quiet
if errorlevel 1 ( echo [错误] PyInstaller 安装失败 & pause & exit /b 1 )

:: ---- 获取 ffmpeg/ffprobe 所在目录（用于打包时一起带入）----
for %%i in ("%FFMPEG_PATH%") do set FFMPEG_DIR=%%~dpi
:: 去掉末尾反斜杠
set FFMPEG_DIR=%FFMPEG_DIR:~0,-1%
echo [INFO] ffmpeg 目录: %FFMPEG_DIR%

:: ---- PyInstaller 打包主程序 ----
echo [4/5] 打包主程序 win-rec.exe ...
pyinstaller ^
    --onedir ^
    --name win-rec ^
    --console ^
    --noconfirm ^
    --add-binary "%FFMPEG_DIR%\ffmpeg.exe;." ^
    --add-binary "%FFMPEG_DIR%\ffprobe.exe;." ^
    --add-data "win_rec;win_rec" ^
    --hidden-import win_rec.cli ^
    --hidden-import win_rec.recorder ^
    --hidden-import win_rec.recorder_win ^
    --hidden-import win_rec.transcribe ^
    --hidden-import win_rec.summarize ^
    --hidden-import win_rec.refine ^
    --hidden-import win_rec.store ^
    --hidden-import win_rec.config ^
    --hidden-import win_rec.glossary ^
    --hidden-import win_rec.diarize ^
    --hidden-import typer ^
    --hidden-import rich ^
    --hidden-import faster_whisper ^
    --hidden-import ctranslate2 ^
    --hidden-import anthropic ^
    --hidden-import openai ^
    --hidden-import filelock ^
    --hidden-import send2trash ^
    --hidden-import pydub ^
    --hidden-import mutagen ^
    --hidden-import yaml ^
    --collect-all faster_whisper ^
    --collect-all ctranslate2 ^
    --collect-all silero_vad ^
    main.py 2>build_main.log
if errorlevel 1 (
    echo [错误] 主程序打包失败，查看 build_main.log
    pause & exit /b 1
)

:: ---- PyInstaller 打包子进程 recorder_win ----
echo [4b/5] 打包子进程 win-rec-recorder.exe ...
pyinstaller ^
    --onefile ^
    --name win-rec-recorder ^
    --console ^
    --noconfirm ^
    win_rec\recorder_win.py 2>build_recorder.log
if errorlevel 1 (
    echo [错误] recorder 打包失败，查看 build_recorder.log
    pause & exit /b 1
)

:: ---- 合并输出目录 ----
echo [5/5] 整理输出目录...
copy /y dist\win-rec-recorder.exe dist\win-rec\win-rec-recorder.exe >nul

:: ---- 完成 ----
echo.
echo ============================================================
echo  打包完成！输出目录：dist\win-rec\
echo ============================================================
echo.
echo  分发给用户时，将整个 dist\win-rec\ 文件夹压缩成 zip 即可。
echo  用户解压后，在文件夹内打开 cmd，运行 win-rec.exe start
echo.

call venv_build\Scripts\deactivate.bat
pause
