#!/usr/bin/env python3
"""
中文 transcript → mp3
完全免费链路 (EdgeTTS + ffmpeg)。不依赖任何 API key。

用法:
  python3 tts-cn.py <中文 markdown/txt 文件> [输出 mp3]

  speaker 序列模式 (避免 Claude 重写整篇 → 防 socket 超时, 推荐):
    1. python3 tts-cn.py <文件> --dump-segments       # 段落清单写到 <文件>.segments.txt
    2. (Claude 分块读清单, 判断每段谁说, 写 speaker 序列文件, 每段一行标签)
    3. python3 tts-cn.py <文件> --speakers <序列文件>  # 按序列贴标签并合成

输入格式 (两种皆可):
  1. 带 speaker 标记的多人对话, 必须在标签里带性别:
       [主持人-女] xxxxx
       [嘉宾-男] yyyyy
       [嘉宾甲-男] 多嘉宾的话用甲乙丙丁区分
       [嘉宾乙-女]

     性别识别规则 (按优先级):
       a. 标签里有 "男" 字 → 男声
       b. 标签里有 "女" 字 → 女声
       c. 兜底: 含 "主持" 默认女声, 其他默认男声

     声音池 (多人同性别时自动轮换, 让不同嘉宾听感不同):
       男声: VOICE_MALE_1, VOICE_MALE_2
       女声: VOICE_FEMALE_1, VOICE_FEMALE_2

  2. 无标记的纯文本: 整体用 VOICE_DEFAULT 合成 (默认女声)

  自动剥除: frontmatter (--- ... ---) / markdown 标题 / 引用块 / 分隔线 /
  各种格式的时间戳 (0:31 · 6:55 · 00：44：33 32 分 03 秒 · 等) / 音频标注
  (\\[音乐\\] \\[笑\\] [gasps] [clears throat] 等)。

环境变量 (默认值):
  VOICE_MALE_1    zh-CN-YunyangNeural   (男声, 新闻腔)
  VOICE_MALE_2    zh-CN-YunjianNeural   (男声, 运动解说腔, 活力)
  VOICE_FEMALE_1  zh-CN-XiaoxiaoNeural  (女声, 通用)
  VOICE_FEMALE_2  zh-CN-XiaoyiNeural    (女声, 温暖)
  VOICE_DEFAULT   = VOICE_FEMALE_1      (无 speaker 标记时用)

  TTS_CONCURRENCY 8  (并发数, 太高会被微软限速)
  TTS_RETRIES     3  (单段失败重试次数)
  PAUSE_SAME_MS   200  (同 speaker 段间静音毫秒)
  PAUSE_SWITCH_MS 500  (speaker 切换静音毫秒)

  兼容旧变量名 VOICE_MALE / VOICE_FEMALE (会覆盖 _1 默认)

列出所有中文 voice:  .venv/bin/edge-tts --list-voices | grep zh-CN
"""

import argparse
import asyncio
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from pydub import AudioSegment

from tts_backends import SynthesisRequest, TTSBackend, build_backend
from tts_cache import FragmentCache

RETRIES = int(os.getenv("TTS_RETRIES", "3"))
PAUSE_SAME_MS = int(os.getenv("PAUSE_SAME_MS", "200"))
PAUSE_SWITCH_MS = int(os.getenv("PAUSE_SWITCH_MS", "500"))

DEFAULT_OUTPUT_DIR = Path.home() / "Desktop" / "音频"

# --------------------------------------------------------------------------
# 文本清理: 时间戳 + 音频标注
# --------------------------------------------------------------------------

# 行首时间戳, 沉浸式翻译/常见 transcript 几种格式都覆盖。
# 1:23 · / 1:23:45 · / 1:23 - / 00：44：33 · / 32 分 03 秒 ·
# 还包括沉浸式翻译奇葩格式 "2: 2:11 ·"
_LEADING_TIMESTAMP = re.compile(
    r"^\s*"
    r"(?:\*+\s*)?"  # 偶尔行首有 markdown 加粗符号
    r"(?:"
    r"\d+\s*[:：]\s*\d+(?:\s*[:：]\s*\d+){0,2}"  # 各种"数字 冒号 数字"链
    r"|"
    r"\d+\s*小时(?:\s*\d+\s*分)?(?:\s*\d+\s*秒)?"  # 中文 "X 小时 Y 分 Z 秒"
    r"|"
    r"\d+\s*[点分](?:\s*\d+\s*[分秒])?"  # 中文 "X 点 Y 分" / "X 分 Y 秒"
    r"|"
    r"\d+\s*[:：]\s*\d+\s*分钟?"  # 偶见 "1:30 分钟"
    r")"
    r"\s*[·\-—]\s+"
)

# 音频/动作标注, 反斜杠转义可有可无
_ANNOTATION = re.compile(
    r"\\?\["
    r"(?:音乐|笑声?|掌声|叹气|清嗓子?|喘气|咳嗽|鼓掌|"
    r"哼(?:唱|了一声|了一下)?|嘘声?|呼气|吸气|沉默|停顿|"
    r"music|laughter|applause|gasps?|clears throat|cough|snorts?)"
    r"\\?\]",
    re.IGNORECASE,
)

# markdown 图片 ![alt](url) 整个删; 链接 [文字](url) 只保留文字
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")

# 裸 URL(带 http(s):// 协议头): 一行里出现就把整行当链接行删掉
_URL = re.compile(r"https?://", re.IGNORECASE)


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def strip_markdown(text: str) -> str:
    """剥掉 frontmatter / markdown 装饰 / 时间戳 / 音频标注 / 裸 URL 行。"""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]

    lines = []
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            lines.append("")
            continue
        if s.startswith("#"):
            continue
        if s.startswith(">"):
            continue
        if re.fullmatch(r"<!--.*-->", s):
            continue
        if re.match(r"^[-*_]{3,}$", s):
            continue

        # 1. 删 markdown 图片 / 链接(链接保留文字)
        line = _MD_IMAGE.sub("", line)
        line = _MD_LINK.sub(r"\1", line)
        # 2. 裸 URL 行(节目链接/赞助商等元信息)整行丢 —— 逐字稿正文不会有带协议头的网址
        if _URL.search(line):
            continue
        # 3. 删转义 markdown 加粗符号 \*文字\* → 文字
        line = line.replace("\\*", "")
        # 4. 删行首时间戳前缀
        line = _LEADING_TIMESTAMP.sub("", line, count=1)
        # 5. 删行内的音频/动作标注
        line = _ANNOTATION.sub("", line)
        # 清完之后整行变空就丢
        if not line.strip():
            continue
        lines.append(line)
    return "\n".join(lines)


def split_into_paragraphs(text: str) -> list[str]:
    """strip_markdown 后的文本按空行切成段落清单 (speaker 序列模式用)。"""
    paras: list[str] = []
    cur: list[str] = []
    for line in text.split("\n"):
        if line.strip():
            cur.append(line.strip())
        elif cur:
            paras.append(" ".join(cur))
            cur = []
    if cur:
        paras.append(" ".join(cur))
    return paras


# 章节目录的时间戳含秒(HH:MM:SS); 正文口语时间多是 H:MM(不含秒)。
# 用"含秒"区分 —— 否则正文里聊作息("4:30 起床, 5:00 锻炼")会被误判成章节目录。
_CHAPTER_TS = re.compile(r"\d{1,2}\s*[:：]\s*\d{2}\s*[:：]\s*\d{2}")
# 已知的节目元信息标题。只匹配明确标签,不再用长度猜测内容是否为噪音。
_NOISE_LABEL = re.compile(
    r"^(?:剧集链接|节目链接|播客详情|节目详情|助力我的使命)(?:\s*[:：].*)?$"
)


def drop_noise_paragraphs(paras: list[str]) -> list[str]:
    """删掉整段是节目元信息的噪音段:章节目录和明确的元信息标签。

    裸 URL 行已在 strip_markdown 删掉; 这里专治 split 之后才看得出的整段噪音。
    判定必须保守:短句可能是真实对话,不能仅凭长度删除。
    """
    out: list[str] = []
    for p in paras:
        s = p.strip()
        if not s:
            continue
        # 1. 章节目录: 以"章节"开头, 或时间戳 HH:MM 出现 >= 4 次
        if s.startswith("章节") or len(_CHAPTER_TS.findall(s)) >= 4:
            continue
        # 2. 只删明确列出的元信息标签,保留“没错”等短对话
        if _NOISE_LABEL.fullmatch(s):
            continue
        out.append(p)
    return out


def apply_speaker_sequence(
    cleaned: str,
    sequence: list[str],
    preview_paragraphs: int | None = None,
) -> str:
    """Validate speakers against the full text, then apply an optional preview."""
    paras = drop_noise_paragraphs(split_into_paragraphs(cleaned))
    if len(sequence) != len(paras):
        raise ValueError(
            f"speaker 序列行数 ({len(sequence)}) ≠ 段落数 ({len(paras)}),对不齐。\n"
            "先跑  --dump-segments  确认段落数,序列文件必须每段正好一行。"
        )

    if preview_paragraphs is not None:
        paras = paras[:preview_paragraphs]
        sequence = sequence[:preview_paragraphs]
    return "\n\n".join(
        f"[{label}] {paragraph}"
        for label, paragraph in zip(sequence, paras)
    )


def apply_preview_limit(cleaned: str, preview_paragraphs: int | None) -> str:
    """Slice plain cleaned text by paragraph only when preview mode is active."""
    if preview_paragraphs is None:
        return cleaned
    paras = drop_noise_paragraphs(split_into_paragraphs(cleaned))
    return "\n\n".join(paras[:preview_paragraphs])


# --------------------------------------------------------------------------
# Speaker → voice 映射
# --------------------------------------------------------------------------


def voice_slot_for_label(label: str, assigned: dict[str, str]) -> str:
    """
    根据标签决定声音。同一个标签字符串始终映射到同一个声音
    (即使同性别有多嘉宾, 也能保持一致)。

    判断顺序:
      1. 已经分配过这个标签 → 复用
      2. 标签含 "男" → 男声池里轮换取一个
      3. 标签含 "女" → 女声池里轮换取一个
      4. 标签含 "主持" → 女声 (默认)
      5. 兜底 → 男声
    """
    if label in assigned:
        return assigned[label]

    if "男" in label:
        used = sum(1 for value in assigned.values() if value.startswith("male_"))
        voice_slot = ("male_1", "male_2")[used % 2]
    elif "女" in label:
        used = sum(1 for value in assigned.values() if value.startswith("female_"))
        voice_slot = ("female_1", "female_2")[used % 2]
    elif "主持" in label:
        used = sum(1 for value in assigned.values() if value.startswith("female_"))
        voice_slot = ("female_1", "female_2")[used % 2]
    else:
        used = sum(1 for value in assigned.values() if value.startswith("male_"))
        voice_slot = ("male_1", "male_2")[used % 2]

    assigned[label] = voice_slot
    return voice_slot


def parse_segments(text: str) -> list[tuple[str, str]]:
    """返回 [(voice, text), ...] 按出现顺序。"""
    pattern = re.compile(r"^\[([^\]]+)\]\s*(.*)$")
    segments: list[tuple[str, str]] = []
    current_voice_slot: str | None = None
    current_buf: list[str] = []
    assigned: dict[str, str] = {}

    def flush() -> None:
        if current_voice_slot and current_buf:
            joined = " ".join(s.strip() for s in current_buf if s.strip())
            if joined:
                segments.append((current_voice_slot, joined))

    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        m = pattern.match(line)
        if m:
            flush()
            label, rest = m.group(1), m.group(2).strip()
            current_voice_slot = voice_slot_for_label(label, assigned)
            current_buf = [rest] if rest else []
        else:
            current_buf.append(line)
    flush()

    if not segments:
        cleaned = " ".join(s.strip() for s in text.split("\n") if s.strip())
        if cleaned:
            segments = [("female_1", cleaned)]
    return segments


def chunk_long(text: str, max_chars: int = 2500) -> list[str]:
    """超长段按句号/问号/感叹号切片。"""
    if len(text) <= max_chars:
        return [text]
    pieces = re.split(r"(?<=[。!?\!\?])", text)
    out: list[str] = []
    cur = ""
    for p in pieces:
        if not p.strip():
            continue
        if len(cur) + len(p) <= max_chars:
            cur += p
        else:
            if cur:
                out.append(cur)
            cur = p
    if cur:
        out.append(cur)
    return out


# --------------------------------------------------------------------------
# 合成: 并行 + 重试
# --------------------------------------------------------------------------


async def synth_all(
    segments: list[tuple[str, str]],
    tmp: Path,
    backend: TTSBackend,
    style: str,
    cache: FragmentCache,
) -> tuple[list[tuple[Path, str]], dict]:
    """
    并行合成所有片段, 失败自动重试。
    返回 ([(file_path, voice), ...] 按原序, stats)
    """
    tmp.mkdir(parents=True, exist_ok=True)

    # 把每段拆成多个 part, 展平成 task list
    tasks: list[tuple[int, int, str, str, Path]] = []
    for i, (voice_slot, text) in enumerate(segments):
        for j, part in enumerate(chunk_long(text, max_chars=backend.max_chars)):
            tasks.append(
                (
                    i,
                    j,
                    voice_slot,
                    part,
                    tmp / f"seg-{i:04d}-{j:03d}{backend.file_suffix}",
                )
            )

    total = len(tasks)
    log(
        f"合成 {total} 个片段(来自 {len(segments)} 段) | "
        f"后端 {backend.name} | 模型 {backend.model} | 风格 {style} | "
        f"并发 {backend.concurrency} | 重试 {RETRIES}"
    )

    sem = asyncio.Semaphore(backend.concurrency)
    stats = {
        "total": total,
        "ok": 0,
        "failed": 0,
        "retried": 0,
        "cached": 0,
        "provider_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "audio_tokens": 0,
    }
    progress_lock = asyncio.Lock()
    key_locks: dict[str, asyncio.Lock] = {}

    async def worker(
        seg_i: int, part_j: int, voice_slot: str, text: str, out: Path
    ):
        request = SynthesisRequest(
            text=text, voice_slot=voice_slot, style=style
        )
        cache_key = cache.key_for(request)
        key_lock = key_locks.setdefault(cache_key, asyncio.Lock())

        async with key_lock:
            cached_path = cache.get(request, audio_is_decodable)
            if cached_path is not None:
                async with progress_lock:
                    stats["ok"] += 1
                    stats["cached"] += 1
                return (cached_path, voice_slot)

            async with sem:
                last_err: Exception | None = None
                for attempt in range(RETRIES):
                    try:
                        async with progress_lock:
                            stats["provider_calls"] += 1
                        usage = await backend.synthesize(request, out)
                        if not audio_is_decodable(out):
                            raise RuntimeError("生成的音频片段无法解码")
                        cached_path = cache.store(request, out)
                        async with progress_lock:
                            stats["ok"] += 1
                            stats["input_tokens"] += usage.input_tokens
                            stats["output_tokens"] += usage.output_tokens
                            stats["audio_tokens"] += usage.audio_tokens
                            if attempt > 0:
                                stats["retried"] += 1
                            if stats["ok"] % 20 == 0 or stats["ok"] == total:
                                log(f"  进度 {stats['ok']}/{total}")
                        return (cached_path, voice_slot)
                    except Exception as e:
                        last_err = e
                        if attempt < RETRIES - 1:
                            # 退避: 0.5s, 1.0s, 1.5s, ...
                            await asyncio.sleep(0.5 * (attempt + 1))
                async with progress_lock:
                    stats["failed"] += 1
                log(
                    f"  ✗ 片段 {seg_i}-{part_j} 失败 "
                    f"({RETRIES} 次): {last_err}"
                )
                return None

    results = await asyncio.gather(
        *[worker(i, j, slot, text, path) for i, j, slot, text, path in tasks]
    )
    successful = [r for r in results if r is not None]
    return successful, stats


def ensure_all_synthesized(stats: dict[str, int]) -> None:
    """拒绝将缺少任何片段的合成结果交给拼接步骤。"""
    failed = stats.get("failed", 0)
    if failed:
        raise RuntimeError(f"{failed} 个音频片段合成失败,未生成最终 MP3")

    ok = stats.get("ok", 0)
    total = stats.get("total", 0)
    if ok != total:
        raise RuntimeError(f"音频片段不完整:成功 {ok}/{total},未生成最终 MP3")


def audio_is_decodable(path: Path) -> bool:
    """Return whether a fragment is non-empty and can be decoded by pydub."""
    try:
        return path.is_file() and path.stat().st_size > 0 and len(
            AudioSegment.from_file(path)
        ) > 0
    except Exception:
        return False


# --------------------------------------------------------------------------
# 拼接: 按 speaker 切换决定停顿长短
# --------------------------------------------------------------------------


def merge(files_with_voices: list[tuple[Path, str]], out: Path) -> None:
    log(f"拼接 {len(files_with_voices)} 个片段 → {out.name}")
    short_silence = AudioSegment.silent(duration=PAUSE_SAME_MS)
    long_silence = AudioSegment.silent(duration=PAUSE_SWITCH_MS)

    combined = AudioSegment.empty()
    prev_voice: str | None = None
    for f, voice in files_with_voices:
        try:
            seg = AudioSegment.from_file(f)
        except Exception as e:
            raise RuntimeError(f"音频片段无法解码:{f.name}: {e}") from e
        if prev_voice is None:
            combined += seg
        elif prev_voice == voice:
            combined += short_silence + seg
        else:
            combined += long_silence + seg
        prev_voice = voice

    combined.export(out, format="mp3")
    fix_mp3_header(out)


def publish_mp3_atomically(temp_path: Path, output_path: Path) -> None:
    """只把已验证的非空临时 MP3 原子替换为最终文件。"""
    if not temp_path.is_file():
        raise RuntimeError(f"待发布 MP3 不存在:{temp_path}")
    if temp_path.stat().st_size == 0:
        raise RuntimeError(f"待发布 MP3 为空:{temp_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.replace(output_path)


def fix_mp3_header(mp3: Path) -> None:
    # pydub 拼接的 mp3 缺 Xing/LAME header,微信/QuickTime/Finder 算时长会错
    tmp = mp3.with_suffix(".tmp.mp3")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(mp3),
             "-c:a", "libmp3lame", "-b:a", "64k", "-ac", "1",
             str(tmp)],
            check=True, capture_output=True,
        )
    except FileNotFoundError:
        log("  ⚠ 没找到 ffmpeg,跳过 header 修复 (mp3 还能播,但时长可能显示错)")
        return
    except subprocess.CalledProcessError as e:
        log(f"  ⚠ ffmpeg 失败,跳过 header 修复: {e.stderr.decode()[:200]}")
        return
    tmp.replace(mp3)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


async def run(
    input_path: Path,
    output_path: Path,
    speakers_file: Path | None = None,
    backend: TTSBackend | None = None,
    style: str = "conversation",
    preview_paragraphs: int | None = None,
) -> None:
    start = time.time()
    backend = backend or build_backend("edge")
    raw = input_path.read_text(encoding="utf-8")
    cleaned = strip_markdown(raw)

    if speakers_file is not None:
        seq: list[str] = []
        for ln in speakers_file.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            seq.append(ln.strip("[]").strip())
        try:
            cleaned = apply_speaker_sequence(
                cleaned,
                seq,
                preview_paragraphs=preview_paragraphs,
            )
        except ValueError as exc:
            sys.exit(str(exc))
        selected_count = len(split_into_paragraphs(cleaned))
        log(f"speaker 序列模式: {selected_count} 段已贴标签")
    else:
        cleaned = apply_preview_limit(cleaned, preview_paragraphs)

    segments = parse_segments(cleaned)
    if not segments:
        sys.exit("输入为空,没合成出来")

    log(f"输入字符数: {len(cleaned)}, 段数: {len(segments)}")

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        cache = FragmentCache(
            output_path.parent / ".tts-cache",
            backend.fingerprint(),
            backend.file_suffix,
        )
        files_with_voices, stats = await synth_all(
            segments,
            tmp,
            backend=backend,
            style=style,
            cache=cache,
        )
        ensure_all_synthesized(stats)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=f".{output_path.stem}.",
            suffix=".tmp.mp3",
            dir=output_path.parent,
            delete=False,
        ) as prepared_file:
            prepared_path = Path(prepared_file.name)
        try:
            merge(files_with_voices, prepared_path)
            publish_mp3_atomically(prepared_path, output_path)
        finally:
            prepared_path.unlink(missing_ok=True)

    elapsed = time.time() - start
    size_mb = output_path.stat().st_size / 1024 / 1024

    # ffprobe 拿真实时长 (秒)
    duration_s: float | None = None
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(output_path)],
            check=True, capture_output=True, text=True,
        )
        duration_s = float(proc.stdout.strip())
    except Exception:
        pass

    log(f"✓ 输出: {output_path} ({size_mb:.1f} MB)")
    summary_parts = [
        f"耗时 {elapsed:.0f}s",
        f"段 {len(segments)}",
        f"片段 {stats['total']}",
        f"失败 {stats['failed']}",
        f"重试成功 {stats['retried']}",
    ]
    optional_totals = (
        ("provider_calls", "服务调用"),
        ("cached", "缓存命中"),
        ("input_tokens", "输入 tokens"),
        ("output_tokens", "输出 tokens"),
        ("audio_tokens", "音频 tokens"),
    )
    for key, label in optional_totals:
        value = stats.get(key, 0)
        if value:
            summary_parts.append(f"{label} {value}")
    if duration_s is not None:
        mm, ss = divmod(int(duration_s), 60)
        summary_parts.insert(0, f"音频时长 {mm}m{ss:02d}s")
        if elapsed > 0:
            summary_parts.append(f"实时倍率 {duration_s / elapsed:.1f}x")
    log("  " + " | ".join(summary_parts))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="中文 transcript → mp3")
    parser.add_argument("input", help="中文 markdown 或 txt 文件路径")
    parser.add_argument("output", nargs="?", help=f"输出 mp3 路径 (默认 {DEFAULT_OUTPUT_DIR}/<同名>.mp3)")
    parser.add_argument(
        "--dump-segments", action="store_true",
        help="段落清单写到 <输入>.segments.txt,不合成。speaker 序列模式第 1 步",
    )
    parser.add_argument(
        "--speakers", metavar="FILE",
        help="speaker 序列文件:每行一个标签([主持人-女]/[嘉宾-男]),与段落一一对应",
    )
    parser.add_argument(
        "--tts",
        choices=("edge", "qwen3-api"),
        default="edge",
        help="语音后端: edge 免费稳定; qwen3-api 高质量且消耗百炼额度",
    )
    parser.add_argument(
        "--style",
        choices=("conversation", "documentary"),
        default="conversation",
        help="表达预设: conversation 访谈; documentary 知识讲解",
    )
    parser.add_argument(
        "--preview-paragraphs",
        type=int,
        help="只合成清理后的前 N 段,用于短试听；不指定则合成全文",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.preview_paragraphs is not None and args.preview_paragraphs <= 0:
        parser.error("--preview-paragraphs 必须大于 0")

    in_path = Path(args.input).expanduser().resolve()
    if not in_path.exists():
        sys.exit(f"找不到输入文件: {in_path}")

    if args.dump_segments:
        cleaned = strip_markdown(in_path.read_text(encoding="utf-8"))
        paras = drop_noise_paragraphs(split_into_paragraphs(cleaned))
        # 段落清单落地成文件: 首句截短到 28 字(够 Claude 认段即可)。
        # 不再把整张清单打到 stdout —— 段数多时那是 100KB+ 的 tool-result,
        # Claude 读它会触发跨境 API ECONNRESET。Claude 改去分块 Read 这个文件。
        seg_file = in_path.with_suffix(".segments.txt")
        lines = [f"{i}\t{p[:28]}" for i, p in enumerate(paras)]
        seg_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"段落清单已写入: {seg_file}")
        print(f"共 {len(paras)} 段。speaker 序列文件必须正好 {len(paras)} 行。")
        return

    speakers_file: Path | None = None
    if args.speakers:
        speakers_file = Path(args.speakers).expanduser().resolve()
        if not speakers_file.exists():
            sys.exit(f"找不到 speaker 序列文件: {speakers_file}")

    if args.output:
        out_path = Path(args.output).expanduser().resolve()
    else:
        DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = DEFAULT_OUTPUT_DIR / (in_path.stem + ".mp3")

    api_key = os.getenv("DASHSCOPE_API_KEY") if args.tts == "qwen3-api" else None
    try:
        backend = build_backend(args.tts, api_key=api_key)
    except ValueError as exc:
        parser.error(str(exc))

    asyncio.run(
        run(
            in_path,
            out_path,
            speakers_file,
            backend=backend,
            style=args.style,
            preview_paragraphs=args.preview_paragraphs,
        )
    )


if __name__ == "__main__":
    main()
