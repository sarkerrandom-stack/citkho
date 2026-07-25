import os
import re
import json
import asyncio
import time
from datetime import datetime
from flask import Flask, request, jsonify
from playwright.async_api import async_playwright
import easyocr
import numpy as np
from PIL import Image
import io

app = Flask(__name__)

# Only allow 1 concurrent scrape to prevent OOM on low-RAM plans
scrape_semaphore = asyncio.Semaphore(1)


# ─────────────────────────────────────────────
# CAPTCHA Solver (EasyOCR)
# ─────────────────────────────────────────────
async def solve_captcha(reader, image_bytes):
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image_np = np.array(image)
        result = reader.readtext(image_np, detail=0, paragraph=False)
        text = ''.join(result)
        captcha_text = ''.join(c for c in text if c.isalnum()).upper()
        print(f"  OCR detected: '{captcha_text}' (len {len(captcha_text)})")
        return captcha_text if len(captcha_text) >= 4 else None
    except Exception as e:
        print(f"  OCR Error: {e}")
        return None


# ─────────────────────────────────────────────
# Extract CitizenKhotian from Next.js HTML
# ─────────────────────────────────────────────
def extract_citizen_khotian(html_content):
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
    async with scrape_semaphore:
        reader = None
        browser = None

        try:
            # 1. Load OCR model
            print(f"[{datetime.now()}] Loading EasyOCR...")
            reader = easyocr.Reader(['en'], gpu=False, verbose=False)

            # 2. Launch browser
            print(f"[{datetime.now()}] Launching Chromium...")
            p = await async_playwright().start()
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                    '--single-process'
                ]
            )
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
            )
            page = await context.new_page()

            # 3. LOGIN
            print(f"[{datetime.now()}] Opening login page...")
            await page.goto(
                "https://lsg-land-owner.land.gov.bd/login",
                wait_until='networkidle',
                timeout=60000
            )

            # Fill form (same selectors as your working code)
            try:
                await page.select_option('#country_code', '880', timeout=3000)
            except Exception:
                pass

            await page.fill('input[name="username"]', username)
            await page.fill('input[name="password"]', password)

            try:
                await page.check('input[value="mobile"]', timeout=3000)
            except Exception:
                pass

            # Solve CAPTCHA
            print(f"[{datetime.now()}] Solving CAPTCHA...")
            await page.wait_for_selector('#mainCaptcha', timeout=10000)

            captcha_solved = False
            for attempt in range(3):
                print(f"  CAPTCHA attempt {attempt + 1}/3")
                captcha_bytes = await page.locator('#mainCaptcha').screenshot()
                captcha_code = await solve_captcha(reader, captcha_bytes)

                if captcha_code and len(captcha_code) >= 4:
                    await page.fill('#txtInput', captcha_code)
                    captcha_solved = True
                    break
                else:
                    print("  Failed, reloading...")
                    await page.reload(wait_until='networkidle')
                    await page.fill('input[name="username"]', username)
                    await page.fill('input[name="password"]', password)
                    await asyncio.sleep(1)

            if not captcha_solved:
                return {"status": "failed", "message": "CAPTCHA failed after 3 attempts"}

            # Submit
            print(f"[{datetime.now()}] Submitting login...")
            await page.click('button[type="submit"]')

            try:
                await page.wait_for_load_state('networkidle', timeout=30000)
            except Exception:
                pass

            current_url = page.url
            print(f"  URL after login: {current_url}")

            if 'login' in current_url.lower():
                return {"status": "failed", "message": "Login failed — bad credentials or CAPTCHA"}

            # 4. Landing page → find LDTax link
            print(f"[{datetime.now()}] Going to landing page...")
            await page.goto(
                "https://lsg-land-owner.land.gov.bd/landing",
                wait_until='networkidle',
                timeout=60000
            )

            print(f"[{datetime.now()}] Looking for LDTax link...")
            ld_tax_link = None

            # Try by Bengali text
            ld_tax_link = await page.query_selector('a:has-text("ভূমি উন্নয়ন কর")')
            # Try by image src
            if not ld_tax_link:
                img = await page.query_selector('img[src*="ldtax"]')
                if img:
                    ld_tax_link = await img.evaluate('el => el.closest("a")')
            # Try by href pattern
            if not ld_tax_link:
                ld_tax_link = await page.query_selector('a[href*="portal.ldtax.gov.bd"]')

            if not ld_tax_link:
                links = await page.query_selector_all('a')
                debug = []
                for link in links[:15]:
                    href = await link.get_attribute('href') or ''
                    text = await link.inner_text() or ''
                    debug.append(f"{text.strip()[:40]} → {href[:80]}")
                return {
                    "status": "failed",
                    "message": "LDTax link not found on landing page",
                    "debug_links": debug
                }

            href = await ld_tax_link.get_attribute('href')
            print(f"  LDTax href: {href}")

            # 5. Follow OAuth redirects
            print(f"[{datetime.now()}] Following OAuth redirects...")
            await page.goto(href, wait_until='networkidle', timeout=60000)

            for i in range(15):
                current_url = page.url
                print(f"  Redirect check {i+1}: {current_url}")
                if 'portal.ldtax.gov.bd/citizen/welcome' in current_url:
                    break
                await asyncio.sleep(2)
                try:
                    await page.wait_for_load_state('networkidle', timeout=10000)
                except Exception:
                    pass
            else:
                return {
                    "status": "failed",
                    "message": f"Never reached welcome page. Stuck at: {page.url}"
                }

            # 6. Go to khotian page
            print(f"[{datetime.now()}] Opening khotian page...")
            await page.goto(
                "https://portal.ldtax.gov.bd/citizen/khotian",
                wait_until='networkidle',
                timeout=60000
            )

            # 7. Extract data
            print(f"[{datetime.now()}] Extracting khotian data...")
            html = await page.content()
            citizen_khotians = extract_citizen_khotian(html)
            print(f"  Found {len(citizen_khotians)} records")

            if not citizen_khotians:
                return {"status": "failed", "message": "No khotian data found", "url": page.url}

            # 8. Find by khotian_no
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

            return {"status": "success", "data": result}

        except Exception as e:
            import traceback
            return {
                "status": "failed",
                "message": str(e),
                "traceback": traceback.format_exc()
            }

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
    """UptimeRobot pings this every 5 min to keep the service alive"""
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