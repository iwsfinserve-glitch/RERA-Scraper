import os
import re
import io
import time
import logging
import math
from glob import glob
from urllib.parse import urljoin
import random

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from selenium.webdriver.support.expected_conditions import alert_is_present

from bs4 import BeautifulSoup, NavigableString
import pandas as pd
from PIL import Image, ImageEnhance
import pytesseract

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

BASE_URL = "https://rera.goa.gov.in"
SEARCH_URL = f"{BASE_URL}/projects"
CACHE_DIR = "./cache"
DEEDS_DIR = "./RERA_Deeds"


# ──────────────────────────────────────────
# Custom Exceptions
# ──────────────────────────────────────────
class CaptchaFailureError(Exception):
    pass


class ParserError(Exception):
    pass


# ══════════════════════════════════════════
# CLASS A — RERADownloader
# ══════════════════════════════════════════
class RERADownloader:
    """Selenium-only downloader. Zero parsing logic."""

    def __init__(self, headless=True):
        os.makedirs(CACHE_DIR, exist_ok=True)
        os.makedirs(DEEDS_DIR, exist_ok=True)
        self.driver = self._build_driver(headless=headless)
        self.wait = WebDriverWait(self.driver, 15)

    def _build_driver(self, headless=True):
        options = ChromeOptions()
        if headless:
            options.add_argument("--headless=new")
        prefs = {
            "download.default_directory": os.path.abspath(DEEDS_DIR),
            "download.prompt_for_download": False,
            "plugins.always_open_pdf_externally": True,
        }
        options.add_experimental_option("prefs", prefs)
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(30)
        driver.implicitly_wait(5)
        
        # Enable file downloads in headless mode (prefs alone don't work)
        download_dir = os.path.abspath(DEEDS_DIR)
        driver.execute_cdp_cmd("Page.setDownloadBehavior", {
            "behavior": "allow",
            "downloadPath": download_dir,
        })
        
        return driver

    # ── CAPTCHA ──────────────────────────
    def _solve_captcha(self, debug=True):
        from PIL import ImageFilter, ImageOps

        captcha_el = self.driver.find_element(By.ID, "captcha_id")
        png_bytes = captcha_el.screenshot_as_png

        img_orig = Image.open(io.BytesIO(png_bytes))

        if debug:
            os.makedirs("./captcha_debug", exist_ok=True)
            ts = int(time.time())
            img_orig.save(f"./captcha_debug/raw_{ts}.png")

        whitelist = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

        # ── Strategy 1: Grayscale with multiple thresholds & PSM modes ──
        for threshold in [160, 180, 140, 200]:
            for contrast in [1.8, 2.5, 1.2]:
                img = img_orig.convert("L")
                img = ImageEnhance.Contrast(img).enhance(contrast)
                img = img.resize((img.width * 4, img.height * 4), Image.LANCZOS)
                img = img.point(lambda x, t=threshold: 0 if x < t else 255)

                for psm in ["--psm 7", "--psm 8", "--psm 13"]:
                    text = pytesseract.image_to_string(
                        img,
                        config=f"{psm} --oem 3 -c tessedit_char_whitelist={whitelist}",
                    )
                    result = re.sub(r"[^A-Za-z0-9]", "", text).strip()
                    if len(result) == 6:
                        if debug:
                            img.save(f"./captcha_debug/processed_{ts}.png")
                        logger.info(f"OCR result: '{result}' (6 chars) [threshold={threshold}, contrast={contrast}, {psm}]")
                        return result

        # ── Strategy 2: Individual RGB channels ──
        for channel_idx, channel_name in enumerate(["R", "G", "B"]):
            ch = img_orig.split()[channel_idx]
            ch = ImageEnhance.Contrast(ch).enhance(3.0)
            ch = ch.resize((ch.width * 4, ch.height * 4), Image.LANCZOS)
            ch = ch.point(lambda x: 0 if x < 150 else 255)

            text = pytesseract.image_to_string(
                ch,
                config=f"--psm 7 --oem 3 -c tessedit_char_whitelist={whitelist}",
            )
            result = re.sub(r"[^A-Za-z0-9]", "", text).strip()
            if len(result) == 6:
                logger.info(f"OCR result: '{result}' (6 chars) [channel={channel_name}]")
                return result

        # ── Strategy 3: Inverted image ──
        img_inv = ImageOps.invert(img_orig.convert("L"))
        img_inv = ImageEnhance.Contrast(img_inv).enhance(2.0)
        img_inv = img_inv.resize((img_inv.width * 4, img_inv.height * 4), Image.LANCZOS)
        img_inv = img_inv.point(lambda x: 0 if x < 128 else 255)
        text = pytesseract.image_to_string(
            img_inv,
            config=f"--psm 7 --oem 3 -c tessedit_char_whitelist={whitelist}",
        )
        result = re.sub(r"[^A-Za-z0-9]", "", text).strip()
        if len(result) == 6:
            logger.info(f"OCR result: '{result}' (6 chars) [inverted]")
            return result

        # ── Strategy 4: Median filter to remove noise, then OCR ──
        img_med = img_orig.convert("L").filter(ImageFilter.MedianFilter(3))
        img_med = ImageEnhance.Contrast(img_med).enhance(2.0)
        img_med = img_med.resize((img_med.width * 4, img_med.height * 4), Image.LANCZOS)
        img_med = img_med.point(lambda x: 0 if x < 160 else 255)
        text = pytesseract.image_to_string(
            img_med,
            config=f"--psm 7 --oem 3 -c tessedit_char_whitelist={whitelist}",
        )
        result = re.sub(r"[^A-Za-z0-9]", "", text).strip()
        if len(result) == 6:
            logger.info(f"OCR result: '{result}' (6 chars) [median-filtered]")
            return result

        # ── None of the strategies produced 6 chars — return best guess ──
        # Re-run the most reliable combo as a last resort
        img = img_orig.convert("L")
        img = ImageEnhance.Contrast(img).enhance(1.8)
        img = img.resize((img.width * 4, img.height * 4), Image.LANCZOS)
        img = img.point(lambda x: 0 if x < 180 else 255)
        if debug:
            img.save(f"./captcha_debug/processed_{ts}.png")
        text = pytesseract.image_to_string(
            img, config=f"--psm 7 --oem 3 -c tessedit_char_whitelist={whitelist}"
        )
        result = re.sub(r"[^A-Za-z0-9]", "", text).strip()
        logger.warning(f"OCR best-guess: '{result}' ({len(result)} chars) — no 6-char match found")
        return result

    def search_with_captcha_retry(self, max_attempts=10):  # bumped to 10
        for attempt in range(1, max_attempts + 1):
            solved = ""
            try:
                # Dismiss any lingering alert first
                try:
                    self.driver.switch_to.alert.accept()
                    logger.debug("Dismissed stale alert")
                except Exception:
                    pass

                solved = self._solve_captcha()

                if len(solved) != 6:
                    logger.warning(
                        f"Attempt {attempt}: OCR gave {len(solved)} chars '{solved}' — refreshing"
                    )
                    self._refresh_captcha()
                    continue

                captcha_input = self.driver.find_element(By.ID, "captcha")
                captcha_input.clear()
                captcha_input.send_keys(solved)
                
                logger.info(f"Attempt {attempt}: submitting '{solved}'")
                self.driver.find_element(By.NAME, "btn1").click()

                # Check for alert (wrong captcha)
                try:
                    WebDriverWait(self.driver, 2).until(EC.alert_is_present())
                    alert_text = self.driver.switch_to.alert.text
                    self.driver.switch_to.alert.accept()
                    logger.warning(f"Attempt {attempt}: portal rejected '{solved}' — alert: '{alert_text}'")
                    self._refresh_captcha()
                    continue
                except TimeoutException:
                    pass  # No alert = captcha accepted, check for results

                # Wait for results
                WebDriverWait(self.driver, 10).until(
                    lambda d: d.find_elements(By.CSS_SELECTOR, "div.search_result_list")
                    or d.find_elements(By.XPATH, "//*[contains(text(),'Showing record')]")
                )
                logger.info(f"SUCCESS on attempt {attempt} with '{solved}'")
                return

            except TimeoutException:
                logger.warning(f"Attempt {attempt}: results didn't load for '{solved}'")
                self._refresh_captcha()

        raise CaptchaFailureError(f"Failed after {max_attempts} attempts")


    def _refresh_captcha(self):
        """Reliably get a fresh CAPTCHA image and reset form state."""
        logger.info("Reloading page to refresh CAPTCHA and clean form state")
        try:
            self.driver.refresh()
        except TimeoutException:
            logger.warning("Page refresh timed out. Attempting to navigate back to SEARCH_URL.")
            try:
                self.driver.get(SEARCH_URL)
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"Error during page refresh: {e}")
            try:
                self.driver.get(SEARCH_URL)
            except Exception:
                pass
        time.sleep(2)
        self._fill_search_form()

    # ── Search Form ──────────────────────
    def _fill_search_form(self):
        """Set all search form dropdowns. Handles cascading load waits."""
        self.wait.until(EC.presence_of_element_located((By.ID, "Regtype")))
        Select(self.driver.find_element(By.ID, "Regtype")).select_by_value("Project")
        logger.info("Set Regtype = Project")
        time.sleep(random.uniform(0.7, 1.5))

        self.wait.until(EC.presence_of_element_located((By.NAME, "projectDist")))
        Select(self.driver.find_element(By.NAME, "projectDist")).select_by_visible_text("North Goa")
        logger.info("Set District = North Goa")
        time.sleep(random.uniform(0.7, 1.5))

        self.wait.until(EC.presence_of_element_located((By.NAME, "subDistrictId")))
        Select(self.driver.find_element(By.NAME, "subDistrictId")).select_by_visible_text("Bardez")
        logger.info("Set Taluka = Bardez")
        time.sleep(random.uniform(0.7, 1.5))

        self.wait.until(EC.presence_of_element_located((By.NAME, "villageId")))
        Select(self.driver.find_element(By.NAME, "villageId")).select_by_visible_text("Siolim (ct)")
        logger.info("Set Village = Siolim (ct)")
        time.sleep(random.uniform(0.7, 1.5))

    def submit_search_form(self):
        try:
            self.driver.get(SEARCH_URL)
            self._fill_search_form()
            self.search_with_captcha_retry()
            logger.info("Search form submitted successfully — results loaded")
        except CaptchaFailureError:
            raise
        except Exception as e:
            logger.error("submit_search_form failed", exc_info=True)
            raise

    def _parse_total_records(self):
        try:
            txt = self.driver.find_element(
                By.XPATH, "//*[contains(text(),'Showing record')]"
            ).text
            match = re.search(r"out of\s+(\d+)", txt)
            if match:
                return int(match.group(1))
        except Exception:
            pass
        return None

    def _count_cards_on_page(self):
        return len(
            self.driver.find_elements(By.CSS_SELECTOR, "div.search_result_list")
        )

    def _find_next_page_offset(self):
        """Find the 'pagging(N)' offset for the next page, or None if on last page.
        
        The Goa RERA portal uses <ul class="pagination"> with <li> items.
        The current page has class="active". We need the first non-active,
        non-disabled <li> link that comes AFTER the active one.
        Its href is like 'javascript:pagging(10)'.
        """
        try:
            pagination = self.driver.find_element(By.CSS_SELECTOR, "ul.pagination")
            items = pagination.find_elements(By.TAG_NAME, "li")
            
            found_active = False
            for item in items:
                classes = item.get_attribute("class") or ""
                if "active" in classes:
                    found_active = True
                    continue
                if found_active and "disabled" not in classes:
                    link = item.find_element(By.TAG_NAME, "a")
                    href = link.get_attribute("href") or ""
                    m = re.search(r"pagging\((\d+)\)", href)
                    if m:
                        return int(m.group(1))
            return None
        except NoSuchElementException:
            return None
        except Exception as e:
            logger.debug(f"Pagination lookup error: {e}")
            return None

    def download_all_result_pages(self):
        try:
            total = self._parse_total_records()
            if total:
                logger.info(f"Total records found: {total}")

            page_num = 1
            while True:
                cards = self._count_cards_on_page()
                path = os.path.join(CACHE_DIR, f"results_page_{page_num}.html")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self.driver.page_source)
                logger.info(f"Saved page {page_num} ({cards} cards)")

                next_offset = self._find_next_page_offset()
                if next_offset is not None:
                    logger.info(f"Navigating to next page (startFrom={next_offset})")
                    self.driver.execute_script(f"pagging({next_offset})")
                    time.sleep(3)
                    WebDriverWait(self.driver, 15).until(
                        EC.presence_of_element_located(
                            (By.CSS_SELECTOR, "div.search_result_list")
                        )
                    )
                    page_num += 1
                else:
                    logger.info("No more pages — pagination complete")
                    break
        except Exception as e:
            logger.error("download_all_result_pages failed", exc_info=True)
            raise

    def download_detail_page(self, url, reg_no):
        try:
            self.driver.get(url)
            time.sleep(2)
            detail_path = os.path.join(CACHE_DIR, f"detail_{reg_no}.html")
            with open(detail_path, "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
            logger.info(f"[{reg_no}] Detail page cached")

            # Find the link in the live DOM. The text often has weird newlines/tabs like:
            # "Land\n\t\tDeed/Agreement". Using contains() for both 'Land' and 'Deed' handles this safely.
            
            # --- COMMENTED OUT: Temporarily disabling deed downloads ---
            # try:
            #     deed_el = self.driver.find_element(
            #         By.XPATH,
            #         "//a[contains(@href, 'download?DOC_ID=') and contains(., 'Land') and contains(., 'Deed')]"
            #     )
            #     deed_href = deed_el.get_attribute("href")
            #     existing_files = set(os.listdir(DEEDS_DIR))
            #     
            #     # Trigger click via JS to bypass any potential scrolling/overlay interception
            #     self.driver.execute_script("arguments[0].click();", deed_el)
            #     
            #     logger.info(f"[{reg_no}] Deed download triggered (href: {deed_href})")
            #     self._wait_for_download(existing_files)
            #     
            # except NoSuchElementException:
            #     logger.warning(f"[{reg_no}] No deed link found on detail page")
            # except Exception as e:
            #     logger.warning(f"[{reg_no}] Failed to trigger deed download: {e}")
            # -----------------------------------------------------------
            
        except Exception as e:
            logger.error(f"[{reg_no}] detail page failed", exc_info=True)

    def _wait_for_download(self, existing_files, timeout=30):
        start = time.time()
        while time.time() - start < timeout:
            current = set(os.listdir(DEEDS_DIR))
            new_files = current - existing_files
            crdownloads = [f for f in current if f.endswith(".crdownload") or f.endswith(".tmp")]
            if new_files and not crdownloads:
                new_pdfs = [f for f in new_files if f.endswith(".pdf")]
                if new_pdfs:
                    logger.info(f"Download complete: {new_pdfs[0]}")
                    return
            time.sleep(1)
        logger.warning("Download wait timed out after 30s")


# ══════════════════════════════════════════
# CLASS B — RERAParser
# ══════════════════════════════════════════
class RERAParser:
    """BeautifulSoup-only parser. Zero network calls, zero selenium imports."""

    def parse_results_page(self, html):
        try:
            soup = BeautifulSoup(html, "html.parser")
            cards = soup.select("div.search_result_list")
            results = []
            for card in cards:
                data = self._parse_single_card(card)
                if data:
                    results.append(data)
            logger.info(f"Parsed {len(results)} cards from results page")
            return results
        except Exception as e:
            logger.error("parse_results_page failed", exc_info=True)
            raise ParserError(f"Results page parse error: {e}")

    def _parse_single_card(self, card):
        record = {}

        # Project name — <h1><span>Project: </span> Aangan</h1>
        # The name is the text node AFTER the span, not inside it
        project_span = card.find("span", string=re.compile(r"Project\s*:", re.I))
        if project_span:
            for sibling in project_span.next_siblings:
                if isinstance(sibling, NavigableString) and sibling.strip():
                    record["project_name"] = sibling.strip()
                    break
            if "project_name" not in record:
                parent_text = project_span.parent.get_text(strip=True)
                span_text = project_span.get_text(strip=True)
                record["project_name"] = parent_text.replace(span_text, "").strip()

        # Registration number
        for tag in card.find_all(string=re.compile(r"Reg\s*No\.?\s*:", re.I)):
            parent = tag.parent if isinstance(tag, NavigableString) else tag
            full = parent.get_text(strip=True)
            m = re.search(r"Reg\s*No\.?\s*:\s*(.+)", full, re.I)
            if m:
                record["reg_no"] = m.group(1).strip()
                break

        # Mini-table columns: PROMOTER | PROMOTER TYPE | TOTAL AREA | PROPERTY TYPE | STATUS
        table = card.find("table")
        if table:
            cells = [td.get_text(strip=True) for td in table.find_all("td")]
            headers_map = [
                "total_area",
                "property_type",
                "status",
            ]
            # Find header row to determine data row
            rows = table.find_all("tr")
            if len(rows) >= 2:
                data_cells = [
                    td.get_text(strip=True) for td in rows[-1].find_all("td")
                ]
                for i, key in enumerate(headers_map):
                    if i < len(data_cells):
                        record[key] = data_cells[i]

        # Read More URL
        read_more = card.find("a", string=re.compile(r"Read\s*More", re.I))
        if not read_more:
            read_more = card.find("a", class_=re.compile(r"read", re.I))
        if read_more and read_more.get("href"):
            href = read_more["href"]
            record["detail_url"] = (
                urljoin(BASE_URL, href) if not href.startswith("http") else href
            )
            # Extract reg_no from URL if not already found
            if "reg_no" not in record:
                m = re.search(r"[?&]projectID=([^&]+)", href)
                if m:
                    record["reg_no"] = m.group(1)

        # Address — text node directly after project name
        if "project_name" not in record:
            record["project_name"] = ""
        if "reg_no" not in record:
            record["reg_no"] = ""

        return record if record.get("reg_no") else record

    def _extract_section(self, soup, heading):
        header_el = None
        
        # 1. Scan structural tags. .get_text() ignores the nested <span> 
        # and cleanly joins "Promoter" and "Details" together.
        for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "div"]):
            tag_text = tag.get_text(separator=" ", strip=True).lower()
            
            # Check if our target heading is in this squished text
            if heading.lower() in tag_text and len(tag_text) < 80:
                header_el = tag
                break

        if not header_el:
            logger.warning(f"Section '{heading}' not found")
            return []

        # 2. Once the <h1> is found, grab the very next <table> in the HTML
        table = header_el.find_next("table")

        if not table:
            logger.warning(f"No table found for section '{heading}'")
            return []

        return self._parse_html_table(table)

    def _parse_html_table(self, table):
        rows = table.find_all("tr")
        if not rows:
            return []

        # Determine headers from thead or first tr
        thead = table.find("thead")
        if thead:
            header_row = thead.find("tr")
            data_rows = table.find("tbody")
            data_rows = data_rows.find_all("tr") if data_rows else rows[1:]
        else:
            header_row = rows[0]
            data_rows = rows[1:]

        headers = [
            th.get_text(strip=True)
            for th in header_row.find_all(["th", "td"])
        ]
        # Normalize header keys
        headers = [
            re.sub(r"\s+", "_", h.lower().strip()) for h in headers if h
        ]

        results = []
        for row in data_rows:
            cells = [
                td.get_text(strip=True).replace("[at]", "@").replace("[dot]", ".")
                for td in row.find_all("td")
            ]
            if len(cells) == len(headers):
                results.append(dict(zip(headers, cells)))
            elif cells:
                entry = {}
                for i, cell in enumerate(cells):
                    key = headers[i] if i < len(headers) else f"col_{i}"
                    entry[key] = cell
                results.append(entry)
        return results

    def parse_detail_page(self, html, reg_no):
        try:
            soup = BeautifulSoup(html, "html.parser")
            merged = {"reg_no": reg_no}

            sections = [
                "Promoter Details",
                "Authorized Person Details",
                "Project Architects",
                "Structural Engineers",
            ]

            for section_name in sections:
                rows = self._extract_section(soup, section_name)
                prefix = re.sub(r"\s+", "_", section_name.lower())
                for row_dict in rows:
                    for key, val in row_dict.items():
                        flat_key = f"{prefix}_{key}"
                        if flat_key in merged:
                            merged[flat_key] += f", {val}"
                        else:
                            merged[flat_key] = val

            return merged
        except Exception as e:
            logger.error(
                f"[{reg_no}] parse_detail_page failed", exc_info=True
            )
            raise ParserError(f"Detail parse error for {reg_no}: {e}")


# ══════════════════════════════════════════
# CLASS C — RERAOrchestrator
# ══════════════════════════════════════════
class RERAOrchestrator:
    """Thin coordinator + Excel export."""

    def run(self, mode="full", limit=None, visible=False):
        start = time.time()
        logger.info(f"Run started — mode={mode}, limit={limit}, visible={visible}")
        all_projects = []

        try:
            if mode in ("download", "full"):
                self._run_download(limit=limit, visible=visible)

            if mode in ("parse", "full"):
                all_projects = self._run_parse(limit=limit)
                self.export_to_excel(all_projects)

            elapsed = time.time() - start
            logger.info(
                f"Run complete — {len(all_projects)} projects — {elapsed:.1f}s"
            )
        except CaptchaFailureError as e:
            logger.error(f"Fatal: {e}")
            raise
        except Exception as e:
            logger.error("Orchestrator run failed", exc_info=True)
            raise

    def _run_download(self, limit=None, visible=False):
        downloader = RERADownloader(headless=not visible)
        parser = RERAParser()
        try:
            # Build set of already-scraped reg_nos to skip re-downloading
            already_scraped = set()
            
            # Check cached detail HTML files
            for cached_file in glob(os.path.join(CACHE_DIR, "detail_*.html")):
                basename = os.path.basename(cached_file)
                # Extract reg_no from filename like "detail_PRGO03262596.html"
                rno = basename.replace("detail_", "").replace(".html", "")
                if rno:
                    already_scraped.add(rno)
            
            # Check existing Excel sheet
            output_path = "./Goa_RERA_Master.xlsx"
            if os.path.exists(output_path):
                try:
                    existing_df = pd.read_excel(output_path, engine="openpyxl")
                    if "reg_no" in existing_df.columns:
                        already_scraped.update(existing_df["reg_no"].dropna().astype(str).tolist())
                except Exception as e:
                    logger.warning(f"Could not read Excel for skip-check: {e}")
            
            if already_scraped:
                logger.info(f"Found {len(already_scraped)} already-scraped projects — will skip them")
            
            downloader.submit_search_form()
            downloader.download_all_result_pages()

            detail_count = 0
            skipped_count = 0
            for page_file in sorted(glob(os.path.join(CACHE_DIR, "results_page_*.html"))):
                with open(page_file, "r", encoding="utf-8") as f:
                    html = f.read()
                cards = parser.parse_results_page(html)
                for card in cards:
                    if limit and detail_count >= limit:
                        logger.info(f"Limit reached ({limit}) — stopping detail downloads")
                        break
                    url = card.get("detail_url")
                    rno = card.get("reg_no")
                    if url and rno:
                        if rno in already_scraped:
                            skipped_count += 1
                            continue
                        downloader.download_detail_page(url, rno)
                        detail_count += 1
                if limit and detail_count >= limit:
                    break
            
            if skipped_count:
                logger.info(f"Skipped {skipped_count} already-scraped projects")
        finally:
            downloader.driver.quit()
            logger.info("Browser closed")

    def _run_parse(self, limit=None):
        parser = RERAParser()
        all_projects = []

        for page_file in sorted(glob(os.path.join(CACHE_DIR, "results_page_*.html"))):
            with open(page_file, "r", encoding="utf-8") as f:
                html = f.read()
            all_projects.extend(parser.parse_results_page(html))

        if limit:
            all_projects = all_projects[:limit]
            logger.info(f"Limit applied — processing {len(all_projects)} projects")

        for project in all_projects:
            rno = project.get("reg_no")
            if not rno:
                continue
            detail_file = os.path.join(CACHE_DIR, f"detail_{rno}.html")
            if os.path.exists(detail_file):
                try:
                    with open(detail_file, "r", encoding="utf-8") as f:
                        detail_html = f.read()
                    detail = parser.parse_detail_page(detail_html, rno)
                    project.update(detail)
                except ParserError:
                    logger.warning(f"[{rno}] Skipping detail — parse error")

        return all_projects

    @staticmethod
    def export_to_excel(projects):
        if not projects:
            logger.warning("No projects to export")
            return
            
        new_df = pd.DataFrame(projects)
        output_path = "./Goa_RERA_Master.xlsx"
        
        if os.path.exists(output_path):
            try:
                existing_df = pd.read_excel(output_path, engine="openpyxl")
                
                # Filter out new projects that are already present in the existing Excel sheet
                if 'reg_no' in existing_df.columns and 'reg_no' in new_df.columns:
                    existing_reg_nos = set(existing_df['reg_no'].dropna())
                    initial_new_count = len(new_df)
                    new_df = new_df[~new_df['reg_no'].isin(existing_reg_nos)]
                    skipped_count = initial_new_count - len(new_df)
                    if skipped_count > 0:
                        logger.info(f"Skipped {skipped_count} duplicate projects that already exist in {output_path}")

                # Concat automatically aligns columns. Missing data becomes NaN.
                combined_df = pd.concat([existing_df, new_df], ignore_index=True)
                df = combined_df
            except Exception as e:
                logger.warning(f"Could not read existing Excel file (it might be corrupted/open), creating new: {e}")
                df = new_df
        else:
            df = new_df

        # Sort columns alphabetically for consistency
        df = df.reindex(sorted(df.columns), axis=1)
        df.to_excel(output_path, index=False, engine="openpyxl")
        logger.info(f"Exported {len(df)} total rows -> {output_path} (Appended {len(new_df)} new rows)")


# ══════════════════════════════════════════
# Entry Point
# ══════════════════════════════════════════
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Goa RERA Portal Scraper")
    ap.add_argument(
        "--mode",
        choices=["download", "parse", "full"],
        default="full",
        help="download=fetch from portal, parse=process cached HTML, full=both",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Dev mode: limit to first N projects (e.g. --limit 5)",
    )
    ap.add_argument(
        "--visible",
        action="store_true",
        help="Show the Chrome browser window in real-time (disable headless mode)",
    )
    args = ap.parse_args()
    RERAOrchestrator().run(mode=args.mode, limit=args.limit, visible=args.visible)
