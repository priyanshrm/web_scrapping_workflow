import re
import time
import sqlite3
import os

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# ==============================
# CONFIG
# ==============================

TARGET_YEAR = 2026
TARGET_MONTH = 4
START_STATE = 0
END_STATE = 2

BASE_URL = "https://impds.nic.in/sale/"
DB_FILE = f"{TARGET_YEAR}_{TARGET_MONTH}_district_data.db"
TABLE_NAME = "district_data"

KEY_FIELDS = ["state", "district_code", "date"]
FIXED_FIELDS = ["state", "district", "district_code", "date"]


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
# DB HELPERS
# ==============================

def get_db():
    """Return a connection with WAL mode for faster concurrent writes."""
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def commodity_to_col(name, is_sub):
    col = name.strip().lower().replace(" ", "_")
    # SQLite column names can't start with '-'; prefix sub-commodities with 'sub_'
    return f"sub_{col}" if is_sub else col


def init_db():
    """Create the table with fixed columns if it doesn't exist."""
    with get_db() as conn:
        cols_ddl = ", ".join(f'"{c}" TEXT' for c in FIXED_FIELDS)
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                {cols_ddl},
                UNIQUE({", ".join(f'"{k}"' for k in KEY_FIELDS)})
            )
        """)
        # Index on key fields for fast upsert lookup
        conn.execute(f"""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_key
            ON {TABLE_NAME} ({", ".join(f'"{k}"' for k in KEY_FIELDS)})
        """)
    print(f"[DB] Initialised → {DB_FILE}")


def get_existing_columns():
    """Return the set of column names currently in the table."""
    with get_db() as conn:
        cur = conn.execute(f"PRAGMA table_info({TABLE_NAME})")
        return {row["name"] for row in cur.fetchall()}


def ensure_columns(col_names: list[str]):
    """
    Add any new commodity columns that aren't in the table yet.
    ALTER TABLE ADD COLUMN is a metadata-only op in SQLite — very fast.
    """
    existing = get_existing_columns()
    new_cols = [c for c in col_names if c not in existing]
    if not new_cols:
        return
    with get_db() as conn:
        for col in new_cols:
            conn.execute(f'ALTER TABLE {TABLE_NAME} ADD COLUMN "{col}" TEXT DEFAULT "0.0"')
    print(f"[DB] Added columns: {new_cols}")


def upsert_row(row: dict):
    """
    Insert or update a district row.
    Uses INSERT OR REPLACE which leverages the UNIQUE index — no full table scan.
    """
    ensure_columns([c for c in row if c not in FIXED_FIELDS])

    cols = list(row.keys())
    placeholders = ", ".join("?" for _ in cols)
    col_clause = ", ".join(f'"{c}"' for c in cols)
    values = [row[c] for c in cols]

    with get_db() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO {TABLE_NAME} ({col_clause}) VALUES ({placeholders})",
            values,
        )


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


def open_states_modal(retries=3):
    """Click the states button and wait for the modal, with retries."""
    for attempt in range(retries):
        try:
            # Wait for page JS to be ready before interacting
            WebDriverWait(driver, 20).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            time.sleep(1)  # extra buffer for Angular binding

            btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "a.textInfo")))
            driver.execute_script("arguments[0].click();", btn)  # JS click avoids intercept issues
            wait.until(EC.visibility_of_element_located((By.ID, "myModal11")))
            return  # success
        except Exception as e:
            print(f"  [WARN] open_states_modal attempt {attempt + 1} failed: {e}")
            if attempt < retries - 1:
                driver.refresh()
                time.sleep(3)
            else:
                raise


# ==============================
# WAIT FOR CORRECT DISTRICT PAGE
# ==============================

def wait_for_district_page(district_name, timeout=15):
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
    time.sleep(0.5)


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
    time.sleep(0.5)


# ==============================
# TABLE PARSER
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


def parse_table() -> dict:
    rows = driver.execute_script(PARSE_TABLE_JS)

    if not rows:
        raise ValueError("Distributed Quantity table found but returned no commodity rows")

    commodity_data = {}
    for r in rows:
        col = commodity_to_col(r["name"], r["isSub"])
        commodity_data[col] = r["val"]

    return commodity_data


# ==============================
# MAIN
# ==============================

init_db()

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

    print(f"States found: {len(states)}")

    for state in states[START_STATE:END_STATE]:

        print(f"\nSTATE: {state['name']}")

        open_home()
        open_states_modal()

        for l in driver.find_elements(By.CSS_SELECTOR, "#myModal11 a"):
            if state["code"] in (l.get_attribute("onclick") or ""):
                l.click()
                break

        wait_for_js_function("liveDistrictdata")
        safe_js(f"liveDistrictdata('{state['code']}')")
        time.sleep(2)

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

        seen = set()
        districts = [d for d in districts if not (d["code"] in seen or seen.add(d["code"]))]
        print(f"Districts: {len(districts)}")

        for d in districts:
            try:
                print(f"  -> {d['name']} (code: {d['code']})")

                wait_for_js_function("stateData")
                safe_js(f"stateData('{d['code']}')")
                wait_for_district_page(d["name"])

                # Table is always present — retry once if DOM isn't ready yet
                try:
                    commodities = parse_table()
                except ValueError:
                    print(f"    [RETRY] Table empty on first attempt, waiting 2s...")
                    time.sleep(2)
                    commodities = parse_table()  # raises again if still empty → caught below

                print(f"    Columns: {list(commodities.keys())}")
                print(f"    Values:  {commodities}")

                row = {
                    "state": state["name"],
                    "district": d["name"],
                    "district_code": d["code"],
                    "date": f"{TARGET_YEAR}-{TARGET_MONTH:02d}",
                    **commodities,
                }

                upsert_row(row)

                safe_js(f"backData('{state['code']}')")
                wait_for_state_page()

            except Exception as e:
                print(f"  District error ({d['name']}): {e}")
                try:
                    safe_js(f"backData('{state['code']}')")
                    wait_for_state_page()
                except Exception:
                    pass

except Exception:
    import traceback
    traceback.print_exc()

finally:
    print(f"\nDONE → {DB_FILE}")
    driver.quit()