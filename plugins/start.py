import random
import os
import asyncio
import humanize
import time
import uuid
import json
from datetime import datetime, timedelta, timezone
from typing import Tuple
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
import base64
from pymongo import MongoClient
from pyrogram import Client, filters, __version__
from pyrogram.enums import ParseMode
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait, UserIsBlocked, InputUserDeactivated, ChannelInvalid, PeerIdInvalid, ChatAdminRequired
from pyrogram.errors.exceptions.bad_request_400 import BadRequest
from bot import Bot
from config import *
from helper_func import subscribed, encode_link, decode_link, get_messages
from database.database import add_user, del_user, full_userbase, present_user, add_special_message, remove_special_message, get_special_messages, get_all_special_messages, add_scheduled_broadcast, get_active_scheduled_broadcasts, deactivate_scheduled_broadcast, delete_scheduled_broadcast, get_schedule_by_id, update_schedule_start_time
from collections import defaultdict
from urllib.parse import quote

# MongoDB connection for educators
mongo_client = MongoClient("mongodb+srv://elvishyadav_opm:naman1811421@cluster0.uxuplor.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")
db = mongo_client["unacademy_db"]
educators_col = db["educators"]

# Encryption keys (matching JavaScript)
ENCRYPTION_KEY = bytes.fromhex('0123456789abcdef0123456789abcdef')  # 16 bytes for AES-256-CBC
IV = b'abcdef9876543210'  # 16 bytes, raw bytes

DECRYPT_URL_BASE = "https://dekhosekdop.onrender.com/op?data="

# Different delete times for different access types
BULK_DELETE_TIME = FILE_AUTO_DELETE
try:
    INDIVIDUAL_DELETE_TIME = INDIVIDUAL_AUTO_DELETE
except NameError:
    INDIVIDUAL_DELETE_TIME = FILE_AUTO_DELETE

codeflixbots = FILE_AUTO_DELETE
subaru = codeflixbots
file_auto_delete = humanize.naturaldelta(subaru)

# Global dictionary to track scheduled broadcast tasks
scheduled_broadcast_tasks = {}

def encrypt_json_item(data: dict) -> str:
    """
    Encrypt a single JSON object using AES-256-CBC.
    Returns IV:encrypted_data (both Base64-encoded, separated by ':').
    """
    json_str = json.dumps(data)
    cipher = AES.new(ENCRYPTION_KEY, AES.MODE_CBC, IV)
    padded_data = pad(json_str.encode('utf-8'), AES.block_size)
    encrypted_data = cipher.encrypt(padded_data)
    iv_base64 = base64.b64encode(IV).decode('utf-8')
    encrypted_base64 = base64.b64encode(encrypted_data).decode('utf-8')
    return f"{encrypted_base64}"

def decrypt_json_item(encrypted_str: str) -> dict:
    if ':' not in encrypted_str:
        raise ValueError("Invalid format")
    iv_base64, encrypted_base64 = encrypted_str.split(':', 1)
    iv = base64.b64decode(iv_base64)
    encrypted_data = base64.b64decode(encrypted_base64)
    cipher = AES.new(ENCRYPTION_KEY, AES.MODE_CBC, iv)
    padded_data = cipher.decrypt(encrypted_data)
    json_str = unpad(padded_data, AES.block_size).decode('utf-8')
    return json.loads(json_str)

async def download_and_decrypt_json(client: Client, msg) -> Tuple[str, str]:
    if not msg.document or not msg.document.file_name.endswith('.json'):
        raise ValueError("Not a JSON file")
    path = await client.download_media(msg)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            encrypted_data = json.load(f)
        decrypted_items = []
        if isinstance(encrypted_data, list):
            for item in encrypted_data:
                if 'encrypted_data' in item:
                    enc_str = item['encrypted_data']
                    if enc_str.startswith(DECRYPT_URL_BASE):
                        enc_str = enc_str[len(DECRYPT_URL_BASE):]
                    decrypted_item = decrypt_json_item(enc_str)
                    decrypted_items.append(decrypted_item)
                else:
                    decrypted_items.append(item)
        else:
            decrypted_items.append(encrypted_data)
        decrypted_json = json.dumps(decrypted_items)
        filename = msg.document.file_name
        return decrypted_json, filename
    finally:
        os.remove(path)

def generate_html_from_decrypted(decrypted_json_str: str, batch_title: str, batch_thumbnail: str) -> str:
    json_data_str = decrypted_json_str.replace("False", "false")
    json_data = json.loads(json_data_str)
    classes = []
    for item in json_data:
        encrypted_item = encrypt_json_item(item)
        url_wrapped = f"{DECRYPT_URL_BASE}{quote(encrypted_item)}"
        live_at_time = item['live_at_time']
        if live_at_time.endswith('Z'):
            dt = datetime.fromisoformat(live_at_time[:-1] + '+00:00')
        else:
            dt = datetime.fromisoformat(live_at_time)
        date_str = dt.strftime('%Y-%m-%d')
        month_str = dt.strftime('%Y-%m')
        link = url_wrapped
        classes.append({
            'class_name': item['class_name'],
            'teacher_name': item['teacher_name'],
            'date_str': date_str,
            'month_str': month_str,
            'thumbnail': item['thumbnail'],
            'link': link
        })
    day_groups = defaultdict(list)
    teacher_groups = defaultdict(list)
    month_groups = defaultdict(list)
    for c in classes:
        day_groups[c['date_str']].append(c)
        teacher_groups[c['teacher_name']].append(c)
        month_groups[c['month_str']].append(c)
    sorted_days = sorted(day_groups.keys(), reverse=True)
    sorted_teachers = sorted(teacher_groups.keys())
    sorted_months = sorted(month_groups.keys(), reverse=True)
    def generate_chips(lectures):
        chips = ''
        for lec in lectures:
            target = ' target="_blank"' if lec['link'] != '#' else ''
            chips += f'''
                <a href="{lec["link"]}"{target} class="chip" title="{lec["class_name"]}">
                  <div class="chip-left">
                    <img src="{lec["thumbnail"]}?q=100&w=48&h=48&fit=crop" alt="{lec["teacher_name"]}" class="icon" loading="lazy">
                    <span class="label">{lec["class_name"]}</span>
                  </div>
                  <div class="meta-container">
                    <span class="meta">Teacher: {lec["teacher_name"]}</span>
                    <span class="meta">Date: {lec["date_str"]}</span>
                  </div>
                </a>
            '''
        return chips
    def generate_section(section_id, title_id, title, content_id, chips, hidden_class=''):
        return f'''
            <section class="section-card{hidden_class}" id="{section_id}" aria-labelledby="{title_id}">
              <div class="section-head">
                <h2 class="section-title" id="{title_id}">{title}</h2>
                <button class="collapse-btn" data-collapse="{section_id}">
                  <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true" fill="none"><path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
                  <span>Collapse</span>
                </button>
              </div>
              <div class="chip-grid" id="{content_id}">
                {chips}
              </div>
            </section>
        '''
    day_sections = ''
    for i, date in enumerate(sorted_days):
        section_id = f"day-wise-{date.replace('-', '')}"
        title_id = f"day-wise-title-{date.replace('-', '')}"
        content_id = f"{section_id}-content"
        chips = generate_chips(day_groups[date])
        day_sections += generate_section(section_id, title_id, date, content_id, chips)
    teacher_sections = ''
    for i, teacher in enumerate(sorted_teachers):
        section_id = f"teacher-wise-{teacher.replace(' ', '_')}"
        title_id = f"teacher-wise-title-{teacher.replace(' ', '_')}"
        content_id = f"{section_id}-content"
        chips = generate_chips(teacher_groups[teacher])
        hidden = ' hidden' if i > 0 else ''
        teacher_sections += generate_section(section_id, title_id, teacher, content_id, chips, hidden)
    month_sections = ''
    for i, month in enumerate(sorted_months):
        section_id = f"month-wise-{month}"
        title_id = f"month-wise-title-{month}"
        content_id = f"{section_id}-content"
        chips = generate_chips(month_groups[month])
        hidden = ' hidden'
        month_sections += generate_section(section_id, title_id, month, content_id, chips, hidden)
    notice_html = f'''
            <!-- Intro notice -->
            <section class="notice" aria-label="Welcome">
              <div class="title">
                <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true" fill="none">
                  <circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2"></circle>
                  <path d="M12 7v6" stroke="currentColor" stroke-width="2" stroke-linecap="round"></path>
                  <circle cx="12" cy="16.5" r="1" fill="currentColor"></circle>
                </svg>
                <span>Welcome to the {batch_title} catalog</span>
              </div>
              <div class="image-container">
                <img src="{batch_thumbnail}" alt="Batch Illustration">
                <div class="image-caption">{batch_title}</div>
              </div>
              <p>Browse lectures by day, teacher, or month using the tabs above.</p>
            </section>
    '''
    full_html = f'''<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{batch_title} — UI Clone</title>
    <meta name="description" content="An advanced UI clone of a {batch_title.lower()} catalog with glassmorphism and light/dark mode." />
    <style>
      :root {{
        /* Light mode colors */
        --primary: #3b82f6; /* Soft blue */
        --primary-dark: #1e40af;
        --accent: #2dd4bf; /* Teal */
        --accent-alt: #f59e0b; /* Amber */
        --bg: #f8fafc;
        --surface: rgba(255, 255, 255, 0.7); /* Glassmorphism */
        --text: #1e293b;
        --text-muted: #6b7280;
        --border: rgba(203, 213, 225, 0.5);
        --shadow: rgba(15, 23, 42, 0.08);
        --shadow-hover: rgba(15, 23, 42, 0.15);
        --backdrop: blur(8px);

        /* 3D and animation properties */
        --radius: 20px;
        --shadow-3d: 0 4px 12px var(--shadow), 0 8px 24px var(--shadow);
        --shadow-3d-hover: 0 6px 16px var(--shadow-hover), 0 12px 32px var(--shadow-hover);
        --ring: 0 0 0 3px color-mix(in oklab, var(--primary) 40%, transparent);
        --perspective: 1000px;
      }}

      /* Dark mode */
      [data-theme="dark"] {{
        --primary: #6366f1; /* Bright indigo */
        --primary-dark: #4f46e5;
        --accent: #22d3ee; /* Cyan */
        --accent-alt: #fb7185; /* Coral */
        --bg: #1e293b;
        --surface: rgba(51, 65, 85, 0.7); /* Glassmorphism */
        --text: #e2e8f0;
        --text-muted: #94a3b8;
        --border: rgba(71, 85, 105, 0.5);
        --shadow: rgba(0, 0, 0, 0.3);
        --shadow-hover: rgba(0, 0, 0, 0.5);
        --backdrop: blur(8px);
      }}

      /* Reset-ish */
      *,*::before,*::after {{ box-sizing: border-box }}
      html, body {{ height: 100% }}
      body {{
        margin: 0;
        font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Noto Sans, Ubuntu, Cantarell, Helvetica Neue, Arial, "Apple Color Emoji", "Segoe UI Emoji";
        color: var(--text);
        background: linear-gradient(135deg, var(--bg) 0%, color-mix(in oklab, var(--bg) 90%, var(--primary)) 100%);
        line-height: 1.5;
        -webkit-font-smoothing: antialiased;
        text-rendering: optimizeLegibility;
        transition: background 0.3s ease, color 0.3s ease;
      }}
      a {{ color: inherit; text-decoration: none }}
      img {{ max-width: 100%; display: block }}

      /* Header */
      header.site-header {{
        background: linear-gradient(180deg, var(--primary-dark), var(--primary));
        color: var(--text);
        position: sticky;
        top: 0;
        z-index: 50;
        box-shadow: var(--shadow-3d);
      }}
      .header-inner {{
        max-width: 1120px;
        margin-inline: auto;
        padding: 16px 20px;
        display: flex;
        justify-content: space-between;
        align-items: center;
      }}
      .brand {{
        display: inline-flex;
        align-items: center;
        gap: 12px;
        font-weight: 700;
        letter-spacing: .3px;
        font-size: clamp(14px, 2.5vw, 18px);
      }}
      .brand svg {{ flex: none }}
      .theme-toggle {{
        background: var(--surface);
        backdrop-filter: var(--backdrop);
        border: 1px solid var(--border);
        cursor: pointer;
        padding: 8px;
        border-radius: 12px;
        color: var(--text);
        display: flex;
        align-items: center;
        transition: transform 0.2s ease;
      }}
      .theme-toggle:hover {{ transform: scale(1.1); }}
      .theme-toggle:focus-visible {{ outline: none; box-shadow: var(--ring); }}

      /* Tab bar */
      .tab-bar {{
        background: var(--surface);
        backdrop-filter: var(--backdrop);
        border-bottom: 1px solid var(--border);
        position: sticky;
        top: 64px;
        z-index: 40;
        box-shadow: var(--shadow-3d);
      }}
      .tabs {{
        max-width: 1120px;
        margin: 0 auto;
        padding: 12px;
        display: flex;
        justify-content: space-around;
        gap: 12px;
      }}
      .tab {{
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 12px 20px;
        border-radius: 12px;
        background: var(--surface);
        backdrop-filter: var(--backdrop);
        border: 1px solid var(--border);
        color: var(--text);
        font-size: clamp(13px, 2vw, 15px);
        font-weight: 600;
        cursor: pointer;
        flex: 1;
        text-align: center;
        box-shadow: inset 0 2px 4px rgba(255, 255, 255, 0.1), inset 0 -2px 4px var(--shadow);
        transition: transform 0.3s ease, box-shadow 0.3s ease, background 0.3s ease;
      }}
      .tab:hover {{
        transform: translateY(-2px) scale(1.03);
        box-shadow: var(--shadow-3d-hover);
      }}
      .tab[aria-current="true"] {{
        background: var(--primary);
        color: var(--text);
        border-color: var(--primary-dark);
        box-shadow: var(--shadow-3d);
      }}
      .tab:focus-visible {{ outline: none; box-shadow: var(--ring); }}

      /* Main container and sections */
      main {{ padding: 28px 14px 64px; }}
      .wrap {{ max-width: 1120px; margin-inline: auto; display: grid; gap: 24px; }}

      .notice {{
        background: var(--surface);
        backdrop-filter: var(--backdrop);
        border: 1px solid var(--border);
        border-radius: var(--radius);
        padding: 16px;
        box-shadow: var(--shadow-3d);
        text-align: center;
        transform: perspective(var(--perspective)) rotateX(1deg);
        transition: transform 0.3s ease, box-shadow 0.3s ease;
      }}
      .notice:hover {{
        transform: perspective(var(--perspective)) rotateX(0deg) translateY(-4px);
        box-shadow: var(--shadow-3d-hover);
      }}
      .notice .title {{
        display: flex; align-items: center; gap: 10px; font-weight: 600; color: var(--primary); justify-content: center;
      }}
      .notice .image-container {{
        margin: 12px auto;
        max-width: min(90vw, 400px);
        width: 100%;
      }}
      .notice .image-container img {{
        border-radius: var(--radius);
        width: 100%;
        height: auto;
        box-shadow: var(--shadow-3d);
      }}
      .notice .image-caption {{
        font-size: clamp(12px, 2vw, 13px);
        color: var(--text-muted);
        margin-top: 8px;
      }}
      .notice p {{ margin: 6px 0 0; font-size: clamp(12px, 2vw, 13px); color: var(--text-muted); }}

      .section-card {{
        background: var(--surface);
        backdrop-filter: var(--backdrop);
        border-radius: var(--radius);
        border: 1px solid var(--border);
        box-shadow: var(--shadow-3d);
        overflow: clip;
        transform: perspective(var(--perspective)) rotateX(1deg);
        transition: transform 0.3s ease, box-shadow 0.3s ease;
      }}
      .section-card:hover {{
        transform: perspective(var(--perspective)) rotateX(0deg) translateY(-4px);
        box-shadow: var(--shadow-3d-hover);
      }}
      .section-head {{
        display: flex; align-items: center; justify-content: space-between;
        padding: 12px 16px;
        background: linear-gradient(180deg, color-mix(in oklab, var(--primary) 15%, var(--surface)), var(--surface));
        border-bottom: 1px solid var(--border);
      }}
      .section-title {{
        font-size: clamp(12px, 2vw, 14px); font-weight: 700; color: var(--primary);
      }}
      .collapse-btn {{
        display: inline-flex; align-items: center; gap: 6px;
        font-size: 12px; color: var(--text-muted);
        background: transparent; border: none; padding: 6px 8px; border-radius: 8px; cursor: pointer;
      }}
      .collapse-btn:hover {{ background: color-mix(in oklab, var(--primary) 10%, var(--surface)); }}
      .collapse-btn:focus-visible {{ outline: none; box-shadow: var(--ring); }}

      .chip-grid {{
        padding: 16px;
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 12px;
      }}
      @media (max-width: 980px) {{
        .chip-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      }}
      @media (max-width: 620px) {{
        .chip-grid {{ grid-template-columns: 1fr; }}
      }}

      .chip {{
        --tone: color-mix(in oklab, var(--primary) 10%, var(--surface));
        --tone-bd: var(--border);
        display: flex; flex-direction: column; gap: 10px;
        border: 1px solid var(--tone-bd);
        background: var(--tone);
        backdrop-filter: var(--backdrop);
        border-radius: 12px;
        padding: 12px;
        min-height: 80px;
        transform: perspective(var(--perspective)) rotateY(2deg);
        transition: transform 0.3s ease, box-shadow 0.3s ease, opacity 0.3s ease;
      }}
      .chip:hover {{
        transform: perspective(var(--perspective)) rotateY(0deg) scale(1.02);
        box-shadow: var(--shadow-3d-hover);
        opacity: 0.95;
      }}
      .chip-left {{ display: flex; align-items: center; gap: 12px; overflow: hidden; flex-wrap: wrap; }}
      .chip .icon {{
        width: 28px; height: 28px; border-radius: 999px;
        display: grid; place-items: center;
        color: var(--primary-dark);
        background: color-mix(in oklab, var(--accent) 20%, var(--surface));
        border: 1px solid var(--border);
        flex: none;
        box-shadow: var(--shadow-3d);
        object-fit: cover;
      }}
      .chip .label {{
        font-size: clamp(11px, 2.5vw, 13px); color: var(--text); font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: normal; word-break: break-word; line-height: 1.3;
        flex: 1;
      }}
      .chip .meta-container {{
        display: flex; justify-content: space-between; gap: 8px; flex-wrap: wrap;
      }}
      .chip .meta {{
        font-size: clamp(10px, 2vw, 11px);
        color: var(--text-muted);
        padding: 4px 10px; border-radius: 999px;
        background: color-mix(in oklab, var(--accent-alt) 15%, var(--surface));
        border: 1px solid var(--border);
      }}

      /* Footer */
      footer {{
        text-align: center;
        color: var(--text-muted);
        font-size: 12px;
        padding: 28px 12px 48px;
      }}

      /* Utilities */
      .sr-only {{
        position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0;
      }}
      .hidden {{ display: none !important; }}
    </style>
</head>

<body>

  <header class="site-header">

    <div class="header-inner">

      <div class="brand">

        <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true" fill="none">

          <rect x="3" y="3" width="18" height="18" rx="4" stroke="currentColor" stroke-width="2" opacity=".9"></rect>

          <circle cx="12" cy="12" r="3.2" fill="currentColor"></circle>

        </svg>

        <span>{batch_title}</span>

      </div>

      <div class="controls">

        <button class="theme-toggle" aria-label="Toggle theme">

          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">

            <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path>

          </svg>

        </button>

      </div>

    </div>

  </header>



  <div class="tab-bar">

    <div class="tabs" id="tabs">

      <button class="tab" data-target="day-wise" aria-current="true">Day Wise</button>

      <button class="tab" data-target="teacher-wise">Teacher Wise</button>

      <button class="tab" data-target="month-wise">Month Wise</button>

    </div>

  </div>



  <main id="main">

    <div class="wrap">

{notice_html}

{day_sections}

{teacher_sections}

{month_sections}

      <footer>

        UI built as an advanced glassmorphic demo with light/dark mode for educational purposes.

      </footer>

    </div>

  </main>



    <script>
      // Theme toggle
      const themeToggle = document.querySelector('.theme-toggle');
      const html = document.documentElement;
      const currentTheme = localStorage.getItem('theme') || 'light';
      html.dataset.theme = currentTheme;

      themeToggle.addEventListener('click', () => {{
        const newTheme = html.dataset.theme === 'light' ? 'dark' : 'light';
        html.dataset.theme = newTheme;
        localStorage.setItem('theme', newTheme);
        themeToggle.querySelector('svg').innerHTML = newTheme === 'light'
          ? '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path>'
          : '<circle cx="12" cy="12" r="5" /><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>';
      }});

      // Tab navigation
      const tabs = document.getElementById('tabs');
      const tabButtons = Array.from(tabs.querySelectorAll('.tab'));
      const sections = document.querySelectorAll('.section-card');

      function setActiveTab(targetId) {{
        tabButtons.forEach(btn => btn.setAttribute('aria-current', String(btn.dataset.target === targetId)));
        sections.forEach(section => {{
          section.classList.toggle('hidden', !section.id.startsWith(targetId));
        }});
      }}

      tabButtons.forEach(tab => {{
        tab.addEventListener('click', () => {{
          setActiveTab(tab.dataset.target);
          const firstSection = document.querySelector(`[id^="${{tab.dataset.target}}"]`);
          if (firstSection) {{
            const top = firstSection.getBoundingClientRect().top + window.scrollY - 100;
            window.scrollTo({{ top, behavior: 'smooth' }});
          }}
        }});
      }});

      // Initialize first tab as active
      setActiveTab('day-wise');

      // Collapsible sections
      document.querySelectorAll('.collapse-btn').forEach(btn => {{
        const key = `collapse:${{btn.dataset.collapse}}`;
        const content = document.getElementById(`${{btn.dataset.collapse}}-content`);
        const svg = btn.querySelector('svg');
        const span = btn.querySelector('span');

        // Check if content exists to prevent errors
        if (!content) {{
          console.warn(`Content element for ${{btn.dataset.collapse}} not found`);
          return;
        }}

        // Initialize state from localStorage
        const state = localStorage.getItem(key);
        content.classList.remove('hidden'); // Ensure initial visibility
        if (state === 'closed') {{
          content.classList.add('hidden');
          span.textContent = 'Expand';
          svg.style.transform = 'rotate(180deg)';
        }} else {{
          span.textContent = 'Collapse';
          svg.style.transform = 'none';
        }}

        btn.addEventListener('click', () => {{
          content.classList.toggle('hidden');
          const isClosed = content.classList.contains('hidden');
          span.textContent = isClosed ? 'Expand' : 'Collapse';
          svg.style.transform = isClosed ? 'rotate(180deg)' : 'none';
          localStorage.setItem(key, isClosed ? 'closed' : 'open');
        }});
      }});

      // Keyboard a11y: jump to main content
      window.addEventListener('keydown', (e) => {{
        if (e.key === '/' && !/input|textarea|select/i.test((document.activeElement || {{}}).tagName || '')) {{
          e.preventDefault();
          document.getElementById('main')?.focus({{ preventScroll: true }});
          window.scrollTo({{ top: 0, behavior: 'smooth' }});
        }}
      }});
    </script>
</body>

</html>'''

    return full_html

async def upload_html(client: Client, html_content: str, batch_title: str, chat_id: int, custom_caption: str = None) -> Message:
    temp_filename = f"{batch_title.replace(' ', '_').lower()}_{uuid.uuid4().hex}.html"
    with open(temp_filename, 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    caption = custom_caption or f"Generated HTML Catalog: {batch_title}.html\nOpen in browser to view the interactive catalog! 😁"
    
    sent_msg = await client.send_document(
        chat_id=chat_id,
        document=temp_filename,
        caption=caption
    )
    
    try:
        os.remove(temp_filename)
    except Exception as e:
        print(f"Failed to delete temporary file {temp_filename}: {e}")
    
    return sent_msg

async def download_and_encrypt_json(client: Client, msg: Message, user_first_name: str, user_id: str, made_at: str) -> Tuple[str, str]:
    """
    Download a .json file, add user details, encrypt each item, and return as a JSON array.
    Returns the JSON array of encrypted strings and the original filename.
    """
    if not msg.document or not msg.document.file_name.endswith('.json'):
        raise ValueError("Message does not contain a .json file")

    # Download the JSON file
    path = await client.download_media(msg)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            json_data = json.load(f)

        encrypted_items = []
        # Handle both single object and array
        if isinstance(json_data, list):
            for item in json_data:
                item['user_first_name'] = user_first_name
                item['user_id'] = user_id
                item['made_at'] = made_at
                encrypted_item = encrypt_json_item(item)
                encrypted_items.append({"encrypted_data": encrypted_item})
        else:
            json_data['user_first_name'] = user_first_name
            json_data['user_id'] = user_id
            json_data['made_at'] = made_at
            encrypted_item = encrypt_json_item(json_data)
            encrypted_items.append({"encrypted_data": encrypted_item})

        # Convert the list of encrypted items to a JSON string
        encrypted_json = json.dumps(encrypted_items)
        filename = msg.document.file_name
        return encrypted_json, filename
    finally:
        try:
            os.remove(path)
        except Exception as e:
            print(f"Failed to delete temporary file {path}: {e}")

async def upload_encrypted_json(client: Client, encrypted_json: str, filename: str, chat_id: int) -> Message:
    """
    Upload encrypted JSON array as a .json file to the specified chat.
    Returns the sent message object.
    """
    temp_filename = f"encrypted_{uuid.uuid4().hex}.json"
    with open(temp_filename, 'w', encoding='utf-8') as f:
        f.write(encrypted_json)
    
    sent_msg = await client.send_document(
        chat_id=chat_id,
        document=temp_filename,
        caption=f"Encrypted JSON: {filename}",
        parse_mode=ParseMode.HTML
    )
    
    try:
        os.remove(temp_filename)
    except Exception as e:
        print(f"Failed to delete temporary file {temp_filename}: {e}")
    
    return sent_msg

async def send_random_special_message(client: Client, chat_id: int):
    """
    Send a random special message (sticker, photo, video, document, text, etc.) to the specified chat.
    Returns the sent message object or None if no message was sent.
    """
    bot_id = client.username
    special_msg_ids = await get_special_messages(bot_id)
    if not special_msg_ids:
        print(f"No special messages found for bot {bot_id}")
        return None

    random_msg_id = random.choice(special_msg_ids)
    try:
        special_msg = await client.get_messages(client.db_channel.id, random_msg_id)
        if not special_msg:
            print(f"Special message {random_msg_id} not found in channel {client.db_channel.id} for bot {bot_id}")
            return None

        caption = f"<b>{special_msg.caption.html}</b>" if special_msg.caption else None

        if special_msg.sticker:
            special_copied_msg = await client.send_sticker(
                chat_id=chat_id,
                sticker=special_msg.sticker.file_id
            )
        elif special_msg.photo:
            special_copied_msg = await client.send_photo(
                chat_id=chat_id,
                photo=special_msg.photo.file_id,
                caption=caption,
                parse_mode=ParseMode.HTML
            )
        elif special_msg.video:
            special_copied_msg = await client.send_video(
                chat_id=chat_id,
                video=special_msg.video.file_id,
                caption=caption,
                parse_mode=ParseMode.HTML
            )
        elif special_msg.document:
            special_copied_msg = await client.send_document(
                chat_id=chat_id,
                document=special_msg.document.file_id,
                caption=caption,
                parse_mode=ParseMode.HTML
            )
        elif special_msg.text:
            special_copied_msg = await client.send_message(
                chat_id=chat_id,
                text=caption or special_msg.text,
                parse_mode=ParseMode.HTML
            )
        elif special_msg.audio:
            special_copied_msg = await client.send_audio(
                chat_id=chat_id,
                audio=special_msg.audio.file_id,
                caption=caption,
                parse_mode=ParseMode.HTML
            )
        elif special_msg.animation:
            special_copied_msg = await client.send_animation(
                chat_id=chat_id,
                animation=special_msg.animation.file_id,
                caption=caption,
                parse_mode=ParseMode.HTML
            )
        else:
            print(f"Unsupported message type for special message {random_msg_id} for bot {bot_id}")
            return None

        return special_copied_msg
    except (ChannelInvalid, PeerIdInvalid, BadRequest, Exception) as e:
        print(f"Failed to fetch/send special message {random_msg_id} for bot {bot_id}: {e}")
        return None

async def perform_broadcast_cycle(client: Client, chat_id: int, msg_id: int, delete_after: int, schedule_id: str, admin_chat_id: int):
    """
    Perform one cycle of broadcasting and schedule deletion.
    Sends stats to admin chat after cycle.
    """
    query = await full_userbase()
    sent_messages = []
    total = 0
    successful = 0
    blocked = 0
    deleted = 0
    unsuccessful = 0

    try:
        broadcast_msg = await client.get_messages(chat_id, msg_id)
        if not broadcast_msg:
            print(f"Broadcast message {msg_id} in chat {chat_id} not found.")
            return
    except Exception as e:
        print(f"Error fetching broadcast message {msg_id}: {e}")
        return

    for user_id in query:
        try:
            sent = await broadcast_msg.copy(user_id)
            sent_messages.append((user_id, sent.id))
            successful += 1
        except FloodWait as e:
            await asyncio.sleep(e.x)
            try:
                sent = await broadcast_msg.copy(user_id)
                sent_messages.append((user_id, sent.id))
                successful += 1
            except Exception:
                unsuccessful += 1
        except UserIsBlocked:
            await del_user(user_id)
            blocked += 1
        except InputUserDeactivated:
            await del_user(user_id)
            deleted += 1
        except Exception:
            unsuccessful += 1
        total += 1

    print(f"Broadcast cycle for {schedule_id}: {successful}/{total} successful")

    stats_msg = f"""<b>📊 Broadcast Cycle Stats for ID: {schedule_id}</b>

ᴛᴏᴛᴀʟ ᴜꜱᴇʀꜱ: <code>{total}</code>
ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟ: <code>{successful}</code>
ʙʟᴏᴄᴋᴇᴅ ᴜꜱᴇʀꜱ: <code>{blocked}</code>
ᴅᴇʟᴇᴛᴇᴅ ᴀᴄᴄᴏᴜɴᴛꜱ: <code>{deleted}</code>
ᴜɴꜱᴜᴄᴄᴇꜱꜱꜰᴜʟ: <code>{unsuccessful}</code>"""
    try:
        await client.send_message(admin_chat_id, stats_msg)
    except Exception as e:
        print(f"Failed to send stats to admin chat {admin_chat_id}: {e}")

    if delete_after > 0:
        await asyncio.sleep(delete_after)
        for user_id, sent_msg_id in sent_messages:
            try:
                await client.delete_messages(user_id, sent_msg_id)
            except Exception as e:
                print(f"Failed to delete broadcast message {sent_msg_id} in {user_id}: {e}")

async def start_scheduled_broadcast(client: Client, schedule_id: str):
    """
    Start the scheduled broadcast loop for a specific schedule.
    """
    schedule = await get_schedule_by_id(schedule_id)
    if not schedule:
        print(f"Schedule {schedule_id} not found.")
        return

    admin_chat_id = schedule['admin_chat_id']
    chat_id = schedule['chat_id']
    reply_msg_id = schedule['reply_msg_id']
    total_time = schedule['total_time']
    interval = schedule['interval']
    delete_after = schedule['delete_after']
    start_time = schedule['start_time']
    start_delay = schedule.get('start_delay', 0)

    if start_delay > 0:
        try:
            await client.send_message(admin_chat_id, f"⏳ Scheduled broadcast {schedule_id} will start in {humanize.naturaldelta(start_delay)}.")
            await asyncio.sleep(start_delay)
        except Exception as e:
            print(f"Failed to notify admin chat {admin_chat_id} about start delay: {e}")

    while True:
        current_schedule = await get_schedule_by_id(schedule_id)
        if not current_schedule or not current_schedule.get('active', False):
            print(f"Scheduled broadcast {schedule_id} deactivated or not found.")
            break

        current_time = time.time()
        elapsed = current_time - start_time - start_delay

        if elapsed >= total_time:
            await deactivate_scheduled_broadcast(schedule_id)
            try:
                await client.send_message(admin_chat_id, f"✅ Scheduled broadcast {schedule_id} ended (total time reached).")
            except Exception as e:
                print(f"Failed to notify admin chat {admin_chat_id}: {e}")
            print(f"Scheduled broadcast {schedule_id} ended (total time reached).")
            break

        await perform_broadcast_cycle(client, chat_id, reply_msg_id, delete_after, schedule_id, admin_chat_id)
        await asyncio.sleep(interval)

async def process_message_for_sending(client: Client, msg: Message, user_id: int, caption: str, reply_markup: InlineKeyboardMarkup, protect_content: bool):
    """
    Process and send a message to a user, handling media download with fallback for non-anonymous group messages.
    Returns the sent message or None if sending fails.
    """
    path = None
    copied_msg = None
    try:
        if msg.video or msg.document or msg.photo or msg.audio or msg.animation or msg.sticker:
            try:
                path = await client.download_media(msg)
            except Exception as e:
                print(f"Direct download failed for message {msg.id}: {e}")
                try:
                    forwarded = await msg.forward(client.db_channel.id, as_copy=False)
                    copied_in_dump = await forwarded.copy(client.db_channel.id)
                    path = await client.download_media(copied_in_dump)
                    await forwarded.delete()
                    await copied_in_dump.delete()
                except Exception as fallback_e:
                    print(f"Fallback download failed for message {msg.id}: {fallback_e}")
                    return None

        if msg.sticker:
            copied_msg = await client.send_sticker(
                chat_id=user_id,
                sticker=path if path else msg.sticker.file_id,
                protect_content=protect_content
            )
        elif msg.photo:
            copied_msg = await client.send_photo(
                chat_id=user_id,
                photo=path if path else msg.photo.file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                protect_content=protect_content
            )
        elif msg.video:
            copied_msg = await client.send_video(
                chat_id=user_id,
                video=path if path else msg.video.file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                protect_content=protect_content
            )
        elif msg.document:
            copied_msg = await client.send_document(
                chat_id=user_id,
                document=path if path else msg.document.file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                protect_content=protect_content
            )
        elif msg.audio:
            copied_msg = await client.send_audio(
                chat_id=user_id,
                audio=path if path else msg.audio.file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                protect_content=protect_content
            )
        elif msg.animation:
            copied_msg = await client.send_animation(
                chat_id=user_id,
                animation=path if path else msg.animation.file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                protect_content=protect_content
            )
        elif msg.text:
            copied_msg = await client.send_message(
                chat_id=user_id,
                text=msg.text.html,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
        else:
            print(f"Unsupported message type for message {msg.id}")
            return None

        return copied_msg
    except FloodWait as e:
        await asyncio.sleep(e.x)
        try:
            if msg.sticker:
                copied_msg = await client.send_sticker(
                    chat_id=user_id,
                    sticker=path if path else msg.sticker.file_id,
                    protect_content=protect_content
                )
            elif msg.photo:
                copied_msg = await client.send_photo(
                    chat_id=user_id,
                    photo=path if path else msg.photo.file_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                    protect_content=protect_content
                )
            elif msg.video:
                copied_msg = await client.send_video(
                    chat_id=user_id,
                    video=path if path else msg.video.file_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                    protect_content=protect_content
                )
            elif msg.document:
                copied_msg = await client.send_document(
                    chat_id=user_id,
                    document=path if path else msg.document.file_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                    protect_content=protect_content
                )
            elif msg.audio:
                copied_msg = await client.send_audio(
                    chat_id=user_id,
                    audio=path if path else msg.audio.file_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                    protect_content=protect_content
                )
            elif msg.animation:
                copied_msg = await client.send_animation(
                    chat_id=user_id,
                    animation=path if path else msg.animation.file_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                    protect_content=protect_content
                )
            elif msg.text:
                copied_msg = await client.send_message(
                    chat_id=user_id,
                    text=msg.text.html,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup
                )
            return copied_msg
        except Exception as retry_e:
            print(f"Retry failed for message {msg.id}: {retry_e}")
            return None
    except Exception as e:
        print(f"Failed to send message {msg.id}: {e}")
        return None
    finally:
        if path:
            try:
                os.remove(path)
            except Exception as e:
                print(f"Failed to delete local file {path}: {e}")

@Bot.on_message(filters.command('start') & filters.private & subscribed)
async def start_command(client: Client, message: Message):
    id = message.from_user.id
    text = message.text
    user_first_name = message.from_user.first_name
    user_id = str(message.from_user.id)
    made_at = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S+00:00')

    if len(text) > 7:
        try:
            base64_string = text.split(" ", 1)[1]
        except IndexError:
            await message.reply_text("❌ No link provided. Please provide a valid link.")
            return
        try:
            link_type, user_id_decoded, f_msg_id, channel_id, s_msg_id = await decode_link(base64_string)
            print(f"Decoded link: type={link_type}, user_id={user_id_decoded}, f_msg_id={f_msg_id}, channel_id={channel_id}, s_msg_id={s_msg_id}")
        except ValueError as e:
            # Assume it's a UID for batch/course
            uid = base64_string
            item = None
            for edu in educators_col.find():
                for course in edu.get('courses', []):
                    if course.get('uid') == uid:
                        item = course
                        break
                if item:
                    break
                for batch in edu.get('batches', []):
                    if batch.get('uid') == uid:
                        item = batch
                        break
                if item:
                    break
            if not item:
                await message.reply_text("❌ Invalid UID or link format.")
                return

            name = item['name']
            opthumbnail = item.get('cover_photo') or item.get('thumbnail')
            teachers = item['teachers']
            channel_id = item['channel_id']
            msg_id = item['channel_msg_id']

            temp_msg = await message.reply("𝗥𝘂𝗸 𝗘𝗸 𝗦𝗲𝗰 👽..")
            try:
                messages = await get_messages(client, [msg_id], channel_id)
                print(f"Fetched message for uid={uid}, channel_id={channel_id}, msg_id={msg_id}")
                if not messages or messages[0] is None:
                    await temp_msg.edit("Failed to fetch message. It may have been deleted or is inaccessible.")
                    return
            except (ChannelInvalid, PeerIdInvalid, BadRequest, Exception) as e:
                await temp_msg.edit(f"Something went wrong: {str(e)}")
                print(f"Error getting message {msg_id} from {channel_id}: {e}")
                return
            finally:
                await temp_msg.delete()

            codeflix_msgs = []
            msg = messages[0]
            if msg.document and msg.document.file_name.endswith('.json'):
                try:
                    decrypted_json, filename = await download_and_decrypt_json(client, msg)
                    batch_title = name
                    try:
                        data = json.loads(decrypted_json)
                        batch_thumbnail = data[0].get('thumbnail', thumbnail) if data else thumbnail
                    except:
                        batch_thumbnail = thumbnail
                    html_content = generate_html_from_decrypted(decrypted_json, batch_title, batch_thumbnail)
                    now_utc = datetime.now(timezone.utc)
                    valid_till = (now_utc + timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S UTC')
                    caption = f"Batch/ Course :- {name}\nTeachers :- {teachers}\nThis is valid till :- {valid_till}"
                    html_msg = await upload_html(client, html_content, batch_title, message.from_user.id, caption)
                    codeflix_msgs.append(html_msg)
                except ValueError as e:
                    await message.reply_text(f"❌ Error: {str(e)}")
                    return
                except Exception as e:
                    await message.reply_text(f"❌ Failed to generate HTML catalog: {str(e)}")
                    return
            else:
                await message.reply_text("❌ Expected JSON file for this UID.")
                return

            if codeflix_msgs:
                special_msg = await send_random_special_message(client, message.from_user.id)
                if special_msg:
                    codeflix_msgs.append(special_msg)

                k = await client.send_message(
                    chat_id=message.from_user.id,
                    text=f"<b>🔥 Hurry! This Catalog will be <u>deleted automatically in 24 hours</u> ⏳</b>\n\n"
                         f"<b>𝘚𝘰 𝘍𝘰𝘳 𝘚𝘢𝘷𝘪𝘯𝘨 𝘓𝘦𝘤𝘵𝘶𝘳𝘦/𝘗𝘥𝘧/𝘑𝘚𝘖𝘕 𝘤𝘭𝘪𝘤𝘬 𝘰𝘯 𝘣𝘦𝘭𝘰𝘸 𝘣𝘶𝘵𝘵𝘰𝘯(😁 𝗖𝗟𝗜𝗖𝗞 𝗧𝗢 𝗦𝗔𝗩𝗘 📥) then 𝘠𝘰𝘶 𝘤𝘢𝘯 𝘚𝘢𝘷𝘦 𝘪𝘯 𝘎𝘢𝘭𝘭𝘦𝘳𝘺 😊</b>\n\n"
                         f"<b>😎 Don’t worry! Even after deletion, you can still re-access everything anytime through our websites 😘</b>\n\n"
                         f"<b><a href='https://yashyasag.github.io/hiddens_officials'>🌟 𝗩𝗶𝘀𝗶𝘁 𝗠𝗼𝗿𝗲 𝗪𝗲𝗯𝘀𝗶𝘁𝗲𝘀 🌟</a></b>",
                )

                codeflix_msgs.append(k)
                asyncio.create_task(delete_files(codeflix_msgs, client, message, k, 24 * 3600))
            return
        except Exception as e:
            await message.reply_text(f"❌ Error decoding link: {str(e)}")
            return

        if link_type == "HACKHEIST":
            if message.from_user.id != int(user_id_decoded):
                await message.reply_text("❌ You are not authorized to access this content!")
                return
            
            temp_msg = await message.reply("𝗥𝘂𝗸 𝗘𝗸 𝗦𝗲𝗰 👽..")
            try:
                messages = await get_messages(client, [f_msg_id], channel_id)
                if not messages or all(msg is None for msg in messages):
                    await temp_msg.edit("Failed to fetch message. It may have been deleted or is inaccessible.")
                    return
            except (ChannelInvalid, PeerIdInvalid, BadRequest, Exception) as e:
                await temp_msg.edit(f"Something went wrong: {str(e)}")
                print(f"Error getting message {f_msg_id} from {channel_id}: {e}")
                return
            finally:
                await temp_msg.delete()

            codeflix_msgs = []
            for msg in messages:
                if not msg:
                    continue
                
                if msg.document and msg.document.file_name.endswith('.json'):
                    try:
                        encrypted_json, filename = await download_and_encrypt_json(client, msg, user_first_name, user_id, made_at)
                        encrypted_msg = await upload_encrypted_json(client, encrypted_json, filename, message.from_user.id)
                        codeflix_msgs.append(encrypted_msg)
                    except ValueError as e:
                        await message.reply_text(f"❌ Error: {str(e)}")
                        continue
                    except Exception as e:
                        await message.reply_text(f"❌ Failed to process JSON file: {str(e)}")
                        continue
                else:
                    filename = "Unknown"
                    media_type = "Unknown"
                    if msg.video:
                        media_type = "Video"
                        filename = msg.video.file_name if msg.video.file_name else "Unnamed Video"
                    elif msg.document:
                        filename = msg.document.file_name if msg.document.file_name else "Unnamed Document"
                        media_type = "PDF" if filename.endswith(".pdf") else "Document"
                    elif msg.photo:
                        media_type = "Image"
                        filename = "Image"
                    elif msg.text:
                        media_type = "Text"
                        filename = "Text Content"

                    caption = (
                        CUSTOM_CAPTION.format(
                            previouscaption=(msg.caption.html if msg.caption else "🔥 𝐇𝐈𝐃𝐃𝐄𝐍𝐒 🔥"),
                            filename=filename,
                            mediatype=media_type,
                        )
                        if bool(CUSTOM_CAPTION)
                        else (msg.caption.html if msg.caption else "")
                    )

                    reply_markup = msg.reply_markup if DISABLE_CHANNEL_BUTTON else None
                    protect_content = False

                    copied_msg = await process_message_for_sending(
                        client=client,
                        msg=msg,
                        user_id=message.from_user.id,
                        caption=caption,
                        reply_markup=reply_markup,
                        protect_content=protect_content
                    )

                    if copied_msg:
                        codeflix_msgs.append(copied_msg)
                    else:
                        await message.reply_text("❌ Failed to send the content!")

            if codeflix_msgs:
                special_msg = await send_random_special_message(client, message.from_user.id)
                if special_msg:
                    codeflix_msgs.append(special_msg)

                k = await client.send_message(
                    chat_id=message.from_user.id,
                    text=f"<b>‼️ 𝐓𝐡𝐢𝐬 𝐋𝐄𝐂𝐓𝐔𝐑𝐄/𝐏𝐃𝐅/𝐉𝐒𝐎𝐍 𝐰𝐢𝐥𝐥 𝐛𝐞 <u>𝗮𝘂𝘁𝗼-𝗱𝗲𝗹𝗲𝘁𝗲𝗱 𝗶𝗻 𝟯 𝗱𝗮𝘆𝘀</u> 💀</b>\n\n"
                         f"<b>⚡ Watch Lecture or Download now ✅ or Save it - Forward, Download & Keep in your Gallery before time runs out!</b>\n\n"
                         f"<b>🤝 Don’t forget—share with friends, knowledge grows when shared ❣️</b>\n\n"
                         f"<b>😎 Chill! Even after deletion, you can always re-access everything on our websites 😉</b>\n\n"
                         f"<b><a href='https://yashyasag.github.io/hiddens_officials'>✨ 𝗘𝘅𝗽𝗹𝗼𝗿𝗲 𝗠𝗼𝗿𝗲 𝗪𝗲𝗯𝘀𝗶𝘁𝗲𝘀 ✨</a></b>",
                )
                
                codeflix_msgs.append(k)
                asyncio.create_task(delete_files(codeflix_msgs, client, message, k, INDIVIDUAL_DELETE_TIME))
            return

        elif link_type == "batch":
            if s_msg_id is not None:
                if f_msg_id <= s_msg_id:
                    ids = list(range(f_msg_id, s_msg_id + 1))
                else:
                    ids = list(range(f_msg_id, s_msg_id - 1, -1))
            else:
                ids = [f_msg_id]

            temp_msg = await message.reply("𝗥𝘂𝗸 𝗘𝗸 𝗦𝗲𝗰 👽..")
            try:
                messages = await get_messages(client, ids, channel_id)
                print(f"Fetched {len(messages)} messages for channel_id={channel_id}, ids={ids}")
                if not messages or all(msg is None for msg in messages):
                    await temp_msg.edit("Failed to fetch messages. They may have been deleted or are inaccessible.")
                    return
            except (ChannelInvalid, PeerIdInvalid, BadRequest, Exception) as e:
                await temp_msg.edit(f"Something went wrong: {str(e)}")
                print(f"Error getting messages from {channel_id}: {e}")
                return
            finally:
                await temp_msg.delete()

            codeflix_msgs = []
            user_id = message.from_user.id
            
            for msg in messages:
                if not msg:
                    continue
                
                if msg.document and msg.document.file_name.endswith('.json'):
                    try:
                        decrypted_json, filename = await download_and_decrypt_json(client, msg)
                        batch_title = filename.replace('.json', '').replace('_', ' ').title()
                        try:
                            data = json.loads(decrypted_json)
                            batch_thumbnail = data[0].get('opthumbnail', 'https://via.placeholder.com/300x200?text=Catalog') if data else 'https://via.placeholder.com/300x200?text=Catalog'
                        except:
                            batch_thumbnail = 'https://via.placeholder.com/300x200?text=Catalog'
                        html_content = generate_html_from_decrypted(decrypted_json, batch_title, batch_thumbnail)
                        html_msg = await upload_html(client, html_content, batch_title, message.from_user.id)
                        codeflix_msgs.append(html_msg)
                    except ValueError as e:
                        await message.reply_text(f"❌ Error: {str(e)}")
                        continue
                    except Exception as e:
                        await message.reply_text(f"❌ Failed to generate HTML catalog: {str(e)}")
                        continue
                else:
                    filename = "Unknown"
                    media_type = "Unknown"
                    if msg.video:
                        media_type = "Video"
                        filename = msg.video.file_name if msg.video.file_name else "Unnamed Video"
                    elif msg.document:
                        filename = msg.document.file_name if msg.document.file_name else "Unnamed Document"
                        media_type = "PDF" if filename.endswith(".pdf") else "Document"
                    elif msg.photo:
                        media_type = "Image"
                        filename = "Image"
                    elif msg.text:
                        media_type = "Text"
                        filename = "Text Content"

                    caption = (
                        CUSTOM_CAPTION.format(
                            previouscaption=(msg.caption.html if msg.caption else "🔥 𝐇𝐈𝐃𝐃𝐄𝐍𝐒 🔥"),
                            filename=filename,
                            mediatype=media_type,
                        )
                        if bool(CUSTOM_CAPTION)
                        else (msg.caption.html if msg.caption else "")
                    )

                    base64_string2 = await encode_link(user_id=user_id, f_msg_id=msg.id, channel_id=channel_id)
                    individual_button = InlineKeyboardButton("😁 𝗖𝗟𝗜𝗖𝗞 𝗧𝗢 𝗦𝗔𝗩𝗘 📥", url=f"https://t.me/{client.username}?start={base64_string2}")

                    if DISABLE_CHANNEL_BUTTON:
                        reply_markup = None
                    elif msg.reply_markup:
                        if msg.reply_markup.inline_keyboard:
                            new_keyboard = msg.reply_markup.inline_keyboard.copy()
                            new_keyboard.append([individual_button])
                            reply_markup = InlineKeyboardMarkup(new_keyboard)
                        else:
                            reply_markup = InlineKeyboardMarkup([[individual_button]])
                    else:
                        reply_markup = InlineKeyboardMarkup([[individual_button]])

                    protect_content = PROTECT_CONTENT

                    copied_msg = await process_message_for_sending(
                        client=client,
                        msg=msg,
                        user_id=user_id,
                        caption=caption,
                        reply_markup=reply_markup,
                        protect_content=protect_content
                    )

                    if copied_msg:
                        codeflix_msgs.append(copied_msg)

            if codeflix_msgs:
                special_msg = await send_random_special_message(client, message.from_user.id)
                if special_msg:
                    codeflix_msgs.append(special_msg)

                k = await client.send_message(
                    chat_id=message.from_user.id,
                    text=f"<b>🔥 Hurry! These Lectures/PDFs/JSONs will be <u>deleted automatically in 4 hours</u> ⏳</b>\n\n"
                         f"<b>𝘚𝘰 𝘍𝘰𝘳 𝘚𝘢𝘷𝘪𝘯𝘨 𝘓𝘦𝘤𝘵𝘶𝘳𝘦/𝘗𝘥𝘧/𝘑𝘚𝘖𝘕 𝘤𝘭𝘪𝘤𝘬 𝘰𝘯 𝘣𝘦𝘭𝘰𝘸 𝘣𝘶𝘵𝘵𝘰𝘯(😁 𝗖𝗟𝗜𝗖𝗞 𝗧𝗢 𝗦𝗔𝗩𝗘 📥) then 𝘠𝘰𝘶 𝘤𝘢𝘯 𝘚𝘢𝘷𝘦 𝘪𝘯 𝘎𝘢𝘭𝘭𝘦𝘳𝘺 😊</b>\n\n"
                         f"<b>😎 Don’t worry! Even after deletion, you can still re-access everything anytime through our websites 😘</b>\n\n"
                         f"<b><a href='https://yashyasag.github.io/hiddens_officials'>🌟 𝗩𝗶𝘀𝗶𝘁 𝗠𝗼𝗿𝗲 𝗪𝗲𝗯𝘀𝗶𝘁𝗲𝘀 🌟</a></b>",
                )

                codeflix_msgs.append(k)
                asyncio.create_task(delete_files(codeflix_msgs, client, message, k, BULK_DELETE_TIME))
            return

    # Default /start behavior: Inform user to provide a link
    reply_markup = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🔥 �_M𝗔𝗜𝗡 𝗪𝗘𝗕𝗦𝗜𝗧𝗘 🔥", url="https://yashyasag.github.io/hiddens_officials")
        ],[
            InlineKeyboardButton("‼️ 𝗕𝗔𝗖𝗞𝗨𝗣 𝗖𝗛𝗔𝗡𝗡𝗘𝗟 ‼️", url="https://t.me/+Sk3pfX_PWTQ3NmI1")
        ],[
            InlineKeyboardButton("👻 ᴄᴏɴᴛᴀᴄᴛ ᴜs 👻", url="https://t.me/TEAM_HIDDENS_BOT")
        ]]
    )
    await message.reply_text(
        text=START_MSG.format(
            first=message.from_user.first_name,
            last=message.from_user.last_name,
            username=None if not message.from_user.username else '@' + message.from_user.username,
            mention=message.from_user.mention,
            id=message.from_user.id
        ) + "\n\nPlease provide a valid link to access content.",
        reply_markup=reply_markup,
        disable_web_page_preview=True,
        quote=True
    )

@Bot.on_message(filters.command('start') & filters.private)
async def not_joined(client: Client, message: Message):
    id = message.from_user.id
    if not await present_user(id):
        try:
            await add_user(id)
            print(f"Added user {id} to database")
        except Exception as e:
            print(f"Error adding user {id}: {e}")

    buttons = [
        [
            InlineKeyboardButton(text="😈 𝗢𝗣𝗠𝗔𝗦𝗧𝗘𝗥𝗦 💀", url=client.invitelink4),
        ],
        [
            InlineKeyboardButton(text="🌟 𝗝𝗼𝗶𝗻 𝟭𝘀𝘁 🌟", url=client.invitelink),
            InlineKeyboardButton(text="💝 𝗝𝗼𝗶𝗻 𝟮𝗻𝗱 💝", url=client.invitelink2),
        ],
        [
            InlineKeyboardButton(text="🕊 𝗝𝗼𝗶𝗻 𝟯𝗿𝗱 🕊", url=client.invitelink3),
        ]        
    ]
    try:
        buttons.append(
            [
                InlineKeyboardButton(
                    text='♻️ 𝐓𝐑𝐘 𝐀𝐆𝐀𝐈𝐍 ♻️',
                    url=f"https://t.me/{client.username}?start={message.command[1]}"
                )
            ]
        )
    except IndexError:
        pass

    await message.reply(
        text=FORCE_MSG.format(
            first=message.from_user.first_name,
            last=message.from_user.last_name,
            username=None if not message.from_user.username else '@' + message.from_user.username,
            mention=message.from_user.mention,
            id=message.from_user.id
        ),
        reply_markup=InlineKeyboardMarkup(buttons),
        quote=True,
        disable_web_page_preview=True
    )

@Bot.on_message(filters.command('users') & filters.private & filters.user(ADMINS))
async def get_users(client: Bot, message: Message):
    msg = await client.send_message(chat_id=message.chat.id, text="Processing...")
    users = await full_userbase()
    await msg.edit(f"{len(users)} Users Are Using This Bot")

@Bot.on_message(filters.command('add_random_message') & filters.private & filters.user(ADMINS))
async def add_random_message(client: Bot, message: Message):
    bot_id = client.username
    try:
        msg_id = int(message.text.split(" ", 1)[1])
        await add_special_message(msg_id, bot_id)
        await message.reply_text(f"✅ Message ID {msg_id} added to special messages for {bot_id}.")
    except IndexError:
        await message.reply_text("❌ Please provide a message ID. Usage: /add_random_message <msg_id>")
    except ValueError:
        await message.reply_text("❌ Message ID must be a number.")
    except Exception as e:
        await message.reply_text(f"❌ Error: {str(e)}")

@Bot.on_message(filters.command('remove_random_message') & filters.private & filters.user(ADMINS))
async def remove_random_message(client: Bot, message: Message):
    bot_id = client.username
    try:
        msg_id = int(message.text.split(" ", 1)[1])
        await remove_special_message(msg_id, bot_id)
        await message.reply_text(f"✅ Message ID {msg_id} removed from special messages for {bot_id}.")
    except IndexError:
        await message.reply_text("❌ Please provide a message ID. Usage: /remove_random_message <msg_id>")
    except ValueError:
        await message.reply_text("❌ Message ID must be a number.")
    except Exception as e:
        await message.reply_text(f"❌ Error: {str(e)}")

@Bot.on_message(filters.command('list_random_messages') & filters.private & filters.user(ADMINS))
async def list_random_messages(client: Bot, message: Message):
    bot_id = client.username
    msg_ids = await get_special_messages(bot_id)
    response = f"<b>Special Messages for {bot_id}:</b>\n\n"
    if msg_ids:
        response += f"Message IDs: {', '.join(map(str, msg_ids))}\n"
    else:
        response += "No special messages configured.\n"

    all_special_msgs = await get_all_special_messages()
    other_bots_msgs = [doc for doc in all_special_msgs if doc['_id'] != f"{bot_id}_special_msg_ids"]
    if other_bots_msgs:
        response += "\n<b>Other Bots' Special Messages (Read-Only):</b>\n\n"
        for doc in other_bots_msgs:
            other_bot_id = doc['_id'].replace("_special_msg_ids", "")
            msg_ids = doc.get('msg_ids', [])
            response += f"Bot: {other_bot_id}\nMessage IDs: {', '.join(map(str, msg_ids)) if msg_ids else 'None'}\n\n"

    response += "Use /add_random_message <msg_id> to add a message.\nUse /remove_random_message <msg_id> to remove a message."
    await message.reply(response)

@Bot.on_message(filters.private & filters.command('broadcast') & filters.user(ADMINS))
async def send_text(client: Bot, message: Message):
    if not message.reply_to_message:
        msg = await message.reply("Reply to a message to broadcast it.")
        await asyncio.sleep(8)
        return await msg.delete()

    try:
        seconds = int(message.text.split(maxsplit=1)[1])
    except (IndexError, ValueError):
        seconds = None

    query = await full_userbase()
    broadcast_msg = message.reply_to_message
    total = 0
    successful = 0
    blocked = 0
    deleted = 0
    unsuccessful = 0
    sent_messages = []

    pls_wait = await message.reply("<i>ʙʀᴏᴀᴅᴄᴀꜱᴛ ᴘʀᴏᴄᴇꜱꜱɪɴɢ ᴛɪʟʟ ᴡᴀɪᴛ ʙʀᴏᴏ...</i>")

    for chat_id in query:
        try:
            sent = await broadcast_msg.copy(chat_id)
            sent_messages.append((chat_id, sent.id))
            successful += 1
        except FloodWait as e:
            await asyncio.sleep(e.x)
            sent = await broadcast_msg.copy(chat_id)
            sent_messages.append((chat_id, sent.id))
            successful += 1
        except UserIsBlocked:
            await del_user(chat_id)
            blocked += 1
        except InputUserDeactivated:
            await del_user(chat_id)
            deleted += 1
        except:
            unsuccessful += 1
        total += 1

    status = f"""<b><u>ʙʀᴏᴀᴅᴄᴀꜱᴛ ᴄᴏᴍᴘʟᴇᴛᴇᴅ</u>

ᴛᴏᴛᴀʟ ᴜꜱᴇʀꜱ: <code>{total}</code>
ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟ: <code>{successful}</code>
ʙʟᴏᴄᴋᴇᴅ ᴜꜱᴇʀꜱ: <code>{blocked}</code>
ᴅᴇʟᴇᴛᴇᴅ ᴀᴄᴄᴏᴜɴᴛꜱ: <code>{deleted}</code>
ᴜɴꜱᴜᴄᴄᴇꜱꜱꜰᴜʟ: <code>{unsuccessful}</code></b>"""

    await pls_wait.edit(status)

    if seconds:
        await asyncio.sleep(seconds)
        for chat_id, msg_id in sent_messages:
            try:
                await client.delete_messages(chat_id, msg_id)
            except Exception as e:
                print(f"Failed to delete broadcast message {msg_id} in chat {chat_id}: {e}")

@Bot.on_message(filters.private & filters.command('broadcast_add') & filters.user(ADMINS))
async def broadcast_add(client: Bot, message: Message):
    if not message.reply_to_message:
        await message.reply("❌ Reply to a message to schedule broadcast.")
        return

    try:
        parts = message.text.split(" ", 1)[1].split(":")
        if len(parts) not in [3, 4]:
            await message.reply("❌ Usage: /broadcast_add {total_time}:{interval}:{delete_after}[:{start_delay}]\nExample: /broadcast_add 86400:3600:600:3600")
            return
        total_time, interval, delete_after = map(int, parts[:3])
        start_delay = int(parts[3]) if len(parts) == 4 else 0
        if total_time <= 0 or interval <= 0 or delete_after < 0 or start_delay < 0:
            await message.reply("❌ Times must be positive integers (total_time and interval >0, delete_after and start_delay >=0).")
            return
        if interval > total_time:
            await message.reply("❌ Interval cannot be greater than total_time.")
            return
    except ValueError:
        await message.reply("❌ Invalid format. Use integers separated by ':'. Example: 86400:3600:600:3600")
        return
    except Exception as e:
        await message.reply(f"❌ Error: {str(e)}")
        return

    total_messages = total_time // interval
    bot_id = client.username
    schedule_id = await add_scheduled_broadcast(
        admin_chat_id=message.chat.id,
        chat_id=message.chat.id,
        reply_msg_id=message.reply_to_message.id,
        total_time=total_time,
        interval=interval,
        delete_after=delete_after,
        start_delay=start_delay,
        bot_id=bot_id
    )

    global scheduled_broadcast_tasks
    task = asyncio.create_task(start_scheduled_broadcast(client, schedule_id))
    scheduled_broadcast_tasks[schedule_id] = task

    await message.reply(
        f"✅ Scheduled broadcast added!\n"
        f"ID: {schedule_id}\n"
        f"Bot: {bot_id}\n"
        f"Total Time: {humanize.naturaldelta(total_time)}\n"
        f"Interval: {humanize.naturaldelta(interval)}\n"
        f"Delete After: {humanize.naturaldelta(delete_after)}\n"
        f"Start Delay: {humanize.naturaldelta(start_delay) if start_delay > 0 else 'Immediate'}\n"
        f"Total Messages: {total_messages}\n"
        f"Started repeating... Stats will be sent after each cycle."
    )

@Bot.on_message(filters.private & filters.command('broadcast_remove') & filters.user(ADMINS))
async def broadcast_remove(client: Bot, message: Message):
    bot_id = client.username
    try:
        schedule_id = message.text.split(" ", 1)[1]
        schedule = await get_schedule_by_id(schedule_id)
        if not schedule:
            await message.reply(f"❌ No scheduled broadcast with ID: {schedule_id}")
            return
        if schedule['bot_id'] != bot_id:
            await message.reply(f"❌ Schedule {schedule_id} belongs to another bot: {schedule['bot_id']}")
            return

        await deactivate_scheduled_broadcast(schedule_id)
        global scheduled_broadcast_tasks
        task = scheduled_broadcast_tasks.get(schedule_id)
        if task and not task.done():
            task.cancel()
        scheduled_broadcast_tasks.pop(schedule_id, None)

        try:
            await client.send_message(schedule['admin_chat_id'], f"✅ Scheduled broadcast {schedule_id} removed by admin.")
        except Exception as e:
            print(f"Failed to notify admin chat {schedule['admin_chat_id']}: {e}")
        await message.reply(f"✅ Scheduled broadcast removed (ID: {schedule_id}).")
    except IndexError:
        schedules = await get_active_scheduled_broadcasts(bot_id)
        all_schedules = await get_active_scheduled_broadcasts()
        if not schedules:
            response = "❌ No active scheduled broadcasts for this bot.\n"
        else:
            response = f"<b>Active Scheduled Broadcasts for {bot_id}:</b>\n\n"
            for schedule in schedules:
                total_messages = schedule['total_time'] // schedule['interval']
                response += (
                    f"ID: {schedule['_id']}\n"
                    f"Bot: {schedule['bot_id']}\n"
                    f"Total Time: {humanize.naturaldelta(schedule['total_time'])}\n"
                    f"Interval: {humanize.naturaldelta(schedule['interval'])}\n"
                    f"Delete After: {humanize.naturaldelta(schedule['delete_after'])}\n"
                    f"Start Delay: {humanize.naturaldelta(schedule['start_delay']) if schedule['start_delay'] > 0 else 'Immediate'}\n"
                    f"Total Messages: {total_messages}\n"
                    f"Started: {humanize.naturaltime(time.time() - schedule['start_time'])} ago\n\n"
                )

        other_schedules = [s for s in all_schedules if s['bot_id'] != bot_id]
        if other_schedules:
            response += "<b>Other Bots' Active Schedules (Read-Only):</b>\n\n"
            for schedule in other_schedules:
                total_messages = schedule['total_time'] // schedule['interval']
                response += (
                    f"ID: {schedule['_id']}\n"
                    f"Bot: {schedule['bot_id']}\n"
                    f"Total Time: {humanize.naturaldelta(schedule['total_time'])}\n"
                    f"Interval: {humanize.naturaldelta(schedule['interval'])}\n"
                    f"Delete After: {humanize.naturaldelta(schedule['delete_after'])}\n"
                    f"Start Delay: {humanize.naturaldelta(schedule['start_delay']) if schedule['start_delay'] > 0 else 'Immediate'}\n"
                    f"Total Messages: {total_messages}\n"
                    f"Started: {humanize.naturaltime(time.time() - schedule['start_time'])} ago\n\n"
                )

        response += "Use /broadcast_remove <schedule_id> to remove a specific schedule owned by this bot.\nUse /resume <schedule_id>[:{start_delay}] to resume a schedule."
        await message.reply(response)
    except Exception as e:
        await message.reply(f"❌ Error: {str(e)}")

@Bot.on_message(filters.private & filters.command('resume') & filters.user(ADMINS))
async def resume_broadcast(client: Bot, message: Message):
    bot_id = client.username
    try:
        parts = message.text.split(" ", 1)[1].split(":")
        schedule_id = parts[0]
        start_delay = int(parts[1]) if len(parts) > 1 else None
    except IndexError:
        await message.reply("❌ Usage: /resume {schedule_id}[:{start_delay}]\nExample: /resume abc123:7200")
        return
    except ValueError:
        await message.reply("❌ Invalid format. Start delay must be an integer.")
        return

    schedule = await get_schedule_by_id(schedule_id)
    if not schedule:
        await message.reply(f"❌ No scheduled broadcast with ID: {schedule_id}")
        return
    if schedule['bot_id'] != bot_id:
        await message.reply(f"❌ Schedule {schedule_id} belongs to another bot: {schedule['bot_id']}")
        return
    if schedule['active']:
        await message.reply(f"❌ Schedule {schedule_id} is already active.")
        return

    new_start_time = time.time()
    await update_schedule_start_time(schedule_id, new_start_time, start_delay)
    await deactivate_scheduled_broadcast(schedule_id)
    await scheduled_broadcasts.update_one(
        {'_id': schedule_id},
        {'$set': {'active': True}}
    )

    global scheduled_broadcast_tasks
    task = asyncio.create_task(start_scheduled_broadcast(client, schedule_id))
    scheduled_broadcast_tasks[schedule_id] = task

    start_delay = start_delay if start_delay is not None else schedule.get('start_delay', 0)
    await message.reply(
        f"✅ Scheduled broadcast resumed!\n"
        f"ID: {schedule_id}\n"
        f"Bot: {bot_id}\n"
        f"Start Delay: {humanize.naturaldelta(start_delay) if start_delay > 0 else 'Immediate'}\n"
        f"Started repeating... Stats will be sent after each cycle."
    )

@Bot.on_message(filters.command("start") & filters.private & filters.user(ADMINS))
async def admin_start(client: Client, message: Message):
    bot_id = client.username
    schedules = await get_active_scheduled_broadcasts(bot_id)
    msg_ids = await get_special_messages(bot_id)
    response = f"📊 {len(schedules)} active scheduled broadcasts running for {bot_id}.\n"
    response += f"📩 {len(msg_ids)} special messages configured for {bot_id}."
    await message.reply(response)

async def delete_files(codeflix_msgs, client, message, k, delete_time=None):
    if delete_time is None:
        delete_time = FILE_AUTO_DELETE
    
    await asyncio.sleep(delete_time)
    
    for msg in codeflix_msgs:
        try:
            await client.delete_messages(chat_id=msg.chat.id, message_ids=[msg.id])
        except Exception as e:

            print(f"The attempt to delete the media {msg.id} was unsuccessful: {e}")



