#!/usr/bin/env python3
"""
Generates the static website (frontend/*.html, config.js, og-image, icons).

REBRAND IN 30 SECONDS:
  1. Edit BRAND / DOMAIN / EMAIL below (or pass them as environment variables)
  2. Run:  python3 tools/build_pages.py
  3. Rebuild/restart the app. Done.

Optional: `pip install cairosvg` lets the script also regenerate the social-share
image (og-image.png) and the app icon with your new brand name.
"""
import colorsys
import io
import json
import os
import re
from pathlib import Path

# ----------------------------------------------------------------------------
# BRAND SETTINGS  (change these)
# ----------------------------------------------------------------------------
BRAND = os.getenv("BRAND", "Tehreer")
DOMAIN = os.getenv("DOMAIN", "tehreer.com")
EMAIL = os.getenv("EMAIL", f"support@{DOMAIN}")
TAGLINE = "Study notes from any YouTube lecture, in English or Roman Urdu"
YEAR_UPDATED = "October 17, 2026"
# ----------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "frontend"
IMG = OUT / "img"
SITE_URL = f"https://{DOMAIN}"

def _asset_version():
    import hashlib
    h = hashlib.md5((BRAND + DOMAIN + EMAIL).encode())
    for f in [OUT / "style.css", OUT / "site.js", OUT / "app.js"] + sorted(IMG.glob("*.svg")):
        try:
            h.update(f.read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:8]

ASSET_VER = _asset_version()

_ICON_PATHS = {
 "file": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h6"/>',
 "check": '<circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-6"/>',
 "layers": '<path d="M12 3l9 5-9 5-9-5 9-5z"/><path d="M3 13l9 5 9-5"/>',
 "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
 "badge": '<circle cx="12" cy="9" r="6"/><path d="M8.5 14L7 21l5-3 5 3-1.5-7"/>',
 "refresh": '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
 "activity": '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
 "download": '<path d="M12 4v11M7 11l5 5 5-5M5 20h14"/>',
 "shield": '<path d="M12 3l8 3v6c0 4.5-3.2 8.3-8 9-4.8-.7-8-4.5-8-9V6l8-3z"/><path d="M9 12l2 2 4-4"/>',
 "lock": '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
 "key": '<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M16 7l3 3"/>',
 "server": '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
 "trash": '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
 "gauge": '<path d="M4 18a8 8 0 1 1 16 0"/><path d="M12 18l4-6"/>',
 "eyeoff": '<path d="M3 3l18 18"/><path d="M10.6 6.2A9 9 0 0 1 12 6c5 0 8 6 8 6a14 14 0 0 1-3 3.6M6.5 8C4.3 9.7 3 12 3 12s3 6 9 6a9 9 0 0 0 3-.5"/>',
 "eye": '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
 "mail": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>',
 "target": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
 "heart": '<path d="M12 20s-8-5-8-11a4.5 4.5 0 0 1 8-2.5A4.5 4.5 0 0 1 20 9c0 6-8 11-8 11z"/>',
 "key2": '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
}
def ico(name):
    return f'<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">{_ICON_PATHS[name]}</svg>'
def icobox(name):
    return f'<div class="ico-box">{ico(name)}</div>'

_svg_counter = [0]
_HEX_ATTR = ("fill", "stroke", "stop-color")
_ALL_HEX = set()

def _tokenize_tag(m):
    """Move hard-coded hex colours of an SVG element into a style attribute using CSS variables."""
    name, attrs, close = m.group(1), m.group(2), m.group(3)
    if name == "svg":
        return m.group(0)
    moved, out_attrs = [], []
    for am in re.finditer(r'\s([a-zA-Z:-]+)="([^"]*)"', attrs):
        k, v = am.group(1), am.group(2)
        if k in _HEX_ATTR and re.fullmatch(r"#[0-9a-fA-F]{6}", v):
            hx = v[1:].lower()
            _ALL_HEX.add(hx)
            moved.append(f"{k}:var(--c-{hx},#{hx})")
        else:
            out_attrs.append(f' {k}="{v}"')
    if moved:
        out_attrs.append(f' style="{";".join(moved)}"')
    return f"<{name}{''.join(out_attrs)}{close}>"

def inline_svg(name, alt, cls):
    """Inline an SVG from frontend/img with CSS-variable colours so it follows the selected theme."""
    svg = (IMG / name).read_text(encoding="utf-8")
    _svg_counter[0] += 1
    n = _svg_counter[0]
    for old in re.findall(r'\sid="([^"]+)"', svg):
        svg = svg.replace(f'id="{old}"', f'id="{old}-{n}"').replace(f"url(#{old})", f"url(#{old}-{n})")
    svg = re.sub(r"<([a-zA-Z][^\s/>]*)((?:\s[^>]*?)?)(/?)>", _tokenize_tag, svg)
    svg = svg.replace("<svg ", f'<svg class="{cls}" role="img" aria-label="{alt}" focusable="false" ', 1)
    return svg

def img(name, alt, w, h, cls="art", eager=False):
    return inline_svg(name, alt, cls)

THEME_INIT = """<script>(function(){try{var t=localStorage.getItem("site_theme");if(/^t[2-5]$/.test(t)){document.documentElement.setAttribute("data-theme",t);var F={t2:"family=Plus+Jakarta+Sans:wght@400;500;600;700;800",t4:"family=Playfair+Display:wght@600;700&family=Source+Sans+3:wght@400;500;600;700",t5:"family=Sora:wght@500;600;700&family=DM+Sans:wght@400;500;600;700"};if(F[t]){var l=document.createElement("link");l.id="theme-fonts";l.rel="stylesheet";l.href="https://fonts.googleapis.com/css2?"+F[t]+"&display=swap";document.head.appendChild(l)}}}catch(e){}})();</script>"""

PATHS = {"index.html": "/", "app.html": "/app", "features.html": "/features", "how-it-works.html": "/how-it-works",
         "solutions.html": "/solutions", "security.html": "/security", "faq.html": "/faq", "about.html": "/about",
         "contact.html": "/contact", "privacy.html": "/privacy", "terms.html": "/terms", "404.html": None,
         "login.html": "/login", "register.html": "/register", "forgot-password.html": "/forgot-password", "account.html": "/account"}

def page(fname, title, desc, body, defer_app=False, home=False, auth=None, scripts=(), noindex=False):
    full_title = f"{BRAND} - {TAGLINE}" if home else f"{title} - {BRAND}"
    path = PATHS[fname]
    og_img = f"{SITE_URL}/static/img/og-image.png" if (IMG / "og-image.png").exists() else ""
    canon = f'<link rel="canonical" href="{SITE_URL}{path}">' if (path and not noindex) else '<meta name="robots" content="noindex">'
    og = f"""<meta property="og:type" content="website">
<meta property="og:site_name" content="{BRAND}">
<meta property="og:title" content="{full_title}">
<meta property="og:description" content="{desc}">""" + (f'\n<meta property="og:url" content="{SITE_URL}{path}">' if path else "") + (
        f'\n<meta property="og:image" content="{og_img}">\n<meta name="twitter:card" content="summary_large_image">' if og_img else '\n<meta name="twitter:card" content="summary">')
    ld = ""
    if home:
        data = {"@context": "https://schema.org", "@type": "Organization", "name": BRAND, "url": SITE_URL,
                "logo": f"{SITE_URL}/static/img/logo.svg", "email": EMAIL}
        ld = f'\n<script type="application/ld+json">{json.dumps(data)}</script>'
    app = f'\n<script src="/static/app.js?v={ASSET_VER}" defer></script>' if defer_app else ""
    for s_ in scripts:
        app += f'\n<script src="/static/{s_}?v={ASSET_VER}" defer></script>'
    body_attr = f' data-auth="{auth}"' if auth else ""
    noscript = '<noscript><p class="noscript">This site needs JavaScript for the menu and the notes tool. Please enable it and reload.</p></noscript>\n' if defer_app else ""
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{full_title}</title>
<meta name="description" content="{desc}">
<meta name="theme-color" content="#1f3a5f">
{canon}
{og}
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/static/img/apple-touch-icon.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=Literata:opsz,wght@7..72,500;7..72,700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/style.css?v={ASSET_VER}">
<link rel="stylesheet" href="/static/themes.css?v={ASSET_VER}">
{THEME_INIT}
<script src="/static/config.js?v={ASSET_VER}" defer></script>
<script src="/static/site.js?v={ASSET_VER}" defer></script>{ld}
</head>
<body{body_attr}>
<a class="skip" href="#main">Skip to content</a>
<div id="site-util"></div>
<div id="site-header"></div>
{noscript}<main id="main">
{body}
</main>
<div id="site-footer"></div>{app}
</body>
</html>
"""
    (OUT / fname).write_text(html, encoding="utf-8")

CTA = f"""<section class="cta-dark">
  <div class="wrap narrow">
    <h2>Ready to turn your next lecture into notes?</h2>
    <p>Bring a YouTube link with captions and your own Gemini key. The Word file is ready when the progress bar reaches 100%.</p>
    <a class="btn gold lg" href="/app">Start converting</a>
  </div>
</section>"""

def q(question, answer):
    return f"<details><summary>{question}</summary><p>{answer}</p></details>"

FAQ_ITEMS = [
 ("Is it free?", "The tool is free to use. It runs on your own Google Gemini API key, which has a free tier subject to Google's quotas and rate limits. Check Google's current pricing for details."),
 ("Where do I get a Gemini API key?", "Open aistudio.google.com/apikey, sign in with your Google account, click Create API key and copy the key. Keep it private."),
 ("Do you store my API key?", "No. The key is sent to the server for your job, passed to the conversion process and used only to call Google's Gemini API. It is not written to disk or to logs. If you tick &ldquo;Remember the key in this browser&rdquo;, it is saved in your own browser's local storage only, and you can clear it any time by unticking the box and starting a new job."),
 ("Which languages can I choose?", "English or Roman Urdu. You pick one on the tool page before each conversion. English notes are written in clear English even if the lecture is in Urdu or Hindi, and Roman Urdu notes are written in Roman Urdu (Urdu in Latin letters)."),
 ("Does it summarise the video?", "No. It converts the transcript sentence by sentence, then organises it into headings, lists, tables and callouts. A word-count check retries any chunk that comes back too short."),
 ("Which videos work?", "Videos with Urdu, Hindi or English captions, manual or automatic. Videos with captions disabled cannot be converted."),
 ("It says no subtitles were found. What can I do?", "The video may be a live stream, may have captions disabled, or YouTube may have temporarily refused the request. Wait for a live stream to finish (captions can take hours) or try a different video."),
 ("How long does a conversion take?", "It depends on the video length, Gemini's rate limits and how busy the server is. A video is converted in chunks of about five minutes, so long lectures take proportionally longer."),
 ("My job shows a warning about failed chunks.", "A chunk can fail when Gemini is rate limited or returns an error. The rest of the notes are still produced. You can try again a little later to get a complete version."),
 ("How long are my files kept?", "Finished files are deleted from the server automatically after 24 hours. Your conversion history (video, title, status and date) stays in your account for 90 days, and you can delete any entry or your whole account at any time from the Account page."),
 ("The Word document shows a different font.", "The document uses the font Roboto Serif. If it is not installed on your computer, Word substitutes another font. You can install Roboto Serif from Google Fonts."),
 ("Why do I need to sign in?", "Your account keeps your conversions separate from everyone else's, shows your history and lets you download your files again later. You can continue with Google, or create an account with your Gmail address and a password."),
 ("I did not receive the verification code.", "Check your spam or promotions folder, make sure the address is typed correctly and wait a minute before asking for a new code. The code is valid for 10 minutes."),
 ("I forgot my password.", "Choose Forgot password on the sign-in page. We e-mail a 6-digit code to your Gmail address and you can set a new password."),
 ("Why only Gmail addresses?", "Confirming a Gmail address (or signing in with Google) shows that every account belongs to a real person, and it keeps temporary e-mail services out."),
 ("Can I delete my account?", "Yes. Open the Account page and choose Delete account. Your profile, history and generated files are removed permanently."),
 ("Can I use it for any video?", "Please only convert videos you have the right to use, and respect YouTube's terms of service and the rights of content creators. Transcripts are sent to Google's Gemini API for conversion."),
]

# ============================================================== HOME
page("index.html", "Home",
 "Turn YouTube lecture videos into formatted study notes in English or Roman Urdu, with headings, tables, callouts and clickable timestamps.",
f"""
<section class="hero">
  <div class="wrap hero-grid">
    <div class="hero-copy">
      <h1>Lecture videos, turned into notes in English or Roman Urdu</h1>
      <p class="lead">Choose your language, paste a YouTube link and get a formatted Word document: every sentence converted, with headings, tables, highlighted questions and definitions, and timestamps that jump to the exact moment in the video.</p>
      <div class="hero-actions">
        <a class="btn gold lg" href="/app">Start converting</a>
        <a class="btn ghost lg" href="/how-it-works">See how it works</a>
      </div>
      <ul class="pills"><li>English or Roman Urdu</li><li>Free with your own Gemini key</li><li>Word + text export</li></ul>
    </div>
    <div class="hero-art">
      {img("hero-scene.svg", "A YouTube lecture is converted into a formatted Roman Urdu notes document, with a live progress card and a Word file ready to download.", 640, 520, "hero-img", True)}
    </div>
  </div>
</section>

<section class="trust" aria-label="Highlights">
  <div class="wrap">
    <ul>
      <li>{ico("lock")}Your API key is never stored</li>
      <li>{ico("file")}Word and text downloads</li>
      <li>{ico("clock")}Clickable video timestamps</li>
      <li>{ico("check")}Google or Gmail sign-in</li>
    </ul>
  </div>
</section>

<section class="section">
  <div class="wrap">
    <div class="section-head reveal">
      <h2>From link to notes in three steps</h2>
      <p>No installation. Create a free account with your Gmail (or continue with Google), and everything happens on one page.</p>
    </div>
    <div class="step-cards">
      <article class="step-card reveal">{img("step-1.svg", "The form: Gemini API key field, YouTube link field and a Create notes button.", 360, 240)}<div class="step-body"><span class="step-no">1</span><h3>Sign in, add key and link</h3><p>Create a free account (or continue with Google), then paste your Gemini API key and the YouTube link.</p></div></article>
      <article class="step-card reveal">{img("step-2.svg", "A progress card showing 64 percent complete with a strip of chunks.", 360, 240)}<div class="step-body"><span class="step-no">2</span><h3>Watch it work</h3><p>A live progress bar, a chunk-by-chunk strip and the full logs show exactly what is happening.</p></div></article>
      <article class="step-card reveal">{img("step-3.svg", "Two file previews, DOCX and TXT, with a Download files button.", 360, 240)}<div class="step-body"><span class="step-no">3</span><h3>Download</h3><p>When it reaches 100%, download the styled Word file or the plain text copy right from the page.</p></div></article>
    </div>
  </div>
</section>

<section class="section band">
  <div class="wrap">
    <div class="section-head reveal">
      <h2>Notes you can actually study from</h2>
      <p>The tool does not summarise. It converts what the instructor said and then organises it like a course book.</p>
    </div>

    <div class="facts reveal">
      <div class="fact"><b>~5 min</b><span>of video per conversion chunk</span></div>
      <div class="fact"><b>1 min</b><span>timestamp resolution</span></div>
      <div class="fact"><b>2 formats</b><span>Word (.docx) and text (.txt)</span></div>
      <div class="fact"><b>2 ways</b><span>to sign in: Google or Gmail</span></div>
    </div>

    <div class="row reveal">
      <div>
        <h3>Every sentence stays</h3>
        <p>Each chunk of the video is converted sentence by sentence. The output is checked by counting words against the source, and any chunk that comes back too short is retried or split into smaller parts and converted again.</p>
      </div>
      <div class="row-art panel" aria-hidden="true">
        <div class="cov"><span>Chunk 1</span><span class="cov-bar"><i style="width:98%"></i></span><span class="tag ok">98% kept</span></div>
        <div class="cov"><span>Chunk 2</span><span class="cov-bar"><i style="width:93%"></i></span><span class="tag ok">93% kept</span></div>
        <div class="cov"><span>Chunk 3</span><span class="cov-bar"><i class="low" style="width:81%"></i></span><span class="tag re">Retried</span></div>
        <small>Illustration of the word-coverage check.</small>
      </div>
    </div>

    <div class="row flip reveal">
      <div>
        <h3>Structure that helps revision</h3>
        <p>Headings, bullet and numbered lists and tables are generated automatically. Questions, definitions and important notes get their own coloured boxes, and key terms and numbers are bolded so you can scan a page quickly.</p>
      </div>
      <div class="row-art panel" aria-hidden="true">
        <div class="callout"><span class="badge">SAWAL:</span> Questions and answers</div>
        <div class="callout def"><span class="badge">TAREEF:</span> Definitions of terms</div>
        <div class="callout note" style="margin:0"><span class="badge">NOTE:</span> Warnings, tips and examples</div>
      </div>
    </div>

    <div class="row reveal">
      <div>
        <h3>One click back to the video</h3>
        <p>Every timestamp in the Word file is a link. Click it and the video opens at that exact second, so you can check the original explanation whenever a line of notes is unclear. A clickable outline at the end lists every section.</p>
      </div>
      <div class="row-art panel" aria-hidden="true">
        <div class="ts-row"><span>Variable kya hota hai?</span><span>00:02:00</span></div>
        <div class="ts-row"><span>Data types</span><span>00:11:00</span></div>
        <div class="ts-row"><span>Lists aur loops</span><span>00:24:00</span></div>
        <small>Illustration of the outline with timestamps.</small>
      </div>
    </div>
  </div>
</section>

<section class="section">
  <div class="wrap">
    <div class="section-head reveal"><h2>Made for people who learn from video</h2><p>Whether you attend lectures, give them or follow courses on your own.</p></div>
    <div class="sol-cards">
      <article class="sol-card reveal">{img("usecase-student.svg", "A laptop playing a lecture next to a notebook and pencil.", 360, 240)}<div class="sol-body"><h3>Students</h3><p>Turn long recorded lectures into notes you can read, highlight and print, in the Roman Urdu you already type and read every day.</p><a class="more" href="/solutions#students">Learn more</a></div></article>
      <article class="sol-card reveal">{img("usecase-teacher.svg", "A teaching board with a video, text lines and a bar chart.", 360, 240)}<div class="sol-body"><h3>Educators</h3><p>Create a written version of your lessons for students who prefer reading, or for revision handouts, without retyping anything.</p><a class="more" href="/solutions#educators">Learn more</a></div></article>
      <article class="sol-card reveal">{img("usecase-learner.svg", "A phone showing study notes with headphones around it.", 360, 240)}<div class="sol-body"><h3>Self-learners</h3><p>Follow online courses at your own pace. Search the text, jump to the timestamp you need and keep a permanent copy.</p><a class="more" href="/solutions#self-learners">Learn more</a></div></article>
    </div>
  </div>
</section>

<section class="section band">
  <div class="wrap split">
    <div class="split-text reveal">
      <h2>Security you can read about</h2>
      <p>Your Gemini key is used for your conversion only and is never stored. Every job runs in its own isolated process, and finished files are deleted automatically.</p>
      <ul class="checklist">
        <li>API key never written to disk or logs</li>
        <li>Unguessable job links, no public file listings</li>
        <li>Automatic deletion of finished files</li>
        <li>No advertising or tracking cookies</li>
      </ul>
      <a class="btn primary" href="/security">Read about security</a>
    </div>
    <div class="split-art reveal">{img("security.svg", "A browser connects over HTTPS to the server, protected by a shield with a padlock, and the server talks to Google Gemini.", 480, 360)}</div>
  </div>
</section>

<section class="section">
  <div class="wrap narrow">
    <div class="section-head reveal"><h2>Common questions</h2></div>
    <div class="faq reveal">
      {q(*FAQ_ITEMS[0])}
      {q(*FAQ_ITEMS[2])}
      {q(*FAQ_ITEMS[3])}
      {q(*FAQ_ITEMS[5])}
    </div>
    <p style="margin-top:18px"><a class="more" href="/faq">See all questions</a></p>
  </div>
</section>

{CTA}
""", home=True)

# ============================================================== TOOL
page("app.html", "Notes tool",
 "Convert a YouTube video to study notes in English or Roman Urdu. Choose the language, enter your Gemini key and the video link, watch progress live and download the files.",
"""
<section class="page-head">
  <div class="wrap">
    <h1>Notes tool</h1>
    <p>Choose a language, convert a YouTube video into study notes and download the Word and text files when it finishes.</p>
  </div>
</section>

<div class="wrap app-grid">
  <div>
    <ol class="stepper" aria-label="Steps">
      <li id="stp1" class="on"><span>1</span>Enter details</li>
      <li id="stp2"><span>2</span>Convert</li>
      <li id="stp3"><span>3</span>Download</li>
    </ol>
    <section id="formCard" class="card" aria-labelledby="formTitle">
      <h2 id="formTitle">New video</h2>
      <div id="langChooser" class="lang-chooser" role="radiogroup" aria-labelledby="langLabel">
        <p id="langLabel" class="lang-q">Which language should your notes be in?</p>
        <div class="lang-options">
          <button type="button" class="lang-opt" role="radio" aria-checked="false" data-lang="english">
            <span class="lang-name">English</span>
            <span class="lang-sub">Clear English notes, even if the lecture is in Urdu or Hindi</span>
          </button>
          <button type="button" class="lang-opt" role="radio" aria-checked="false" data-lang="roman_urdu">
            <span class="lang-name">Roman Urdu</span>
            <span class="lang-sub">Notes in Urdu written with English letters</span>
          </button>
        </div>
        <p id="langHint" class="fine">Choose one to continue.</p>
      </div>
      <form id="jobForm" novalidate hidden>
        <p class="lang-picked">Notes language: <b id="langPicked"></b> <button type="button" id="langChange" class="linkbtn">Change</button></p>
        <div class="field">
          <label for="apiKey">Gemini API key</label>
          <div class="input-row">
            <input id="apiKey" type="password" autocomplete="off" spellcheck="false" placeholder="AIza... or AQ...." required>
            <button type="button" id="toggleKey" class="ghost" aria-pressed="false">Show</button>
          </div>
          <label class="check"><input type="checkbox" id="rememberKey"> Remember the key in this browser (cleared when you sign out)</label>
        </div>

        <div class="field">
          <label for="videoUrl">YouTube video link</label>
          <div class="input-row">
            <input id="videoUrl" type="url" inputmode="url" autocomplete="off" placeholder="https://www.youtube.com/watch?v=..." required>
            <button type="button" id="pasteBtn" class="ghost">Paste</button>
          </div>
        </div>

        <p id="formError" class="error" role="alert" hidden></p>
        <button type="submit" id="startBtn" class="primary">Create notes</button>
      </form>

      <details class="help">
        <summary>How do I get a Gemini API key?</summary>
        <ol>
          <li>Open <a href="https://aistudio.google.com/apikey" target="_blank" rel="noopener">aistudio.google.com/apikey</a> and sign in with your Google account.</li>
          <li>Click <b>Create API key</b> and pick a project (or let it create a new one).</li>
          <li>Copy the key that appears and paste it above.</li>
        </ol>
        <p>The key is free, but Google's quota limits apply. Long videos can take a while.</p>
      </details>

      <p class="privacy">Your key is sent to Google's Gemini API only while the conversion runs. It is never saved or logged on our server. Gemini processes the video transcript. See the <a href="/privacy">privacy policy</a> and <a href="/security">security</a> page.</p>
    </section>

    <section id="jobCard" class="card" hidden aria-labelledby="jobTitle" aria-live="polite">
      <div class="job-head">
        <div>
          <h2 id="jobTitle">Converting</h2>
          <div class="job-meta"><span id="jobLang" class="lang-badge" hidden></span><p id="videoTitle" class="muted"></p></div>
        </div>
        <div class="pct" id="pctBig" aria-hidden="true">0%</div>
      </div>

      <div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" aria-labelledby="stageText" id="bar">
        <div class="bar-fill" id="barFill"></div>
      </div>
      <p class="stage"><span id="stageText">Waiting in the queue...</span> <span id="elapsed" class="muted"></span></p>

      <div id="chunkWrap" hidden>
        <p class="label">Chunks (about 5 minutes of video each)</p>
        <div id="chunks" class="chunks"></div>
        <ul class="legend">
          <li><i class="c done"></i>Done</li>
          <li><i class="c active"></i>In progress</li>
          <li><i class="c failed"></i>Failed</li>
          <li><i class="c pending"></i>Waiting</li>
        </ul>
      </div>

      <p id="jobWarn" class="warn" hidden></p>
      <p id="jobError" class="error" role="alert" hidden></p>

      <div class="actions" id="actions">
        <button type="button" id="cancelBtn" class="ghost danger">Cancel</button>
        <a id="dlDocx" class="primary btn" hidden>Download Word file (.docx)</a>
        <a id="dlTxt" class="secondary btn" hidden>Download text file (.txt)</a>
        <button type="button" id="newBtn" class="ghost" hidden>New video</button>
      </div>

      <details class="logs" id="logsBox" open>
        <summary>Logs</summary>
        <pre id="logs" tabindex="0" aria-label="Job logs"></pre>
      </details>
    </section>
  </div>

  <aside class="side" aria-label="Tool information">
    <div class="card">
      <h3>Your account</h3>
      <p class="fine" style="margin:0 0 8px">Signed in as <b id="whoami">...</b></p>
      <a class="more" href="/account">My conversions</a>
    </div>
    <div class="card">
      <h3>Server status</h3>
      <p class="status"><span class="dot" aria-hidden="true"></span><span id="serverStatus">Checking...</span></p>
    </div>
    <div class="card">
      <h3>Before you start</h3>
      <ul>
        <li>The video must have captions (manual or automatic).</li>
        <li>Live streams usually have no captions until they end.</li>
        <li>Use the full video link from the address bar or the Share button.</li>
      </ul>
    </div>
    <div class="card">
      <h3>Good to know</h3>
      <ul>
        <li>Long videos take longer, because they are converted in 5-minute chunks.</li>
        <li>You can leave the page open or come back; the job continues on the server.</li>
        <li>Files are kept for 24 hours. Your history stays in your account.</li>
      </ul>
      <p class="fine" style="margin:10px 0 0">Need help? <a href="/faq">Read the FAQ</a> or <a href="/contact">contact us</a>.</p>
    </div>
  </aside>
</div>
""", defer_app=True, auth="required", noindex=True)

# ============================================================== HOW IT WORKS
page("how-it-works.html", "How it works",
 f"See how {BRAND} turns a YouTube video into formatted notes in English or Roman Urdu, step by step.",
f"""
<section class="page-head">
  <div class="wrap">
    <h1>How it works</h1>
    <p>A short tour of what happens between pasting a link and downloading your notes.</p>
  </div>
</section>

<section class="section" style="padding-top:20px">
  <div class="wrap narrow">
    <ol class="steps long">
      <li><h3>Fetch the transcript</h3><p>The subtitles of the video are downloaded (Urdu, Hindi or English, manual or automatic) and grouped into one-minute blocks, each keeping its timestamp.</p></li>
      <li><h3>Split into chunks</h3><p>The minutes are grouped into chunks of about five minutes. Smaller chunks make it far less likely that the AI skips anything.</p></li>
      <li><h3>Convert with Gemini</h3><p>Each chunk is sent to Google Gemini with strict instructions: write every sentence in the language you chose (clear English, or Roman Urdu), do not summarise, do not merge, do not skip, and organise the result into headings, lists, tables and callout boxes.</p></li>
      <li><h3>Check and retry</h3><p>The number of words in the output is compared with the source. A chunk that falls below the coverage threshold is retried, and if needed split into smaller parts and converted again. If one model is rate limited, the next available model is tried.</p></li>
      <li><h3>Build the document</h3><p>The text is cleaned up and rendered into a styled Word document with a header and footer, coloured callouts, tables, bolded key terms, clickable timestamps and an outline at the end. A plain text copy is saved too.</p></li>
    </ol>
  </div>
</section>

<section class="section band">
  <div class="wrap split">
    <div class="split-text prose">
      <h2>What you need</h2>
      <ul>
        <li><b>A free account.</b> Continue with Google, or register with your Gmail address and a password.</li>
        <li><b>A YouTube video with captions.</b> Automatic captions are fine. Live streams usually get captions only after they end.</li>
        <li><b>A free Google Gemini API key.</b> Open <a href="https://aistudio.google.com/apikey" target="_blank" rel="noopener">aistudio.google.com/apikey</a>, sign in, choose <i>Create API key</i> and copy the key.</li>
      </ul>
      <h2>What affects the speed</h2>
      <ul>
        <li><b>Video length.</b> A longer video means more chunks to convert.</li>
        <li><b>Gemini limits.</b> Free keys have rate limits. When they are reached the tool waits and retries automatically.</li>
        <li><b>Queue.</b> To keep things fair, only a limited number of jobs run at once. If the server is busy your job waits and shows its position.</li>
      </ul>
      <p>Questions about a specific case? The <a href="/faq">FAQ</a> covers the most common ones.</p>
    </div>
    <div class="split-art">{img("step-2.svg", "A progress card showing 64 percent complete with a strip of chunks.", 360, 240)}</div>
  </div>
</section>
{CTA}
""")

# ============================================================== SOLUTIONS
def sol(sid, title, text, bullets, image, alt, band, rev):
    lis = "".join(f"<li>{b}</li>" for b in bullets)
    cls = "section band" if band else "section"
    r = " rev" if rev else ""
    return f"""<section class="{cls}" id="{sid}"><div class="wrap split{r}">
  <div class="split-text"><h2>{title}</h2><p>{text}</p><ul class="checklist">{lis}</ul><a class="btn primary" href="/app">Convert a lecture</a></div>
  <div class="split-art">{img(image, alt, 360, 240)}</div>
</div></section>"""

page("solutions.html", "Solutions",
 f"How students, educators and self-learners use {BRAND} to turn lecture videos into English or Roman Urdu study notes.",
f"""
<section class="page-head">
  <div class="wrap">
    <h1>Solutions</h1>
    <p>The same notes engine, used in three different ways.</p>
  </div>
</section>
{sol("students", "Students", "Recorded lectures are great until exam week, when you need to find one explanation inside two hours of video. Written notes let you search, skim and revise in a fraction of the time.", ["Read and print lectures in the Roman Urdu you already use", "Questions, definitions and notes stand out in coloured boxes", "Click any timestamp to rewatch the exact moment"], "usecase-student.svg", "A laptop playing a lecture next to a notebook and pencil.", False, False)}
{sol("educators", "Educators", "Teachers and tutors already have the content: their videos. Turn them into handouts, revision sheets and written lessons without retyping a word.", ["Make a reading version of any lesson you have recorded", "Share a clean Word file that students can annotate", "Keep your own wording, nothing is rewritten or summarised"], "usecase-teacher.svg", "A teaching board with a video, text lines and a bar chart.", True, True)}
{sol("self-learners", "Self-learners", "Free online courses are only as useful as what you retain. A searchable written copy makes it easier to come back to a topic months later.", ["Search the text instead of scrubbing through video", "Keep a permanent copy that works offline", "Learn at your own pace on any device"], "usecase-learner.svg", "A phone showing study notes with headphones around it.", False, False)}
{CTA}
""")

# ============================================================== FEATURES
ICON_FOR = {"Two languages":"file","Quality check":"check","Structured notes":"layers","Clickable timestamps":"clock",
            "Automatic branding":"badge","Resilient processing":"refresh","Live dashboard":"activity",
            "Two download formats":"download","Private by design":"shield"}
def fl(h, *ps):
    return f'<div class="fcard reveal">{icobox(ICON_FOR[h])}<h3>{h}</h3>' + "".join(f"<p>{p}</p>" for p in ps) + "</div>"

page("features.html", "Features",
 "Everything the notes tool does: faithful conversion, quality checks, structured notes, clickable timestamps and a live dashboard.",
f"""
<section class="page-head">
  <div class="wrap">
    <h1>Features</h1>
    <p>Built to produce notes you can trust and study from, not a loose summary.</p>
  </div>
</section>

<section class="section">
  <div class="wrap"><div class="fgrid">
    {fl("Two languages", "Choose English or Roman Urdu before every conversion. English notes are clear, natural English even when the lecture is in Urdu or Hindi. Roman Urdu notes keep the speaker's words in Roman Urdu.", "Either way, every sentence, example, number and warning is kept: nothing is summarised, merged or deleted.")}
    {fl("Quality check", "The output of every chunk is compared with its source by word count. Chunks that fall below the coverage threshold are retried or split into smaller parts automatically.")}
    {fl("Structured notes", "Headings, bullet lists, numbered steps and tables are created for you. Questions and answers, definitions and notes appear in colour-coded boxes.", "Key terms, numbers, amounts and percentages are bolded, and the most important lines are highlighted so you can revise quickly.")}
    {fl("Clickable timestamps", "Each timestamp links to the exact second in the original video. The outline at the end lists every section with a link to the page and a link to the video.")}
    {fl("Automatic branding", "The header and tagline are generated from the channel name and the subject of the video, so every document looks like a proper course handout.")}
    {fl("Resilient processing", "If one Gemini model is rate limited, the next one is tried. Failed requests are retried after a short wait, and the job report tells you if any chunk could not be converted.")}
    {fl("Live dashboard", "See the percentage complete, which chunk is being converted, the elapsed time and the complete logs. You can cancel a job at any time.")}
    {fl("Two download formats", "A styled Word document (.docx) and a plain text copy (.txt), available from the page as soon as the job is done and from your account history for 24 hours.")}
    {fl("Private by design", "Your Gemini key is used for your job only and is never stored on our server. Finished files are deleted automatically after 24 hours.")}
  </div></div>
</section>
{CTA}
""")

# ============================================================== SECURITY
def sc(h, icon, text):
    return f'<div class="fcard reveal">{icobox(icon)}<h3>{h}</h3><p>{text}</p></div>'

page("security.html", "Security",
 f"How {BRAND} protects your Gemini API key, your files and your data.",
f"""
<section class="page-head">
  <div class="wrap">
    <h1>Security</h1>
    <p>What we do with your API key and your files, explained plainly.</p>
  </div>
</section>

<section class="section">
  <div class="wrap split">
    <div class="split-text">
      <h2>Built so there is little to protect</h2>
      <p>The safest data is data we never keep. Your Gemini key is used for your conversion and then forgotten, and the files we generate are deleted automatically.</p>
      <p>The sections below describe exactly how the service handles your information.</p>
    </div>
    <div class="split-art">{img("security.svg", "A browser connects over HTTPS to the server, protected by a shield with a padlock, and the server talks to Google Gemini.", 480, 360)}</div>
  </div>
</section>

<section class="section band">
  <div class="wrap">
    <div class="fgrid">
      {sc("Your API key", "key", "The key is verified with Google, handed to the conversion process for your job only and replaced by asterisks if it ever appears in output. It is never written to disk or to our logs.")}
      {sc("Encrypted in transit", "lock", "The site is served over HTTPS, so your key, your video link and your downloads are encrypted between your browser and the server.")}
      {sc("Isolated jobs", "server", "Every conversion runs in its own process with its own working folder. One user's job cannot read another's files.")}
      {sc("Private files", "eyeoff", "Each job gets a long random identifier, and only the signed-in owner can open its status or download its files. There is no public listing of jobs or documents.")}
      {sc("Automatic deletion", "trash", "Finished files are deleted from the server after 24 hours, or immediately when you delete the conversion from your account.")}
      {sc("Abuse protection", "gauge", "Input is validated (only YouTube video links are accepted), jobs are queued and rate limited per account and network, and a job that runs too long is stopped.")}
      {sc("Strong password protection", "key", "Passwords are stored only as Argon2 hashes and must be long and complex. Accounts lock for a while after repeated wrong attempts, and every Gmail address is confirmed with an e-mailed code.")}
      {sc("Protected sessions", "lock", "You stay signed in with a random token in an HttpOnly cookie. Only a hash of it is stored, and you can sign out at any time.")}
      {sc("Your data, your control", "trash", "Delete any conversion, or your whole account with its history and files, from the Account page whenever you like.")}
    </div>
  </div>
</section>

<section class="section">
  <div class="wrap narrow prose">
    <h2>What to keep in mind</h2>
    <ul>
      <li>The video transcript is sent to <b>Google's Gemini API</b> for conversion, so Google's terms apply to that processing.</li>
      <li><b>Use a dedicated key.</b> Create a separate API key for this tool and delete it at aistudio.google.com/apikey when you no longer need it.</li>
      <li>The page sets standard security headers and does not use advertising or tracking cookies.</li>
    </ul>

    <h2>Report a vulnerability</h2>
    <p>If you believe you have found a security issue, please email <a data-site-email href="#">{EMAIL}</a> with the details and steps to reproduce it. Please do not include real API keys in your report.</p>

    <p>See also the <a href="/privacy">privacy policy</a> and <a href="/terms">terms of use</a>.</p>
  </div>
</section>
{CTA}
""")

# ============================================================== FAQ
page("faq.html", "FAQ",
 f"Answers to common questions about {BRAND}: cost, API keys, supported videos, privacy and troubleshooting.",
f"""
<section class="page-head">
  <div class="wrap">
    <h1>Frequently asked questions</h1>
    <p>Can't find your answer? <a href="/contact">Contact us</a>.</p>
  </div>
</section>

<section class="section" style="padding-top:40px">
  <div class="wrap narrow faq">
    {"".join(q(a, b) for a, b in FAQ_ITEMS)}
  </div>
</section>
{CTA}
""")

# ============================================================== ABOUT
def pr(h, icon, text):
    return f'<div class="fcard reveal">{icobox(icon)}<h3>{h}</h3><p>{text}</p></div>'

page("about.html", "About",
 f"Why {BRAND} exists and the principles behind it.",
f"""
<section class="page-head">
  <div class="wrap">
    <h1>About {BRAND}</h1>
    <p>We help people learn from video lectures by turning them into readable notes in the script they use every day.</p>
  </div>
</section>

<section class="section">
  <div class="wrap split">
    <div class="split-text prose">
      <h2>Why we built it</h2>
      <p>A lot of excellent teaching in Urdu and Hindi lives in long YouTube videos, and most people in Pakistan read and type Roman Urdu on their phones daily. Taking notes from a two-hour lecture by hand is slow, and a short AI summary throws away exactly the examples and explanations that make a lecture useful.</p>
      <p>So we built a tool that keeps the whole lecture. It writes out what the instructor said in English or Roman Urdu (your choice), organises it like a course book, and links every part back to the original video.</p>
    </div>
    <div class="split-art">{img("usecase-learner.svg", "A phone showing study notes with headphones around it.", 360, 240)}</div>
  </div>
</section>

<section class="section band">
  <div class="wrap">
    <div class="section-head"><h2>Our principles</h2></div>
    <div class="fgrid">
      {pr("Faithful over clever", "target", "We check word counts and retry instead of trusting a single pass.")}
      {pr("Visible progress", "eye", "You can see what the tool is doing, chunk by chunk, with full logs.")}
      {pr("Your key, your quota", "key", "Conversions use your own Gemini key, and we do not keep it.")}
    </div>
  </div>
</section>

<section class="section">
  <div class="wrap narrow prose">
    <div class="note-box">This service is independent. It is not affiliated with, endorsed by or sponsored by YouTube or Google. Video transcripts are processed with the Google Gemini API.</div>
    <p>Respect for creators matters to us: every note links back to the source video. Questions or ideas? <a href="/contact">Get in touch</a>.</p>
  </div>
</section>
{CTA}
""")

# ============================================================== CONTACT
page("contact.html", "Contact",
 f"Contact the {BRAND} team for support, feedback or questions.",
f"""
<section class="page-head">
  <div class="wrap">
    <h1>Contact</h1>
    <p>Questions, feedback or a problem with a conversion? We would like to hear from you.</p>
  </div>
</section>

<section class="section" style="padding-top:40px">
  <div class="wrap">
    <div class="contact-box">
      {icobox("mail")}
      <p class="muted" style="margin-bottom:6px">Email us at</p>
      <p><a class="mail" data-site-email href="#">{EMAIL}</a></p>
      <p class="fine" style="margin:0">We reply as soon as we can.</p>
    </div>

    <div class="narrow prose">
      <h2>Reporting a problem</h2>
      <p>To help us fix it quickly, please include:</p>
      <ul>
        <li>The link of the video you tried (or a description if it is private).</li>
        <li>What you expected to happen and what happened instead.</li>
        <li>The text from the <b>Logs</b> box on the tool page. <b>Never include your API key.</b></li>
      </ul>
      <p>Common issues such as missing subtitles and failed chunks are explained in the <a href="/faq">FAQ</a>. Security reports: see the <a href="/security">security</a> page.</p>
    </div>
  </div>
</section>
""")

# ============================================================== PRIVACY
page("privacy.html", "Privacy policy",
 f"How {BRAND} handles your Gemini API key, video links and generated files.",
f"""
<section class="page-head">
  <div class="wrap narrow">
    <h1>Privacy policy</h1>
    <p>Last updated: {YEAR_UPDATED}</p>
  </div>
</section>

<section class="section" style="padding-top:40px">
  <div class="wrap narrow prose">
    <p>This policy explains what information {BRAND} handles when you use the notes tool, and what we do with it.</p>

    <h2>Your account</h2>
    <ul>
      <li><b>Account details.</b> When you register we store your first and last name, username, phone number (not verified) and Gmail address. If you continue with Google, we receive your name, Gmail address, profile picture and Google account ID from Google.</li>
      <li><b>Password.</b> Stored only as a salted Argon2 hash, never in clear text. Accounts created with Google have no password unless you set one.</li>
      <li><b>E-mail codes.</b> Codes that confirm your Gmail address or reset your password are stored only as a one-way hash, expire after 10 minutes and are deleted after a day.</li>
      <li><b>Sign-in records.</b> For security we log sign-in events with the time, IP address and browser type, and keep them for up to 180 days.</li>
      <li><b>Conversion history.</b> For each conversion we keep the video link, video title, status and dates, for up to 90 days, so you can see your history. You can delete entries, or your whole account, at any time.</li>
    </ul>

    <h2>Information you provide</h2>
    <ul>
      <li><b>Gemini API key.</b> You enter your own key so the conversion can run on your Google quota. It is used only for your job, passed to the conversion process and sent to Google's Gemini API. It is not written to disk or to our logs.</li>
      <li><b>YouTube video link.</b> We use it to fetch the video's title, channel name and subtitles.</li>
    </ul>

    <h2>Information we create</h2>
    <ul>
      <li><b>Generated files.</b> The Word and text notes are stored on the server only so you can download them. They are deleted automatically after 24 hours, or earlier when you delete the conversion. Job logs are kept in the server's memory for the same period.</li>
      <li><b>Technical data.</b> Your IP address is held in the server's memory to limit abuse (for example the number of jobs per hour). Our web server may also keep standard access logs.</li>
    </ul>

    <h2>Third parties</h2>
    <ul>
      <li><b>Google Gemini.</b> The video transcript is sent to Google's Gemini API using your key. Google's own terms and privacy policy apply to that processing.</li>
      <li><b>YouTube.</b> We retrieve public video information and subtitles from YouTube.</li>
      <li><b>Google sign-in.</b> If you choose Continue with Google, Google's sign-in service is used for that step, and Google's terms and privacy policy apply to it.</li>
      <li><b>Google Fonts.</b> Pages load fonts from Google Fonts, which means your browser contacts Google's servers to download them.</li>
    </ul>

    <h2>Your browser</h2>
    <p>If you tick &ldquo;Remember the key in this browser&rdquo;, the key is saved in your browser's local storage on your device. We cannot read it from our server. Signing out removes it, and you can also untick the box and start a new job. The page also stores the ID of your current job in local storage so progress survives a refresh.</p>

    <h2>Cookies</h2>
    <p>We use one essential cookie to keep you signed in (it is HttpOnly and only contains a random token). We do not use advertising or tracking cookies. Signing out removes it.</p>

    <h2>Your choices</h2>
    <p>You can delete any conversion, or your whole account with all its data, from the Account page, and files expire on their own after 24 hours. If you believe a key was exposed, revoke it at aistudio.google.com/apikey and create a new one.</p>

    <h2>Changes to this policy</h2>
    <p>We may update this policy. The date at the top shows the latest version.</p>

    <h2>Contact</h2>
    <p>Questions about privacy? Email <a data-site-email href="#">{EMAIL}</a>.</p>
  </div>
</section>
""")

# ============================================================== TERMS
page("terms.html", "Terms of use",
 f"The terms for using {BRAND}.",
f"""
<section class="page-head">
  <div class="wrap narrow">
    <h1>Terms of use</h1>
    <p>Last updated: {YEAR_UPDATED}</p>
  </div>
</section>

<section class="section" style="padding-top:40px">
  <div class="wrap narrow prose">
    <p>By using {BRAND} you agree to these terms. If you do not agree, please do not use the service.</p>

    <h2>Accounts</h2>
    <p>You create an account with a Gmail address that you control (or continue with Google) and keep your password confidential. You are responsible for activity under your account. We may limit or disable accounts that abuse the service. You can delete your account at any time from the Account page.</p>

    <h2>The service</h2>
    <p>The service converts the subtitles of YouTube videos into notes in English or Roman Urdu using the Google Gemini API and your own API key. It is provided as is, without any guarantee that it will be available at all times or that results will be complete or error free.</p>

    <h2>Your responsibilities</h2>
    <ul>
      <li>Use only an API key that belongs to you, and keep it private.</li>
      <li>Convert only videos you have the right to use, and respect YouTube's terms of service and the rights of content creators.</li>
      <li>Do not misuse the service, attempt to overload it, bypass its limits or use it for anything unlawful.</li>
    </ul>

    <h2>Costs and quotas</h2>
    <p>Conversions use your Google Gemini quota. Any usage charges or limits set by Google are your responsibility.</p>

    <h2>Accuracy</h2>
    <p>Notes are generated by an AI model from automatic or manual subtitles. They can contain mistakes, omissions or misheard words. Check important details against the original video, which every timestamp links to.</p>

    <h2>Limits and availability</h2>
    <p>We may limit the number of jobs, delete stored files after a short period and suspend or change the service at any time.</p>

    <h2>Liability</h2>
    <p>To the extent permitted by law, we are not liable for any loss arising from your use of the service or from reliance on the generated notes.</p>

    <h2>Changes</h2>
    <p>We may update these terms. Continued use of the service means you accept the updated terms.</p>

    <h2>Contact</h2>
    <p>Questions about these terms? Email <a data-site-email href="#">{EMAIL}</a>.</p>
  </div>
</section>
""")

# ============================================================== 404
page("404.html", "Page not found",
 "The page you were looking for could not be found.",
"""
<section class="center-page">
  <div class="wrap narrow">
    <div class="big">404</div>
    <h1 style="margin-top:12px">Page not found</h1>
    <p class="lead" style="margin:0 auto 24px">The page you were looking for does not exist or has moved.</p>
    <a class="btn primary lg" href="/">Back to home</a>
    <a class="btn ghost lg" href="/app">Open the tool</a>
  </div>
</section>
""")


# ============================================================== shared auth snippets
def pw_field(fid, label, autocomplete):
    return f"""<div class="field">
  <label for="{fid}">{label}</label>
  <div class="input-row">
    <input id="{fid}" type="password" autocomplete="{autocomplete}" spellcheck="false" required>
    <button type="button" class="ghost pw-toggle" data-for="{fid}" aria-pressed="false">Show</button>
  </div>
</div>"""

PW_RULES = """<ul class="pw-rules" id="pwRules" aria-live="polite">
  <li data-rule="len">At least 10 characters</li>
  <li data-rule="lower">One lowercase letter</li>
  <li data-rule="upper">One uppercase letter</li>
  <li data-rule="digit">One number</li>
  <li data-rule="symbol">One symbol (! @ # $ %)</li>
</ul>
<div class="pw-meter" aria-hidden="true"><i id="pwBar"></i></div>"""

G_LOGO = ('<svg viewBox="0 0 48 48" aria-hidden="true" focusable="false">'
          '<path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/>'
          '<path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/>'
          '<path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/>'
          '<path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>')

def google_block(label):
    return f"""<div class="google-box">
  <a id="googleBtn" class="google-btn" href="/api/auth/google/start">{G_LOGO}<span>{label}</span></a>
  <p id="googleError" class="error" role="alert" hidden></p>
</div>"""

# ============================================================== LOGIN
page("login.html", "Sign in",
 f"Sign in to {BRAND} with Google or with your Gmail address and password.",
f"""
<section class="auth-section">
  <div class="wrap auth-grid">
    <div class="auth-intro">
      <h1>Welcome back</h1>
      <p class="lead">Sign in to convert videos, see your history and download your notes.</p>
      <ul class="checklist">
        <li>Your own account, separate from everyone else</li>
        <li>History of your conversions with re-download for 24 hours</li>
        <li>Delete your data whenever you want</li>
      </ul>
      <div class="auth-art">{img("security.svg", "A shield with a padlock protecting the connection between your browser and the server.", 480, 360)}</div>
    </div>

    <div class="card auth-card">
      <h2>Sign in</h2>
      {google_block("Continue with Google")}
      <div class="divider"><span>or sign in with your Gmail</span></div>
      <form id="loginForm" novalidate>
        <div class="field">
          <label for="email">Gmail address</label>
          <input id="email" type="email" inputmode="email" autocomplete="username" spellcheck="false" placeholder="yourname@gmail.com" required>
        </div>
        {pw_field("password", "Password", "current-password")}
        <p class="auth-row"><a href="/forgot-password">Forgot password?</a></p>
        <p id="loginError" class="error" role="alert" hidden></p>
        <p id="verifyHint" class="warn" hidden>Your e-mail is not confirmed yet. <a id="verifyLink" href="/register">Enter your code</a>.</p>
        <button type="submit" id="loginBtn" class="primary">Sign in with Email</button>
      </form>
      <p class="auth-switch">New here? <a id="toRegister" href="/register">Create an account</a></p>
      <p class="privacy">By continuing you agree to the <a href="/terms">Terms of use</a> and the <a href="/privacy">Privacy policy</a>.</p>
    </div>
  </div>
</section>
""", scripts=("auth.js",), noindex=True)

# ============================================================== REGISTER
page("register.html", "Create account",
 f"Create your free {BRAND} account with your Gmail address.",
f"""
<section class="auth-section">
  <div class="wrap auth-grid">
    <div class="auth-intro">
      <h1>Create your free account</h1>
      <p class="lead">Register with your Gmail address, or continue with Google: one click, nothing to type.</p>
      <ul class="checklist">
        <li>Convert YouTube lectures into English or Roman Urdu notes</li>
        <li>Keep a history and re-download your files</li>
        <li>Gmail only, so every account belongs to a real person</li>
      </ul>
      <div class="auth-art">{img("usecase-student.svg", "A laptop playing a lecture next to a notebook and pencil.", 360, 240)}</div>
    </div>

    <div class="card auth-card">
      <div id="stepForm">
        <h2>Sign up</h2>
        {google_block("Sign up with Google")}
        <div class="divider"><span>or register with your Gmail</span></div>
        <form id="registerForm" novalidate>
          <div class="two-col">
            <div class="field"><label for="first_name">First name</label><input id="first_name" type="text" autocomplete="given-name" maxlength="60" required></div>
            <div class="field"><label for="last_name">Last name</label><input id="last_name" type="text" autocomplete="family-name" maxlength="60" required></div>
          </div>
          <div class="field"><label for="phone">Phone number</label><input id="phone" type="tel" inputmode="tel" autocomplete="tel" placeholder="03001234567 or +923001234567" required></div>
          <div class="field"><label for="email">Gmail address</label><input id="email" type="email" inputmode="email" autocomplete="email" spellcheck="false" placeholder="yourname@gmail.com" required></div>
          <div class="field"><label for="username">Username</label><input id="username" type="text" autocomplete="username" maxlength="30" placeholder="letters, numbers, dot or underscore" required></div>
          {pw_field("password", "Password", "new-password")}
          {PW_RULES}
          {pw_field("password2", "Confirm password", "new-password")}
          <label class="check"><input type="checkbox" id="terms"> I agree to the <a href="/terms">Terms of use</a> and the <a href="/privacy">Privacy policy</a></label>
          <p id="registerError" class="error" role="alert" hidden style="margin-top:14px"></p>
          <button type="submit" id="registerBtn" class="primary" style="margin-top:14px">Create account</button>
        </form>
        <p class="auth-switch">Already have an account? <a href="/login">Sign in</a></p>
      </div>

      <div id="stepVerify" hidden>
        <h2>Confirm your Gmail</h2>
        <p class="muted">We sent a 6-digit code to <b id="sentTo"></b>. It is valid for 10 minutes.</p>
        <p id="devHint" class="warn" hidden>Development mode: the code is printed in the server log instead of being e-mailed.</p>
        <form id="verifyForm" novalidate>
          <div class="field">
            <label for="code">6-digit code</label>
            <input id="code" class="code-input" type="text" inputmode="numeric" autocomplete="one-time-code" maxlength="7" placeholder="000000" required>
          </div>
          <p id="verifyError" class="error" role="alert" hidden></p>
          <button type="submit" id="verifyBtn" class="primary">Verify and continue</button>
        </form>
        <p class="auth-links">
          <button type="button" id="resendBtn" class="linkbtn" disabled>Resend code</button>
          <span id="resendTimer" class="fine"></span>
          <span class="sep">&middot;</span>
          <button type="button" id="backBtn" class="linkbtn">Change my details</button>
        </p>
      </div>
    </div>
  </div>
</section>
""", scripts=("auth.js",), noindex=True)

# ============================================================== FORGOT PASSWORD
page("forgot-password.html", "Forgot password",
 f"Reset your {BRAND} password with a code sent to your Gmail address.",
f"""
<section class="auth-section">
  <div class="wrap auth-grid">
    <div class="auth-intro">
      <h1>Forgot your password?</h1>
      <p class="lead">Enter your Gmail address. We will e-mail you a 6-digit code so you can choose a new password.</p>
      <ul class="checklist">
        <li>The code works once and expires in 10 minutes</li>
        <li>Signing in with Google? You can use this to add a password too</li>
      </ul>
    </div>

    <div class="card auth-card">
      <div id="stepEmail">
        <h2>Reset password</h2>
        <form id="forgotForm" novalidate>
          <div class="field"><label for="email">Gmail address</label><input id="email" type="email" inputmode="email" autocomplete="email" spellcheck="false" placeholder="yourname@gmail.com" required></div>
          <p id="forgotError" class="error" role="alert" hidden></p>
          <button type="submit" id="forgotBtn" class="primary">Send me a code</button>
        </form>
        <p class="auth-switch"><a href="/login">Back to sign in</a></p>
      </div>

      <div id="stepReset" hidden>
        <h2>Choose a new password</h2>
        <p class="muted">If an account exists for <b id="sentTo"></b>, a 6-digit code is on its way.</p>
        <p id="devHint" class="warn" hidden>Development mode: the code is printed in the server log instead of being e-mailed.</p>
        <form id="resetForm" novalidate>
          <div class="field"><label for="code">6-digit code</label><input id="code" class="code-input" type="text" inputmode="numeric" autocomplete="one-time-code" maxlength="7" placeholder="000000" required></div>
          {pw_field("password", "New password", "new-password")}
          {PW_RULES}
          {pw_field("password2", "Confirm new password", "new-password")}
          <p id="resetError" class="error" role="alert" hidden></p>
          <button type="submit" id="resetBtn" class="primary">Set new password</button>
        </form>
        <p class="auth-links">
          <button type="button" id="resendBtn" class="linkbtn" disabled>Resend code</button>
          <span id="resendTimer" class="fine"></span>
          <span class="sep">&middot;</span>
          <button type="button" id="backBtn" class="linkbtn">Use a different address</button>
        </p>
      </div>

      <div id="stepDone" hidden>
        <h2>Password updated</h2>
        <p class="muted">Your password was changed and you were signed out everywhere. Sign in with your new password.</p>
        <a class="btn primary" href="/login">Go to sign in</a>
      </div>
    </div>
  </div>
</section>
""", scripts=("auth.js",), noindex=True)

# ============================================================== ACCOUNT
page("account.html", "My account",
 f"Your {BRAND} profile, conversion history and downloads.",
f"""
<section class="page-head">
  <div class="wrap">
    <h1>My account</h1>
    <p>Your conversions, downloads, profile and password in one place.</p>
  </div>
</section>

<div class="wrap app-grid">
  <div>
    <section class="card" aria-labelledby="histTitle">
      <div class="card-head">
        <h2 id="histTitle">Your conversions</h2>
        <a class="btn primary" href="/app">New conversion</a>
      </div>
      <p id="historyNote" class="fine"></p>
      <div id="historyList" class="history" aria-live="polite"><p class="muted">Loading...</p></div>
    </section>
  </div>

  <aside class="side" aria-label="Account settings">
    <section class="card">
      <h3>Profile</h3>
      <form id="profileForm" novalidate>
        <label for="pfirst">First name</label><input id="pfirst" type="text" maxlength="60" autocomplete="given-name">
        <label for="plast" style="margin-top:10px">Last name</label><input id="plast" type="text" maxlength="60" autocomplete="family-name">
        <label for="pphone" style="margin-top:10px">Phone</label><input id="pphone" type="tel" autocomplete="tel">
        <p class="fine" style="margin:10px 0 2px">Gmail: <b id="pemail">...</b></p>
        <p class="fine" style="margin:0 0 2px">Username: <b id="puser">...</b></p>
        <p class="fine" style="margin:0 0 12px">Member since <span id="psince">...</span></p>
        <p id="profileError" class="error" role="alert" hidden></p>
        <button type="submit" class="primary">Save</button>
        <span id="pmsg" class="fine" role="status"></span>
      </form>
    </section>
    <section class="card" id="passwordCard">
      <h3>Password</h3>
      <div id="pwNone" hidden>
        <p class="fine">This account signs in with Google and has no password yet. You can add one with a code we e-mail you.</p>
        <a class="btn ghost sm" href="/forgot-password">Set a password</a>
      </div>
      <form id="pwForm" hidden novalidate>
        <label for="curpw">Current password</label><input id="curpw" type="password" autocomplete="current-password">
        <label for="newpw" style="margin-top:10px">New password</label><input id="newpw" type="password" autocomplete="new-password">
        <p class="fine" style="margin:6px 0 10px">At least 10 characters with upper and lower case letters, a number and a symbol.</p>
        <p id="pwError" class="error" role="alert" hidden></p>
        <button type="submit" class="ghost">Change password</button>
        <span id="pwMsg" class="fine" role="status"></span>
      </form>
    </section>
    <section class="card">
      <h3>Session</h3>
      <button type="button" id="logoutBtn" class="ghost">Sign out</button>
    </section>
    <section class="card">
      <h3>Delete account</h3>
      <p class="fine">Permanently removes your profile, history and generated files. This cannot be undone.</p>
      <details class="help">
        <summary>I want to delete my account</summary>
        <div id="delPwWrap" hidden>
          <label for="delPw" style="margin-top:10px">Your password</label>
          <input id="delPw" type="password" autocomplete="current-password">
        </div>
        <label for="delConfirm" style="margin-top:10px">Type DELETE to confirm</label>
        <input id="delConfirm" type="text" autocomplete="off" placeholder="DELETE">
        <p id="delError" class="error" role="alert" hidden style="margin-top:10px"></p>
        <button type="button" id="delBtn" class="ghost danger" style="margin-top:10px">Delete everything</button>
      </details>
    </section>
  </aside>
</div>
""", auth="required", scripts=("auth.js",), noindex=True)

# ============================================================== themes.css (illustration colours per theme)
THEME_COLORS = {
    "t2": dict(pri_h=203, pri_s=1.1, acc_h=262, acc_s=1.0, over={"e6eef9": "16233d", "f8fafc": "0f1a30"}),
    "t3": dict(pri_h=222, pri_s=1.5, acc_h=178, acc_s=1.0, over={}),
    "t4": dict(pri_h=152, pri_s=0.9, acc_h=14,  acc_s=1.0, over={}),
    "t5": dict(pri_h=265, pri_s=1.2, acc_h=328, acc_s=1.0, over={}),
}
KEEP = {"e5604f", "f1c76b", "5fc58a", "7fd6a0", "2e7d4f", "eaf5ee", "9bbfa9"}

def shift(hx, cfg):
    if hx in KEEP:
        return hx
    if hx in cfg["over"]:
        return cfg["over"][hx]
    r, g, b = (int(hx[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    deg = h * 360
    if 185 <= deg <= 255:
        deg, s = cfg["pri_h"], min(1.0, s * cfg["pri_s"])
    elif 25 <= deg <= 55 and s > 0.3:
        deg, s = cfg["acc_h"], min(1.0, s * cfg["acc_s"])
    else:
        return hx
    r, g, b = colorsys.hls_to_rgb(deg / 360, l, s)
    return "%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))

css = "/* Generated by tools/build_pages.py: illustration colours for each theme. */\n"
for tid, cfg in THEME_COLORS.items():
    css += f"[data-theme={tid}]{{" + "".join(f"--c-{hx}:#{shift(hx, cfg)};" for hx in sorted(_ALL_HEX)) + "}\n"
(OUT / "themes.css").write_text(css, encoding="utf-8")

# ============================================================== config + images
(OUT / "config.js").write_text(
    "/* Generated by tools/build_pages.py - edit BRAND/DOMAIN/EMAIL there. */\n"
    f"window.SITE = {json.dumps({'name': BRAND, 'domain': DOMAIN, 'email': EMAIL, 'tagline': TAGLINE})};\n",
    encoding="utf-8")

hero = (IMG / "hero-scene.svg").read_text(encoding="utf-8")
inner = hero[hero.index(">") + 1: hero.rindex("</svg>")]
og_svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630" fill="none" font-family="Poppins, Arial, Helvetica, sans-serif">
<defs><linearGradient id="ogbg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#13233f"/><stop offset="1" stop-color="#2a4d7c"/></linearGradient>
<linearGradient id="oggold" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#e0ad4a"/><stop offset="1" stop-color="#b8862b"/></linearGradient></defs>
<rect width="1200" height="630" fill="url(#ogbg)"/>
<g transform="translate(70 66) scale(1.1)"><rect width="64" height="64" rx="15" fill="#fff" opacity=".12"/><path d="M19 12h19l9 9v31a3 3 0 0 1-3 3H19a3 3 0 0 1-3-3V15a3 3 0 0 1 3-3z" fill="#fff"/><path d="M26 31v14l12-7z" fill="url(#oggold)"/><rect x="22" y="23" width="14" height="3" rx="1.5" fill="#1f3a5f"/></g>
<text x="160" y="116" font-size="46" font-weight="700" fill="#fff">{BRAND}</text>
<text x="70" y="270" font-size="58" font-weight="700" fill="#fff">Lecture videos into</text>
<text x="70" y="345" font-size="58" font-weight="700" fill="#fff">study notes in</text>
<text x="70" y="420" font-size="58" font-weight="700" fill="#e0ad4a">English or Urdu</text>
<text x="70" y="490" font-size="26" fill="#c9d6e8">Word notes from any YouTube video</text>
<text x="70" y="570" font-size="28" font-weight="600" fill="#e0ad4a">{DOMAIN}</text>
<svg x="610" y="90" width="560" height="455" viewBox="0 0 640 520" fill="none">{inner}</svg>
</svg>"""
(IMG / "og-image.svg").write_text(og_svg, encoding="utf-8")
try:
    import cairosvg
    cairosvg.svg2png(bytestring=og_svg.encode(), write_to=str(IMG / "og-image.png"), output_width=1200)
    cairosvg.svg2png(url=str(IMG / "logo.svg"), write_to=str(IMG / "apple-touch-icon.png"), output_width=180)
    print("images: og-image.png and apple-touch-icon.png generated")
except Exception as e:  # cairosvg missing - keep any existing PNGs
    print("note: cairosvg not available, social image not regenerated (pip install cairosvg):", type(e).__name__)

print(f"pages built for {BRAND} ({DOMAIN})")
