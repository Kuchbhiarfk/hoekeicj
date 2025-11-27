import random
import os
import asyncio
import humanize
import time
import uuid
import json
import aiohttp
import re
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
import pytz
import dateutil.parser

# MongoDB connection for educators
mongo_client = MongoClient("mongodb+srv://elvishyadav_opm:naman1811421@cluster0.uxuplor.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")
db = mongo_client["unacademy_db"]
educators_col = db["educators"]

# Encryption keys
ENCRYPTION_KEY = bytes.fromhex('0123456789abcdef0123456789abcdef')
IV = b'abcdef9876543210'

DECRYPT_URL_BASE = "https://bhundacademy-users.onrender.com/op?data="

# ⚠️ IMPORTANT: Add this to your config.py
LOG_CHANNEL = -1003385990498  # REPLACE WITH YOUR ACTUAL LOG CHANNEL ID

# Delete times
BULK_DELETE_TIME = FILE_AUTO_DELETE
try:
    INDIVIDUAL_DELETE_TIME = INDIVIDUAL_AUTO_DELETE
except NameError:
    INDIVIDUAL_DELETE_TIME = FILE_AUTO_DELETE

codeflixbots = FILE_AUTO_DELETE
subaru = codeflixbots
file_auto_delete = humanize.naturaldelta(subaru)

scheduled_broadcast_tasks = {}

# ============================================================
# UNACADEMY API FUNCTIONS (For Direct Extraction)
# ============================================================

async def fetch_unacademy_schedule_api(schedule_url: str, item_type: str, item_data: dict) -> Tuple[list, str]:
    """Fetch schedule from Unacademy API directly"""
    async with aiohttp.ClientSession() as session:
        for attempt in range(10):
            try:
                timeout = aiohttp.ClientTimeout(total=45)
                async with session.get(schedule_url, timeout=timeout) as response:
                    if response.status == 429:
                        retry_after = int(response.headers.get("Retry-After", 5))
                        await asyncio.sleep(retry_after)
                        continue
                    
                    response.raise_for_status()
                    data = await response.json()
                    results = data.get('results', [])

                    if not results:
                        return [], None

                    current_time = datetime.now(pytz.UTC)
                    item_name = item_data.get("name", "N/A")
                    item_starts_at = item_data.get("starts_at", "N/A")
                    item_ends_at = item_data.get("ends_at", "N/A") if item_type == "course" else item_data.get("completed_at", "N/A")
                    item_teachers = [item_data.get("author", {})] if item_type == "course" else item_data.get("authors", [])

                    results_list = []

                    if item_type == 'course':
                        for item in results:
                            value = item.get("value", {})
                            uid = value.get("uid", None)
                            if not uid:
                                continue
                            results_list.append(extract_course_item(
                                value.get("title", "N/A"),
                                value.get("live_class", {}).get("author", {}),
                                value.get("live_class", {}).get("live_at", "N/A"),
                                value.get("live_class", {}).get("video_url"),
                                value.get("live_class", {}).get("slides_pdf", {}),
                                value.get("is_offline", "N/A")
                            ))
                    else:  # batch
                        async def fetch_batch_collection_item(item):
                            properties = item.get('properties', {})
                            author = properties.get('author', {})
                            permalink = properties.get('permalink', '')
                            data_id_match = re.search(r'/course/[^/]+/([A-Z0-9]+)', permalink)
                            data_id = data_id_match.group(1) if data_id_match else None
                            uid = properties.get('uid', None)
                            live_at = properties.get('live_at', 'N/A')

                            if not data_id or not uid:
                                return None

                            collection_url = f"https://unacademy.com/api/v3/collection/{data_id}/items?limit=10000"
                            for retry in range(5):
                                try:
                                    async with session.get(collection_url, timeout=timeout) as collection_response:
                                        if collection_response.status == 429:
                                            retry_after = int(collection_response.headers.get("Retry-After", 5))
                                            await asyncio.sleep(retry_after)
                                            continue
                                        collection_response.raise_for_status()
                                        collection_data = await collection_response.json()
                                        items = collection_data.get("results", [])
                                        for collection_item in items:
                                            value = collection_item.get("value", {})
                                            if value.get("uid") == uid:
                                                return extract_course_item(
                                                    value.get("title", properties.get('name', 'N/A')),
                                                    value.get("live_class", {}).get("author", author),
                                                    value.get("live_at", live_at),
                                                    value.get("live_class", {}).get("video_url"),
                                                    value.get("live_class", {}).get("slides_pdf", {}),
                                                    value.get("is_offline", "N/A")
                                                )
                                        return None
                                except:
                                    if retry < 4:
                                        await asyncio.sleep(2 ** retry)
                                        continue
                                    return handle_collection_failure_api(live_at, properties.get('name', 'N/A'), author)
                            return None

                        tasks = [fetch_batch_collection_item(item) for item in results]
                        collection_results = await asyncio.gather(*tasks, return_exceptions=True)
                        results_list.extend([r for r in collection_results if r is not None and not isinstance(r, Exception)])

                    results_list = [r for r in results_list if r]
                    results_list.sort(key=lambda x: x.get("live_at_time") or datetime.min.replace(tzinfo=pytz.UTC).isoformat(), reverse=True)

                    teachers = ", ".join([f"{t.get('first_name', '')} {t.get('last_name', '')}".strip() for t in item_teachers if t.get('first_name')])
                    last_checked = datetime.now(pytz.timezone('Asia/Kolkata')).strftime("%Y-%m-%d %H:%M:%S %Z")
                    
                    if item_type == "course":
                        caption = (
                            f"Course Name: {item_name}\n"
                            f"Course Teacher: {teachers}\n"
                            f"Start_at: {item_starts_at}\n"
                            f"Ends_at: {item_ends_at}\n"
                            f"Last_checked_at: {last_checked}"
                        )
                    else:
                        caption = (
                            f"Batch Name: {item_name}\n"
                            f"Batch Teachers: {teachers}\n"
                            f"Start_at: {item_starts_at}\n"
                            f"Completed_at: {item_ends_at}\n"
                            f"Last_checked_at: {last_checked}"
                        )

                    return results_list, caption

            except Exception as e:
                print(f"Error in schedule API (attempt {attempt + 1}/10): {e}")
                await asyncio.sleep(2 ** min(attempt, 6))

        return [], None

def extract_course_item(title, author, live_at, video_url, slides_pdf, is_offline):
    """Extract and format course item details"""
    current_time = datetime.now(pytz.UTC)
    live_at_time = None
    if live_at != "N/A":
        try:
            live_at_time = datetime.strptime(live_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC)
        except ValueError:
            live_at_time = None

    class_url = "N/A"
    slides_url = slides_pdf.get('with_annotation', 'N/A') if slides_pdf else "N/A"
    
    if live_at_time:
        if live_at_time < current_time:
            if not video_url and not (slides_pdf and slides_pdf.get('with_annotation', None)):
                class_url = "Class Cancelled"
                slides_url = "Class Cancelled"
            elif isinstance(video_url, str):
                match = re.search(r"uid=([A-Z0-9]+)", video_url)
                if match:
                    vid = match.group(1)
                    class_url = f"https://uamedia.uacdn.net/lesson-raw/{vid}/output.webm"
        else:
            class_url = "Live Soon"
            slides_url = "Live Soon"
    else:
        if isinstance(video_url, str):
            match = re.search(r"uid=([A-Z0-9]+)", video_url)
            if match:
                vid = match.group(1)
                class_url = f"https://uamedia.uacdn.net/lesson-raw/{vid}/output.webm"
        else:
            class_url = f"Live At: {live_at}"

    live_at_time_str = live_at_time.isoformat() if live_at_time else "N/A"

    return {
        "class_name": title,
        "teacher_name": f"{author.get('first_name', '')} {author.get('last_name', '')}".strip(),
        "live_at": live_at,
        "thumbnail": author.get('avatar', 'N/A'),
        "class_url": class_url,
        "slides_url": slides_url,
        "is_offline": is_offline,
        "live_at_time": live_at_time_str
    }

def handle_collection_failure_api(live_at, class_name, author):
    """Handle collection API failure"""
    current_time = datetime.now(pytz.UTC)
    class_url = "N/A"
    slides_url = "N/A"
    live_at_time = None

    if live_at != "N/A":
        try:
            live_at_time = datetime.strptime(live_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC)
            if live_at_time < current_time:
                class_url = "Class Cancelled"
                slides_url = "Class Cancelled"
            else:
                class_url = "Live Soon"
                slides_url = "Live Soon"
        except ValueError:
            class_url = f"Live At: {live_at}"
            slides_url = "N/A"

    live_at_time_str = live_at_time.isoformat() if live_at_time else "N/A"

    return {
        "class_name": class_name,
        "teacher_name": f"{author.get('first_name', '')} {author.get('last_name', '')}".strip(),
        "live_at": live_at,
        "thumbnail": author.get('avatar', 'N/A'),
        "class_url": class_url,
        "slides_url": slides_url,
        "is_offline": "N/A",
        "live_at_time": live_at_time_str
    }

async def fetch_course_details_by_uid(uid: str) -> dict:
    """Fetch course details from Unacademy API by UID"""
    url = f"https://unacademy.com/api/v3/course/{uid}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=15) as response:
                if response.status == 429:
                    retry_after = int(response.headers.get("Retry-After", 5))
                    await asyncio.sleep(retry_after)
                    return await fetch_course_details_by_uid(uid)
                
                response.raise_for_status()
                data = await response.json()
                
                course_data = data.get("course", {})
                if course_data:
                    author = course_data.get("author", {})
                    return {
                        "uid": uid,
                        "name": course_data.get("name", "Unknown Course"),
                        "slug": course_data.get("slug", "N/A"),
                        "thumbnail": course_data.get("thumbnail", "N/A"),
                        "starts_at": course_data.get("starts_at", "N/A"),
                        "ends_at": course_data.get("ends_at", "N/A"),
                        "author": {
                            "first_name": author.get("first_name", "N/A"),
                            "last_name": author.get("last_name", "N/A"),
                            "username": author.get("username", "N/A"),
                            "avatar": author.get("avatar", "N/A")
                        }
                    }
                return None
        except Exception as e:
            print(f"Failed to fetch course {uid}: {e}")
            return None

async def fetch_batch_details_by_uid(uid: str) -> dict:
    """Fetch batch details from Unacademy API by UID"""
    url = f"https://unacademy.com/api/v1/batch/{uid}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=15) as response:
                if response.status == 429:
                    retry_after = int(response.headers.get("Retry-After", 5))
                    await asyncio.sleep(retry_after)
                    return await fetch_batch_details_by_uid(uid)
                
                response.raise_for_status()
                data = await response.json()
                
                batch_data = data.get("batch", {})
                if batch_data:
                    authors = batch_data.get("educators", [])
                    goal = batch_data.get("goal", {})
                    return {
                        "uid": uid,
                        "name": batch_data.get("name", "Unknown Batch"),
                        "slug": batch_data.get("slug", "N/A"),
                        "cover_photo": batch_data.get("cover_photo", "N/A"),
                        "exam_type": goal.get("name", "N/A"),
                        "syllabus_tag": batch_data.get("syllabus_tag", "N/A"),
                        "starts_at": batch_data.get("starts_at", "N/A"),
                        "completed_at": batch_data.get("completed_at", "N/A"),
                        "authors": [
                            {
                                "first_name": author.get("first_name", "N/A"),
                                "last_name": author.get("last_name", "N/A"),
                                "username": author.get("username", "N/A"),
                                "avatar": author.get("avatar", "N/A")
                            } for author in authors
                        ]
                    }
                return None
        except Exception as e:
            print(f"Failed to fetch batch {uid}: {e}")
            return None

# ============================================================
# ENCRYPTION FUNCTIONS
# ============================================================

def encrypt_json_item(data: dict) -> str:
    """Encrypt a single JSON object using AES-256-CBC."""
    json_str = json.dumps(data)
    cipher = AES.new(ENCRYPTION_KEY, AES.MODE_CBC, IV)
    padded_data = pad(json_str.encode('utf-8'), AES.block_size)
    encrypted_data = cipher.encrypt(padded_data)
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

# ============================================================
# HTML GENERATION FUNCTION
# ============================================================

def generate_html_from_decrypted(decrypted_json_str: str, batch_title: str, batch_thumbnail: str = None, user_first_name: str = "", user_id: str = "", made_at: str = "") -> str:
    """Generate HTML catalog with user details encrypted in each link"""
    json_data_str = decrypted_json_str.replace("False", "false")
    json_data = json.loads(json_data_str)
    
    if not batch_thumbnail:
        batch_thumbnail = 'https://via.placeholder.com/400x200?text=Catalog'
    
    classes = []
    for item in json_data:
        # Add user details to item before encrypting
        item['user_first_name'] = user_first_name
        item['user_id'] = user_id
        item['made_at'] = made_at
        
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
                <a href="{lec["link"]}"{target} class="chip" data-lecture-name="{lec["class_name"].lower()}" data-teacher="{lec["teacher_name"].lower()}" data-date="{lec["date_str"]}">
                  <img src="{lec["thumbnail"]}?q=80&w=48&h=48&fit=crop" alt="{lec["teacher_name"]}" class="chip-icon" loading="lazy">
                  <div class="chip-content">
                    <div class="chip-title">{lec["class_name"]}</div>
                    <div class="chip-meta">
                      <span>{lec["teacher_name"]}</span>
                      <span>{lec["date_str"]}</span>
                    </div>
                  </div>
                </a>
            '''
        return chips
    
    def generate_section(section_id, title, chips):
        return f'''
            <section class="section" data-section-type="{section_id.split('-')[0]}">
              <div class="section-header">
                <h2 class="section-title">{title}</h2>
                <button class="collapse-btn" data-section="{section_id}">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <polyline points="6 9 12 15 18 9"></polyline>
                  </svg>
                </button>
              </div>
              <div class="chip-grid" id="{section_id}-content">
                {chips}
              </div>
            </section>
        '''
    
    day_sections = ''
    for date in sorted_days:
        section_id = f"day-{date}"
        chips = generate_chips(day_groups[date])
        day_sections += generate_section(section_id, date, chips)
    
    teacher_sections = ''
    for teacher in sorted_teachers:
        section_id = f"teacher-{teacher.replace(' ', '_')}"
        chips = generate_chips(teacher_groups[teacher])
        teacher_sections += generate_section(section_id, teacher, chips)
    
    month_sections = ''
    for month in sorted_months:
        section_id = f"month-{month}"
        chips = generate_chips(month_groups[month])
        month_sections += generate_section(section_id, month, chips)
    
    full_html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{batch_title}</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        :root {{
            --primary: #2563eb;
            --primary-dark: #1e40af;
            --accent: #10b981;
            --bg: #f5f5f5;
            --surface: #ffffff;
            --text: #1e293b;
            --text-muted: #64748b;
            --border: #e2e8f0;
            --shadow: rgba(0, 0, 0, 0.08);
            --radius: 8px;
        }}
        
        [data-theme="dark"] {{
            --primary: #3b82f6;
            --primary-dark: #2563eb;
            --accent: #34d399;
            --bg: #0f172a;
            --surface: #1e293b;
            --text: #f1f5f9;
            --text-muted: #94a3b8;
            --border: #334155;
            --shadow: rgba(0, 0, 0, 0.3);
        }}
        
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg);
            color: var(--text);
            line-height: 1.6;
        }}
        
        .header {{
            background: var(--primary);
            color: white;
            padding: 16px 20px;
            position: sticky;
            top: 0;
            z-index: 100;
            box-shadow: 0 2px 4px var(--shadow);
        }}
        
        .header-content {{
            max-width: 1200px;
            margin: 0 auto;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        
        .brand {{
            font-size: 18px;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        
        .theme-toggle {{
            background: rgba(255, 255, 255, 0.2);
            border: none;
            border-radius: 6px;
            padding: 8px;
            cursor: pointer;
            display: flex;
            align-items: center;
            color: white;
            transition: background 0.2s;
        }}
        
        .theme-toggle:hover {{
            background: rgba(255, 255, 255, 0.3);
        }}
        
        .tabs {{
            background: var(--surface);
            border-bottom: 2px solid var(--border);
            position: sticky;
            top: 56px;
            z-index: 99;
        }}
        
        .tabs-content {{
            max-width: 1200px;
            margin: 0 auto;
            display: flex;
            gap: 4px;
            padding: 8px 20px;
            overflow-x: auto;
        }}
        
        .tab {{
            background: transparent;
            border: none;
            padding: 10px 20px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 600;
            color: var(--text-muted);
            white-space: nowrap;
            transition: all 0.2s;
        }}
        
        .tab:hover {{
            background: var(--bg);
            color: var(--text);
        }}
        
        .tab.active {{
            background: var(--primary);
            color: white;
        }}
        
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }}
        
        .banner {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 20px;
            margin-bottom: 20px;
            text-align: center;
        }}
        
        .banner-img {{
            max-width: 400px;
            width: 100%;
            height: auto;
            border-radius: var(--radius);
            margin: 12px auto;
        }}
        
        .banner-title {{
            font-size: 20px;
            font-weight: 700;
            color: var(--primary);
            margin-bottom: 8px;
        }}
        
        .banner-text {{
            color: var(--text-muted);
            font-size: 14px;
        }}
        
        .search-container {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 16px;
            margin-bottom: 20px;
        }}
        
        .search-box {{
            width: 100%;
            padding: 12px 16px;
            font-size: 14px;
            border: 2px solid var(--border);
            border-radius: 6px;
            background: var(--bg);
            color: var(--text);
            outline: none;
            transition: border-color 0.2s;
        }}
        
        .search-box:focus {{
            border-color: var(--primary);
        }}
        
        .search-box::placeholder {{
            color: var(--text-muted);
        }}
        
        .no-results {{
            text-align: center;
            padding: 40px 20px;
            color: var(--text-muted);
            font-size: 14px;
            display: none;
        }}
        
        .no-results.show {{
            display: block;
        }}
        
        .section {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            margin-bottom: 16px;
            overflow: hidden;
        }}
        
        .section.hidden {{
            display: none;
        }}
        
        .section-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 14px 16px;
            background: var(--bg);
            border-bottom: 1px solid var(--border);
        }}
        
        .section-title {{
            font-size: 16px;
            font-weight: 700;
            color: var(--primary);
        }}
        
        .collapse-btn {{
            background: transparent;
            border: none;
            cursor: pointer;
            padding: 4px;
            color: var(--text-muted);
            display: flex;
            align-items: center;
            transition: transform 0.2s, color 0.2s;
        }}
        
        .collapse-btn:hover {{
            color: var(--text);
        }}
        
        .collapse-btn.collapsed svg {{
            transform: rotate(-90deg);
        }}
        
        .chip-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
            gap: 12px;
            padding: 16px;
            transition: max-height 0.3s ease-out;
        }}
        
        .chip-grid.collapsed {{
            display: none;
        }}
        
        @media (max-width: 768px) {{
            .chip-grid {{
                grid-template-columns: 1fr;
            }}
        }}
        
        .chip {{
            display: flex;
            gap: 12px;
            padding: 12px;
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: 6px;
            text-decoration: none;
            color: var(--text);
            transition: all 0.2s;
        }}
        
        .chip:hover {{
            transform: translateY(-2px);
            box-shadow: 0 4px 8px var(--shadow);
            border-color: var(--primary);
        }}
        
        .chip.search-hidden {{
            display: none;
        }}
        
        .chip-icon {{
            width: 48px;
            height: 48px;
            border-radius: 6px;
            object-fit: cover;
            flex-shrink: 0;
        }}
        
        .chip-content {{
            flex: 1;
            min-width: 0;
        }}
        
        .chip-title {{
            font-size: 14px;
            font-weight: 600;
            margin-bottom: 6px;
            overflow: hidden;
            text-overflow: ellipsis;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
        }}
        
        .chip-meta {{
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            font-size: 12px;
            color: var(--text-muted);
        }}
        
        .chip-meta span {{
            background: var(--surface);
            padding: 2px 8px;
            border-radius: 4px;
            border: 1px solid var(--border);
        }}
        
        footer {{
            text-align: center;
            padding: 24px 20px;
            color: var(--text-muted);
            font-size: 13px;
        }}
        
        @media (max-width: 640px) {{
            .header {{
                padding: 12px 16px;
            }}
            
            .brand {{
                font-size: 16px;
            }}
            
            .tabs-content {{
                padding: 6px 12px;
            }}
            
            .tab {{
                padding: 8px 16px;
                font-size: 13px;
            }}
            
            .container {{
                padding: 16px 12px;
            }}
            
            .banner {{
                padding: 16px;
            }}
            
            .search-container {{
                padding: 12px;
            }}
            
            .section-header {{
                padding: 12px;
            }}
            
            .chip-grid {{
                padding: 12px;
            }}
        }}
    </style>
</head>
<body>
    <header class="header">
        <div class="header-content">
            <div class="brand">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <rect x="3" y="3" width="18" height="18" rx="2" />
                    <circle cx="12" cy="12" r="3" />
                </svg>
                <span>{batch_title}</span>
            </div>
            <button class="theme-toggle" id="themeToggle">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
                </svg>
            </button>
        </div>
    </header>

    <nav class="tabs">
        <div class="tabs-content">
            <button class="tab active" data-tab="day">Day Wise</button>
            <button class="tab" data-tab="teacher">Teacher Wise</button>
            <button class="tab" data-tab="month">Month Wise</button>
        </div>
    </nav>

    <main class="container">
        <div class="banner">
            <div class="banner-title">📚 {batch_title}</div>
            <img src="{batch_thumbnail}" alt="{batch_title}" class="banner-img" loading="lazy">
            <p class="banner-text">Browse lectures by day, teacher, or month using the tabs above.</p>
        </div>

        <div class="search-container">
            <input type="text" class="search-box" id="searchBox" placeholder="🔍 Search lectures by name or teacher...">
        </div>

        <div class="no-results" id="noResults">
            No lectures found matching your search.
        </div>

        <div id="sectionsContainer">
            {day_sections}
            {teacher_sections}
            {month_sections}
        </div>
    </main>

    <footer>
        Built for educational purposes • {batch_title}
    </footer>

    <script>
        const themeToggle = document.getElementById('themeToggle');
        const html = document.documentElement;
        const savedTheme = localStorage.getItem('theme') || 'light';
        html.dataset.theme = savedTheme;

        themeToggle.addEventListener('click', () => {{
            const newTheme = html.dataset.theme === 'light' ? 'dark' : 'light';
            html.dataset.theme = newTheme;
            localStorage.setItem('theme', newTheme);
        }});

        const tabs = document.querySelectorAll('.tab');
        const searchBox = document.getElementById('searchBox');
        const noResults = document.getElementById('noResults');
        let currentTab = 'day';

        function showSections(tabType) {{
            const allSections = document.querySelectorAll('.section');
            allSections.forEach(section => {{
                const sectionType = section.dataset.sectionType;
                if (sectionType === tabType) {{
                    section.classList.remove('hidden');
                }} else {{
                    section.classList.add('hidden');
                }}
            }});
            currentTab = tabType;
        }}

        tabs.forEach(tab => {{
            tab.addEventListener('click', () => {{
                tabs.forEach(t => t.classList.remove('active'));
                tab.classList.add('active');
                const tabType = tab.dataset.tab;
                showSections(tabType);
                searchBox.value = '';
                filterLectures('');
            }});
        }});

        searchBox.addEventListener('input', (e) => {{
            const searchTerm = e.target.value.toLowerCase().trim();
            filterLectures(searchTerm);
        }});

        function filterLectures(searchTerm) {{
            const visibleSections = document.querySelectorAll('.section:not(.hidden)');
            let hasVisibleResults = false;

            if (searchTerm === '') {{
                visibleSections.forEach(section => {{
                    const chips = section.querySelectorAll('.chip');
                    chips.forEach(chip => chip.classList.remove('search-hidden'));
                }});
                noResults.classList.remove('show');
                return;
            }}

            visibleSections.forEach(section => {{
                const chips = section.querySelectorAll('.chip');
                let sectionHasResults = false;

                chips.forEach(chip => {{
                    const lectureName = chip.dataset.lectureName || '';
                    const teacher = chip.dataset.teacher || '';
                    
                    if (lectureName.includes(searchTerm) || teacher.includes(searchTerm)) {{
                        chip.classList.remove('search-hidden');
                        sectionHasResults = true;
                        hasVisibleResults = true;
                    }} else {{
                        chip.classList.add('search-hidden');
                    }}
                }});

                if (!sectionHasResults) {{
                    section.style.display = 'none';
                }} else {{
                    section.style.display = 'block';
                }}
            }});

            if (hasVisibleResults) {{
                noResults.classList.remove('show');
            }} else {{
                noResults.classList.add('show');
            }}
        }}

        document.addEventListener('click', (e) => {{
            if (e.target.closest('.collapse-btn')) {{
                const btn = e.target.closest('.collapse-btn');
                const sectionId = btn.dataset.section;
                const content = document.getElementById(sectionId + '-content');
                
                if (content) {{
                    content.classList.toggle('collapsed');
                    btn.classList.toggle('collapsed');
                    
                    const isCollapsed = content.classList.contains('collapsed');
                    localStorage.setItem('collapse-' + sectionId, isCollapsed ? '1' : '0');
                }}
            }}
        }});

        document.querySelectorAll('.collapse-btn').forEach(btn => {{
            const sectionId = btn.dataset.section;
            const content = document.getElementById(sectionId + '-content');
            const isCollapsed = localStorage.getItem('collapse-' + sectionId) === '1';
            
            if (isCollapsed && content) {{
                content.classList.add('collapsed');
                btn.classList.add('collapsed');
            }}
        }});

        showSections('day');
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
    """Download a .json file, add user details, encrypt each item, and return as a JSON array."""
    if not msg.document or not msg.document.file_name.endswith('.json'):
        raise ValueError("Message does not contain a .json file")

    path = await client.download_media(msg)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            json_data = json.load(f)

        encrypted_items = []
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

        encrypted_json = json.dumps(encrypted_items)
        filename = msg.document.file_name
        return encrypted_json, filename
    finally:
        try:
            os.remove(path)
        except Exception as e:
            print(f"Failed to delete temporary file {path}: {e}")

async def upload_encrypted_json(client: Client, encrypted_json: str, filename: str, chat_id: int) -> Message:
    """Upload encrypted JSON array as a .json file."""
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

# ============================================================
# LOG CHANNEL UPLOAD FUNCTION
# ============================================================

async def upload_to_log_channel(client: Client, json_data: str, item_name: str, item_type: str, uid: str, user_id: str, user_first_name: str):
    """Upload JSON to log channel with proper formatting"""
    
    try:
        # Create temp JSON file
        temp_filename = f"{item_type}_{uid}_{uuid.uuid4().hex[:8]}.json"
        
        with open(temp_filename, 'w', encoding='utf-8') as f:
            f.write(json_data)
        
        # Prepare caption
        hashtag = f"#{item_type}_{uid}"
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        
        caption = (
            f"📊 <b>{item_type.upper()} ACCESS LOG</b>\n\n"
            f"📚 <b>Name</b>: {item_name}\n"
            f"🆔 <b>UID</b>: <code>{uid}</code>\n\n"
            f"👤 <b>Accessed by</b>:\n"
            f"   • Name: {user_first_name}\n"
            f"   • ID: <code>{user_id}</code>\n"
            f"   • Time: {timestamp}\n\n"
            f"{hashtag}\n"
            f"#user_{user_id}"
        )
        
        # Upload to log channel
        await client.send_document(
            chat_id=LOG_CHANNEL,
            document=temp_filename,
            caption=caption,
            parse_mode=ParseMode.HTML
        )
        
        print(f"✅ Logged {item_type} {uid} access by user {user_id}")
        
        # Delete temp file
        try:
            os.remove(temp_filename)
        except Exception as e:
            print(f"Failed to delete temp file: {e}")
            
    except Exception as e:
        print(f"❌ Failed to upload to log channel: {e}")
        import traceback
        traceback.print_exc()

# ============================================================
# UID ACCESS HANDLER (Direct API Extraction)
# ============================================================

async def handle_uid_access(client: Client, message: Message, uid_param: str, user_first_name: str, user_id: str, made_at: str):
    """Handle UID-based access for batch/course - Extract directly from API"""
    
    # Extract type and UID
    if uid_param.startswith("batch_"):
        item_type = "batch"
        uid = uid_param.replace("batch_", "")
    elif uid_param.startswith("course_"):
        item_type = "course"
        uid = uid_param.replace("course_", "")
    else:
        await message.reply_text("❌ Invalid UID format.")
        return

    # Show processing message
    temp_msg = await message.reply("⏳ Extracting data from Unacademy API...")

    try:
        # Fetch item details from API
        if item_type == "course":
            item = await fetch_course_details_by_uid(uid)
            if not item:
                await temp_msg.edit(f"❌ Course not found with UID: {uid}")
                return
            
            # Build schedule URL
            schedule_url = f"https://unacademy.com/api/v3/collection/{uid}/items?limit=10000"
            
            # Format for API function
            item_data = {
                "name": item.get("name", "Unknown Course"),
                "starts_at": item.get("starts_at", "N/A"),
                "ends_at": item.get("ends_at", "N/A"),
                "author": item.get("author", {})
            }
            
            teachers = f"{item_data['author'].get('first_name', '')} {item_data['author'].get('last_name', '')}".strip()
            batch_thumbnail = item.get("thumbnail", "https://via.placeholder.com/400x200?text=Course")
            
        else:  # batch
            item = await fetch_batch_details_by_uid(uid)
            if not item:
                await temp_msg.edit(f"❌ Batch not found with UID: {uid}")
                return
            
            # Build schedule URL
            schedule_url = f"https://api.unacademy.com/api/v1/batch/{uid}/schedule/?limit=100000&offset=None&past=True&rank=100000&timezone_difference=330"
            
            # Format for API function
            item_data = {
                "name": item.get("name", "Unknown Batch"),
                "starts_at": item.get("starts_at", "N/A"),
                "completed_at": item.get("completed_at", "N/A"),
                "authors": item.get("authors", [])
            }
            
            teachers = ", ".join([f"{t.get('first_name', '')} {t.get('last_name', '')}".strip() for t in item_data["authors"]])
            batch_thumbnail = item.get("cover_photo", "https://via.placeholder.com/400x200?text=Batch")

        name = item.get("name", "Unknown")
        
        # Update progress message
        await temp_msg.edit(f"⏳ Fetching schedule for {name}...")
        
        # Fetch schedule from API
        results, base_caption = await fetch_unacademy_schedule_api(schedule_url, item_type, item_data)
        
        if not results or not base_caption:
            await temp_msg.edit(f"❌ Failed to fetch schedule for {item_type} {uid}")
            return
        
        # Convert to JSON
        json_data = json.dumps(results, indent=2)
        
        # 📤 UPLOAD TO LOG CHANNEL
        await upload_to_log_channel(
            client=client,
            json_data=json_data,
            item_name=name,
            item_type=item_type,
            uid=uid,
            user_id=user_id,
            user_first_name=user_first_name
        )
        
        # Update progress
        await temp_msg.edit("⏳ Generating HTML catalog...")
        
        # Generate HTML
        batch_title = name
        html_content = generate_html_from_decrypted(
            json_data, 
            batch_title, 
            batch_thumbnail, 
            user_first_name, 
            user_id, 
            made_at
        )
        
        # Calculate validity
        now_utc = datetime.now(timezone.utc)
        valid_till = (now_utc + timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S UTC')
        
        caption = (
            f"📚 <b>{item_type.title()}</b>: {name}\n"
            f"👨‍🏫 <b>Teachers</b>: {teachers}\n"
            f"⏰ <b>Valid till</b>: {valid_till}\n\n"
            f"<i>Open the HTML file in browser for best experience!</i>"
        )
        
        # Upload HTML to user
        html_msg = await upload_html(client, html_content, batch_title, message.from_user.id, caption)
        
        await temp_msg.delete()
        
        # Send random special message
        codeflix_msgs = [html_msg]
        special_msg = await send_random_special_message(client, message.from_user.id)
        if special_msg:
            codeflix_msgs.append(special_msg)

        # Send deletion warning
        k = await client.send_message(
            chat_id=message.from_user.id,
            text=f"<b>🔥 Hurry! This Catalog will be <u>deleted automatically in 24 hours</u> ⏳</b>\n\n"
                 f"<b>💡 Save it now - Forward or Download before it's gone!</b>\n\n"
                 f"<b>😎 Chill! You can re-access anytime via our websites 😘</b>\n\n"
                 f"<b><a href='https://yashyasag.github.io/hiddens_officials'>🌟 𝗩𝗶𝘀𝗶𝘁 𝗠𝗼𝗿𝗲 𝗪𝗲𝗯𝘀𝗶𝘁𝗲𝘀 🌟</a></b>",
        )
        
        codeflix_msgs.append(k)
        
        # Schedule auto-delete (24 hours)
        asyncio.create_task(delete_files(codeflix_msgs, client, message, k, 24 * 3600))
        
    except Exception as e:
        await temp_msg.edit(f"❌ Error: {str(e)}")
        print(f"Error in UID access: {e}")
        import traceback
        traceback.print_exc()

# ============================================================
# RANDOM SPECIAL MESSAGE
# ============================================================

async def send_random_special_message(client: Client, chat_id: int):
    """Send a random special message to the specified chat."""
    bot_id = client.username
    special_msg_ids = await get_special_messages(bot_id)
    if not special_msg_ids:
        print(f"No special messages found for bot {bot_id}")
        return None

    random_msg_id = random.choice(special_msg_ids)
    try:
        special_msg = await client.get_messages(client.db_channel.id, random_msg_id)
        if not special_msg:
            print(f"Special message {random_msg_id} not found")
            return None

        caption = f"<b>{special_msg.caption.html}</b>" if special_msg.caption else None

        if special_msg.sticker:
            special_copied_msg = await client.send_sticker(chat_id=chat_id, sticker=special_msg.sticker.file_id)
        elif special_msg.photo:
            special_copied_msg = await client.send_photo(chat_id=chat_id, photo=special_msg.photo.file_id, caption=caption, parse_mode=ParseMode.HTML)
        elif special_msg.video:
            special_copied_msg = await client.send_video(chat_id=chat_id, video=special_msg.video.file_id, caption=caption, parse_mode=ParseMode.HTML)
        elif special_msg.document:
            special_copied_msg = await client.send_document(chat_id=chat_id, document=special_msg.document.file_id, caption=caption, parse_mode=ParseMode.HTML)
        elif special_msg.text:
            special_copied_msg = await client.send_message(chat_id=chat_id, text=caption or special_msg.text, parse_mode=ParseMode.HTML)
        elif special_msg.audio:
            special_copied_msg = await client.send_audio(chat_id=chat_id, audio=special_msg.audio.file_id, caption=caption, parse_mode=ParseMode.HTML)
        elif special_msg.animation:
            special_copied_msg = await client.send_animation(chat_id=chat_id, animation=special_msg.animation.file_id, caption=caption, parse_mode=ParseMode.HTML)
        else:
            print(f"Unsupported message type for special message {random_msg_id}")
            return None

        return special_copied_msg
    except (ChannelInvalid, PeerIdInvalid, BadRequest, Exception) as e:
        print(f"Failed to fetch/send special message {random_msg_id}: {e}")
        return None

# ============================================================
# BROADCAST FUNCTIONS
# ============================================================

async def perform_broadcast_cycle(client: Client, chat_id: int, msg_id: int, delete_after: int, schedule_id: str, admin_chat_id: int):
    """Perform one cycle of broadcasting and schedule deletion."""
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
            print(f"Broadcast message {msg_id} not found.")
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
        print(f"Failed to send stats to admin: {e}")

    if delete_after > 0:
        await asyncio.sleep(delete_after)
        for user_id, sent_msg_id in sent_messages:
            try:
                await client.delete_messages(user_id, sent_msg_id)
            except Exception as e:
                print(f"Failed to delete message {sent_msg_id} in {user_id}: {e}")

async def start_scheduled_broadcast(client: Client, schedule_id: str):
    """Start the scheduled broadcast loop."""
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
            print(f"Failed to notify admin: {e}")

    while True:
        current_schedule = await get_schedule_by_id(schedule_id)
        if not current_schedule or not current_schedule.get('active', False):
            print(f"Scheduled broadcast {schedule_id} deactivated.")
            break

        current_time = time.time()
        elapsed = current_time - start_time - start_delay

        if elapsed >= total_time:
            await deactivate_scheduled_broadcast(schedule_id)
            try:
                await client.send_message(admin_chat_id, f"✅ Scheduled broadcast {schedule_id} ended (total time reached).")
            except Exception as e:
                print(f"Failed to notify admin: {e}")
            print(f"Scheduled broadcast {schedule_id} ended.")
            break

        await perform_broadcast_cycle(client, chat_id, reply_msg_id, delete_after, schedule_id, admin_chat_id)
        await asyncio.sleep(interval)

async def process_message_for_sending(client: Client, msg: Message, user_id: int, caption: str, reply_markup: InlineKeyboardMarkup, protect_content: bool):
    """Process and send a message to a user."""
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
                    print(f"Fallback download failed: {fallback_e}")
                    return None

        if msg.sticker:
            copied_msg = await client.send_sticker(chat_id=user_id, sticker=path if path else msg.sticker.file_id, protect_content=protect_content)
        elif msg.photo:
            copied_msg = await client.send_photo(chat_id=user_id, photo=path if path else msg.photo.file_id, caption=caption, parse_mode=ParseMode.HTML, reply_markup=reply_markup, protect_content=protect_content)
        elif msg.video:
            copied_msg = await client.send_video(chat_id=user_id, video=path if path else msg.video.file_id, caption=caption, parse_mode=ParseMode.HTML, reply_markup=reply_markup, protect_content=protect_content)
        elif msg.document:
            copied_msg = await client.send_document(chat_id=user_id, document=path if path else msg.document.file_id, caption=caption, parse_mode=ParseMode.HTML, reply_markup=reply_markup, protect_content=protect_content)
        elif msg.audio:
            copied_msg = await client.send_audio(chat_id=user_id, audio=path if path else msg.audio.file_id, caption=caption, parse_mode=ParseMode.HTML, reply_markup=reply_markup, protect_content=protect_content)
        elif msg.animation:
            copied_msg = await client.send_animation(chat_id=user_id, animation=path if path else msg.animation.file_id, caption=caption, parse_mode=ParseMode.HTML, reply_markup=reply_markup, protect_content=protect_content)
        elif msg.text:
            copied_msg = await client.send_message(chat_id=user_id, text=msg.text.html, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        else:
            print(f"Unsupported message type for message {msg.id}")
            return None

        return copied_msg
    except FloodWait as e:
        await asyncio.sleep(e.x)
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

# ============================================================
# START COMMAND
# ============================================================

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
            await message.reply_text("❌ No link provided.")
            return

        # ✅ CHECK FOR UID-BASED ACCESS (batch_{uid} or course_{uid})
        if base64_string.startswith("batch_") or base64_string.startswith("course_"):
            await handle_uid_access(client, message, base64_string, user_first_name, user_id, made_at)
            return

        # Existing link decoding logic
        try:
            link_type, user_id_decoded, f_msg_id, channel_id, s_msg_id = await decode_link(base64_string)
        except ValueError as e:
            await message.reply_text("❌ Invalid link format.")
            return

        if link_type == "HACKHEIST":
            if message.from_user.id != int(user_id_decoded):
                await message.reply_text("❌ You are not authorized!")
                return
            
            temp_msg = await message.reply("𝗥𝘂𝗸 𝗘𝗸 𝗦𝗲𝗰 👽..")
            try:
                messages = await get_messages(client, [f_msg_id], channel_id)
                if not messages or all(msg is None for msg in messages):
                    await temp_msg.edit("Failed to fetch message.")
                    return
            except Exception as e:
                await temp_msg.edit(f"Something went wrong: {str(e)}")
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
                    except Exception as e:
                        await message.reply_text(f"❌ Failed to process JSON: {str(e)}")
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

            if codeflix_msgs:
                special_msg = await send_random_special_message(client, message.from_user.id)
                if special_msg:
                    codeflix_msgs.append(special_msg)

                k = await client.send_message(
                    chat_id=message.from_user.id,
                    text=f"<b>‼️ 𝐓𝐡𝐢𝐬 𝐋𝐄𝐂𝐓𝐔𝐑𝐄/𝐏𝐃𝐅/𝐉𝐒𝐎𝐍 𝐰𝐢𝐥𝐥 𝐛𝐞 <u>𝗮𝘂𝘁𝗼-𝗱𝗲𝗹𝗲𝘁𝗲𝗱 𝗶𝗻 𝟯 𝗱𝗮𝘆𝘀</u> 💀</b>\n\n"
                         f"<b>⚡ Watch Lecture or Download now ✅ or Save it - Forward, Download & Keep in your Gallery before time runs out!</b>\n\n"
                         f"<b>🤝 Don't forget—share with friends, knowledge grows when shared ❣️</b>\n\n"
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
                if not messages or all(msg is None for msg in messages):
                    await temp_msg.edit("Failed to fetch messages.")
                    return
            except Exception as e:
                await temp_msg.edit(f"Something went wrong: {str(e)}")
                return
            finally:
                await temp_msg.delete()

            codeflix_msgs = []
            
            for msg in messages:
                if not msg:
                    continue
                
                if msg.document and msg.document.file_name.endswith('.json'):
                    try:
                        decrypted_json, filename = await download_and_decrypt_json(client, msg)
                        batch_title = filename.replace('.json', '').replace('_', ' ').title()
                        batch_thumbnail = 'https://via.placeholder.com/400x200?text=Catalog'
                        html_content = generate_html_from_decrypted(decrypted_json, batch_title, batch_thumbnail, user_first_name, user_id, made_at)
                        html_msg = await upload_html(client, html_content, batch_title, message.from_user.id)
                        codeflix_msgs.append(html_msg)
                    except Exception as e:
                        await message.reply_text(f"❌ Failed to generate catalog: {str(e)}")
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

                    base64_string2 = await encode_link(user_id=id, f_msg_id=msg.id, channel_id=channel_id)
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
                        user_id=id,
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
                         f"<b>😎 Don't worry! Even after deletion, you can still re-access everything anytime through our websites 😘</b>\n\n"
                         f"<b><a href='https://yashyasag.github.io/hiddens_officials'>🌟 𝗩𝗶𝘀𝗶𝘁 𝗠𝗼𝗿𝗲 𝗪𝗲𝗯𝘀𝗶𝘁𝗲𝘀 🌟</a></b>",
                )

                codeflix_msgs.append(k)
                asyncio.create_task(delete_files(codeflix_msgs, client, message, k, BULK_DELETE_TIME))
            return

    # Default start
    reply_markup = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🔥 𝗠𝗔𝗜𝗡 𝗪𝗘𝗕𝗦𝗜𝗧𝗘 🔥", url="https://yashyasag.github.io/hiddens_officials")
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
        except Exception as e:
            print(f"Error adding user {id}: {e}")

    buttons = [
        [InlineKeyboardButton(text="😈 𝗢𝗣𝗠𝗔𝗦𝗧𝗘𝗥𝗦 💀", url=client.invitelink4)],
        [
            InlineKeyboardButton(text="🌟 𝗝𝗼𝗶𝗻 𝟭𝘀𝘁 🌟", url=client.invitelink),
            InlineKeyboardButton(text="💝 𝗝𝗼𝗶𝗻 𝟮𝗻𝗱 💝", url=client.invitelink2),
        ],
        [InlineKeyboardButton(text="🕊 𝗝𝗼𝗶𝗻 𝟯𝗿𝗱 🕊", url=client.invitelink3)]
    ]
    try:
        buttons.append([InlineKeyboardButton(text='♻️ 𝐓𝐑𝐘 𝐀𝐆𝐀𝐈𝐍 ♻️', url=f"https://t.me/{client.username}?start={message.command[1]}")])
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

# ============================================================
# ADMIN COMMANDS
# ============================================================

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
        await message.reply_text(f"✅ Message ID {msg_id} added.")
    except (IndexError, ValueError):
        await message.reply_text("❌ Usage: /add_random_message <msg_id>")

@Bot.on_message(filters.command('remove_random_message') & filters.private & filters.user(ADMINS))
async def remove_random_message(client: Bot, message: Message):
    bot_id = client.username
    try:
        msg_id = int(message.text.split(" ", 1)[1])
        await remove_special_message(msg_id, bot_id)
        await message.reply_text(f"✅ Message ID {msg_id} removed.")
    except (IndexError, ValueError):
        await message.reply_text("❌ Usage: /remove_random_message <msg_id>")

@Bot.on_message(filters.command('list_random_messages') & filters.private & filters.user(ADMINS))
async def list_random_messages(client: Bot, message: Message):
    bot_id = client.username
    msg_ids = await get_special_messages(bot_id)
    response = f"<b>Special Messages for {bot_id}:</b>\n\n"
    if msg_ids:
        response += f"Message IDs: {', '.join(map(str, msg_ids))}\n"
    else:
        response += "No special messages configured.\n"
    await message.reply(response)

# ============================================================
# GENERATE LINK COMMAND (NEW)
# ============================================================

@Bot.on_message(filters.command('getlink') & filters.private & filters.user(ADMINS))
async def get_share_link(client: Bot, message: Message):
    """
    Usage: /getlink batch ABC123
           /getlink course XYZ789
    """
    try:
        parts = message.text.split()
        if len(parts) != 3:
            await message.reply("❌ Usage: /getlink {batch|course} {uid}")
            return
        
        item_type = parts[1].lower()
        uid = parts[2]
        
        if item_type not in ['batch', 'course']:
            await message.reply("❌ Type must be 'batch' or 'course'")
            return
        
        # Verify UID exists in API
        if item_type == "course":
            item = await fetch_course_details_by_uid(uid)
        else:
            item = await fetch_batch_details_by_uid(uid)
        
        if not item:
            await message.reply(f"❌ {item_type.title()} with UID {uid} not found in Unacademy API")
            return
        
        link = f"https://t.me/{client.username}?start={item_type}_{uid}"
        
        await message.reply(
            f"✅ <b>Share Link Generated</b>\n\n"
            f"📚 <b>Name</b>: {item.get('name', 'Unknown')}\n"
            f"🔗 <b>Link</b>: <code>{link}</code>\n\n"
            f"📋 Type: {item_type.title()}\n"
            f"🆔 UID: <code>{uid}</code>",
            disable_web_page_preview=True
        )
        
    except Exception as e:
        await message.reply(f"❌ Error: {str(e)}")
        import traceback
        traceback.print_exc()

# ============================================================
# BROADCAST COMMANDS
# ============================================================

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

    pls_wait = await message.reply("<i>ʙʀᴏᴀᴅᴄᴀꜱᴛ ᴘʀᴏᴄᴇꜱꜱɪɴɢ...</i>")

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

    status = f"""<b>ʙʀᴏᴀᴅᴄᴀꜱᴛ ᴄᴏᴍᴘʟᴇᴛᴇᴅ

ᴛᴏᴛᴀʟ ᴜꜱᴇʀꜱ: {total}
ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟ: {successful}
ʙʟᴏᴄᴋᴇᴅ: {blocked}
ᴅᴇʟᴇᴛᴇᴅ: {deleted}
ᴜɴꜱᴜᴄᴄᴇꜱꜱꜰᴜʟ: {unsuccessful}</b>"""

    await pls_wait.edit(status)

    if seconds:
        await asyncio.sleep(seconds)
        for chat_id, msg_id in sent_messages:
            try:
                await client.delete_messages(chat_id, msg_id)
            except Exception as e:
                print(f"Failed to delete: {e}")

@Bot.on_message(filters.private & filters.command('broadcast_add') & filters.user(ADMINS))
async def broadcast_add(client: Bot, message: Message):
    if not message.reply_to_message:
        await message.reply("❌ Reply to a message to schedule broadcast.")
        return

    try:
        parts = message.text.split(" ", 1)[1].split(":")
        if len(parts) not in [3, 4]:
            await message.reply("❌ Usage: /broadcast_add {total_time}:{interval}:{delete_after}[:{start_delay}]")
            return
        total_time, interval, delete_after = map(int, parts[:3])
        start_delay = int(parts[3]) if len(parts) == 4 else 0
    except (ValueError, IndexError):
        await message.reply("❌ Invalid format.")
        return

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
        f"Interval: {humanize.naturaldelta(interval)}\n"
        f"Started..."
    )

@Bot.on_message(filters.private & filters.command('broadcast_remove') & filters.user(ADMINS))
async def broadcast_remove(client: Bot, message: Message):
    bot_id = client.username
    try:
        schedule_id = message.text.split(" ", 1)[1]
        schedule = await get_schedule_by_id(schedule_id)
        if not schedule:
            await message.reply(f"❌ No schedule with ID: {schedule_id}")
            return
        if schedule['bot_id'] != bot_id:
            await message.reply(f"❌ Schedule belongs to another bot.")
            return

        await deactivate_scheduled_broadcast(schedule_id)
        global scheduled_broadcast_tasks
        task = scheduled_broadcast_tasks.get(schedule_id)
        if task and not task.done():
            task.cancel()
        scheduled_broadcast_tasks.pop(schedule_id, None)

        await message.reply(f"✅ Scheduled broadcast removed (ID: {schedule_id}).")
    except IndexError:
        schedules = await get_active_scheduled_broadcasts(bot_id)
        if not schedules:
            response = "❌ No active scheduled broadcasts."
        else:
            response = f"<b>Active Schedules:</b>\n\n"
            for schedule in schedules:
                response += f"ID: {schedule['_id']}\n"
        await message.reply(response)

# ============================================================
# DELETE FILES FUNCTION
# ============================================================

async def delete_files(codeflix_msgs, client, message, k, delete_time=None):
    if delete_time is None:
        delete_time = FILE_AUTO_DELETE
    
    await asyncio.sleep(delete_time)
    
    for msg in codeflix_msgs:
        try:
            await client.delete_messages(chat_id=msg.chat.id, message_ids=[msg.id])
        except Exception as e:
            print(f"Failed to delete media {msg.id}: {e}")
