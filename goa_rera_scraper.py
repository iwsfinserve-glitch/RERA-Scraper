import os
import re
import io
import time
import logging
import math
from glob import glob
from urllib.parse import urljoin

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

    def __init__(self):
        os.makedirs(CACHE_DIR, exist_ok=True)
        os.makedirs(DEEDS_DIR, exist_ok=True)
        self.driver = self._build_driver(headless=True)
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
        driver.implicitly_wait(5)
        return driver

    # ── CAPTCHA ──────────────────────────
    def _solve_captcha(self, debug=False):
        from PIL import ImageFilter, ImageOps

        captcha_el = self.driver.find_element(By.ID, "captcha_id")
        png_bytes = captcha_el.screenshot_as_png

        img_orig = Image.open(io.BytesIO(png_bytes))

        if debug:
            os.makedirs("./captcha_debug", exist_ok=True)
            ts = int(time.time())
            img_orig.save(f"./captcha_debug/raw_{ts}.png")

        # ── OPTIMAL PIPELINE (tested against real CAPTCHA image) ──────────────
        # This CAPTCHA: light gray bg (~213), dark italic text (~39)
        # Key insight: PSM 7 (single line) beats PSM 8 (single word)
        # for this handwritten/italic font style.
        # Minimal processing is better — over-processing destroys thin strokes.

        img = img_orig.convert("L")                                    # grayscale
        img = ImageEnhance.Contrast(img).enhance(1.8)                  # gentle boost only
        img = img.resize((img.width * 4, img.height * 4), Image.LANCZOS)  # 4x upscale
        img = img.point(lambda x: 0 if x < 180 else 255)              # binarize at 180

        if debug:
            img.save(f"./captcha_debug/processed_{ts}.png")

        whitelist = (
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        )

        # PRIMARY: PSM 7 — single text line mode (most accurate for this font)
        text = pytesseract.image_to_string(
            img,
            config=f"--psm 7 --oem 1 -c tessedit_char_whitelist={whitelist}",
        )
        result = re.sub(r"[^A-Za-z0-9]", "", text).strip()
        logger.info(f"OCR result: '{result}' ({len(result)} chars)")

        # FALLBACK: if PSM 7 gives wrong length, try gentle-contrast variant
        if len(result) != 6:
            img2 = img_orig.convert("L")
            img2 = img2.resize((img2.width * 4, img2.height * 4), Image.LANCZOS)
            text2 = pytesseract.image_to_string(
                img2,
                config=f"--psm 7 --oem 1 -c tessedit_char_whitelist={whitelist}",
            )
            result2 = re.sub(r"[^A-Za-z0-9]", "", text2).strip()
            logger.info(f"OCR fallback: '{result2}' ({len(result2)} chars)")
            if len(result2) == 6:
                return result2

        return result
        from PIL import ImageFilter
        
        captcha_el = self.driver.find_element(By.ID, "captcha_id")
        png_bytes = captcha_el.screenshot_as_png

        os.makedirs("./captcha_debug", exist_ok=True)
        ts = int(time.time())
        
        img_orig = Image.open(io.BytesIO(png_bytes))
        
        if debug:
            img_orig.save(f"./captcha_debug/raw_{ts}.png")

        # Strategy 1: Standard grayscale + aggressive contrast
        img = img_orig.convert("L")
        img = ImageEnhance.Contrast(img).enhance(3.0)
        img = img.resize((img.width * 4, img.height * 4), Image.LANCZOS)
        img = img.point(lambda x: 0 if x < 150 else 255)
        img = img.filter(ImageFilter.SHARPEN)
        
        if debug:
            img.save(f"./captcha_debug/processed_{ts}.png")

        psm_modes = ["--psm 8", "--psm 7", "--psm 13"]
        whitelist = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        
        for psm in psm_modes:
            text = pytesseract.image_to_string(
                img,
                config=f"{psm} --oem 3 -c tessedit_char_whitelist={whitelist}",
            )
            result = re.sub(r"[^A-Za-z0-9]", "", text).strip()
            logger.info(f"OCR ({psm}): '{result}' ({len(result)} chars)")
            if len(result) == 6:
                return result

        # Strategy 2: Try on RGB channels separately (catches colored-text CAPTCHAs)
        for channel_idx, channel_name in enumerate(["R", "G", "B"]):
            ch = img_orig.split()[channel_idx]
            ch = ImageEnhance.Contrast(ch).enhance(3.0)
            ch = ch.resize((ch.width * 4, ch.height * 4), Image.LANCZOS)
            ch = ch.point(lambda x: 0 if x < 150 else 255)
            
            if debug:
                ch.save(f"./captcha_debug/channel_{channel_name}_{ts}.png")
            
            text = pytesseract.image_to_string(
                ch,
                config=f"--psm 8 --oem 3 -c tessedit_char_whitelist={whitelist}",
            )
            result = re.sub(r"[^A-Za-z0-9]", "", text).strip()
            logger.info(f"OCR (channel {channel_name}): '{result}' ({len(result)} chars)")
            if len(result) == 6:
                return result

        # Return best guess even if not 6 chars (let retry loop handle it)
        text = pytesseract.image_to_string(
            img, config=f"--psm 8 --oem 3 -c tessedit_char_whitelist={whitelist}"
        )
        return re.sub(r"[^A-Za-z0-9]", "", text).strip()

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
        """Reliably get a fresh CAPTCHA image."""
        refreshed = False
        
        # Try clicking a refresh/reload icon near the captcha
        refresh_xpaths = [
            "//img[@id='captcha_id']/following-sibling::img",
            "//img[@id='captcha_id']/following-sibling::a",
            "//*[contains(@onclick,'captcha') or contains(@onclick,'refresh')]",
            "//i[contains(@class,'refresh') or contains(@class,'reload')]",
            "//span[contains(@class,'refresh')]",
        ]
        for xpath in refresh_xpaths:
            try:
                el = self.driver.find_element(By.XPATH, xpath)
                el.click()
                time.sleep(1.5)
                refreshed = True
                logger.debug(f"Refreshed CAPTCHA via: {xpath}")
                break
            except Exception:
                continue

        if not refreshed:
            # Nuclear option: reload page and re-select Project
            logger.info("No refresh button found — reloading page")
            self.driver.refresh()
            time.sleep(2)
            try:
                self.wait.until(EC.presence_of_element_located((By.ID, "Regtype")))
                Select(self.driver.find_element(By.ID, "Regtype")).select_by_value("Project")
                time.sleep(1)
            except Exception as e:
                logger.warning(f"Re-select after refresh failed: {e}")
            """Click the CAPTCHA refresh icon to get a new image."""
            try:
                # Most RERA portals have a refresh icon next to the captcha
                refresh = self.driver.find_element(
                    By.XPATH,
                    "//*[@id='captcha_id']/following-sibling::*[1] | "
                    "//img[contains(@src,'refresh')] | "
                    "//*[contains(@onclick,'refresh') or contains(@onclick,'captcha')]"
                )
                refresh.click()
            except NoSuchElementException:
                # Fallback: reload the page and re-submit the form
                logger.info("No refresh button found — reloading page")
                self.driver.refresh()
                self.wait.until(EC.presence_of_element_located((By.ID, "Regtype")))
                Select(self.driver.find_element(By.ID, "Regtype")).select_by_value("Project")
            time.sleep(1.5)

    # ── Search Form ──────────────────────
    def submit_search_form(self):
        try:
            self.driver.get(SEARCH_URL)
            self.wait.until(EC.presence_of_element_located((By.ID, "Regtype")))
            Select(self.driver.find_element(By.ID, "Regtype")).select_by_value(
                "Project"
            )
            time.sleep(1)
            self.search_with_captcha_retry()
            logger.info("Search form submitted successfully — results loaded")
        except CaptchaFailureError:
            raise
        except Exception as e:
            logger.error("submit_search_form failed", exc_info=True)
            raise

    # ── Pagination ───────────────────────
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

    def _find_next_button(self):
        selectors = [
            (By.CSS_SELECTOR, 'a[aria-label="Next"]'),
            (By.CSS_SELECTOR, 'a.page-link[rel="next"]'),
        ]
        for by, sel in selectors:
            try:
                el = self.driver.find_element(by, sel)
                if el.is_displayed() and el.is_enabled():
                    return el
            except NoSuchElementException:
                continue
        # XPath fallback
        try:
            el = self.driver.find_element(
                By.XPATH,
                '//a[contains(text(),"Next") and not(@disabled)]',
            )
            if el.is_displayed() and el.is_enabled():
                return el
        except NoSuchElementException:
            pass
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

                next_btn = self._find_next_button()
                if next_btn:
                    next_btn.click()
                    time.sleep(3)
                    WebDriverWait(self.driver, 10).until(
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

    # ── Detail + PDF ─────────────────────
    def download_detail_page(self, url, reg_no):
        try:
            self.driver.get(url)
            time.sleep(2)
            detail_path = os.path.join(CACHE_DIR, f"detail_{reg_no}.html")
            with open(detail_path, "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
            logger.info(f"[{reg_no}] Detail page cached")

            soup = BeautifulSoup(self.driver.page_source, "html.parser")
            deed_link = soup.find("a", string=re.compile(r"Land Deed/Agreement", re.I))

            if deed_link and deed_link.get("href"):
                deed_url = urljoin(BASE_URL, deed_link["href"])
                existing_files = set(os.listdir(DEEDS_DIR))
                self.driver.get(deed_url)
                logger.info(f"[{reg_no}] Deed download triggered: {deed_link.text}")
                self._wait_for_download(existing_files)
            else:
                logger.warning(f"[{reg_no}] No deed link found on detail page")
        except Exception as e:
            logger.error(f"[{reg_no}] detail page failed", exc_info=True)

    def _wait_for_download(self, existing_files, timeout=30):
        start = time.time()
        while time.time() - start < timeout:
            current = set(os.listdir(DEEDS_DIR))
            new_files = current - existing_files
            crdownloads = [f for f in current if f.endswith(".crdownload")]
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

        # Project name — look for text containing "PROJECT:"
        for tag in card.find_all(["h2", "h3", "h4", "strong", "b", "span", "div"]):
            txt = tag.get_text(strip=True)
            if txt.upper().startswith("PROJECT:"):
                record["project_name"] = txt.split(":", 1)[1].strip()
                break

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
                "promoter",
                "promoter_type",
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
        header = soup.find(
            ["h1", "h2", "h3", "h4", "h5"],
            string=lambda t: t and heading.lower() in t.lower(),
        )
        if not header:
            logger.warning(f"Section '{heading}' not found")
            return []

        table = None
        for sibling in header.next_siblings:
            if isinstance(sibling, NavigableString):
                continue
            if sibling.name == "table":
                table = sibling
                break
            # Stop if we hit another heading
            if sibling.name in ["h1", "h2", "h3", "h4", "h5"]:
                break
            # Check for nested table
            nested = sibling.find("table")
            if nested:
                table = nested
                break

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
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
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
                "Project Details",
                "Project Architects",
                "Structural Engineers",
            ]

            for section_name in sections:
                rows = self._extract_section(soup, section_name)
                prefix = re.sub(r"\s+", "_", section_name.lower())
                for idx, row_dict in enumerate(rows):
                    for key, val in row_dict.items():
                        flat_key = f"{prefix}_{idx}_{key}"
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

    def run(self, mode="full", limit=None):
        start = time.time()
        logger.info(f"Run started — mode={mode}, limit={limit}")
        all_projects = []

        try:
            if mode in ("download", "full"):
                self._run_download(limit=limit)

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

    def _run_download(self, limit=None):
        downloader = RERADownloader()
        parser = RERAParser()
        try:
            downloader.submit_search_form()
            downloader.download_all_result_pages()

            detail_count = 0
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
                        downloader.download_detail_page(url, rno)
                        detail_count += 1
                if limit and detail_count >= limit:
                    break
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
        df = pd.DataFrame(projects)
        df = df.reindex(sorted(df.columns), axis=1)
        output_path = "./Goa_RERA_Master.xlsx"
        df.to_excel(output_path, index=False, engine="openpyxl")
        logger.info(f"Exported {len(df)} rows -> {output_path}")


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
    args = ap.parse_args()
    RERAOrchestrator().run(mode=args.mode, limit=args.limit)
