from __future__ import annotations

import json
import shutil
import subprocess
import time

from . import config
from .transcribe import Segment


# Retry policy for transient LLM failures (network timeouts, 5xx, rate limit).
# Only kicks in for exceptions whose class name is NOT in _NON_RETRYABLE_NAMES.
MAX_ATTEMPTS = 2
RETRY_WAIT_SEC = 30
_NON_RETRYABLE_NAMES = frozenset({
    # Anthropic & OpenAI SDK exceptions that mean "your request is invalid"
    "AuthenticationError",
    "PermissionDeniedError",
    "BadRequestError",
    "NotFoundError",
    "UnprocessableEntityError",
})


def _is_retryable(exc: BaseException) -> bool:
    """True if the exception looks transient (network/server), False if user-fixable."""
    return type(exc).__name__ not in _NON_RETRYABLE_NAMES


SYSTEM_PROMPT = """你是一位资深会议纪要助理。基于下面的会议转写文字，输出一份结构化的中文会议纪要。

要求：
1. 使用 Markdown 格式输出。
2. 包含以下章节（如无内容则写"无"）：
   - 会议主题
   - 参与方
   - 关键议题（要点列表）
   - 讨论摘要（按议题分小节）
   - 决议
   - 行动项（表格：负责人 / 事项 / 截止时间；若信息缺失留空）
   - 待跟进问题
3. 保持原始事实，不臆造未在转写中出现的细节。
4. 中英混合发言保留英文术语原文，必要时在括号给出中文。
5. 简洁清晰，避免冗余客套话。
"""


def _format_transcript(segments: list[Segment]) -> str:
    return "\n".join(f"[{_fmt_ts(s.start)}] {s.speaker}: {s.text}" for s in segments)


def _fmt_ts(seconds: float) -> str:
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}:{s:02d}"


def _build_user_content(segments: list[Segment], meta: dict | None) -> str:
    meta_header = ""
    if meta:
        bits = []
        if meta.get("name"):
            bits.append(f"会议名称：{meta['name']}")
        if meta.get("started_at_iso"):
            bits.append(f"开始时间：{meta['started_at_iso']}")
        if meta.get("duration"):
            bits.append(f"时长：{int(meta['duration'])}秒")
        if bits:
            meta_header = "\n".join(bits) + "\n\n"
    return f"{meta_header}会议转写：\n\n{_format_transcript(segments)}"


def _pick_backend() -> str:
    explicit = config.SUMMARY_BACKEND
    if explicit in {"api", "anthropic"}:
        return "api"
    if explicit in {"claude_code", "cli"}:
        return "claude_code"
    if explicit in {"openai_chat", "openai"}:
        return "openai_chat"
    if config.ANTHROPIC_API_KEY or config.ANTHROPIC_AUTH_TOKEN:
        return "api"
    if config.OPENAI_BASE_URL and config.OPENAI_API_KEY:
        return "openai_chat"
    if shutil.which(config.CLAUDE_CLI):
        return "claude_code"
    raise RuntimeError(
        "未找到可用的纪要生成后端，请至少配置以下之一：\n"
        "  【DeepSeek】 OPENAI_API_KEY=<your-deepseek-key>  "
        "OPENAI_BASE_URL=https://api.deepseek.com  "
        "AI_REC_SUMMARY_MODEL=deepseek-chat  "
        "AI_REC_SUMMARY_BACKEND=openai_chat\n"
        "  【Anthropic Claude】 ANTHROPIC_API_KEY=<your-anthropic-key>\n"
        "  【跳过纪要】 运行 win-rec process latest --no-summary"
    )


def summarize(segments: list[Segment], meta: dict | None = None, *,
              on_retry=None) -> str:
    """Generate a Chinese meeting summary. Retries transient failures.

    `on_retry(attempt, max_attempts, exception)` is called before each sleep-then-retry,
    letting the CLI show progress. Non-retryable errors (auth, bad request, permission)
    are raised immediately.
    """
    if not segments:
        return "（无可总结的转写内容）"
    backend = _pick_backend()
    user_content = _build_user_content(segments, meta)

    last_err: BaseException | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            if backend == "api":
                return _summarize_via_api(user_content)
            if backend == "openai_chat":
                return _summarize_via_openai_chat(user_content)
            return _summarize_via_claude_code(user_content)
        except Exception as e:
            last_err = e
            if not _is_retryable(e):
                raise
            if attempt >= MAX_ATTEMPTS:
                break
            if on_retry:
                on_retry(attempt, MAX_ATTEMPTS, e)
            time.sleep(RETRY_WAIT_SEC)

    # All attempts exhausted — re-raise the last error
    assert last_err is not None
    raise last_err


SUMMARY_TIMEOUT_SEC = 120  # summary content is longer than refine batches, allow more time
SUMMARY_CLI_TIMEOUT_SEC = 240  # claude CLI wraps a full summary; give the subprocess extra headroom


def _summarize_via_api(user_content: str) -> str:
    from anthropic import Anthropic

    kwargs: dict = {"timeout": SUMMARY_TIMEOUT_SEC}
    if config.ANTHROPIC_AUTH_TOKEN:
        kwargs["auth_token"] = config.ANTHROPIC_AUTH_TOKEN
    elif config.ANTHROPIC_API_KEY:
        kwargs["api_key"] = config.ANTHROPIC_API_KEY
    else:
        raise RuntimeError("neither ANTHROPIC_AUTH_TOKEN nor ANTHROPIC_API_KEY is set")
    if config.ANTHROPIC_BASE_URL:
        kwargs["base_url"] = config.ANTHROPIC_BASE_URL

    client = Anthropic(**kwargs)
    message = client.messages.create(
        model=config.SUMMARY_MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    return "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    ).strip()


def _summarize_via_openai_chat(user_content: str) -> str:
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set (required for openai_chat backend)")
    from openai import OpenAI

    kwargs = {"api_key": config.OPENAI_API_KEY, "timeout": SUMMARY_TIMEOUT_SEC}
    if config.OPENAI_BASE_URL:
        kwargs["base_url"] = config.OPENAI_BASE_URL
    client = OpenAI(**kwargs)

    response = client.chat.completions.create(
        model=config.SUMMARY_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        max_tokens=4096,
        temperature=0.3,
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError(f"openai_chat backend returned empty content: {response}")
    return content.strip()


def _summarize_via_claude_code(user_content: str) -> str:
    cli = shutil.which(config.CLAUDE_CLI)
    if not cli:
        raise RuntimeError(f"claude CLI not found in PATH: {config.CLAUDE_CLI}")

    proc = subprocess.run(
        [
            cli, "-p",
            "--output-format", "json",
            "--model", config.CLAUDE_CLI_MODEL,
            "--append-system-prompt", SYSTEM_PROMPT,
        ],
        input=user_content,
        capture_output=True,
        text=True,
        timeout=SUMMARY_CLI_TIMEOUT_SEC,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude CLI failed (exit {proc.returncode}):\n"
            f"stderr: {proc.stderr.strip()}\nstdout: {proc.stdout[:500]}"
        )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"claude CLI returned non-JSON: {proc.stdout[:500]}") from e
    if payload.get("is_error"):
        raise RuntimeError(f"claude CLI error: {payload.get('result') or payload}")
    result = payload.get("result")
    if not result:
        raise RuntimeError(f"claude CLI returned empty result: {payload}")
    return result.strip()
