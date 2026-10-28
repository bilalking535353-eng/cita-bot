"""
Cita Previa Bot v8 - Fully Perfected & WAF-Resilient Edition
"""

import asyncio
import json
import logging
import os
import random
import re
import time
import unicodedata
from pathlib import Path

import httpx
from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

# =============================================================================
# 1. CONFIGURATION
# =============================================================================
TARGET_LOCATION = "ARABA"
PROCEDURE_TEXT = "TOMA DE HUELLAS"
OFFICE_KEYWORD = ""            
USE_CLAVE = True                

NIE_INPUT = "Z5597174Z"          
FULL_NAME = "PANJWANI NILAM"    
NATIONALITY = "INDIA"
PHONE_INPUT = "618488816"       
EMAIL_INPUT = "bilalking535353@gmail.com"

WHATSAPP_ALERT_NUMBER = "+923148789255"
CALLMEBOT_APIKEY = os.environ.get("CALLMEBOT_APIKEY", "")

PORTAL_URL = os.environ.get(
    "CITA_PORTAL_URL", "https://icp.administracionelectronica.gob.es/icpplus/index.html")
PORTAL_HOST = os.environ.get("CITA_PORTAL_HOST", "administracionelectronica.gob.es")

BROWSER_CHANNEL = "chrome"      
AUTO_PRESS_ENTER_FOR_CERT_BOX = True   

SPEED = float(os.environ.get("CITA_SPEED", "1"))
POLL_SECONDS = (25, 40)         
REJECTION_WAITS = (30, 90, 300)  
REFRESHES_BEFORE_NEW_LOGIN = 5  
RESTART_DELAY_SECONDS = 15
RATE_LIMIT_BACKOFF_SECONDS = 15 * 60
HUMAN_WAIT_SECONDS = 600        
MAX_IDLE_STEPS = 40
REMINDER_COUNT = 6
REMINDER_EVERY_SECONDS = 45

NATIONALITY_PORTAL_LABEL = {"pakistani": "PAKISTAN", "pakistan": "PAKISTAN", "india": "INDIA"}
NO_SLOT_PHRASES = ("no hay citas", "no hay horas", "no existen citas")
NIE_REJECTED_PHRASES = ("debe acabar por una letra", "debe comenzar por la letra",
                        "nie introducido es incorrecto", "es demasiado corto")

SEL = {
    "cookie": "#cookie_action_close_header",
    "province": "select#form",
    "procedure_any": "select[id^='tramiteGrupo']",
    "accept": "#btnAceptar, input[value='Aceptar']",
    "clave_btn": "#btnAccesoClave",
    "no_clave_btn": "#btnEntrar",
    "nie_radio": "#rdbTipoDocNie",
    "nie": "#txtIdCitado, input[name='txtIdCitado']",
    "name": "#txtDesCitado, input[name='txtDesCitado']",
    "country": "#txtPaisNac, select[name='txtPaisNac']",
    "nie_accept": "#btnEnviar, #btnAceptar, input[value='Aceptar'], input[value='Accept']",
    "solicitar": "input[value='Solicitar Cita']",
    "office": "select#idSede",
    "next": "#btnSiguiente",
    "phone": "#txtTelefonoCitado",
    "email1": "#emailUNO",
    "email2": "#emailDOS",
    "dates": "input[name='rdbCita'], #txtCaptcha, #captcha",
}
DNIE_SELECTORS = [
    "text=/Acceso DNIe/i",
    "button:has-text('Certificado electr')",
    "a:has-text('Certificado electr')",
    "text=/Access e-?ldentifier/i",
    "text=/Access DNIe/i",
]
OK_SELECTORS = ["button:has-text('OK')", "input[value='OK']", "#btnOk"]

Path("debug").mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cita-bot-perfect")


class RateLimited(Exception):
    pass


class ConfigError(Exception):
    pass


class SessionRejected(Exception):
    pass


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


async def sleep(seconds: float):
    await asyncio.sleep(seconds * SPEED)


async def human_pause(a: float = 1.5, b: float = 3.0):
    await sleep(random.uniform(a, b))


async def settle(page, extra=(2.0, 4.0)):
    try:
        await page.wait_for_load_state("networkidle", timeout=int(6000 * min(1, SPEED)) or 500)
    except Exception:
        pass
    await human_pause(*extra)


async def send_whatsapp(message: str) -> bool:
    if not CALLMEBOT_APIKEY:
        log.warning("CALLMEBOT_APIKEY not set - WhatsApp alert skipped: %s", message)
        return False
    params = {"phone": WHATSAPP_ALERT_NUMBER, "text": message, "apikey": CALLMEBOT_APIKEY}
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.get("https://api.callmebot.com/whatsapp.php", params=params)
        return r.status_code == 200
    except Exception as exc:
        log.error("WhatsApp send error: %s", exc)
        return False


def beep():
    try:
        import winsound
        for _ in range(5):
            winsound.Beep(1200, 400)
    except Exception:
        print("\a", end="", flush=True)


def press_enter_os(window_title: str = "") -> bool:
    if os.name == "nt":
        try:
            import ctypes
            user32 = ctypes.windll.user32
            user32.keybd_event(0x0D, 0, 0, 0)
            time.sleep(0.08)
            user32.keybd_event(0x0D, 0, 2, 0)
            return True
        except Exception as exc:
            log.warning("Windows key press failed: %s", exc)
    try:
        import pyautogui
        pyautogui.press("enter")
        return True
    except Exception:
        return False


async def body_text(page) -> str:
    try:
        return norm(await page.inner_text("body", timeout=5000))
    except Exception:
        return ""


async def check_page(page, body: str = ""):
    body = body or await body_text(page)
    if re.search(r"\b0019\b", body) or "sobrepasado el numero permitido" in body:
        raise RateLimited("Rate limited / IP blocked (0019)")
    if "requested url was rejected" in body or "replay attack" in body or "request rejected" in body:
        raise SessionRejected("Firewall rejected the request ('requested URL was rejected')")
    if "service unavailable" in body or "servicio no disponible" in body:
        raise RuntimeError("Service unavailable (503)")


async def vis(page, selector: str) -> bool:
    try:
        return await page.locator(selector).first.is_visible()
    except Exception:
        return False


async def first_visible_click(page, selectors) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible():
                await loc.click(timeout=2500, no_wait_after=True)
                return True
        except Exception:
            continue
    return False


async def dismiss_cookie_bar(page):
    try:
        loc = page.locator(SEL["cookie"]).first
        if await loc.is_visible():
            await loc.click(timeout=1500)
    except Exception:
        pass


async def click_and_wait(page, selector: str, gone: str = "", fallback_name: str = "") -> bool:
    clicked = False
    try:
        loc = page.locator(selector).first
        if await loc.is_visible():
            await loc.scroll_into_view_if_needed()
            await human_pause(1.2, 2.2) 
            box = await loc.bounding_box()
            if box:
                await page.mouse.move(
                    box["x"] + box["width"] * random.uniform(0.3, 0.7),
                    box["y"] + box["height"] * random.uniform(0.3, 0.7),
                    steps=random.randint(5, 10)
                )
            await loc.click(timeout=6000, delay=random.randint(120, 250), no_wait_after=True)
            clicked = True
    except Exception:
        pass
    if not clicked and fallback_name:
        try:
            await page.get_by_role("button", name=re.compile(fallback_name, re.I)).first.click(timeout=3000)
            clicked = True
        except Exception:
            pass
    if not clicked:
        return False
    if gone:
        try:
            await page.locator(gone).first.wait_for(state="hidden", timeout=12000)
        except Exception:
            pass
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    return True


async def fill(page, selector: str, value: str) -> bool:
    try:
        loc = page.locator(selector).first
        if await loc.is_visible():
            await loc.click(timeout=2500)
            await loc.fill("")
            for char in value:
                await loc.press_sequentially(char, delay=random.randint(100, 250)) 
                await asyncio.sleep(random.uniform(0.05, 0.12))
            await human_pause(1.0, 1.8)
            return True
    except Exception:
        pass
    return False


async def pick_any(page, wanted: str, selector: str = "select") -> bool:
    for sel in await page.locator(selector).all():
        try:
            for o in await sel.locator("option").all_inner_texts():
                if norm(wanted) in norm(o):
                    await sel.select_option(label=o)
                    log.info("Selected '%s'", o.strip())
                    await human_pause(1.0, 2.0) 
                    return True
        except Exception:
            continue
    return False


async def has_option(page, wanted: str) -> bool:
    try:
        opts = await page.evaluate(
            "() => Array.from(document.querySelectorAll('select')).flatMap("
            "s => Array.from(s.options).map(o => o.text))")
    except Exception:
        return False
    return any(norm(wanted) in norm(o) for o in opts)


async def wait_for_login(page):
    log.info("On Cl@ve site - handling certificate securely")
    start = time.monotonic()
    last_click = None
    enters = 0
    while PORTAL_HOST not in page.url:
        now = time.monotonic()
        if now - start > HUMAN_WAIT_SECONDS:
            raise RuntimeError("Cl@ve login took too long")

        if last_click is None or now - last_click > 12:
            if await first_visible_click(page, DNIE_SELECTORS):
                log.info("Clicked DNIe / certificate button")
                last_click = now
                enters = 0
        elif AUTO_PRESS_ENTER_FOR_CERT_BOX and enters < 6:
            try:
                title = await page.title()
            except Exception:
                title = ""
            if press_enter_os(title):
                log.info("Pressed Enter/OK on certificate popup (try %d)", enters + 1)
                enters += 1
                await asyncio.sleep(1.5)
            else:
                enters = 6

        if await first_visible_click(page, OK_SELECTORS):
            log.info("Clicked an 'OK' button")

        await asyncio.sleep(0.8)

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    log.info("Back on portal after login.")


async def advance(page, fresh: bool) -> bool:
    if fresh:
        await page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=30000)
        await settle(page, (2.5, 4.5))

    requested = False
    nie_submits = 0
    idle = 0

    while idle < MAX_IDLE_STEPS:
        await dismiss_cookie_bar(page)
        
        body = await body_text(page)
        if "request rejected" in body or "requested url was rejected" in body:
            log.warning("WAF ne request reject kar di. Session reset kar rahe hain...")
            raise SessionRejected("Firewall hit Request Rejected page")

        if PORTAL_HOST not in page.url:
            await wait_for_login(page)
            idle = 0
            continue

        await check_page(page, body)
        no_slots = any(p in body for p in NO_SLOT_PHRASES)

        if await vis(page, SEL["dates"]) or await vis(page, SEL["office"]):
            return True

        nie_here = await vis(page, SEL["nie"])
        if nie_here and no_slots:
            return False
        elif requested and no_slots:
            return False

        if await vis(page, SEL["province"]):
            if not await pick_any(page, TARGET_LOCATION, SEL["province"]):
                raise RuntimeError(f"Province not found: {TARGET_LOCATION}")
            await human_pause(2.0, 3.0) # Extra safe pause after picking province to bypass WAF block
            await click_and_wait(page, SEL["accept"], gone=SEL["province"])
            idle = 0
            continue

        if await has_option(page, PROCEDURE_TEXT):
            if not await pick_any(page, PROCEDURE_TEXT, SEL["procedure_any"]):
                raise RuntimeError(f"Procedure not found: {PROCEDURE_TEXT}")
            await sleep(2.0)  
            if not await click_and_wait(page, SEL["accept"], gone=SEL["procedure_any"]):
                raise RuntimeError("Procedure chosen but no Aceptar button found")
            idle = 0
            continue

        if await vis(page, SEL["clave_btn"]) or await vis(page, SEL["no_clave_btn"]):
            btn = SEL["clave_btn"] if USE_CLAVE else SEL["no_clave_btn"]
            log.info("Choosing mode: %s", "con Cl@ve" if USE_CLAVE else "sin Cl@ve")
            await click_and_wait(page, btn, gone=SEL["no_clave_btn"])
            idle = 0
            continue

        if nie_here:
            nie_submits += 1
            if nie_submits > 2:
                raise RuntimeError("NIE form keeps returning.")
            log.info("Filling NIE details safely...")
            try:
                radio = page.locator(SEL["nie_radio"]).first
                if await radio.is_visible():
                    await radio.check()
            except Exception:
                pass
            await fill(page, SEL["nie"], NIE_INPUT)
            await fill(page, SEL["name"], FULL_NAME)
            try:
                nat = page.locator(SEL["country"]).first
                if await nat.is_visible():
                    await pick_any(page, NATIONALITY_PORTAL_LABEL.get(norm(NATIONALITY), NATIONALITY), SEL["country"])
            except Exception:
                pass
            if not await click_and_wait(page, SEL["nie_accept"], gone=SEL["nie"], fallback_name="^(aceptar|accept)$"):
                raise RuntimeError("NIE form: could not find Aceptar button")
            if await vis(page, SEL["nie"]):
                b2 = await body_text(page)
                if any(p in b2 for p in NIE_REJECTED_PHRASES):
                    raise ConfigError("Portal rejected NIE / name configuration.")
            idle = 0
            continue

        if await vis(page, SEL["solicitar"]):
            if requested:
                return False
            requested = True
            log.info("Pressing 'Solicitar Cita'")
            await click_and_wait(page, SEL["solicitar"], gone=SEL["solicitar"])
            idle = 0
            continue

        idle += 1
        await sleep(1.5)

    raise RuntimeError("Stuck on unrecognised page state.")


async def book_slot(page):
    log.info("SLOT FOUND - filling contact details safely")
    await page.bring_to_front()
    await send_whatsapp(f"SLOT FOUND for {TARGET_LOCATION}! Complete booking manually.")
    beep()

    if await vis(page, SEL["office"]):
        office = page.locator(SEL["office"]).first
        options = await office.locator("option").all_inner_texts()
        target = None
        if OFFICE_KEYWORD:
            target = next((o for o in options if norm(OFFICE_KEYWORD) in norm(o)), None)
        if target is None:
            real = [o for o in options if o.strip() and "selecc" not in norm(o)]
            target = real[0] if real else None
        if target:
            await office.select_option(label=target)
            log.info("Office selected: %s", target.strip()[:80])
        await click_and_wait(page, SEL["next"], gone=SEL["office"])

    if await vis(page, SEL["phone"]) or await page.locator(SEL["phone"]).count():
        await page.locator(SEL["phone"]).first.wait_for(state="visible", timeout=15000)
        await fill(page, SEL["phone"], PHONE_INPUT)
        await fill(page, SEL["email1"], EMAIL_INPUT)
        await fill(page, SEL["email2"], EMAIL_INPUT)
        await click_and_wait(page, SEL["next"], gone=SEL["phone"])

    reached = False
    try:
        await page.locator(SEL["dates"]).first.wait_for(state="visible", timeout=15000)
        reached = True
    except Exception:
        pass

    msg = ("DATES ON SCREEN! Solve captcha and choose date now." if reached else
           "Details submitted. Check browser window to complete booking.")
    log.info(msg)
    beep()

    for i in range(REMINDER_COUNT):
        await send_whatsapp(f"{msg} (reminder {i + 1}/{REMINDER_COUNT})")
        try:
            await page.wait_for_event("close", timeout=REMINDER_EVERY_SECONDS * 1000)
            return
        except PWTimeout:
            continue
    await page.wait_for_event("close", timeout=0)


async def launch(pw):
    kwargs = dict(
        headless=False,
        args=[
            "--start-maximized",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ]
    )
    if BROWSER_CHANNEL:
        try:
            return await pw.chromium.launch(channel=BROWSER_CHANNEL, **kwargs)
        except Exception:
            pass
    return await pw.chromium.launch(**kwargs)


async def run_bot_workflow() -> bool:
    async with async_playwright() as pw:
        browser = await launch(pw)
        holder = {}

        async def fresh_page():
            old = holder.get("ctx")
            if old:
                try:
                    await old.close()
                except Exception:
                    pass
            ctx = await browser.new_context(
                no_viewport=True,
                locale="es-ES",
                timezone_id="Europe/Madrid",
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                extra_http_headers={
                    "Accept-Language": "es-ES,es;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"Windows"',
                }
            )
            
            await ctx.add_init_script("""
                delete Object.getPrototypeOf(navigator).webdriver;
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
                window.navigator.chrome = {
                    runtime: {}
                };
            """)

            pg = await ctx.new_page()
            pg.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
            holder["ctx"] = ctx
            return pg

        page = await fresh_page()
        try:
            attempt = 0
            refreshes = 0
            rejections = 0
            first = True
            while True:
                attempt += 1
                log.info("Check #%d", attempt)
                try:
                    found = await advance(page, fresh=first)
                except SessionRejected as exc:
                    rejections += 1
                    if rejections > len(REJECTION_WAITS):
                        raise RateLimited("Portal rejected too many requests in a row")
                    wait = REJECTION_WAITS[rejections - 1]
                    log.warning("%s - waiting %d s and restarting full fresh flow", exc, wait)
                    await asyncio.sleep(wait)
                    page = await fresh_page()
                    rejections = 0
                    first = True
                    continue
                except RateLimited as exc:
                    log.warning("Rate limited: %s. Pausing for 15 minutes.", exc)
                    await asyncio.sleep(RATE_LIMIT_BACKOFF_SECONDS)
                    page = await fresh_page()
                    first = True
                    continue

                if found:
                    await book_slot(page)
                    return True

                refreshes += 1
                first = False
                if refreshes >= REFRESHES_BEFORE_NEW_LOGIN:
                    log.info("Refreshing full session flow...")
                    await asyncio.sleep(RESTART_DELAY_SECONDS)
                    page = await fresh_page()
                    refreshes = 0
                    first = True
                else:
                    delay = random.uniform(*POLL_SECONDS)
                    log.info("No slots available. Waiting %.1f s...", delay)
                    await asyncio.sleep(delay)
                    try:
                        await page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=20000)
                        first = True
                    except Exception:
                        page = await fresh_page()
                        first = True
        finally:
            try:
                await browser.close()
            except Exception:
                pass
    return False


def main():
    log.info("Starting Perfected Cita Bot for %s (%s)", TARGET_LOCATION, PROCEDURE_TEXT)
    while True:
        try:
            asyncio.run(run_bot_workflow())
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break
        except ConfigError as exc:
            log.error("Configuration error: %s", exc)
            break
        except Exception as exc:
            log.error("Unexpected error: %s. Restarting in 30 seconds...", exc, exc_info=True)
            time.sleep(30)


if __name__ == "__main__":
    main()