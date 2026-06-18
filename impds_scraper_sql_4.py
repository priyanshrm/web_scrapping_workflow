import re
import time
import os
import sqlite3

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# ==============================
# CONFIG
# ==============================

TARGET_YEAR = 2023
TARGET_MONTH = 4
START_STATE = 0
END_STATE = None

BASE_URL = "https://impds.nic.in/sale/"
DB_FILE = f"{TARGET_YEAR}_{TARGET_MONTH}_district_data.db"

FIXED_FIELDS = ["state", "district", "district_code", "date"]
KEY_FIELDS = ["state", "district_code", "date"]


# ==============================
# DRIVER
# ==============================

options = webdriver.ChromeOptions()
options.add_argument("--headless")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")
options.add_argument("--disable-gpu")
options.add_argument("--disable-blink-features=AutomationControlled")
options.add_argument("--start-maximized")

driver = webdriver.Chrome(options=options)
wait = WebDriverWait(driver, 20)


# ==============================
# SQL HELPERS
# ==============================

def get_connection():
    return sqlite3.connect(DB_FILE)


def init_db():
    """Create the table with fixed columns if it doesn't exist."""
    with get_connection() as con:
        cols_def = ", ".join(
            f'"{c}" TEXT' if c != "district_code" else f'"{c}" TEXT'
            for c in FIXED_FIELDS
        )
        con.execute(f"""
            CREATE TABLE IF NOT EXISTS district_data (
                {cols_def},
                PRIMARY KEY ({', '.join(f'"{k}"' for k in KEY_FIELDS)})
            )
        """)
        con.commit()


def get_existing_columns():
    """Return the current column names in the table."""
    with get_connection() as con:
        cur = con.execute("PRAGMA table_info(district_data)")
        return [row[1] for row in cur.fetchall()]


def ensure_columns(col_names):
    """Add any new commodity columns that don't exist yet."""
    existing = set(get_existing_columns())
    new_cols = [c for c in col_names if c not in existing]
    if not new_cols:
        return
    with get_connection() as con:
        for col in new_cols:
            con.execute(f'ALTER TABLE district_data ADD COLUMN "{col}" TEXT DEFAULT "0.0"')
        con.commit()
    print(f"  [DB] New columns added: {new_cols}")


def fill_missing_sub_columns(row):
    """Fill sub-commodity columns that are absent in this row with '0.0'."""
    existing = get_existing_columns()
    for col in existing:
        if col.startswith('-') and col not in row:
            row[col] = "0.0"


def upsert_row(new_row):
    """
    Ensure all columns exist, then INSERT OR REPLACE the row.
    SQLite's PRIMARY KEY constraint handles the upsert: if the composite key
    (state, district_code, date) already exists, the row is replaced in full.
    """
    ensure_columns(new_row.keys())
    all_cols = get_existing_columns()

    # Build the full row dict, filling any missing columns with empty string
    full_row = {col: new_row.get(col, "") for col in all_cols}

    cols_sql = ", ".join(f'"{c}"' for c in all_cols)
    placeholders = ", ".join("?" for _ in all_cols)
    values = [full_row[c] for c in all_cols]

    with get_connection() as con:
        con.execute(
            f'INSERT OR REPLACE INTO district_data ({cols_sql}) VALUES ({placeholders})',
            values
        )
        con.commit()


# Initialize DB on startup
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
    """
    Wait until the page breadcrumb confirms this specific district is loaded.
    The district page has:  <div class="status m_menu" key="district">SOUTH ANDAMANS</div>
    This element MUST be found — the caller should never proceed without it.
    Raises TimeoutException if not found within timeout, which the caller must handle
    as a hard retry/abort (not silently skip).
    """
    district_upper = district_name.strip().upper()

    def correct_district_loaded(d):
        try:
            result = d.execute_script("""
                var els = document.querySelectorAll('[key="district"]');
                for (var i = 0; i < els.length; i++) {
                    var text = els[i].innerText.trim().toUpperCase();
                    if (text === arguments[0]) return true;
                }
                return false;
            """, district_upper)
            return result
        except Exception:
            return False

    WebDriverWait(driver, timeout).until(correct_district_loaded)
    time.sleep(0.5)  # small buffer for table to fully render


# ==============================
# WAIT FOR STATE PAGE
# ==============================

def wait_for_state_page(timeout=15):
    """
    Wait until the district breadcrumb is removed or cleared,
    signaling that the page has successfully returned to the state view.
    """
    def state_page_loaded(d):
        return d.execute_script("""
            var els = document.querySelectorAll('[key="district"]');
            return els.length === 0 || els[0].innerText.trim() === '';
        """)

    WebDriverWait(driver, timeout).until(state_page_loaded)
    time.sleep(0.5)


# ==============================
# TABLE PARSER — pure JS, no Selenium element refs
# ==============================

PARSE_TABLE_JS = """
    var result = [];
    var tables = document.querySelectorAll('table');
    for (var t of tables) {
        var ariaLabel = t.getAttribute('aria-label') || '';
        var ths = Array.from(t.querySelectorAll('th')).map(function(h){ return h.innerText.trim(); });
        if (ariaLabel.indexOf('Distributed Quantity') === -1 &&
            (ths.indexOf('Commodity') === -1 || ths.indexOf('Total') === -1)) continue;

        var inDefault = false;
        var parent = t.parentElement;
        while (parent) {
            if (parent.id === 'stateDefaultDivId') { inDefault = true; break; }
            parent = parent.parentElement;
        }
        if (inDefault) continue;

        var rows = t.querySelectorAll('tr');
        for (var i = 0; i < rows.length; i++) {
            var row = rows[i];
            var cells = row.querySelectorAll('td');
            if (cells.length < 5) continue;

            var nameCell = cells[0];
            var name = nameCell.innerText.trim();

            var btn = nameCell.querySelector('.menu-toggle');
            if (btn) {
                name = btn.innerText.trim();
            }

            if (!name || name.toLowerCase() === 'total') continue;

            var val = cells[4].innerText.trim();
            var isSub = row.className.indexOf('customRow') !== -1;

            result.push({name: name, val: val, isSub: isSub});
        }
        return result;
    }
    return result;
"""


def commodity_to_col(name, is_sub):
    col = name.strip().lower().replace(" ", "_")
    return f"-{col}" if is_sub else col


def parse_table() -> dict:
    """Extract all commodity data from the district Distributed Quantity table via JS."""
    try:
        rows = driver.execute_script(PARSE_TABLE_JS)
    except Exception as e:
        pass  # JS parse error; retry will handle
        return {}

    if not rows:
        pass  # table not found; retry will handle
        return {}

    commodity_data = {}
    for r in rows:
        col = commodity_to_col(r["name"], r["isSub"])
        commodity_data[col] = r["val"]

    return commodity_data


# ==============================
# DISTRICT SCRAPER WITH MANDATORY ELEMENT ENFORCEMENT
# ==============================

MAX_DISTRICT_RETRIES = 3


def scrape_district(d, state):
    """
    Scrape a single district. Retries up to MAX_DISTRICT_RETRIES times if the
    district breadcrumb element ([key="district"]) is not found.
    Raises after exhausting retries — the caller must handle (log + move on to
    next district, NOT silently skip).
    """
    last_exc = None

    for attempt in range(1, MAX_DISTRICT_RETRIES + 1):
        try:
            print(f"  [{attempt}/{MAX_DISTRICT_RETRIES}] Scraping: {d['name']}")

            wait_for_js_function("stateData")
            safe_js(f"stateData('{d['code']}')")

            # This is the mandatory guard — must confirm the correct district page
            # is rendered before reading any data. TimeoutException here triggers retry.
            wait_for_district_page(d["name"])

            commodities = parse_table()

            row = {
                "state": state["name"],
                "district": d["name"],
                "district_code": d["code"],
                "date": f"{TARGET_YEAR}-{TARGET_MONTH:02d}",
                **commodities
            }

            fill_missing_sub_columns(row)
            upsert_row(row)
            print(f"  [OK] {d['name']} — inserted")
            return  # success

        except Exception as e:
            last_exc = e
            print(f"  [RETRY {attempt}/{MAX_DISTRICT_RETRIES}] {d['name']} — {type(e).__name__}")
            # Navigate back to state view before retrying
            try:
                safe_js(f"backData('{state['code']}')")
                wait_for_state_page()
            except Exception:
                pass
            time.sleep(1)

    # All retries exhausted — raise so caller can log and continue to next district
    raise RuntimeError(
        f"District '{d['name']}' (code: {d['code']}) failed after "
        f"{MAX_DISTRICT_RETRIES} attempts. Last error: {last_exc}"
    )


# ==============================
# MAIN
# ==============================

try:
    open_home()
    change_month(TARGET_YEAR, TARGET_MONTH)
    open_states_modal()

    states = []
    links = driver.find_elements(By.CSS_SELECTOR, "#myModal11 a")
    for l in links:
        onclick = l.get_attribute("onclick")
        match = re.search(r"stateData\('(\d+)'\)", str(onclick))
        if match:
            states.append({
                "name": l.text.strip(),
                "code": match.group(1)
            })

    print(f"[INFO] {len(states)} states found — processing [{START_STATE}:{END_STATE}]")

    for state in states[START_STATE:END_STATE]:

        print(f"\n{'='*50}\n[STATE] {state['name']}")

        open_home()
        open_states_modal()

        # click state
        for l in driver.find_elements(By.CSS_SELECTOR, "#myModal11 a"):
            if state["code"] in (l.get_attribute("onclick") or ""):
                l.click()
                break

        wait_for_js_function("liveDistrictdata")
        safe_js(f"liveDistrictdata('{state['code']}')")
        time.sleep(2)

        # collect districts
        districts = []
        for l in driver.find_elements(By.TAG_NAME, "a"):
            onclick = l.get_attribute("onclick")
            if onclick and "stateData(" in onclick:
                match = re.search(r"stateData\('(\d+)'\)", onclick)
                if match:
                    imgs = l.find_elements(By.TAG_NAME, "img")
                    if imgs and imgs[0].get_attribute("width") == "12":
                        districts.append({
                            "name": imgs[0].get_attribute("aria-label"),
                            "code": match.group(1)
                        })

        # dedupe
        seen = set()
        districts = [d for d in districts if not (d["code"] in seen or seen.add(d["code"]))]
        print(f"[INFO] {len(districts)} districts found")

        for d in districts:
            try:
                scrape_district(d, state)
            except RuntimeError as e:
                # District exhausted all retries — log it and continue to the next one
                print(f"  [FAILED] {d['name']} — exhausted {MAX_DISTRICT_RETRIES} retries")
            finally:
                # Always navigate back to state view before next district
                try:
                    safe_js(f"backData('{state['code']}')")
                    wait_for_state_page()
                except Exception:
                    pass

except Exception:
    import traceback
    traceback.print_exc()

finally:
    print(f"\n{'='*50}\n[DONE] Data saved to {DB_FILE}")
    driver.quit()