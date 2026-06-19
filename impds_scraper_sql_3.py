import re
import os
import time
import signal
import sqlite3
import argparse

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# ==============================
# CONFIG
# ==============================

parser = argparse.ArgumentParser()
parser.add_argument("--start", type=int, default=0)
parser.add_argument("--end",   type=int, default=None)
parser.add_argument("--year",  type=int, default=2023)
parser.add_argument("--month", type=int, default=4)
args = parser.parse_args()

TARGET_YEAR  = args.year
TARGET_MONTH = args.month
START_STATE  = args.start
END_STATE    = args.end if (args.end is None or args.end != 99) else None

BASE_URL = "https://impds.nic.in/sale/"
DB_FILE  = f"{TARGET_YEAR}_{TARGET_MONTH}_{START_STATE}_{END_STATE}.db"

FIXED_FIELDS = ["state", "district", "district_code", "date"]
KEY_FIELDS   = ["state", "district_code", "date"]

TABLE_WAIT_TIMEOUT    = 20    # seconds to wait for table per attempt
DISTRICT_HARD_TIMEOUT = 180   # 3 min hard ceiling per attempt
ATTEMPTS_BEFORE_RELAUNCH = 3  # relaunch browser after every N consecutive failures


# ==============================
# CHROME SETUP
# ==============================

def make_chrome_options():
    opts = webdriver.ChromeOptions()
    opts.add_argument("--headless")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--start-maximized")

    # Use the exact Chrome binary installed by setup-chrome@v2
    chrome_path = os.environ.get("CHROME_PATH")
    if chrome_path:
        opts.binary_location = chrome_path

    return opts


def make_driver():
    """
    Create a Chrome driver using the matched Chrome + ChromeDriver pair
    installed by browser-actions/setup-chrome@v2.
    Falls back to system defaults if env vars are not set (local runs).
    """
    chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")
    service = Service(executable_path=chromedriver_path) if chromedriver_path else Service()
    return webdriver.Chrome(service=service, options=make_chrome_options())


driver = make_driver()
wait   = WebDriverWait(driver, 20)


# ==============================
# HARD TIMEOUT MACHINERY
# ==============================

class DistrictTimeout(Exception):
    pass

def _timeout_handler(signum, frame):
    raise DistrictTimeout("Hard timeout exceeded")


# ==============================
# SQL HELPERS
# ==============================

def get_connection():
    return sqlite3.connect(DB_FILE)


def init_db():
    with get_connection() as con:
        cols_def = ", ".join(f'"{c}" TEXT' for c in FIXED_FIELDS)
        con.execute(f"""
            CREATE TABLE IF NOT EXISTS district_data (
                {cols_def},
                PRIMARY KEY ({', '.join(f'"{k}"' for k in KEY_FIELDS)})
            )
        """)
        con.commit()


def get_existing_columns():
    with get_connection() as con:
        cur = con.execute("PRAGMA table_info(district_data)")
        return [row[1] for row in cur.fetchall()]


def ensure_columns(col_names):
    existing = set(get_existing_columns())
    new_cols  = [c for c in col_names if c not in existing]
    if not new_cols:
        return
    with get_connection() as con:
        for col in new_cols:
            con.execute(f'ALTER TABLE district_data ADD COLUMN "{col}" TEXT DEFAULT "0.0"')
        con.commit()
    print(f"  [DB] New columns added: {new_cols}")


def fill_missing_sub_columns(row):
    existing = get_existing_columns()
    for col in existing:
        if col.startswith('-') and col not in row:
            row[col] = "0.0"


def upsert_row(new_row):
    ensure_columns(new_row.keys())
    all_cols     = get_existing_columns()
    full_row     = {col: new_row.get(col, "") for col in all_cols}
    cols_sql     = ", ".join(f'"{c}"' for c in all_cols)
    placeholders = ", ".join("?" for _ in all_cols)
    values       = [full_row[c] for c in all_cols]
    with get_connection() as con:
        con.execute(
            f'INSERT OR REPLACE INTO district_data ({cols_sql}) VALUES ({placeholders})',
            values
        )
        con.commit()


def already_scraped(district_code, date):
    with get_connection() as con:
        cur = con.execute(
            "SELECT 1 FROM district_data WHERE district_code=? AND date=?",
            (district_code, date)
        )
        return cur.fetchone() is not None


init_db()


# ==============================
# JS SAFETY
# ==============================

def wait_for_js_function(name, timeout=10):
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script(f"return typeof {name} === 'function'")
    )


def safe_js(script):
    for _ in range(3):
        try:
            return driver.execute_script(script)
        except Exception:
            time.sleep(1)
    raise Exception(f"JS failed: {script}")


# ==============================
# NAVIGATION
# ==============================

def open_home():
    driver.get(BASE_URL)
    wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))


def change_month(year, month):
    wait.until(EC.element_to_be_clickable((By.ID, "calModal"))).click()
    wait.until(EC.presence_of_element_located((By.ID, "selectedyear")))
    driver.execute_script("""
        let y = document.getElementById('selectedyear');
        y.value = arguments[0];
        y.dispatchEvent(new Event('change'));
    """, str(year))
    time.sleep(1)
    months = wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, ".cal_month a")))
    driver.execute_script("arguments[0].click();", months[month - 1])
    time.sleep(2)


def open_states_modal():
    btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "a.textInfo")))
    btn.click()
    wait.until(EC.visibility_of_element_located((By.ID, "myModal11")))


# ==============================
# WAIT FOR CORRECT DISTRICT PAGE
# ==============================

def wait_for_district_page(district_name, timeout=15):
    district_upper = district_name.strip().upper()

    def correct_district_loaded(d):
        try:
            return d.execute_script("""
                var els = document.querySelectorAll('[key="district"]');
                for (var i = 0; i < els.length; i++) {
                    if (els[i].innerText.trim().toUpperCase() === arguments[0]) return true;
                }
                return false;
            """, district_upper)
        except Exception:
            return False

    WebDriverWait(driver, timeout).until(correct_district_loaded)


# ==============================
# WAIT FOR STATE PAGE
# ==============================

def wait_for_state_page(timeout=15):
    def state_page_loaded(d):
        return d.execute_script("""
            var els = document.querySelectorAll('[key="district"]');
            return els.length === 0 || els[0].innerText.trim() === '';
        """)
    WebDriverWait(driver, timeout).until(state_page_loaded)


# ==============================
# WAIT FOR DISTRICT TABLE
# ==============================

def wait_for_district_table(timeout=TABLE_WAIT_TIMEOUT):
    """
    The 'Distributed Quantity(In MT)' table is GUARANTEED on every district page.
    Poll until it exists outside stateDefaultDivId AND has at least one data row.
    Timeout here means page render stalled — caller retries the whole attempt.
    """
    def table_has_data(d):
        try:
            return d.execute_script("""
                var tables = document.querySelectorAll(
                    'table[aria-label="Distributed Quantity(In MT)"]'
                );
                if (!tables.length) return false;

                for (var t of tables) {
                    var p = t.parentElement;
                    var inDefault = false;
                    while (p) {
                        if (p.id === 'stateDefaultDivId') { inDefault = true; break; }
                        p = p.parentElement;
                    }
                    if (inDefault) continue;

                    var rows = t.querySelectorAll('tbody tr');
                    for (var r of rows) {
                        if (r.querySelectorAll('td').length >= 5) return true;
                    }
                }
                return false;
            """)
        except Exception:
            return False

    WebDriverWait(driver, timeout).until(table_has_data)


# ==============================
# TABLE PARSER
# ==============================

PARSE_TABLE_JS = """
    var result = [];
    var tables = document.querySelectorAll(
        'table[aria-label="Distributed Quantity(In MT)"]'
    );

    for (var t of tables) {
        var p = t.parentElement;
        var inDefault = false;
        while (p) {
            if (p.id === 'stateDefaultDivId') { inDefault = true; break; }
            p = p.parentElement;
        }
        if (inDefault) continue;

        var rows = t.querySelectorAll('tbody tr');
        for (var row of rows) {
            var cells = row.querySelectorAll('td');
            if (cells.length < 5) continue;

            var nameCell = cells[0];
            var name = nameCell.innerText.trim();

            var btn = nameCell.querySelector('.menu-toggle');
            if (btn) {
                name = btn.innerText.replace(/[+-]/g, '').trim();
            }

            if (!name || name.toLowerCase() === 'total') continue;

            var convertEl = cells[4].querySelector('.convert');
            var val = convertEl
                ? convertEl.innerText.trim()
                : cells[4].innerText.trim();

            var isSub = row.classList.contains('customRow');
            result.push({ name: name, val: val, isSub: isSub });
        }

        return result;
    }
    return result;
"""


def commodity_to_col(name, is_sub):
    col = name.strip().lower().replace(" ", "_")
    return f"-{col}" if is_sub else col


def parse_table() -> dict:
    try:
        rows = driver.execute_script(PARSE_TABLE_JS)
    except Exception as e:
        print(f"    [PARSE ERROR] {e}")
        return {}

    if not rows:
        return {}

    commodity_data = {}
    for r in rows:
        col = commodity_to_col(r["name"], r["isSub"])
        commodity_data[col] = r["val"]

    return commodity_data


# ==============================
# DRIVER RELAUNCH
# ==============================

def relaunch_driver():
    """Kill the current browser entirely and start a fresh matched pair."""
    global driver, wait
    try:
        driver.quit()
    except Exception:
        pass
    driver = make_driver()
    wait   = WebDriverWait(driver, 20)
    print("  [RELAUNCH] Fresh browser started")


def navigate_to_state_fresh(state):
    """
    From a cold fresh browser, navigate all the way to the state view.
    Called after a relaunch so the next district attempt starts cleanly.
    """
    open_home()
    change_month(TARGET_YEAR, TARGET_MONTH)
    open_states_modal()
    for l in driver.find_elements(By.CSS_SELECTOR, "#myModal11 a"):
        if state["code"] in (l.get_attribute("onclick") or ""):
            l.click()
            break
    wait_for_js_function("liveDistrictdata")
    safe_js(f"liveDistrictdata('{state['code']}')")
    wait_for_state_page()
    print(f"  [RELAUNCH] Ready at state: {state['name']}")


# ==============================
# NAVIGATE BACK TO STATE (soft, within existing session)
# ==============================

def navigate_back_to_state(state):
    """
    Soft nav: try backData() first. If that fails, full page reload back to state.
    Used between normal retries (no browser relaunch).
    """
    try:
        safe_js(f"backData('{state['code']}')")
        wait_for_state_page()
        return
    except Exception as e:
        print(f"    [NAV FAIL] backData failed ({e}) — doing full reload")

    try:
        open_home()
        change_month(TARGET_YEAR, TARGET_MONTH)
        open_states_modal()
        for l in driver.find_elements(By.CSS_SELECTOR, "#myModal11 a"):
            if state["code"] in (l.get_attribute("onclick") or ""):
                l.click()
                break
        wait_for_js_function("liveDistrictdata")
        safe_js(f"liveDistrictdata('{state['code']}')")
        wait_for_state_page()
        print(f"    [NAV OK] Recovered to state: {state['name']}")
    except Exception as e:
        print(f"    [NAV CRITICAL] Full reload also failed: {e}")


# ==============================
# DISTRICT SCRAPER
# ==============================

def scrape_district(d, state):
    """
    Scrape one district. The Distributed Quantity table is GUARANTEED to exist
    on every district page — so we loop forever until data is extracted and saved.

    Retry strategy:
      - Every attempt has a DISTRICT_HARD_TIMEOUT ceiling (SIGALRM) to break
        out of any infinite WebDriverWait hang.
      - After every ATTEMPTS_BEFORE_RELAUNCH consecutive failures, the browser
        is killed and relaunched fresh with a matched Chrome+ChromeDriver pair,
        then navigated back to the state view.
      - Between relaunches, soft navigate_back_to_state is used.
      - Empty parse result = retry (table is guaranteed, empty = render incomplete).
      - The only exit from the while loop is a successful DB save.
    """
    date_str = f"{TARGET_YEAR}-{TARGET_MONTH:02d}"

    if already_scraped(d["code"], date_str):
        print(f"  [SKIP] {d['name']} already in DB")
        return

    attempt = 0

    while True:  # guaranteed table = loop until saved
        attempt += 1

        # Every ATTEMPTS_BEFORE_RELAUNCH failures: relaunch browser entirely
        if attempt > 1 and (attempt - 1) % ATTEMPTS_BEFORE_RELAUNCH == 0:
            print(f"  [RELAUNCH] {attempt - 1} attempts failed for "
                  f"'{d['name']}' — killing and relaunching browser")
            relaunch_driver()
            navigate_to_state_fresh(state)

        # Arm hard timeout for this attempt
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(DISTRICT_HARD_TIMEOUT)

        try:
            print(f"  [attempt {attempt}] Scraping: {d['name']}")

            # 1. Navigate to district
            wait_for_js_function("stateData")
            safe_js(f"stateData('{d['code']}')")

            # 2. Confirm breadcrumb
            wait_for_district_page(d["name"])

            # 3. Wait for guaranteed table to have real rows
            wait_for_district_table(timeout=TABLE_WAIT_TIMEOUT)

            # 4. Parse
            commodities = parse_table()

            # 5. Empty = render not complete, retry
            if not commodities:
                raise ValueError(
                    f"Table present but parse returned empty for '{d['name']}'"
                )

            # 6. Save
            row = {
                "state":         state["name"],
                "district":      d["name"],
                "district_code": d["code"],
                "date":          date_str,
                **commodities
            }
            fill_missing_sub_columns(row)
            upsert_row(row)

            signal.alarm(0)  # disarm on success
            print(f"  [OK] {d['name']} — {len(commodities)} commodities saved")
            return  # only exit: successful save

        except DistrictTimeout:
            print(f"  [HARD TIMEOUT] '{d['name']}' attempt {attempt} "
                  f"exceeded {DISTRICT_HARD_TIMEOUT}s")
            navigate_back_to_state(state)
            time.sleep(2)

        except Exception as e:
            signal.alarm(0)
            print(f"  [RETRY {attempt}] '{d['name']}' "
                  f"— {type(e).__name__}: {e}")
            navigate_back_to_state(state)
            time.sleep(min(2 ** attempt, 30))

        finally:
            signal.alarm(0)  # always disarm


# ==============================
# MAIN
# ==============================

try:
    open_home()
    change_month(TARGET_YEAR, TARGET_MONTH)
    open_states_modal()

    states = []
    links  = driver.find_elements(By.CSS_SELECTOR, "#myModal11 a")
    for l in links:
        onclick = l.get_attribute("onclick")
        match   = re.search(r"stateData\('(\d+)'\)", str(onclick))
        if match:
            states.append({
                "name": l.text.strip(),
                "code": match.group(1)
            })

    print(f"[INFO] {len(states)} states found — processing [{START_STATE}:{END_STATE}]")

    for state in states[START_STATE:END_STATE]:

        print(f"\n{'='*50}\n[STATE] {state['name']}")

        # Retry state navigation up to 3 times with full reload each time
        for nav_attempt in range(1, 4):
            try:
                open_home()

                # Wait for the page to be fully interactive before clicking modal
                wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "a.textInfo")))

                open_states_modal()

                for l in driver.find_elements(By.CSS_SELECTOR, "#myModal11 a"):
                    if state["code"] in (l.get_attribute("onclick") or ""):
                        l.click()
                        break

                wait_for_js_function("liveDistrictdata")
                safe_js(f"liveDistrictdata('{state['code']}')")

                wait.until(EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "a[onclick*='stateData(']")
                ))
                break  # navigation succeeded

            except Exception as e:
                print(f"  [STATE NAV RETRY {nav_attempt}/3] {state['name']} — {type(e).__name__}: {e}")
                if nav_attempt == 3:
                    # Full browser relaunch if all nav attempts fail
                    print(f"  [STATE RELAUNCH] Relaunching browser for {state['name']}")
                    relaunch_driver()
                time.sleep(3)
                
except Exception:
    import traceback
    traceback.print_exc()

finally:
    print(f"\n{'='*50}\n[DONE] Data saved to {DB_FILE}")
    driver.quit()