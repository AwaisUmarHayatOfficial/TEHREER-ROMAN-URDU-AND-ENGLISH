import html
import json
import math
import os
import re
import subprocess
import sys
import time
import warnings

# Suppress warnings
warnings.filterwarnings("ignore")

# Print safely on consoles that are not UTF-8 (Windows, minimal Linux locales)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_TAB_ALIGNMENT, WD_TAB_LEADER
from docx.oxml import OxmlElement, parse_xml
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml.ns import nsdecls, qn
from docx.shared import Pt, RGBColor, Twips
from google import genai
from google.genai import types
import requests
from youtube_transcript_api import YouTubeTranscriptApi

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# =====================================================================
#  DOCUMENT DESIGN SETTINGS  (change the look & feel here)
# =====================================================================
# Header / tagline are AUTO-DETECTED (from the YouTube channel + Gemini).
# To force a value, write it here; otherwise leave it empty.
BRAND_OVERRIDE = ""  # e.g. "Maxam Trading"
SUBJECT_OVERRIDE = ""  # e.g. "FOREX"
FOOTER_LABEL = "English"  # Footer: "English | Page 2 of 19"
FONT_NAME = "Roboto Serif"  # Install it from Google Fonts (or use "Georgia"/"Cambria")
SHOW_TIMESTAMPS = True  # Set False to remove the [04:00] markers from the document
CHUNK_MINUTES = 5  # smaller chunk = Gemini skips less
MIN_COVERAGE = 0.80  # the output must contain at least this fraction of the words (a translation can be a little shorter or longer)
AUTO_BOLD = True  # auto-bold numbers, $ amounts, % and CAPS terms (USD, BTC, SL...)
ADD_CHUNK_HEADINGS = False  # Set True to add a "Section (00:00 - 14:00)" heading for every chunk
GEMINI_TIMEOUT_SECONDS = 180  # one Gemini request must finish within this time (no endless hangs)

NAVY = "1F3A5F"
GOLD = "B8862B"
GREEN = "2E7D4F"
RED = "9C2A1B"  # very important lines (italic + red)
TEXT = "0B0B0B"
GREY = "5F6B7A"
LINE = "C9D1DC"

# callout type -> (left border color, background fill)
CALLOUT_STYLES = {
    "note": (GOLD, "FFF6E0"),
    "question": (NAVY, "EEF2F7"),
    "definition": (GREEN, "EAF5EE"),
}

# Highlighter colors
CALLOUT_HILITE = {
    "question": "C7DDF5",
    "definition": "BFE6CC",
    "note": "FFE29A",
}
IMPORTANT_SHADE = "FFF0A6"  # ==very important lines== highlight
BADGE_TEXT = "FFFFFF"  # QUESTION / ANSWER / NOTE badge text color

# A4, margins (twips: 1440 = 1 inch)
PAGE_W, PAGE_H = 11906, 16838
MARGIN_LR, MARGIN_TOP, MARGIN_BOTTOM = 1134, 1300, 1200
CONTENT_W = PAGE_W - 2 * MARGIN_LR  # 9638

VIDEO_ID = None  # set by create_ms_word_doc() (for clickable timestamps)
BULLET_ABS_ID = 90
NUMBER_ABS_ID = 91

AUTO_BOLD_RE = re.compile(
    r"(\$\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?%|\b[A-Z][A-Z0-9]{1,}\b)"
)
TIMESTAMP_RE = re.compile(r"^\[\d{2}:\d{2}(?::\d{2})?\]$")
INLINE_RE = re.compile(
    r"(\*\*\*.+?\*\*\*|\*\*.+?\*\*|==.+?==|\*(?!\s)[^*]+?(?<!\s)\*|\[\d{2}:\d{2}(?::\d{2})?\])"
)


def extract_video_id(url):
    """Extract the Video ID from a YouTube URL"""
    pattern = r"(?:v=|\/|be\/|embed\/|shorts\/|watch\?v=)([a-zA-Z0-9_-]{11})"
    match = re.search(pattern, url)
    return match.group(1) if match else url.strip()


def get_video_info(video_id):
    """Fetch the YouTube video Title and Channel name"""
    title, channel = None, None
    try:
        cmd = [
            "yt-dlp",
            "--skip-download",
            "--dump-single-json",
            f"https://www.youtube.com/watch?v={video_id}",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if res.returncode == 0 and res.stdout:
            info = json.loads(res.stdout)
            title = info.get("title")
            channel = info.get("channel") or info.get("uploader")
    except Exception:
        pass

    if not title or not channel:
        try:
            url = f"https://www.youtube.com/watch?v={video_id}"
            resp = requests.get(
                url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10
            )
            if resp.status_code == 200:
                if not title:
                    match = re.search(r"<title>(.*?)</title>", resp.text)
                    if match:
                        t = (
                            match.group(1)
                            .replace(" - YouTube", "")
                            .replace("- YouTube", "")
                            .strip()
                        )
                        title = html.unescape(t) or title
                if not channel:
                    m2 = re.search(r'"ownerChannelName":"([^"]+)"', resp.text)
                    if m2:
                        channel = _json_unescape(m2.group(1))
        except Exception:
            pass

    return {
        "title": title or f"notes_{video_id}",
        "channel": (channel or "").strip(),
    }


# ---------------------------------------------------------------------
#  Robustness helpers (safe file names, XML-safe text, Gemini retries)
# ---------------------------------------------------------------------
_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff\ud800-\udfff]")


def clean_xml_text(s):
    """Removes characters Word/XML cannot store (control characters, lone surrogates)."""
    return _XML_BAD.sub("", s) if isinstance(s, str) else s


def _json_unescape(s):
    try:
        return json.loads('"' + s + '"')
    except Exception:
        return s


_FATAL_MARKERS = ("API_KEY_INVALID", "API key not valid", "API key expired", "PERMISSION_DENIED", "UNAUTHENTICATED")
_RETRY_MARKERS = ("429", "resource_exhausted", "503", "unavailable", "500", "internal", "overloaded", "deadline", "timed out", "timeout")


def _generate_with_retries(client, models_to_try, prompt, config, rounds=4):
    """Asks Gemini (trying each model in turn). Backs off when rate limited, stops at once when the API key is wrong."""
    last_error = None
    for attempt in range(rounds):
        retryable = False
        for model_name in models_to_try:
            try:
                response = client.models.generate_content(
                    model=model_name, contents=prompt, config=config
                )
                text = getattr(response, "text", None)
                if text and text.strip():
                    return clean_xml_text(text)
                last_error = RuntimeError(f"Empty response from {model_name}")
            except Exception as e:
                last_error = e
                msg = str(e)
                if any(k in msg for k in _FATAL_MARKERS):
                    raise  # a wrong or expired key cannot be fixed by retrying
                if any(k in msg.lower() for k in _RETRY_MARKERS):
                    retryable = True
        if attempt == rounds - 1:
            break
        wait = min(65, 15 * 2 ** attempt) if retryable else 5
        print(f"    [!] Gemini is busy or rate limited, waiting {wait}s before trying again...", flush=True)
        time.sleep(wait)
    raise last_error or RuntimeError("No response from Gemini")


def _make_client(api_key):
    """Gemini client with a request timeout (so one stuck request cannot block the whole run)."""
    try:
        from google.genai import types as _types
        return genai.Client(api_key=api_key, http_options=_types.HttpOptions(timeout=GEMINI_TIMEOUT_SECONDS * 1000))
    except Exception:
        return genai.Client(api_key=api_key)


def _missing_note(blocks):
    """Visible placeholder for minutes that could not be converted (instead of a silent gap)."""
    a = blocks[0].split()[0].strip("[]") if blocks else "?"
    b = blocks[-1].split()[0].strip("[]") if blocks else "?"
    return f"""> **NOTE:** The part of the video from {a} to {b} could not be converted (Gemini error or rate limit). Convert the video again to include it."""


def _pick_track(tracks):
    """Chooses a subtitle language from yt-dlp's list: en/hi/ur (also en-US, hi-IN ...), otherwise any."""
    for lang in TRANSCRIPT_LANGS:
        if lang in tracks:
            return lang
        for code in tracks:
            if code.startswith(lang + "-"):
                return code
    for code in tracks:
        if code != "live_chat":
            return code
    return None


def sanitize_filename(name):
    """Removes illegal characters and keeps the file name short enough for any file system (255 bytes)."""
    clean = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "", name or "")
    clean = re.sub(r"\s+", "_", clean).strip("_. ")
    while len(clean.encode("utf-8")) > 120:  # long Hindi / Urdu / emoji titles use 2-4 bytes per character
        clean = clean[:-1]
    return clean.strip("_. ") or "youtube_notes"


def format_time(seconds):
    """Convert seconds to [HH:MM:00] or [MM:00]"""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"[{h:02d}:{m:02d}:00]"
    return f"[{m:02d}:00]"


# Transcript language priority: English first, then Hindi / Urdu, then anything available
TRANSCRIPT_LANGS = ["en", "en-US", "en-GB", "hi", "ur"]


def get_transcript_grouped_by_minute(video_id):
    """Group subtitles into 1-minute blocks"""
    data = []

    try:
        ytt = YouTubeTranscriptApi()
        try:
            fetched = ytt.fetch(video_id, languages=TRANSCRIPT_LANGS)
        except Exception:
            fetched = ytt.fetch(video_id)

        if hasattr(fetched, "to_raw_data"):
            data = fetched.to_raw_data()
        else:
            data = list(fetched)
    except Exception:
        pass

    if not data:
        try:
            cmd = [
                "yt-dlp",
                "--skip-download",
                "--write-auto-subs",
                "--write-subs",
                "--sub-lang",
                "en,hi,ur",
                "--sub-format",
                "json3",
                "--dump-single-json",
                f"https://www.youtube.com/watch?v={video_id}",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if res.returncode == 0 and res.stdout:
                info = json.loads(res.stdout)
                tracks = {}  # manual subtitles first, then automatic captions
                for source in (info.get("subtitles") or {}, info.get("automatic_captions") or {}):
                    for code, sub_list in source.items():
                        tracks.setdefault(code, sub_list)
                code = _pick_track(tracks)
                json3_sub = next((s for s in tracks.get(code, []) if s.get("ext") == "json3"), None)
                if json3_sub:
                    resp = requests.get(json3_sub["url"], timeout=30)
                    resp.raise_for_status()
                    for event in resp.json().get("events", []):
                        start_ms = event.get("tStartMs", event.get("startTimestampMs", 0))
                        segs = event.get("segs", [])
                        line = "".join([s.get("utf8", "") for s in segs]).strip()
                        if line:
                            data.append({"start": start_ms / 1000, "text": line})
        except Exception:
            pass

    if not data:
        return []

    grouped = {}
    for item in data:
        if "text" not in item or "start" not in item:
            continue
        start_sec = item["start"]
        text = item["text"].strip()
        if not text:
            continue

        minute_mark = int(start_sec // 60) * 60
        if minute_mark not in grouped:
            grouped[minute_mark] = []
        grouped[minute_mark].append(text)

    minute_blocks = []
    for sec in sorted(grouped.keys()):
        t_str = format_time(sec)
        block_text = " ".join(grouped[sec])
        minute_blocks.append(f"{t_str} {block_text}")

    return minute_blocks


# =====================================================================
#  GEMINI: Transcript -> English (structured markdown)
# =====================================================================
def convert_chunk_to_english(
    client, chunk_text, models_to_try, video_title="", chunk_num=1, total_chunks=1, strict=False
):
    """Convert a transcript chunk into complete, clean ENGLISH course notes in the structure the Word design needs"""
    strict_note = (
        "WARNING: In the previous attempt many words / sentences were MISSING. Now EVERY sentence and EVERY idea "
        "from the input MUST appear in the output. The output must never be noticeably shorter than the input."
        if strict
        else ""
    )
    prompt = f"""
    Your goal is to convert the minute-by-minute YouTube tutorial transcript segment below into COMPLETE, NATURAL, PROFESSIONAL ENGLISH and organize it as a PROFESSIONAL COURSE-NOTES / STUDY-GUIDE.

    Video Title: {video_title}
    This is chunk {chunk_num} of {total_chunks}.
    {strict_note}

    CONTENT RULES (MANDATORY):
    1. OUTPUT LANGUAGE = ENGLISH ONLY: The transcript may be in English, Hindi, Urdu, Roman Urdu or a mix. Whatever the input language, write the output in clear, natural, grammatically correct English. Do NOT use Devanagari, Urdu script or Roman Urdu/Hindi words in the output (technical terms, names of tools, brands and coins stay as they are).
    2. ZERO SKIPPING (MOST IMPORTANT RULE): Translate / write out EVERY sentence, example, number, step, warning and repeated point the speaker says. Do NOT summarize, shorten, merge or delete anything. Do NOT add ideas of your own.
       - You may remove only pure filler noise (like "umm", "uh", "so yeah" repeated meaninglessly) and fix obvious auto-caption mistakes and broken punctuation, but keep every piece of meaning.
       - If the transcript is already English, keep the speaker's content intact: just fix punctuation, grammar, capitalization and obvious speech-to-text errors, and organize it.
       - If a sentence is incomplete or odd, still convert what is there.
       - Read the full text under each timestamp before writing and make sure every sentence is represented.
    3. TERMINOLOGY: Use standard English technical vocabulary for the subject (programming, trading, electronics, design, etc.). Keep code, commands, file names, URLs, menu paths and numbers exactly as spoken/shown.
    4. TIMESTAMPS: Keep each minute's timestamp marker (e.g. `[00:00]`, `[01:00]`, `[04:39:00]`) exactly as given, at the very start of the paragraph or bullet where that minute's content begins.

    STRUCTURE RULES (OUTPUT MARKDOWN ONLY):
    5. HEADINGS (plain text only, no ** or * inside headings):
       - `# Title` : a very big new topic.
       - `## Title` : a main part of a topic (most common).
       - `### Title` : a small sub-topic.
       Do NOT repeat the video title as a heading.
    6. LISTS:
       - Put each separate point / sentence of the instructor in its own `- ` bullet (short bullets instead of long paragraphs).
       - Steps, sequences, counts, methods: use `1. `, `2. ` numbered lists.
       - Nested point: indent by 2 spaces with `  - `.

    EMPHASIS RULES (VERY IMPORTANT):
    7. IMPORTANT WORDS => **bold**:
       In every bullet / paragraph, bold the important words: names (tools, platforms, software, brokers, coins), terms, concepts, numbers, amounts, commands, rules, warnings, key phrases. Each bullet should have at least 1-2 bold words. Do NOT bold whole sentences, only the important words.
    8. VERY IMPORTANT LINES => ==whole line==:
       Lines that are extremely important (golden rule, main takeaway, special advice from the instructor, warning, conclusion) must be written inside double equals like ==this==. They appear in italic and a different color in the document. Only 1-3 such lines per section, not more. Everything else stays normal.

    CALLOUT BOXES (MANDATORY FOR EVERY QUESTION / DEFINITION / NOTE):
    9. ALL lines of a callout start with `>` and there is one empty line between two different callouts.
       - QUESTION-ANSWER: whenever the instructor or a student asks a question (Q., "do you know...?", poll question, a question the instructor asks himself):
         > **QUESTION [MM:SS]:** <the question>
         > **ANSWER:** <short part of the answer>
         (If the answer is long, continue it in normal bullets below the callout.)
       - DEFINITION: whenever a term / concept is explained ("X means...", "X is called...", "define X"):
         > **DEFINITION:** <Name of the term>
         > <Details of the definition>
       - NOTE: whenever there is "remember this", "note this", a warning, tip, scam alert, example, practical exercise, or special advice from the instructor:
         > **NOTE:** <text>
         (Inside a note you may use `> - point` bullets if needed.)
    10. TABLES: for comparisons, lists of values, categories (sizes, sessions, units, differences) or shortcuts/commands, make a markdown table:
       | Column 1 | Column 2 | Column 3 |
       | --- | --- | --- |
       | ... | ... | ... |

    OUTPUT: Do NOT write any intro/outro sentence (like "Here is the output"). Do NOT wrap in ``` code fences. Give only the converted markdown.

    Transcript Chunk:
    {chunk_text}
    """

    config = types.GenerateContentConfig(
        automatic_function_calling=types.AutomaticFunctionCallingConfig(
            disable=True
        ),
        temperature=0.2,  # low temperature = stay faithful to the source
    )

    return strip_code_fences(
        _generate_with_retries(client, models_to_try, prompt, config)
    )


# ---------------------------------------------------------------------
#  Verification: did every word make it to the output? (word count check)
# ---------------------------------------------------------------------
_TS_RE = re.compile(r"\[\d{2}:\d{2}(?::\d{2})?\]")


def _count_tokens(text):
    return sum(
        1
        for tok in text.split()
        if re.search(r"[A-Za-z0-9\u0900-\u097F\u0600-\u06FF]", tok)
    )


def count_source_words(chunk_text):
    return _count_tokens(_TS_RE.sub(" ", chunk_text))


def count_output_words(md):
    t = _TS_RE.sub(" ", md)
    t = re.sub(r"[>|]", " ", t)
    t = re.sub(r"(?m)^\s*(?:[-*\u2022]|\d+[.)])\s+", "", t)
    t = re.sub(r"\b(?:QUESTION|ANSWER|DEFINITION|NOTE)\b\s*:?", " ", t)
    t = re.sub(r"[*=#_`]", " ", t)
    return _count_tokens(t)


def convert_with_verification(
    client, blocks, models_to_try, video_title, chunk_num, total_chunks
):
    """Convert a chunk; if too few words come back, retry, then split into smaller parts"""
    chunk_text = "\n".join(blocks)
    src = count_source_words(chunk_text)
    tries = 3 if len(blocks) == 1 else 1
    best, best_cov = None, -1.0

    for attempt in range(tries):
        try:
            md = convert_chunk_to_english(
                client, chunk_text, models_to_try, video_title,
                chunk_num, total_chunks, strict=attempt > 0,
            )
        except Exception:
            if best is not None:  # a later attempt failed: keep the best text we already have
                break
            raise
        cov = count_output_words(md) / max(src, 1)
        if cov > best_cov:
            best, best_cov = md, cov
        if src < 12 or cov >= MIN_COVERAGE:
            return best

    if len(blocks) > 1:  # chunk was too long: split in half and convert again
        mid = len(blocks) // 2
        print(
            f"    [!] Coverage {best_cov:.0%} - splitting chunk into smaller parts and converting again...",
            flush=True,
        )
        parts, errors = [], []
        for half in (blocks[:mid], blocks[mid:]):
            try:
                parts.append(
                    convert_with_verification(
                        client, half, models_to_try, video_title, chunk_num, total_chunks
                    )
                )
            except Exception as e:  # keep what worked, mark what did not
                errors.append(e)
                parts.append(_missing_note(half))
        if len(errors) == 2:
            raise errors[0]
        return "\n\n".join(parts)

    print(f"    [!] Coverage for this minute was {best_cov:.0%} (best attempt kept).", flush=True)
    return best


# ---------------------------------------------------------------------
#  Clean up Gemini output into callouts
#  (even if Gemini forgets '>' , Question / Definition / Note still become boxes)
#  Old Roman Urdu labels (Sawal / Jawab / Tareef) are still understood too.
# ---------------------------------------------------------------------
_PFX = r"^(?:[-*\u2022]\s+|\d+[.)]\s+)?(?:\*\*)?\s*"
RE_QUESTION = re.compile(
    _PFX + r"(?:Q\d*\s*[.:)]|(?:Question|Sawal)(?:\s*\[[^\]]*\])?\s*:)\s*(?:\*\*)?\s*(.+)$",
    re.I,
)
RE_ANSWER = re.compile(
    _PFX + r"(?:Answer|Ans|Jawab)\s*:\s*(?:\*\*)?\s*(.+)$", re.I
)
RE_DEFINITION = re.compile(
    _PFX
    + r"(?:Definition|Tareef(?:\s*/\s*Definition)?|Define\s+Technically)\s*:\s*(?:\*\*)?\s*(.*)$",
    re.I,
)
RE_NOTE = re.compile(
    _PFX + r"(?:Note|Important|Remember|Warning|Tip|Yaad\s+rakhein)\s*:\s*(?:\*\*)?\s*(.+)$",
    re.I,
)


def _balance_bold(text):
    text = text.strip()
    if text.count("**") % 2 == 1:
        text = text.replace("**", "", 1) if text.endswith("**") is False else text[:-2]
    return text.strip()


def _is_plain_line(t):
    """A normal sentence (not heading, list, table, callout or label)"""
    t = t.strip()
    if not t or t.startswith((">", "|", "#", "- ", "* ", "\u2022")):
        return False
    if re.match(r"^\d+[.)]\s", t):
        return False
    for rx in (RE_QUESTION, RE_ANSWER, RE_DEFINITION, RE_NOTE):
        if rx.match(t):
            return False
    return True


def normalize_markdown(md):
    lines = md.replace("\r", "").split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        i += 1
        if not s or s.startswith((">", "|", "#")):
            out.append(line)
            continue

        m = RE_ANSWER.match(s)
        if m:
            if out and out[-1].strip() == "" and len(out) > 1 and out[-2].lstrip().startswith(">"):
                out.pop()
            out.append(f"> **ANSWER:** {_balance_bold(m.group(1))}")
            continue

        for rx, label in (
            (RE_QUESTION, "QUESTION"),
            (RE_DEFINITION, "DEFINITION"),
            (RE_NOTE, "NOTE"),
        ):
            m = rx.match(s)
            if m:
                if out and out[-1].strip() != "":
                    out.append("")
                out.append(f"> **{label}:** {_balance_bold(m.group(1))}")
                # normal sentence after a definition (its detail) goes in the same box
                if label == "DEFINITION":
                    j = i
                    while j < len(lines) and not lines[j].strip():
                        j += 1
                    if j < len(lines) and _is_plain_line(lines[j]):
                        out.append(f"> {lines[j].strip()}")
                        i = j + 1
                out.append("")
                break
        else:
            out.append(line)

    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


# ---------------------------------------------------------------------
#  Header / Tagline AUTO-DETECT
# ---------------------------------------------------------------------
def build_meta(video_title, channel="", subject="", short_title=""):
    brand = (BRAND_OVERRIDE or channel or "").strip()
    brand = re.sub(r"\s+", " ", brand)[:40].strip() or "Course Notes"
    subject = (SUBJECT_OVERRIDE or subject or "").strip().upper()[:40]
    mid = f"{subject} COURSE NOTES" if subject else "VIDEO NOTES"
    right = (short_title or video_title or "").strip()
    if len(right) > 60:
        right = right[:57].rstrip() + "..."
    return {
        "brand": brand,
        "tagline": f"{brand.upper()}  |  {mid}  |  ENGLISH",
        "header_right": right,
    }


def detect_doc_meta(client, models_to_try, video_title, channel, sample_text):
    """Ask Gemini to detect the video's subject / brand / short title (in English)"""
    prompt = f"""
    I need to create a header for the notes of a YouTube tutorial video. Info:
    Video Title: {video_title}
    Channel Name: {channel}
    Beginning of transcript: {sample_text[:1500]}

    Give only one JSON object (no extra text, no ```), in this format:
    {{"brand": "...", "subject": "...", "short_title": "..."}}

    - brand: Academy / channel name (keep the Channel Name unless the transcript has a clearly better brand).
    - subject: topic of the video in 1-3 English words (e.g. Forex, Python, Islamic History, Digital Marketing, Electronics).
    - short_title: a shorter ENGLISH version of the video title, under 60 characters.
    """
    cfg = types.GenerateContentConfig(
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)
    )
    for model_name in models_to_try[:3]:
        try:
            r = client.models.generate_content(model=model_name, contents=prompt, config=cfg)
            m = re.search(r"\{.*\}", r.text or "", re.S)
            if m:
                d = json.loads(m.group(0))
                return build_meta(
                    video_title,
                    channel=str(d.get("brand") or channel),
                    subject=str(d.get("subject") or ""),
                    short_title=str(d.get("short_title") or ""),
                )
        except Exception:
            continue
    return build_meta(video_title, channel=channel)


def translate_title_to_english(client, models_to_try, video_title):
    """If the video title is not English (Hindi/Urdu/Roman Urdu), get a clean English title for the document"""
    if not video_title or re.fullmatch(r"[\x00-\x7F]+", video_title) and not re.search(
        r"\b(kaise|kese|sikhein|seekhein|seekhen|ka|ki|ke|mein|hai|urdu|hindi)\b", video_title, re.I
    ):
        return video_title
    prompt = (
        "Translate this YouTube video title into a clean, natural English title. "
        "Give ONLY the title, nothing else, no quotes.\n\nTitle: " + video_title
    )
    cfg = types.GenerateContentConfig(
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)
    )
    for model_name in models_to_try[:3]:
        try:
            r = client.models.generate_content(model=model_name, contents=prompt, config=cfg)
            t = (r.text or "").strip().strip('"').strip("'").splitlines()[0].strip()
            if t:
                return t
        except Exception:
            continue
    return video_title


_BAD_MODEL_WORDS = (
    "tts", "image", "embedding", "live", "audio", "aqa", "imagen", "veo",
    "learnlm", "robotics", "computer-use", "gemma", "vision", "native-audio",
)
_PREFERRED_MODELS = (
    "gemini-2.5-flash", "gemini-2.5-pro", "gemini-flash-latest",
    "gemini-2.0-flash", "gemini-2.5-flash-lite",
)


def rank_models(names):
    """Best text models first (removing tts / image / embedding models)"""
    good = [
        n for n in names
        if n.startswith("gemini") and not any(b in n for b in _BAD_MODEL_WORDS)
    ]
    first = [m for m in _PREFERRED_MODELS if m in good]
    rest = sorted(
        (n for n in good if n not in first),
        key=lambda x: ("flash" not in x.lower(), "lite" in x.lower(), x),
    )
    return first + rest


def strip_code_fences(text):
    """Remove ```markdown ... ``` fences if Gemini added them"""
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*\n", "", text)
    text = re.sub(r"\n```\s*$", "", text)
    return text.strip()


# =====================================================================
#  WORD DOCUMENT: low-level helpers
# =====================================================================
PPR_AFTER_PBDR = (
    "w:shd", "w:tabs", "w:suppressAutoHyphens", "w:kinsoku", "w:wordWrap",
    "w:overflowPunct", "w:topLinePunct", "w:autoSpaceDE", "w:autoSpaceDN",
    "w:bidi", "w:adjustRightInd", "w:snapToGrid", "w:spacing", "w:ind",
    "w:contextualSpacing", "w:mirrorIndents", "w:suppressOverlap", "w:jc",
    "w:textDirection", "w:textAlignment", "w:textboxTightWrap",
    "w:outlineLvl", "w:divId", "w:cnfStyle", "w:rPr", "w:sectPr",
    "w:pPrChange",
)

RPR_AFTER_SPACING = (
    "w:w", "w:kern", "w:position", "w:sz", "w:szCs", "w:highlight", "w:u",
    "w:effect", "w:bdr", "w:shd", "w:fitText", "w:vertAlign", "w:rtl",
    "w:cs", "w:em", "w:lang", "w:eastAsianLayout", "w:specVanish", "w:oMath",
)


def set_bottom_border(ppr, color, sz, space=4):
    """Add a bottom line (pBdr) to a paragraph / style - in schema order"""
    for old in ppr.findall(qn("w:pBdr")):
        ppr.remove(old)
    pbdr = parse_xml(
        f'<w:pBdr {nsdecls("w")}><w:bottom w:val="single" w:sz="{sz}" '
        f'w:space="{space}" w:color="{color}"/></w:pBdr>'
    )
    ppr.insert_element_before(pbdr, *PPR_AFTER_PBDR)


def set_char_spacing(run, twentieths):
    rpr = run._r.get_or_add_rPr()
    sp = OxmlElement("w:spacing")
    sp.set(qn("w:val"), str(twentieths))
    rpr.insert_element_before(sp, *RPR_AFTER_SPACING)


def set_style_font(style, size, bold=False, italic=False, color=TEXT):
    """Fully reset a style's font to FONT_NAME, size, color"""
    old = style.element.find(qn("w:rPr"))
    if old is not None:
        style.element.remove(old)
    style.font.name = FONT_NAME
    rfonts = style.element.rPr.find(qn("w:rFonts"))
    rfonts.set(qn("w:cs"), FONT_NAME)
    rfonts.set(qn("w:eastAsia"), FONT_NAME)
    style.font.size = Pt(size)
    style.font.bold = bold
    style.font.italic = italic
    style.font.color.rgb = RGBColor.from_string(color)


RPR_AFTER_SHD = (
    "w:fitText", "w:vertAlign", "w:rtl", "w:cs", "w:em", "w:lang",
    "w:eastAsianLayout", "w:specVanish", "w:oMath",
)


def set_run_shading(run, fill):
    """Highlighter-like background color behind text"""
    rpr = run._r.get_or_add_rPr()
    for old in rpr.findall(qn("w:shd")):
        rpr.remove(old)
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    rpr.insert_element_before(shd, *RPR_AFTER_SHD)


def make_run(p, text, size=None, bold=False, italic=False, color=None, shade=None):
    text = clean_xml_text(text)
    r = p.add_run(text)
    if size:
        r.font.size = Pt(size)
    if bold:
        r.bold = True
    if italic:
        r.italic = True
    if color:
        r.font.color.rgb = RGBColor.from_string(color)
    if shade:
        set_run_shading(r, shade)
    return r


def add_field(p, instr, size, color):
    """Insert Word fields like PAGE / NUMPAGES"""

    def _run():
        r = p.add_run()
        r.font.size = Pt(size)
        r.font.color.rgb = RGBColor.from_string(color)
        return r

    for kind in ("begin", "instr", "separate", "text", "end"):
        r = _run()
        if kind == "instr":
            it = OxmlElement("w:instrText")
            it.set(qn("xml:space"), "preserve")
            it.text = f" {instr} "
            r._r.append(it)
        elif kind == "text":
            r.text = "1"
        else:
            fc = OxmlElement("w:fldChar")
            fc.set(qn("w:fldCharType"), kind)
            r._r.append(fc)


# ---------------------------------------------------------------------
#  Styles, numbering, header / footer
# ---------------------------------------------------------------------
def configure_styles(doc):
    # replace theme fonts in docDefaults with FONT_NAME
    defaults = doc.styles.element.find(qn("w:docDefaults"))
    if defaults is not None:
        for rf in defaults.iter(qn("w:rFonts")):
            for a in list(rf.attrib):
                del rf.attrib[a]
            for a in ("ascii", "hAnsi", "cs", "eastAsia"):
                rf.set(qn(f"w:{a}"), FONT_NAME)

    normal = doc.styles["Normal"]
    set_style_font(normal, 10, color=TEXT)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(5)
    normal.paragraph_format.line_spacing = 1.35

    specs = {
        "Title": dict(size=26, color=NAVY, before=0, after=14, border=(GOLD, 18, 8)),
        "Heading 1": dict(size=18, color=NAVY, before=26, after=10, border=(GOLD, 12, 4)),
        "Heading 2": dict(size=13.5, color=NAVY, before=18, after=6, border=(LINE, 6, 4)),
        "Heading 3": dict(size=11.5, color=GOLD, before=12, after=4, border=None),
    }
    for name, s in specs.items():
        st = doc.styles[name]
        set_style_font(st, s["size"], bold=True, color=s["color"])
        pf = st.paragraph_format
        pf.space_before = Pt(s["before"])
        pf.space_after = Pt(s["after"])
        pf.line_spacing = 1.15
        pf.keep_with_next = True
        pf.keep_together = True
        ppr = st.element.get_or_add_pPr()
        if s["border"]:
            set_bottom_border(ppr, *s["border"])
        else:
            for old in ppr.findall(qn("w:pBdr")):
                ppr.remove(old)


def setup_numbering(doc):
    """Define gold bullets and navy numbered lists"""
    numbering = doc.part.numbering_part.element

    bullet = parse_xml(
        f'<w:abstractNum {nsdecls("w")} w:abstractNumId="{BULLET_ABS_ID}">'
        '<w:multiLevelType w:val="hybridMultilevel"/>'
        '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/>'
        '<w:lvlText w:val="&#8226;"/><w:lvlJc w:val="left"/>'
        '<w:pPr><w:ind w:left="360" w:hanging="220"/></w:pPr>'
        f'<w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:cs="Arial"/><w:b/><w:color w:val="{GOLD}"/></w:rPr></w:lvl>'
        '<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="bullet"/>'
        '<w:lvlText w:val="&#8211;"/><w:lvlJc w:val="left"/>'
        '<w:pPr><w:ind w:left="720" w:hanging="220"/></w:pPr>'
        f'<w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:cs="Arial"/><w:color w:val="{GOLD}"/></w:rPr></w:lvl>'
        "</w:abstractNum>"
    )
    number = parse_xml(
        f'<w:abstractNum {nsdecls("w")} w:abstractNumId="{NUMBER_ABS_ID}">'
        '<w:multiLevelType w:val="hybridMultilevel"/>'
        '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/>'
        '<w:lvlText w:val="%1."/><w:lvlJc w:val="left"/>'
        '<w:pPr><w:ind w:left="400" w:hanging="320"/></w:pPr>'
        f'<w:rPr><w:b/><w:color w:val="{NAVY}"/></w:rPr></w:lvl>'
        '<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/>'
        '<w:lvlText w:val="%2)"/><w:lvlJc w:val="left"/>'
        '<w:pPr><w:ind w:left="800" w:hanging="320"/></w:pPr>'
        f'<w:rPr><w:b/><w:color w:val="{NAVY}"/></w:rPr></w:lvl>'
        "</w:abstractNum>"
    )
    first_num = numbering.find(qn("w:num"))
    for el in (bullet, number):
        if first_num is not None:
            first_num.addprevious(el)
        else:
            numbering.append(el)


def new_num(doc, abstract_id, restart=False):
    """Create a new numId (restart=True so a numbered list starts from 1)"""
    numbering = doc.part.numbering_part.element
    ids = [int(n.get(qn("w:numId"))) for n in numbering.findall(qn("w:num"))]
    nid = max(ids + [0]) + 1
    override = (
        '<w:lvlOverride w:ilvl="0"><w:startOverride w:val="1"/></w:lvlOverride>'
        if restart
        else ""
    )
    numbering.append(
        parse_xml(
            f'<w:num {nsdecls("w")} w:numId="{nid}">'
            f'<w:abstractNumId w:val="{abstract_id}"/>{override}</w:num>'
        )
    )
    return nid


def apply_list(p, num_id, level=0):
    numpr = p._p.get_or_add_pPr().get_or_add_numPr()
    numpr.get_or_add_ilvl().val = level
    numpr.get_or_add_numId().val = num_id
    p.paragraph_format.space_after = Pt(3)
    p.paragraph_format.line_spacing = 1.3


def setup_page(doc, doc_title, meta):
    sec = doc.sections[0]
    sec.page_width = Twips(PAGE_W)
    sec.page_height = Twips(PAGE_H)
    sec.left_margin = sec.right_margin = Twips(MARGIN_LR)
    sec.top_margin = Twips(MARGIN_TOP)
    sec.bottom_margin = Twips(MARGIN_BOTTOM)
    sec.header_distance = Twips(600)
    sec.footer_distance = Twips(600)
    sec.different_first_page_header_footer = True  # no header/footer on the first page

    hp = sec.header.paragraphs[0]
    hp.style = doc.styles["Normal"]
    hp.paragraph_format.space_after = Pt(0)
    hp.paragraph_format.tab_stops.add_tab_stop(Twips(CONTENT_W), WD_TAB_ALIGNMENT.RIGHT)
    make_run(hp, meta["brand"], size=8.5, bold=True, color=GOLD)
    make_run(hp, "\t" + meta["header_right"], size=8.5, color=GREY)
    set_bottom_border(hp._p.get_or_add_pPr(), LINE, 4)

    fp = sec.footer.paragraphs[0]
    fp.style = doc.styles["Normal"]
    fp.alignment = 1  # center
    make_run(fp, f"{FOOTER_LABEL}  |  Page ", size=8.5, color=GREY)
    add_field(fp, "PAGE", 8.5, GREY)
    make_run(fp, " of ", size=8.5, color=GREY)
    add_field(fp, "NUMPAGES", 8.5, GREY)


def add_title_block(doc, doc_title, meta):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(8)
    r = make_run(p, meta["tagline"], size=8.5, bold=True, color=GOLD)
    set_char_spacing(r, 20)
    t = doc.add_paragraph(style="Title")
    t.add_run(doc_title)
    link = video_url()
    if link:
        lp = doc.add_paragraph()
        lp.paragraph_format.space_after = Pt(10)
        make_run(lp, "YouTube Video:  ", size=9, bold=True, color=GOLD)
        add_hyperlink(lp, link, link, size=9, color=NAVY, underline=True)


# ---------------------------------------------------------------------
#  Inline text: **bold**, *italic*, ==highlight==, [timestamps]
# ---------------------------------------------------------------------
def video_url(seconds=None):
    if not VIDEO_ID:
        return None
    url = f"https://www.youtube.com/watch?v={VIDEO_ID}"
    if seconds is not None:
        url += f"&t={int(seconds)}s"
    return url


def timestamp_to_seconds(ts):
    """'[04:00]' or '[01:04:00]' -> seconds"""
    nums = [int(x) for x in ts.strip("[]").split(":")]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return nums[0] * 60 + nums[1]


def add_hyperlink(p, url, text, size, bold=False, color=None, underline=False, shade=None):
    """Create a clickable external link run"""
    r_id = p.part.relate_to(url, RT.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), r_id)
    link.set(qn("w:history"), "1")
    run = make_run(p, text, size=size, bold=bold, color=color, shade=shade)
    run.underline = underline
    link.append(run._r)
    p._p.append(link)


def add_timestamp(p, ts, size, color):
    """Make the timestamp clickable: clicking plays the video from that second"""
    url = video_url(timestamp_to_seconds(ts))
    if url:
        add_hyperlink(p, url, ts, size=size, bold=True, color=color)
    else:
        make_run(p, ts, size=size, bold=True, color=color)
    make_run(p, " ", size=size)


def add_inline(p, text, size=None, bold=False, italic=False, color=None, shade=None):
    for part in INLINE_RE.split(text):
        if not part:
            continue
        if part.startswith("***") and part.endswith("***") and len(part) > 6:
            add_inline(p, part[3:-3], size, True, True, color, shade)
        elif part.startswith("**") and part.endswith("**") and len(part) > 4:
            add_inline(p, part[2:-2], size, True, italic, color, shade)
        elif part.startswith("==") and part.endswith("==") and len(part) > 4:
            add_inline(p, part[2:-2], size, bold, True, RED, IMPORTANT_SHADE)
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            add_inline(p, part[1:-1], size, bold, True, color, shade)
        elif TIMESTAMP_RE.match(part):
            if SHOW_TIMESTAMPS:
                add_timestamp(p, part, 8.5, GOLD)
        else:
            if AUTO_BOLD and not bold:
                for sub in AUTO_BOLD_RE.split(part):
                    if sub:
                        make_run(
                            p, sub, size,
                            bool(AUTO_BOLD_RE.fullmatch(sub)), italic, color, shade,
                        )
            else:
                make_run(p, part, size, bold, italic, color, shade)


def clean_heading_text(text):
    text = re.sub(r"[*`]+", "", text)
    text = re.sub(r"==", "", text)
    return text.strip().rstrip("#").strip()


# ---------------------------------------------------------------------
#  Callout boxes
# ---------------------------------------------------------------------
def detect_callout_kind(first_line):
    up = first_line.upper()
    if "DEFINITION" in up or "TAREEF" in up:
        return "definition"
    if "QUESTION" in up or "ANSWER" in up or "SAWAL" in up or "JAWAB" in up or up.startswith("Q"):
        return "question"
    return "note"


def set_simple_table_props(tbl, borders_xml):
    tblpr = tbl._tbl.tblPr
    for ch in list(tblpr):
        tblpr.remove(ch)
    tblpr.append(parse_xml(f'<w:tblW {nsdecls("w")} w:w="{CONTENT_W}" w:type="dxa"/>'))
    tblpr.append(parse_xml(borders_xml))
    tblpr.append(parse_xml(f'<w:tblLayout {nsdecls("w")} w:type="fixed"/>'))


NO_BORDERS = (
    f'<w:tblBorders {nsdecls("w")}>'
    + "".join(
        f'<w:{s} w:val="nil"/>'
        for s in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    + "</w:tblBorders>"
)

GRID_BORDERS = (
    f'<w:tblBorders {nsdecls("w")}>'
    + "".join(
        f'<w:{s} w:val="single" w:sz="4" w:space="0" w:color="{LINE}"/>'
        for s in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    + "</w:tblBorders>"
)


def add_spacer(doc, height_pt=6):
    sp = doc.add_paragraph()
    pf = sp.paragraph_format
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)
    pf.line_spacing = Pt(height_pt)


_NEW_BOX_RE = re.compile(
    r"^\*\*\s*(QUESTION|SAWAL|Q\b|DEFINITION|TAREEF|NOTE)", re.I
)


def split_callout_block(lines):
    """If one '>' block has several labels, make separate boxes (ANSWER stays with the previous QUESTION)"""
    groups, cur = [], []
    for ln in lines:
        if cur and _NEW_BOX_RE.match(ln):
            groups.append(cur)
            cur = []
        cur.append(ln)
    if cur:
        groups.append(cur)
    return groups


def add_callout(doc, lines, bullet_num_id):
    kind = detect_callout_kind(lines[0])
    border, fill = CALLOUT_STYLES[kind]

    tbl = doc.add_table(rows=1, cols=1)
    set_simple_table_props(tbl, NO_BORDERS)
    tbl.columns[0].width = Twips(CONTENT_W)
    cell = tbl.cell(0, 0)
    cell.width = Twips(CONTENT_W)

    tcpr = cell._tc.get_or_add_tcPr()
    tcpr.append(
        parse_xml(
            f'<w:tcBorders {nsdecls("w")}>'
            '<w:top w:val="nil"/>'
            f'<w:left w:val="single" w:sz="24" w:space="0" w:color="{border}"/>'
            '<w:bottom w:val="nil"/><w:right w:val="nil"/></w:tcBorders>'
        )
    )
    tcpr.append(parse_xml(f'<w:shd {nsdecls("w")} w:val="clear" w:color="auto" w:fill="{fill}"/>'))
    tcpr.append(
        parse_xml(
            f'<w:tcMar {nsdecls("w")}><w:top w:w="140" w:type="dxa"/>'
            '<w:left w:w="240" w:type="dxa"/><w:bottom w:w="100" w:type="dxa"/>'
            '<w:right w:w="200" w:type="dxa"/></w:tcMar>'
        )
    )

    hl = CALLOUT_HILITE[kind]
    note_shade = hl if kind == "note" else None

    for idx, line in enumerate(lines):
        p = cell.paragraphs[0] if idx == 0 else cell.add_paragraph()
        p.paragraph_format.space_after = Pt(3)
        p.paragraph_format.line_spacing = 1.3

        mb = re.match(r"^[-*•]\s+(.*)$", line)
        if mb:
            apply_list(p, bullet_num_id, 0)
            add_inline(p, mb.group(1), size=9.5, shade=note_shade)
            continue

        # Label (QUESTION:, ANSWER:, DEFINITION:, NOTE:) = colored badge (white text)
        ml = re.match(r"^\*\*([^*]{1,80}?:)\*\*\s*(.*)$", line)
        if not ml:
            m2 = re.match(r"^\*\*([^*:]{1,80}?)\*\*\s*:\s*(.*)$", line)
            if m2:
                ml = m2
                label, rest = m2.group(1).strip() + ":", m2.group(2)
        else:
            label, rest = ml.group(1), ml.group(2)

        if ml:
            if not SHOW_TIMESTAMPS:
                label = re.sub(r"\s*\[\d{2}:\d{2}(?::\d{2})?\]", "", label)
            segs = [x for x in re.split(r"(\[\d{2}:\d{2}(?::\d{2})?\])", label) if x]
            for k, seg in enumerate(segs):
                shown = ("\u00a0" if k == 0 else "") + seg + ("\u00a0" if k == len(segs) - 1 else "")
                if TIMESTAMP_RE.match(seg):
                    url = video_url(timestamp_to_seconds(seg))
                    if url:
                        add_hyperlink(p, url, shown, size=9.5, bold=True, color=BADGE_TEXT, shade=border)
                        continue
                make_run(p, shown, size=9.5, bold=True, color=BADGE_TEXT, shade=border)
            make_run(p, " ", size=9.5)
            lab = label.upper()
            if kind == "question" and ("QUESTION" in lab or "SAWAL" in lab or lab.startswith("Q")):
                add_inline(p, rest, size=9.5, bold=True, color=NAVY, shade=hl)  # question highlight
            elif kind == "definition" and idx == 0:
                add_inline(p, rest, size=9.5, bold=True, color=GREEN, shade=hl)  # term highlight
            else:
                add_inline(p, rest, size=9.5, shade=note_shade)
        else:
            add_inline(p, line, size=9.5, shade=note_shade)

    cell.paragraphs[-1].paragraph_format.space_after = Pt(0)
    add_spacer(doc)


# ---------------------------------------------------------------------
#  Data tables (navy header, zebra rows)
# ---------------------------------------------------------------------
def parse_table_block(block_lines):
    rows = []
    for l in block_lines:
        s = l.strip()
        if "-" in s and re.match(r"^\|?[\s:\-|]+\|?$", s):
            continue  # separator row
        cells = [c.strip() for c in s.strip("|").split("|")]
        rows.append(cells)
    if not rows:
        return []
    ncols = max(len(r) for r in rows)
    return [r + [""] * (ncols - len(r)) for r in rows]


def plain_len(text):
    return len(re.sub(r"[*=]+|<br\s*/?>", "", text))


def add_data_table(doc, rows):
    ncols = len(rows[0])
    weights = []
    for c in range(ncols):
        longest = max(plain_len(r[c]) for r in rows)
        weights.append(min(max(longest, 8), 40))
    total = sum(weights)
    widths = [int(CONTENT_W * w / total) for w in weights]
    widths[-1] += CONTENT_W - sum(widths)

    tbl = doc.add_table(rows=len(rows), cols=ncols)
    set_simple_table_props(tbl, GRID_BORDERS)
    for i, w in enumerate(widths):
        tbl.columns[i].width = Twips(w)

    for ri, row in enumerate(rows):
        tr = tbl.rows[ri]._tr
        trpr = tr.get_or_add_trPr()
        trpr.append(OxmlElement("w:cantSplit"))
        if ri == 0:
            trpr.append(OxmlElement("w:tblHeader"))  # repeat header on page break
        for ci, text in enumerate(row):
            cell = tbl.cell(ri, ci)
            cell.width = Twips(widths[ci])
            fill = NAVY if ri == 0 else ("F6F8FB" if ri % 2 == 0 else "FFFFFF")
            tcpr = cell._tc.get_or_add_tcPr()
            tcpr.append(parse_xml(f'<w:shd {nsdecls("w")} w:val="clear" w:color="auto" w:fill="{fill}"/>'))
            tcpr.append(
                parse_xml(
                    f'<w:tcMar {nsdecls("w")}><w:top w:w="90" w:type="dxa"/>'
                    '<w:left w:w="130" w:type="dxa"/><w:bottom w:w="90" w:type="dxa"/>'
                    '<w:right w:w="130" w:type="dxa"/></w:tcMar>'
                )
            )
            parts = re.split(r"<br\s*/?>", text) if text else [""]
            for pi, part in enumerate(parts):
                p = cell.paragraphs[0] if pi == 0 else cell.add_paragraph()
                p.paragraph_format.space_after = Pt(0)
                p.paragraph_format.line_spacing = 1.2
                if ri == 0:
                    add_inline(p, part.strip(), size=9, bold=True, color="FFFFFF")
                else:
                    add_inline(p, part.strip(), size=9.5)
    add_spacer(doc)


# ---------------------------------------------------------------------
#  Outline - at the END of the document
# ---------------------------------------------------------------------
def add_bookmark(p, name, bm_id):
    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), str(bm_id))
    start.set(qn("w:name"), name)
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), str(bm_id))
    p.runs[0]._r.addprevious(start)
    p._p.append(end)


def add_internal_link(p, anchor, text, size, bold=False, color=None):
    link = OxmlElement("w:hyperlink")
    link.set(qn("w:anchor"), anchor)
    link.set(qn("w:history"), "1")
    run = make_run(p, text, size=size, bold=bold, color=color)
    link.append(run._r)
    p._p.append(link)


def _find_timestamp_after(lines, start):
    for ln in lines[start:]:
        if re.match(r"^#{1,6}\s", ln.strip()):
            break
        m = _TS_RE.search(ln)
        if m:
            return m.group(0)
    return None


def _find_timestamp_before(lines, end):
    for ln in reversed(lines[:end]):
        found = list(_TS_RE.finditer(ln))
        if found:
            return found[-1].group(0)
    return None


def add_outline(doc, headings):
    """Outline of all headings (at the end): click an entry = jump to that section, click a timestamp = video plays from there"""
    if len(headings) < 2:
        return
    top = min(h["level"] for h in headings)

    h = doc.add_paragraph(style="Heading 1")
    h.paragraph_format.page_break_before = True
    h.add_run("Document Outline")

    intro = doc.add_paragraph()
    make_run(
        intro,
        "All topics of the file at a glance. Click any entry to jump to that section, "
        "and click a timestamp to play the video from that point.",
        size=9, italic=True, color=GREY,
    )

    for hd in headings:
        lvl = min(hd["level"] - top + 1, 3)
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.left_indent = Twips({1: 0, 2: 360, 3: 720}[lvl])
        pf.space_before = Pt(8 if lvl == 1 else 0)
        pf.space_after = Pt(3)
        pf.line_spacing = 1.25
        pf.keep_together = True
        pf.tab_stops.add_tab_stop(Twips(CONTENT_W), WD_TAB_ALIGNMENT.RIGHT, WD_TAB_LEADER.DOTS)

        if lvl == 1:
            add_internal_link(p, hd["bm"], hd["text"], 10.5, bold=True, color=NAVY)
        elif lvl == 2:
            add_internal_link(p, hd["bm"], hd["text"], 10, color=TEXT)
        else:
            add_internal_link(p, hd["bm"], hd["text"], 9.5, color=GREY)

        if SHOW_TIMESTAMPS and hd.get("ts"):
            make_run(p, "\t", size=8.5)
            label = hd["ts"].strip("[]")
            url = video_url(timestamp_to_seconds(hd["ts"]))
            if url:
                add_hyperlink(p, url, label, size=8.5, bold=True, color=GOLD)
            else:
                make_run(p, label, size=8.5, bold=True, color=GOLD)


# =====================================================================
#  Markdown -> Word
# =====================================================================
def render_markdown(doc, markdown_text):
    lines = markdown_text.replace("\r", "").split("\n")
    bullet_num_id = new_num(doc, BULLET_ABS_ID)
    cur_num = None  # numId of the running numbered list
    headings = []  # for the outline
    i = 0

    while i < len(lines):
        raw = lines[i]
        s = raw.strip()

        if not s:  # an empty line does not break a numbered list
            i += 1
            continue

        # Callout (consecutive '>' lines = one box)
        if s.startswith(">"):
            block = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                block.append(lines[i].strip().lstrip(">").strip())
                i += 1
            block = [b for b in block if b]
            for grp in split_callout_block(block):
                add_callout(doc, grp, bullet_num_id)
            cur_num = None
            continue

        # Table
        if s.startswith("|"):
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            rows = parse_table_block(block)
            if rows:
                add_data_table(doc, rows)
            cur_num = None
            continue

        # Headings
        mh = re.match(r"^(#{1,6})\s+(.*)$", s)
        if mh:
            level = min(len(mh.group(1)), 3)
            htext = clean_heading_text(mh.group(2))
            p = doc.add_paragraph(style=f"Heading {level}")
            p.add_run(htext)
            bm = f"_Toc{len(headings) + 1:04d}"
            add_bookmark(p, bm, 1000 + len(headings))
            headings.append({
                "level": len(mh.group(1)),
                "text": htext,
                "bm": bm,
                "ts": _find_timestamp_after(lines, i + 1) or _find_timestamp_before(lines, i),
            })
            cur_num = None
            i += 1
            continue

        # Horizontal rule
        if re.match(r"^([-*_])\1{2,}$", s):
            i += 1
            continue

        # Bullets
        mb = re.match(r"^(\s*)[-*•]\s+(.*)$", raw)
        if mb:
            level = 1 if len(mb.group(1)) >= 2 else 0
            if level == 0:
                cur_num = None
            p = doc.add_paragraph()
            apply_list(p, bullet_num_id, level)
            add_inline(p, mb.group(2).strip())
            i += 1
            continue

        # Numbered list
        mn = re.match(r"^(\s*)\d+[.)]\s+(.*)$", raw)
        if mn:
            level = 1 if (len(mn.group(1)) >= 3 and cur_num) else 0
            if cur_num is None:
                cur_num = new_num(doc, NUMBER_ABS_ID, restart=True)
            p = doc.add_paragraph()
            apply_list(p, cur_num, level)
            p.paragraph_format.space_after = Pt(4)
            add_inline(p, mn.group(2).strip())
            i += 1
            continue

        # Normal paragraph
        p = doc.add_paragraph()
        add_inline(p, s)
        cur_num = None
        i += 1

    return headings


def create_ms_word_doc(markdown_text, output_filename, doc_title, meta=None, video_id=None):
    """Generate the book-style MS Word (.docx) document"""
    markdown_text = clean_xml_text(markdown_text)
    doc_title = clean_xml_text(doc_title)
    if meta:
        meta = {k: clean_xml_text(v) for k, v in meta.items()}
    global VIDEO_ID
    VIDEO_ID = video_id
    meta = meta or build_meta(doc_title)
    markdown_text = normalize_markdown(markdown_text)
    doc = Document()
    configure_styles(doc)
    setup_numbering(doc)
    setup_page(doc, doc_title, meta)
    add_title_block(doc, doc_title, meta)
    headings = render_markdown(doc, markdown_text)
    add_outline(doc, headings)  # outline goes last

    zoom = doc.settings.element.find(qn("w:zoom"))
    if zoom is not None and zoom.get(qn("w:percent")) is None:
        zoom.set(qn("w:percent"), "100")

    doc.core_properties.title = doc_title
    doc.core_properties.author = meta["brand"]
    doc.save(output_filename)


def main():
    global GEMINI_API_KEY
    if not GEMINI_API_KEY:
        GEMINI_API_KEY = input(
            "Enter Gemini API Key (e.g. AIzaSy...): "
        ).strip()

    if len(sys.argv) > 1:
        url = sys.argv[1]
    else:
        url = input("Enter YouTube Video URL: ").strip()

    video_id = extract_video_id(url)
    print(f"[*] Processing Video ID: {video_id}", flush=True)

    print("[*] Fetching Video Title from YouTube...", flush=True)
    vinfo = get_video_info(video_id)
    video_title, channel = vinfo["title"], vinfo["channel"]
    clean_title = sanitize_filename(video_title)
    print(f"[✓] Title Detected: '{video_title}'", flush=True)
    print(f"[✓] Channel Detected: '{channel or 'N/A'}'", flush=True)

    print(
        "[*] Fetching subtitles and splitting them into 1-minute blocks...",
        flush=True,
    )
    minute_blocks = get_transcript_grouped_by_minute(video_id)

    if not minute_blocks:
        print("[X] Could not fetch subtitles/transcript.", flush=True)
        return

    total_minutes = len(minute_blocks)
    print(f"[✓] Total Video Length: ~{total_minutes} Minutes", flush=True)

    client = _make_client(GEMINI_API_KEY)

    models_to_try = []
    try:
        all_models = list(client.models.list())
        for m in all_models:
            m_name = getattr(m, "name", str(m)).replace("models/", "")
            methods = getattr(m, "supported_generation_methods", [])
            if not methods or "generateContent" in methods:
                models_to_try.append(m_name)
        models_to_try = rank_models(models_to_try)
    except Exception:
        pass

    if not models_to_try:
        models_to_try = [
            "gemini-2.5-flash",
            "gemini-2.0-flash",
            "gemini-1.5-flash-latest",
            "gemini-1.5-flash",
        ]

    # Document title in English (if the video title is Hindi/Urdu/Roman Urdu)
    doc_title = translate_title_to_english(client, models_to_try, video_title)
    if doc_title != video_title:
        print(f"[✓] English Title: '{doc_title}'", flush=True)

    print("[*] Detecting document header (brand / subject)...", flush=True)
    sample_text = " ".join(minute_blocks[:3])
    meta = detect_doc_meta(client, models_to_try, doc_title, channel, sample_text)
    print(f"[✓] Header: {meta['brand']}  |  {meta['tagline']}", flush=True)

    CHUNK_SIZE = CHUNK_MINUTES
    total_chunks = math.ceil(total_minutes / CHUNK_SIZE)

    print(
        f"[*] Converting the video in {total_chunks} chunks (English, Book Format + Auto Header)...\n",
        flush=True,
    )

    full_converted_result = []
    converted_ok = 0

    for i in range(0, total_minutes, CHUNK_SIZE):
        chunk = minute_blocks[i : i + CHUNK_SIZE]
        chunk_num = (i // CHUNK_SIZE) + 1
        chunk_text = "\n".join(chunk)

        start_time = chunk[0].split()[0]
        end_time = chunk[-1].split()[0]

        print(
            f"[*] Processing Chunk {chunk_num}/{total_chunks} ({start_time} to {end_time})... Please wait...",
            flush=True,
        )

        try:
            converted_chunk = convert_with_verification(
                client, chunk, models_to_try, doc_title, chunk_num, total_chunks
            )
            coverage = count_output_words(converted_chunk) / max(
                count_source_words(chunk_text), 1
            )
            if ADD_CHUNK_HEADINGS:
                full_converted_result.append(
                    f"\n\n# Section ({start_time} - {end_time})\n"
                )
            else:
                full_converted_result.append("\n")
            full_converted_result.append(converted_chunk)
            converted_ok += 1
            print(
                f"[✓] Chunk {chunk_num} completed! (Words coverage: {coverage:.0%})",
                flush=True,
            )
        except Exception as e:
            print(
                f"[X] Error in Chunk {chunk_num}: {e}. Skipping chunk...",
                flush=True,
            )
            full_converted_result.append("\n" + _missing_note(chunk) + "\n")

    if converted_ok == 0:
        print("[X] None of the chunks could be converted, so no document was created. Check the API key / quota and try again.", flush=True)
        sys.exit(1)

    final_markdown = "\n".join(full_converted_result)

    txt_filename = f"{clean_title}.txt"
    with open(txt_filename, "w", encoding="utf-8") as f:
        f.write(final_markdown)

    docx_filename = f"{clean_title}.docx"
    create_ms_word_doc(final_markdown, docx_filename, doc_title, meta, video_id)

    print("\n" + "=" * 60, flush=True)
    print("                 PROCESS COMPLETE                 ", flush=True)
    print("=" * 60, flush=True)
    print(f"[✓] Clean Text File: {txt_filename}", flush=True)
    print(
        f"[✓] Professional MS Word Document (.docx): {docx_filename}",
        flush=True,
    )


if __name__ == "__main__":
    main()
