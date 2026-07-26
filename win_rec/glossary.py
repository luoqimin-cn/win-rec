"""User-maintained glossary of proper nouns, injected into the refine LLM prompt.

Purpose: ASR (Whisper) frequently mis-transcribes names, company names, product
names, and jargon — especially in Chinese-English mixed meetings. Rather than
fight the recognizer with prompts (which triggers hallucinations), we let it be
imperfect and correct via LLM refine, giving the LLM a user-curated glossary
so it knows which spellings are canonical.

Format: ~/AI_Rec_Data/glossary.yaml

    people:
      Sanjay Gupta:
        - Sanjay
        - 三家
    companies:
      Anthropic: []
    terms:
      Kubernetes: [库伯, K8S]

Top-level categories (people/companies/terms/etc.) are free-form and only
help the user organize; the loader flattens them into a single dict of
{canonical_name: [variants]}.
"""
from __future__ import annotations

from pathlib import Path

from . import config


GLOSSARY_PATH = config.DATA_ROOT / "glossary.yaml"


class GlossaryError(RuntimeError):
    """Raised when the glossary file has YAML syntax errors.

    Silently returning {} would be dangerous: refine would run with NO
    glossary and the user would see no signal that their edits were lost.
    """
    pass


def load(strict: bool = False) -> dict[str, list[str]]:
    """Return flat {canonical_name: [variants]}. Empty dict if glossary missing.

    On YAML parse error: prints a loud stderr warning by default; raises
    GlossaryError when strict=True (used by `win-rec glossary` CLI so users
    hear it clearly). refine.py uses strict=False so pipeline still runs
    even if glossary is broken, but with visible warning in the logs.
    """
    if not GLOSSARY_PATH.exists():
        return {}
    try:
        import yaml
        raw = yaml.safe_load(GLOSSARY_PATH.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        msg = (
            f"\n⚠  GLOSSARY YAML ERROR — refine will run WITHOUT glossary.\n"
            f"   File: {GLOSSARY_PATH}\n"
            f"   Error: {e}\n"
            f"   Fix with: win-rec glossary --edit\n"
        )
        if strict:
            raise GlossaryError(msg) from e
        import sys
        print(msg, file=sys.stderr)
        return {}
    if not isinstance(raw, dict):
        return {}

    flat: dict[str, list[str]] = {}
    for _category, entries in raw.items():
        if not isinstance(entries, dict):
            continue
        for name, variants in entries.items():
            if not isinstance(name, str) or not name.strip():
                continue
            if variants is None:
                v_list: list[str] = []
            elif isinstance(variants, str):
                v_list = [variants]
            elif isinstance(variants, list):
                v_list = [str(v) for v in variants if v and isinstance(v, (str, int, float))]
            else:
                v_list = []
            flat[name.strip()] = v_list
    return flat


def as_prompt_fragment() -> str:
    """Render the glossary as a Chinese prompt block for the refine LLM.

    Returns empty string if the glossary is missing or empty, so the caller
    can simply concatenate it in front of the base system prompt.
    """
    g = load()
    if not g:
        return ""
    lines = [
        "【本次用户的专有名词表】以下是本用户会议中会反复出现的正式名称。ASR 转写中如果出现听起来像它们的变体（同音字或近似发音），请在本次转写中修正为下列正式拼写。名单里没有的名字保持 ASR 原样，不要主观猜测：",
        "",
    ]
    for canonical, variants in sorted(g.items()):
        if variants:
            lines.append(f"- 【{canonical}】常见错听：{ '、'.join(variants) }")
        else:
            lines.append(f"- 【{canonical}】")
    lines.append("")
    return "\n".join(lines)


# ─── Auto-scan: extract new proper nouns from a fresh summary ─────────

_SCAN_SYSTEM_PROMPT = """你是一位会议纪要专有名词提取助理。从会议 summary 中识别所有专有名词，并排除已知列表和通用词。

要提取的类型：
- 人名（中英文，如 张三、Alex）
- 公司/机构名（如 阿里巴巴、Anthropic）
- 产品/技术/项目名（如 Kubernetes、Notion、Copilot）
- 客户特定的术语/项目代号

不要提取：
- 已知列表中已有的（canonical 或其变体）
- 代词（我、我们、对方、客户、同事、老板 等）
- 通用职位（CEO、CTO、PM、HR、老师 等）
- 全球通用英文缩写（API、HTTP、PDF、URL、JSON 等）
- 一般名词（会议、项目、系统、平台 等）

严格输出格式：只输出 JSON 数组，无 markdown 代码块无解释文字：
[{"name": "张小明", "type": "person"}, {"name": "阿里云", "type": "company"}]

type 取值：person / company / product / term
"""


def already_known_names() -> set[str]:
    """All canonicals + variants + _suspected_asr_errors entries as a lowercase-normalized set.

    Case-insensitive dedup so "sanjay" and "Sanjay" don't both count as new.
    Whitespace stripped. Returns empty set on parse error (fail open — caller
    treats everything as new, so nothing gets missed, only duplicates added).
    """
    if not GLOSSARY_PATH.exists():
        return set()
    try:
        import yaml
        raw = yaml.safe_load(GLOSSARY_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return set()
    known: set[str] = set()
    if not isinstance(raw, dict):
        return known
    for _cat, entries in raw.items():
        if isinstance(entries, dict):
            for name, variants in entries.items():
                if isinstance(name, str):
                    known.add(name.strip().lower())
                if isinstance(variants, list):
                    for v in variants:
                        if isinstance(v, (str, int, float)):
                            known.add(str(v).strip().lower())
        elif isinstance(entries, list):
            for v in entries:
                if isinstance(v, (str, int, float)):
                    known.add(str(v).strip().lower())
    return known


def scan_summary(summary_text: str, session_id: str) -> list[tuple[str, str]]:
    """Call LLM to extract proper nouns from summary; filter against known.

    Returns list of (name, type). Empty list on any failure (never breaks
    the process pipeline — auto-scan is best-effort supplementary).
    """
    if not summary_text.strip():
        return []
    known = already_known_names()
    known_sample = sorted(known)[:400]

    user_content = (
        "【已知列表 — 请勿重复提取】\n"
        + "\n".join(f"- {n}" for n in known_sample)
        + "\n\n【会议 summary】\n"
        + summary_text[:12000]
    )

    try:
        from . import refine
        raw = refine._call_llm(user_content, system=_SCAN_SYSTEM_PROMPT)
    except Exception as e:
        import sys
        print(f"  [warning] glossary auto-scan LLM call failed: {e}", file=sys.stderr)
        return []

    import re, json
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", raw.strip(), flags=re.M)
    try:
        arr = json.loads(cleaned)
    except json.JSONDecodeError:
        import sys
        print(f"  [warning] glossary auto-scan: LLM output not JSON, skipping", file=sys.stderr)
        return []
    if not isinstance(arr, list):
        return []

    result: list[tuple[str, str]] = []
    seen_in_batch: set[str] = set()
    for item in arr:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        typ = str(item.get("type", "term")).strip()
        if not name:
            continue
        if name.lower() in known:
            continue
        if name.lower() in seen_in_batch:
            continue
        seen_in_batch.add(name.lower())
        result.append((name, typ))
    return result


def append_candidates_to_suspected(
    candidates: list[tuple[str, str]], session_id: str
) -> int:
    """Append (name, type) tuples to the _suspected_asr_errors: bucket.

    Simple text-append at EOF — assumes _suspected_asr_errors: is the last
    top-level YAML key (which is our seed template's convention). Preserves
    all user comments and formatting exactly. Returns count actually written.

    Deduplication is caller's responsibility (scan_summary already dedups
    vs known).

    Concurrency: the entire read → verify-structure → append sequence runs
    under an advisory file lock on the glossary file itself. Two processes
    (e.g. user's manual `process` colliding with a scheduled job) would
    otherwise interleave their writes and corrupt the YAML.
    """
    if not candidates:
        return 0
    if not GLOSSARY_PATH.exists():
        return 0

    import filelock, sys

    lock_path = str(GLOSSARY_PATH) + ".lock"
    lock = filelock.FileLock(lock_path)
    with lock:
        content = GLOSSARY_PATH.read_text(encoding="utf-8")

        try:
            import yaml
            raw = yaml.safe_load(content) or {}
        except Exception:
            print(
                "  [warning] glossary auto-scan skipped: YAML has syntax errors; "
                "run `win-rec glossary` to see them",
                file=sys.stderr,
            )
            return 0
        if not isinstance(raw, dict):
            return 0
        keys = list(raw.keys())
        if not keys or keys[-1] != "_suspected_asr_errors":
            print(
                "  [warning] glossary auto-scan skipped: _suspected_asr_errors "
                "is not the last top-level key",
                file=sys.stderr,
            )
            return 0

        from datetime import date
        today = date.today().isoformat()
        header = f"\n  # 自动补充于 {today} (来自 {session_id})\n"
        lines = [header]
        for name, typ in candidates:
            safe = name.replace('"', '\\"')
            lines.append(f'  - "{safe}"  # {typ}\n')

        with GLOSSARY_PATH.open("a", encoding="utf-8") as fh:
            if not content.endswith("\n"):
                fh.write("\n")
            fh.write("".join(lines))
            fh.flush()
        return len(candidates)


def ensure_seed_file() -> None:
    """Create the glossary file with commented-out examples if it doesn't exist yet.

    Called on first access from CLI so users have a template to edit rather than
    a blank file. Skipped if the file already exists (never overwrites).
    """
    if GLOSSARY_PATH.exists():
        return
    GLOSSARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    GLOSSARY_PATH.write_text(_SEED_CONTENT, encoding="utf-8")


_SEED_CONTENT = """# ~/AI_Rec_Data/glossary.yaml
# ═══════════════════════════════════════════════════════════════════
# 会议专有名词表。refine 步骤会把这个文件注入 LLM 系统提示，让它
# 把 ASR 听错的变体改回正式名称。
#
# 格式：
#   category:
#     Canonical Name:
#       - variant 1
#       - variant 2       # 变体是可选的，空列表 [] 也可以
#
# category（people/companies/products/terms 等）只是给你自己看的分组，
# 加载时会被展平；用什么名字都行。
#
# 修改这个文件后，让**新**会议自动生效——它们的 refine 阶段会读到。
# 让**旧** session 应用新 glossary：win-rec process <session_id>
# （会用 transcript.raw.json 缓存，只重跑 refine + summary，几分钟）
# ═══════════════════════════════════════════════════════════════════

people:
  # 例（删掉换成你自己常遇到的）:
  # Sanjay Gupta:
  #   - Sanjay
  #   - 三家
  #   - 山家

companies:
  # Anthropic: []
  # Microsoft Teams:
  #   - Teams

products:
  # Kubernetes:
  #   - 库伯
  #   - K8S

terms:
  # SLA: []

_suspected_asr_errors:
  # 由 win-rec process 自动追加的候选词，供你决定是否移入上方正式分组
"""
