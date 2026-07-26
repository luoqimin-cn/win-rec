from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import config, glossary
from .transcribe import Segment


def _system_prompt() -> str:
    """Base system prompt, optionally prefixed with the user's glossary."""
    return glossary.as_prompt_fragment() + SYSTEM_PROMPT


LLM_TIMEOUT_SEC = 60      # per-request timeout for a single LLM call (Anthropic / OpenAI SDKs)
CLI_TIMEOUT_SEC = 120     # claude CLI subprocess (small overhead vs raw HTTP, keep a bit larger)
MAX_RETRY_DEPTH = 2       # after 2 splits (80 → 40 → 20), give up and keep raw
PARALLEL_WORKERS = 5      # concurrent LLM calls

# When set, refine dumps every parse-failed LLM output to a directory for later inspection.
# Set via AI_REC_REFINE_DEBUG_DIR=/some/path or defaults to <recording_dir>/refine_debug/
# passed in via `debug_dir=` kwarg.
_debug_lock = threading.Lock()


SYSTEM_PROMPT = """你是一位会议转写校对助理。你会收到一份由 ASR（自动语音识别）生成的会议转写，可能含有识别错误和幻听。

任务分两类：
【修正】只修正明显的**同音字错误、专业术语、人名地名、公司/产品名、英文缩写**，并做**基本标点整理**。
【删除】识别 ASR 幻听并将其 text 输出为空字符串 ""，常见幻听包括但不限于：
- "中文字幕提供"、"字幕由XX提供"、"字幕组"、"感谢观看"、"请订阅"、"点赞订阅"
- 反复出现的同一个短语（如整段都是 "H&M"、"you"、"the"）
- "Thank you"/"Thank you for watching" 之类 YouTube 字幕污染
- 明显与前后文完全无关的孤立短句（尤其在明显是静音的时段）

严格约束：
1. 输出的数组长度必须与输入完全一致（幻听条目 text 设为 "" 而非删除元素）。
2. 保留发言者说话的口吻和句式。
3. 不臆造未在原文中出现的内容。
4. 中英混合发言中的英文术语原样保留。
5. 输出必须是严格的 JSON 数组，每个元素只有一个字段 `text`。

输入格式（每条一行）：
INDEX. [SPEAKER] TEXT

输出格式（示例）：
[{"text": "修正后的第一条文本"}, {"text": ""}, {"text": "第三条正常内容"}, ...]
"""


BATCH_SIZE = 80  # segments per LLM call; keeps output JSON well within token limits


class RefineError(RuntimeError):
    pass


def refine_segments(segments: list[Segment], *,
                    on_progress=None,
                    debug_dir: Path | None = None) -> list[Segment]:
    """Return a new list of Segments with LLM-refined text; timestamps/speakers preserved.

    Top-level batches (BATCH_SIZE segments each) run concurrently across PARALLEL_WORKERS
    threads. On parse failure, the batch is split in half and each half retried, up to
    MAX_RETRY_DEPTH splits (so 80 → 40 → 20). Any segments still failing are kept as-is
    (raw). Any partial failure is surfaced via RefineError so callers can warn the user.

    If `debug_dir` is provided (or AI_REC_REFINE_DEBUG_DIR is set), every LLM response that
    fails to parse gets written there, so root cause can be diagnosed.
    """
    if not segments:
        return segments

    if debug_dir is None:
        env_dir = os.environ.get("AI_REC_REFINE_DEBUG_DIR")
        if env_dir:
            debug_dir = Path(env_dir)
    if debug_dir is not None:
        debug_dir.mkdir(parents=True, exist_ok=True)

    initial_batches = [
        (i * BATCH_SIZE, min((i + 1) * BATCH_SIZE, len(segments)))
        for i in range(0, (len(segments) + BATCH_SIZE - 1) // BATCH_SIZE)
    ]
    total_batches = len(initial_batches)
    corrected: list[str | None] = [None] * len(segments)
    failed_ranges: list[tuple[int, int, str]] = []
    ranges_lock = threading.Lock()
    progress_lock = threading.Lock()
    completed = [0]

    def _worker(start: int, end: int) -> bool:
        ok = _try_batch(segments, start, end, 0, corrected, failed_ranges,
                        ranges_lock, debug_dir)
        with progress_lock:
            completed[0] += 1
            if on_progress:
                on_progress(
                    completed[0], total_batches,
                    error=None if ok else "kept as raw after retries"
                )
        return ok

    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as pool:
        futures = [pool.submit(_worker, s, e) for s, e in initial_batches]
        for f in as_completed(futures):
            f.result()

    out: list[Segment] = []
    for i, seg in enumerate(segments):
        new_text = corrected[i]
        if new_text is None:
            out.append(seg)
            continue
        if new_text == "":
            continue
        out.append(Segment(
            speaker=seg.speaker,
            start=seg.start,
            end=seg.end,
            text=new_text,
            source=seg.source,
        ))

    if failed_ranges:
        failed_seg_count = sum(end - start for start, end, _ in failed_ranges)
        details = "; ".join(
            f"segs[{s}:{e}] ({segments[s].start:.0f}s-{segments[e-1].end:.0f}s): {msg[:50]}"
            for s, e, msg in failed_ranges[:3]
        )
        raise RefineError(
            f"{failed_seg_count}/{len(segments)} segments not refined "
            f"across {len(failed_ranges)} sub-batch(es) [{details}] — kept as-is"
        )
    return out


def _try_batch(segments: list[Segment], start: int, end: int, depth: int,
               corrected: list[str | None],
               failed_ranges: list[tuple[int, int, str]],
               ranges_lock: threading.Lock,
               debug_dir: Path | None) -> bool:
    """Try to refine segments[start:end]. On failure, split in half and recurse,
    up to MAX_RETRY_DEPTH times. Returns True if at least one sub-range succeeded.
    If debug_dir is set, parse-failed LLM outputs are dumped there."""
    batch = segments[start:end]
    err: str | None = None
    raw_output: str | None = None
    try:
        raw_output = _call_llm(_format_input(batch))
        parsed = _parse_output(raw_output, expected=len(batch))
        if parsed is not None:
            for i, text in enumerate(parsed):
                corrected[start + i] = text
            return True
        err = "output JSON did not match expected shape"
    except Exception as e:
        err = f"LLM error: {e}"

    # If we have a debug_dir and a raw output that failed to parse, save it.
    if debug_dir is not None and raw_output is not None:
        _dump_debug(debug_dir, start, end, depth, batch, raw_output, err)

    if depth >= MAX_RETRY_DEPTH:
        with ranges_lock:
            failed_ranges.append((start, end, err or "unknown"))
        return False

    mid = start + (end - start) // 2
    left_ok = _try_batch(segments, start, mid, depth + 1, corrected, failed_ranges,
                         ranges_lock, debug_dir)
    right_ok = _try_batch(segments, mid, end, depth + 1, corrected, failed_ranges,
                          ranges_lock, debug_dir)
    return left_ok or right_ok


def _dump_debug(debug_dir: Path, start: int, end: int, depth: int,
                batch: list[Segment], raw_output: str, err: str) -> None:
    with _debug_lock:
        ts = int(time.time() * 1000)
        fname = f"parse_fail_d{depth}_segs{start:04d}-{end:04d}_{ts}.txt"
        path = debug_dir / fname
        with path.open("w", encoding="utf-8") as f:
            f.write(f"# refine parse failure\n")
            f.write(f"# depth: {depth}\n")
            f.write(f"# segments: [{start}:{end}] ({end-start} items)\n")
            f.write(f"# time range: {batch[0].start:.1f}s → {batch[-1].end:.1f}s\n")
            f.write(f"# error: {err}\n")
            f.write(f"# raw LLM output length: {len(raw_output)} chars\n")
            f.write(f"\n=== FIRST 20 INPUT SEGMENTS ===\n")
            for i, seg in enumerate(batch[:20]):
                f.write(f"{i}. [{seg.speaker}] {seg.text[:150]}\n")
            f.write(f"\n=== RAW LLM OUTPUT (first 4000 chars) ===\n")
            f.write(raw_output[:4000])
            if len(raw_output) > 4000:
                f.write(f"\n\n=== ... truncated, {len(raw_output) - 4000} more chars ===\n")
                f.write(f"\n=== LAST 500 chars ===\n")
                f.write(raw_output[-500:])


def _format_input(segments: list[Segment]) -> str:
    lines = []
    for i, s in enumerate(segments):
        text = s.text.replace("\n", " ").strip()
        lines.append(f"{i}. [{s.speaker}] {text}")
    return "\n".join(lines)


def _parse_output(raw: str, expected: int) -> list[str] | None:
    """Extract the first JSON array from `raw` and validate it.

    LLMs frequently prepend prose before the JSON (e.g. "I'll analyze..." or
    "Key observations: - Items [13-18]..."). A bare `find("[")` then hits a
    prose `[` and fails. Strategy: prefer the `[` that immediately follows a
    code fence (```json\n[ or ```\n[); fall back to the first `[` only when
    no fence is present.

    Also handles count mismatches with a best-effort fallback: if the JSON is
    structurally valid but has ±few items vs expected, pad with "" or truncate
    rather than discarding the whole batch.
    """
    text = raw.lstrip()

    # Try to find a code-fence block first, even if prose precedes it.
    fence_bracket = -1
    for fence in ("```json\n[", "```JSON\n[", "```\n["):
        idx = text.find(fence)
        if idx != -1:
            fence_bracket = idx + len(fence) - 1  # points at '['
            break

    if fence_bracket >= 0:
        bracket_idx = fence_bracket
    else:
        bracket_idx = text.find("[")

    if bracket_idx < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(text[bracket_idx:])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None

    out: list[str] = []
    for item in data:
        if not isinstance(item, dict) or "text" not in item:
            return None
        val = item["text"]
        if val is None:
            out.append("")
        elif isinstance(val, str):
            out.append(val.strip())
        else:
            return None

    # Exact match — ideal path.
    if len(out) == expected:
        return out

    # Count mismatch: best-effort rather than discarding the whole batch.
    # Truncate if LLM merged items; pad with "" if it dropped trailing items.
    if len(out) > expected:
        return out[:expected]
    # len(out) < expected: pad missing tail with empty strings
    return out + [""] * (expected - len(out))


def _call_llm(user_content: str, system: str | None = None) -> str:
    """Route to whatever backend is configured. `system` overrides the
    default refine system prompt (used by glossary auto-scan)."""
    backend = _pick_backend()
    if backend == "api":
        return _via_anthropic(user_content, system=system)
    if backend == "openai_chat":
        return _via_openai_chat(user_content, system=system)
    return _via_claude_code(user_content, system=system)


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
    raise RuntimeError("no LLM backend available for refine step")


def _via_anthropic(user_content: str, system: str | None = None) -> str:
    from anthropic import Anthropic

    kwargs: dict = {"timeout": LLM_TIMEOUT_SEC}
    if config.ANTHROPIC_AUTH_TOKEN:
        kwargs["auth_token"] = config.ANTHROPIC_AUTH_TOKEN
    elif config.ANTHROPIC_API_KEY:
        kwargs["api_key"] = config.ANTHROPIC_API_KEY
    if config.ANTHROPIC_BASE_URL:
        kwargs["base_url"] = config.ANTHROPIC_BASE_URL

    client = Anthropic(**kwargs)
    message = client.messages.create(
        model=config.SUMMARY_MODEL,
        max_tokens=8192,
        system=system if system is not None else _system_prompt(),
        messages=[{"role": "user", "content": user_content}],
    )
    return "".join(
        b.text for b in message.content if getattr(b, "type", None) == "text"
    )


def _via_openai_chat(user_content: str, system: str | None = None) -> str:
    from openai import OpenAI

    kwargs = {"api_key": config.OPENAI_API_KEY, "timeout": LLM_TIMEOUT_SEC}
    if config.OPENAI_BASE_URL:
        kwargs["base_url"] = config.OPENAI_BASE_URL
    client = OpenAI(**kwargs)
    resp = client.chat.completions.create(
        model=config.SUMMARY_MODEL,
        messages=[
            {"role": "system", "content": system if system is not None else _system_prompt()},
            {"role": "user", "content": user_content},
        ],
        max_tokens=8192,
        temperature=0.0,
    )
    return resp.choices[0].message.content or ""


def _via_claude_code(user_content: str, system: str | None = None) -> str:
    cli = shutil.which(config.CLAUDE_CLI)
    if not cli:
        raise RuntimeError(f"claude CLI not found: {config.CLAUDE_CLI}")
    proc = subprocess.run(
        [cli, "-p", "--output-format", "json",
         "--model", config.CLAUDE_CLI_MODEL,
         "--append-system-prompt", system if system is not None else _system_prompt()],
        input=user_content,
        capture_output=True, text=True, timeout=CLI_TIMEOUT_SEC,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {proc.stderr[:400]}")
    payload = json.loads(proc.stdout)
    if payload.get("is_error"):
        raise RuntimeError(f"claude CLI error: {payload.get('result')}")
    return payload.get("result") or ""
