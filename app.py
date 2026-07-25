import os
import re
import json
import asyncio
import time
import traceback
from datetime import datetime
from flask import Flask, request, jsonify
from playwright.async_api import async_playwright
from PIL import Image, ImageEnhance, ImageFilter
import pytesseract
import io

app = Flask(__name__)

# ─────────────────────────────────────────────
# CAPTCHA Solver (Tesseract - lightweight)
# ─────────────────────────────────────────────
async def solve_captcha(image_bytes):
    """Solve CAPTCHA using Tesseract OCR (much lighter than EasyOCR)"""
    try:
        image = Image.open(io.BytesIO(image_bytes))
        
        # Preprocess for better accuracy
        image = image.convert('L')  # Grayscale
        image = image.filter(ImageFilter.MedianFilter(size=3))
        enhancer = ImageEnhance.Contrast(image)
        image = enhancer.enhance(2.0)
        
        # Tesseract with strict alphanumeric whitelist
        text = pytesseract.image_to_string(
            image,
            config='--psm 7 --oem 3 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
        )
        
        captcha_text = ''.join(c for c in text if c.isalnum()).upper().strip()
        print(f"  Tesseract result: '{captcha_text}' (len {len(captcha_text)})")
        return captcha_text if len(captcha_text) >= 4 else None
        
    except Exception as e:
        print(f"  OCR Error: {e}")
        return None


# ─────────────────────────────────────────────
# Extract CitizenKhotian from HTML
# ─────────────────────────────────────────────
def extract_citizen_khotian(html_content):
    """Extract CitizenKhotian array from Next.js rendered HTML"""
    
    # Method 1: Standard __NEXT_DATA__ script tag
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html_content, re.DOTALL
    )
    if m:
        try:
            data = json.loads(m.group(1))
            props = data.get('props', {}).get('pageProps', {})
            if 'CitizenKhotian' in props:
                return props['CitizenKhotian']
        except Exception:
            pass

    # Method 2: Next.js flight data (self.__next_f.push)
    pushes = re.findall(
        r'self\.__next_f\.push\(\[1,"(.*?)"\]\)',
        html_content, re.DOTALL
    )
    full_text = ''.join(pushes)
    m = re.search(r'"CitizenKhotian":(\[.*?\]),"profileStatus"', full_text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass

    # Method 3: Direct search in raw HTML
    m = re.search(r'"CitizenKhotian":(\[.*?\]),"profileStatus"', html_content, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass

    return []


# ─────────────────────────────────────────────
# Main Scraping Logic
# ─────────────────────────────────────────────
async def scrape_khotian(username, password, khotian_no):
    browser = None
    start_time = time.time()

    try:
        print(f"\n{'='*60}")
        print(f"[{datetime.now()}] Starting scrape for khotian: {khotian_no}")
        print(f"[{datetime.now()}] Launching Chromium...")

        p = await async_playwright().start()
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disable-extensions',
                '--disable-software-rasterizer',
                '--single-process'
            ]
        )
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 720},
            user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
        )
        page = await context.new_page()

        # ── 1. LOGIN ──
        print(f"[{datetime.now()}] Opening login page...")
        await page.goto(
            "https://lsg-land-owner.land.gov.bd/login",
            wait_until='domcontentloaded',
            timeout=60000
        )

        # Fill form
        try:
            await page.select_option('#country_code', '880', timeout=5000)
        except Exception:
            pass

        await page.fill('input[name="username"]', username)
        await page.fill('input[name="password"]', password)

        try:
            await page.check('input[value="mobile"]', timeout=5000)
        except Exception:
            pass

        # Solve CAPTCHA
        print(f"[{datetime.now()}] Solving CAPTCHA...")
        await page.wait_for_selector('#mainCaptcha', timeout=15000)

        captcha_solved = False
        for attempt in range(3):
            print(f"  CAPTCHA attempt {attempt + 1}/3")
            captcha_bytes = await page.locator('#mainCaptcha').screenshot()
            captcha_code = await solve_captcha(captcha_bytes)

            if captcha_code and len(captcha_code) >= 4:
                await page.fill('#txtInput', captcha_code)
                captcha_solved = True
                break
            else:
                print("  Failed, reloading...")
                await page.reload(wait_until='domcontentloaded')
                await page.fill('input[name="username"]', username)
                await page.fill('input[name="password"]', password)
                await asyncio.sleep(1)

        if not captcha_solved:
            return {"status": "failed", "message": "CAPTCHA failed after 3 attempts"}

        # Submit login
        print(f"[{datetime.now()}] Submitting login...")
        await page.click('button[type="submit"]')

        # Wait for redirect (poll for up to 20 seconds)
        for i in range(20):
            await asyncio.sleep(1)
            current_url = page.url
            if 'login' not in current_url.lower():
                break
        else:
            return {"status": "failed", "message": "Login failed — still on login page"}

        print(f"  Logged in. URL: {current_url}")

        # ── 2. LANDING PAGE → find LDTax link ──
        print(f"[{datetime.now()}] Going to landing page...")
        await page.goto(
            "https://lsg-land-owner.land.gov.bd/landing",
            wait_until='domcontentloaded',
            timeout=60000
        )

        print(f"[{datetime.now()}] Looking for LDTax link...")
        ld_tax_link = None

        # Try multiple selectors
        selectors = [
            'a:has-text("ভূমি উন্নয়ন কর")',
            'a:has-text("LDTax")',
            'a:has(img[src*="ldtax"])',
            'a[href*="portal.ldtax.gov.bd"]'
        ]

        for sel in selectors:
            try:
                ld_tax_link = await page.query_selector(sel)
                if ld_tax_link:
                    break
            except Exception:
                continue

        # Fallback: scan all links
        if not ld_tax_link:
            links = await page.query_selector_all('a')
            for link in links:
                href = await link.get_attribute('href') or ''
                text = await link.inner_text() or ''
                if 'ldtax' in href.lower() or 'ভূমি' in text:
                    ld_tax_link = link
                    break

        if not ld_tax_link:
            return {"status": "failed", "message": "LDTax link not found on landing page"}

        href = await ld_tax_link.get_attribute('href')
        print(f"  LDTax href: {href}")

        # ── 3. Follow OAuth redirects ──
        print(f"[{datetime.now()}] Following OAuth redirects...")
        await page.goto(href, wait_until='domcontentloaded', timeout=60000)

        for i in range(20):
            current_url = page.url
            print(f"  Redirect {i+1}: {current_url}")
            if 'portal.ldtax.gov.bd/citizen/welcome' in current_url:
                break
            await asyncio.sleep(2)
            try:
                await page.wait_for_load_state('networkidle', timeout=10000)
            except Exception:
                pass
        else:
            return {"status": "failed", "message": f"Never reached welcome page. Stuck at: {page.url}"}

        # ── 4. Go to khotian page ──
        print(f"[{datetime.now()}] Opening khotian page...")
        await page.goto(
            "https://portal.ldtax.gov.bd/citizen/khotian",
            wait_until='domcontentloaded',
            timeout=60000
        )

        # Wait for JS to render data
        await asyncio.sleep(3)

        # ── 5. Extract data ──
        print(f"[{datetime.now()}] Extracting khotian data...")
        html = await page.content()
        citizen_khotians = extract_citizen_khotian(html)
        print(f"  Found {len(citizen_khotians)} records")

        if not citizen_khotians:
            # Try one more time after waiting
            await asyncio.sleep(3)
            html = await page.content()
            citizen_khotians = extract_citizen_khotian(html)
            if not citizen_khotians:
                return {"status": "failed", "message": "No khotian data found", "url": page.url}

        # ── 6. Find by khotian_no ──
        result = None
        for k in citizen_khotians:
            if str(k.get('khotian_no')) == str(khotian_no):
                result = {
                    "khotian_id": k.get('id'),
                    "citizen_id": k.get('citizen_id'),
                    "khotian_no": k.get('khotian_no'),
                    "holding_no": k.get('holding_no'),
                    "district_bn": k.get('districts', {}).get('name_bn'),
                    "district_en": k.get('districts', {}).get('name_en'),
                    "upazila_bn": k.get('upazilas', {}).get('name_bd'),
                    "upazila_en": k.get('upazilas', {}).get('name_en'),
                    "mouja_bn": k.get('moujas', {}).get('name_bd'),
                    "mouja_jl_no": k.get('moujas', {}).get('jl_no')
                }
                break

        if not result:
            available = [str(k.get('khotian_no')) for k in citizen_khotians]
            return {
                "status": "failed",
                "message": f"Khotian '{khotian_no}' not found",
                "available_khotians": available
            }

        elapsed = round(time.time() - start_time, 2)
        print(f"[{datetime.now()}] Success in {elapsed}s")
        return {"status": "success", "data": result, "elapsed_seconds": elapsed}

    except Exception as e:
        err = traceback.format_exc()
        print(f"[{datetime.now()}] ERROR: {str(e)}")
        return {"status": "failed", "message": str(e), "traceback": err}

    finally:
        if browser:
            try:
                await browser.close()
                print(f"[{datetime.now()}] Browser closed")
            except Exception:
                pass


# ─────────────────────────────────────────────
# Flask Routes
# ─────────────────────────────────────────────
@app.route('/health', methods=['GET'])
def health():
    """UptimeRobot pings this to keep the service alive"""
    return jsonify({
        "status": "alive",
        "service": "khotian-scraper",
        "time": time.time()
    })


@app.route('/scrape', methods=['POST'])
def scrape_endpoint():
    data = request.get_json(force=True, silent=True) or {}
    username = data.get('username')
    password = data.get('password')
    khotian_no = data.get('khotian_no')

    if not all([username, password, khotian_no]):
        return jsonify({
            "status": "failed",
            "message": "Missing fields. Required: username, password, khotian_no"
        }), 400

    print(f"\n{'='*60}")
    print(f"REQUEST: khotian_no={khotian_no} | user={username}")
    print(f"{'='*60}")

    # Run async scrape inside sync Flask
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(scrape_khotian(username, password, khotian_no))
    finally:
        loop.close()

    return jsonify(result)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
