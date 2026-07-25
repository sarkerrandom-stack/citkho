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
    """Solve CAPTCHA using Tesseract OCR"""
    try:
        image = Image.open(io.BytesIO(image_bytes)).convert('L')
        image = image.filter(ImageFilter.MedianFilter(size=3))
        enhancer = ImageEnhance.Contrast(image)
        image = enhancer.enhance(2.0)
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
    # Method 1: Standard __NEXT_DATA__
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
        except: pass

    # Method 2: Next.js flight data
    pushes = re.findall(
        r'self\.__next_f\.push\(\[1,"(.*?)"\]\)',
        html_content, re.DOTALL
    )
    full_text = ''.join(pushes)
    m = re.search(r'"CitizenKhotian":(\[.*?\]),"profileStatus"', full_text, re.DOTALL)
    if m:
        try: return json.loads(m.group(1))
        except: pass

    # Method 3: Direct search
    m = re.search(r'"CitizenKhotian":(\[.*?\]),"profileStatus"', html_content, re.DOTALL)
    if m:
        try: return json.loads(m.group(1))
        except: pass
    return []


# ─────────────────────────────────────────────
# Main Scraping Logic (follows your working code pattern)
# ─────────────────────────────────────────────
async def scrape_khotian(username, password, khotian_no):
    browser = None
    start_time = time.time()

    try:
        print(f"\n{'='*60}")
        print(f"[{datetime.now()}] Starting scrape for khotian: {khotian_no}")

        p = await async_playwright().start()
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu'
            ]
        )
        context = await browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
        )
        page = await context.new_page()

        # ── 1. LOGIN (exact pattern from your working code) ──
        print(f"[{datetime.now()}] Opening login page...")
        await page.goto(
            "https://lsg-land-owner.land.gov.bd/login",
            wait_until='networkidle',
            timeout=60000
        )

        # Fill form exactly like working code
        await page.select_option('#country_code', '880')
        await page.fill('input[name="username"]', username)
        await page.fill('input[name="password"]', password)
        await page.check('input[value="mobile"]')

        # Solve CAPTCHA (same pattern as working code)
        print(f"[{datetime.now()}] Solving CAPTCHA...")
        await page.wait_for_selector('#mainCaptcha', timeout=10000)

        captcha_code = None
        for attempt in range(3):
            print(f"  CAPTCHA attempt {attempt + 1}/3")

            captcha_bytes = await page.locator('#mainCaptcha').screenshot()
            captcha_code = await solve_captcha(captcha_bytes)

            if captcha_code and len(captcha_code) >= 4:
                print(f"  ✅ CAPTCHA solved: {captcha_code}")
                break
            else:
                print(f"  ❌ Failed, refreshing...")
                await page.reload(wait_until='networkidle')
                await page.select_option('#country_code', '880')
                await page.fill('input[name="username"]', username)
                await page.fill('input[name="password"]', password)
                await page.check('input[value="mobile"]')
                await asyncio.sleep(1)

        if not captcha_code:
            return {"status": "failed", "message": "CAPTCHA failed after 3 attempts"}

        await page.fill('#txtInput', captcha_code)

        # Submit (same as working code)
        print(f"[{datetime.now()}] Submitting form...")
        await page.click('button[type="submit"]')

        try:
            await page.wait_for_load_state('networkidle', timeout=30000)
        except:
            pass

        current_url = page.url
        print(f"  URL after login: {current_url}")

        # Check for login failure
        if 'login' in current_url.lower() or 'error' in current_url.lower():
            content = await page.content()
            if 'captcha' in content.lower() and 'invalid' in content.lower():
                return {"status": "failed", "message": "Login failed: Invalid captcha or credentials"}

        # Handle OAuth callback redirect (same as working code)
        if 'citizen-callback?code=' in current_url or 'callback?code=' in current_url:
            await page.reload(wait_until='networkidle')

        final_url = page.url
        print(f"  ✅ Login successful! Final URL: {final_url}")

        # ── 2. LANDING PAGE → find LDTax link ──
        print(f"[{datetime.now()}] Going to landing page...")
        await page.goto(
            "https://lsg-land-owner.land.gov.bd/landing",
            wait_until='networkidle',
            timeout=60000
        )

        print(f"[{datetime.now()}] Looking for LDTax link...")
        ld_tax_link = None

        # Try by Bengali text
        try:
            ld_tax_link = await page.query_selector('a:has-text("ভূমি উন্নয়ন কর")')
        except: pass

        # Try by image src
        if not ld_tax_link:
            try:
                img = await page.query_selector('img[src*="ldtax"]')
                if img:
                    ld_tax_link = await img.evaluate('el => el.closest("a")')
            except: pass

        # Try by href pattern
        if not ld_tax_link:
            try:
                ld_tax_link = await page.query_selector('a[href*="portal.ldtax.gov.bd"]')
            except: pass

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
        await page.goto(href, wait_until='networkidle', timeout=60000)

        for i in range(15):
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

        # ── 4. Khotian Page ──
        print(f"[{datetime.now()}] Opening khotian page...")
        await page.goto(
            "https://portal.ldtax.gov.bd/citizen/khotian",
            wait_until='networkidle',
            timeout=60000
        )

        # Wait for Next.js to render data
        await asyncio.sleep(3)

        # ── 5. Extract Data ──
        print(f"[{datetime.now()}] Extracting khotian data...")
        html = await page.content()
        citizen_khotians = extract_citizen_khotian(html)
        print(f"  Found {len(citizen_khotians)} records")

        if not citizen_khotians:
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


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "alive", "service": "khotian-scraper", "time": time.time()})


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
