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
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--start", type=int, default=0)
parser.add_argument("--end", type=int, default=None)
parser.add_argument("--year", type=int, default=2023)
parser.add_argument("--month", type=int, default=4)
parser.add_argument(
    "--mode",
    choices=["all", "one"],
    default="all",
    help=(
        "'all' = scrape every district in each state (default). "
        "'one' = scrape only one verified district per state, falling back "
        "to the next district if the current one exhausts its retries."
    ),
)
args = parser.parse_args()

TARGET_YEAR = args.year
TARGET_MONTH = args.month
START_STATE = args.start
END_STATE = args.end if args.end != 99 else None
SCRAPE_MODE = args.mode  # "all" or "one"

BASE_URL = "https://impds.nic.in/sale/"
DB_FILE = f"{TARGET_YEAR}_{TARGET_MONTH}_{START_STATE}_{END_STATE}_{SCRAPE_MODE}.db"

FIXED_FIELDS = ["state", "district", "district_code", "date"]
KEY_FIELDS = ["state", "district_code", "date"]

# Retry tuning
MAX_DISTRICT_RETRIES = 5      # district-level retries (re-click + re-wait + re-parse)
MAX_STATE_RETRIES = 3         # state-level retries (re-open state, re-collect districts)
TABLE_POLL_TIMEOUT = 20       # seconds to wait for a *verified* table per attempt
TABLE_POLL_INTERVAL = 0.4     # seconds between verification polls

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
        cols_def = ", ".join(f'"{c}" TEXT' for c in FIXED_FIELDS)
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


init_db()


# ==============================
# JS SAFETY
# ==============================

def wait_for_js_function(name, timeout=10):
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script(f"return typeof {name} === 'function'")
    )


def safe_js(script):
    last_exc = None
    for _ in range(3):
        try:
            return driver.execute_script(script)
        except Exception as e:
            last_exc = e
            time.sleep(1)
    raise RuntimeError(f"JS failed after retries: {script} ({last_exc})")


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
# WAIT FOR CORRECT DISTRICT PAGE (breadcrumb only — necessary but not sufficient)
# ==============================

def wait_for_district_page(district_name, timeout=15):
    """
    Wait until the page breadcrumb confirms this specific district is loaded.
    This only confirms navigation succeeded — it does NOT confirm the
    Distributed Quantity table has finished rendering. Use
    wait_for_verified_table() afterwards for that.
    """
    district_upper = district_name.strip().upper()

    def correct_district_loaded(d):
        try:
            return d.execute_script("""
                var els = document.querySelectorAll('[key="district"]');
                for (var i = 0; i < els.length; i++) {
                    var text = els[i].innerText.trim().toUpperCase();
                    if (text === arguments[0]) return true;
                }
                return false;
            """, district_upper)
        except Exception:
            return False

    WebDriverWait(driver, timeout).until(correct_district_loaded)


def wait_for_state_page(timeout=15):
    """Wait until the district breadcrumb is removed/cleared (back on state view)."""
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
        var sawTotal = false;
        var totalNonEmpty = false;

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

            if (!name) continue;

            var val = cells[4].innerText.trim();

            if (name.toLowerCase() === 'total') {
                sawTotal = true;
                if (val !== '') totalNonEmpty = true;
                continue;
            }

            var isSub = row.className.indexOf('customRow') !== -1;
            result.push({name: name, val: val, isSub: isSub});
        }

        // Only accept this table as the right one if it actually has a
        // populated Total row — otherwise treat it as "not rendered yet".
        if (sawTotal && totalNonEmpty && result.length > 0) {
            return {rows: result, ready: true};
        } else {
            return {rows: [], ready: false};
        }
    }
    return {rows: [], ready: false};
"""


def commodity_to_col(name, is_sub):
    col = name.strip().lower().replace(" ", "_")
    return f"-{col}" if is_sub else col


def try_parse_table():
    """
    Single attempt to read the table. Returns (commodity_dict, ready_bool).
    Never raises for "not ready yet" — only for genuine JS execution errors,
    which the caller treats the same as "not ready" and retries.
    """
    try:
        result = driver.execute_script(PARSE_TABLE_JS)
    except Exception:
        return {}, False

    if not result or not result.get("ready"):
        return {}, False

    commodity_data = {}
    for r in result["rows"]:
        col = commodity_to_col(r["name"], r["isSub"])
        commodity_data[col] = r["val"]

    return commodity_data, True


def wait_for_verified_table(timeout=TABLE_POLL_TIMEOUT, interval=TABLE_POLL_INTERVAL):
    """
    Poll until the Distributed Quantity table is present AND its Total row
    is populated (i.e. data has actually loaded, not just the skeleton).
    Returns the parsed commodity dict on success.
    Raises TimeoutError if the table never becomes ready within `timeout`.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        commodities, ready = try_parse_table()
        if ready:
            return commodities
        time.sleep(interval)

    raise TimeoutError("Distributed Quantity table did not become ready (no populated Total row) in time")


# ==============================
# DISTRICT SCRAPER — only ever returns after a VERIFIED successful extraction,
# or raises after exhausting retries.
# ==============================

def scrape_district(d, state):
    """
    Scrape a single district. Retries up to MAX_DISTRICT_RETRIES times.
    A district is only considered done (DB write happens) once
    wait_for_verified_table() confirms real data was read.
    Raises RuntimeError after exhausting retries — caller must catch, log,
    and move on to the next district.
    """
    last_exc = None

    for attempt in range(1, MAX_DISTRICT_RETRIES + 1):
        try:
            print(f"  [{attempt}/{MAX_DISTRICT_RETRIES}] Scraping: {d['name']}")

            wait_for_js_function("stateData")
            safe_js(f"stateData('{d['code']}')")

            # Step 1: confirm navigation landed on the right district
            wait_for_district_page(d["name"])

            # Step 2: confirm the table itself has rendered with real data
            commodities = wait_for_verified_table()

            row = {
                "state": state["name"],
                "district": d["name"],
                "district_code": d["code"],
                "date": f"{TARGET_YEAR}-{TARGET_MONTH:02d}",
                **commodities
            }

            fill_missing_sub_columns(row)
            upsert_row(row)
            print(f"  [OK] {d['name']} — inserted ({len(commodities)} commodity fields)")
            return  # verified success

        except Exception as e:
            last_exc = e
            print(f"  [RETRY {attempt}/{MAX_DISTRICT_RETRIES}] {d['name']} — {type(e).__name__}: {e}")
            # Navigate back to state view before retrying
            try:
                safe_js(f"backData('{state['code']}')")
                wait_for_state_page()
            except Exception:
                pass
            time.sleep(1)

    raise RuntimeError(
        f"District '{d['name']}' (code: {d['code']}) failed after "
        f"{MAX_DISTRICT_RETRIES} attempts. Last error: {last_exc}"
    )


# ==============================
# STATE-LEVEL COLLECTION (isolated so one bad state doesn't kill the run)
# ==============================

def collect_districts_for_state(state):
    """
    Navigate to a state's district list and return the list of districts.
    Retries the whole navigation sequence up to MAX_STATE_RETRIES times.
    Raises RuntimeError if it never succeeds.
    """
    last_exc = None

    for attempt in range(1, MAX_STATE_RETRIES + 1):
        try:
            open_home()
            open_states_modal()

            clicked = False
            for l in driver.find_elements(By.CSS_SELECTOR, "#myModal11 a"):
                if state["code"] in (l.get_attribute("onclick") or ""):
                    l.click()
                    clicked = True
                    break

            if not clicked:
                raise RuntimeError(f"Could not find clickable link for state {state['name']}")

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

            if not districts:
                raise RuntimeError("No districts found for this state — page likely not fully loaded")

            return districts

        except Exception as e:
            last_exc = e
            print(f"  [STATE RETRY {attempt}/{MAX_STATE_RETRIES}] {state['name']} — {type(e).__name__}: {e}")
            time.sleep(2)

    raise RuntimeError(
        f"State '{state['name']}' (code: {state['code']}) failed to load districts after "
        f"{MAX_STATE_RETRIES} attempts. Last error: {last_exc}"
    )


# ==============================
# PER-STATE DISTRICT SCRAPING — mode aware ("all" vs "one")
# ==============================

def scrape_districts_for_state(districts, state, mode):
    """
    Drive district scraping for a single state according to `mode`:

      "all" — scrape every district. A district that exhausts its retries
              is logged and skipped; the rest of the state's districts are
              still attempted.

      "one" — scrape districts in order until ONE is successfully verified
              and inserted, then stop. If a district exhausts its retries,
              fall through to the next district instead of giving up on the
              state outright. Only if every district fails does the state
              end up with zero rows.

    Always navigates back to the state view between districts, regardless
    of mode or outcome.
    """
    got_one = False

    for d in districts:
        try:
            scrape_district(d, state)
            got_one = True
        except RuntimeError:
            print(f"  [FAILED] {d['name']} — exhausted {MAX_DISTRICT_RETRIES} retries")
        finally:
            try:
                safe_js(f"backData('{state['code']}')")
                wait_for_state_page()
            except Exception:
                pass

        if mode == "one" and got_one:
            break  # we have our one verified district for this state

    if mode == "one" and not got_one:
        print(f"  [STATE FAILED] {state['name']} — no district could be scraped "
              f"(tried all {len(districts)} districts)")


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

    print(f"[INFO] {len(states)} states found — processing [{START_STATE}:{END_STATE}] — mode={SCRAPE_MODE}")

    for state in states[START_STATE:END_STATE]:

        print(f"\n{'='*50}\n[STATE] {state['name']}")

        # Per-state isolation: a state that never loads is logged and skipped,
        # it does NOT abort the whole batch.
        try:
            districts = collect_districts_for_state(state)
        except RuntimeError as e:
            print(f"  [STATE FAILED] {state['name']} — {e}")
            continue

        print(f"[INFO] {len(districts)} districts found")

        scrape_districts_for_state(districts, state, SCRAPE_MODE)

except Exception:
    import traceback
    traceback.print_exc()

finally:
    print(f"\n{'='*50}\n[DONE] Data saved to {DB_FILE}")
    driver.quit()