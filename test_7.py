import re
import time
import csv
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
END_STATE = 1

BASE_URL = "https://impds.nic.in/sale/"
CSV_FILE = f"{TARGET_YEAR}_{TARGET_MONTH}_district_data.csv"

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
# CSV HELPERS
# ==============================

def commodity_to_col(name, is_sub):
    col = name.strip().lower().replace(" ", "_")
    return f"-{col}" if is_sub else col


def get_fields():
    if not os.path.exists(CSV_FILE):
        return list(FIXED_FIELDS)
    with open(CSV_FILE, "r", encoding="utf-8") as f:
        return next(csv.reader(f))


def ensure_columns(cols):
    current = get_fields()
    new_cols = [c for c in cols if c not in current]
    if not new_cols:
        return
    updated = current + new_cols
    rows = []
    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=updated, restval="")
        writer.writeheader()
        writer.writerows(rows)
    print(f"[CSV] Added columns: {new_cols}")


def fill_missing_sub_columns(row):
    for col in get_fields():
        if col.startswith('-') and col not in row:
            row[col] = "0.0"


def upsert_row(new_row):
    ensure_columns(new_row.keys())
    fields = get_fields()
    rows = []
    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                if not all(r.get(k) == new_row[k] for k in KEY_FIELDS):
                    rows.append(r)
    rows.append(new_row)
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, restval="")
        writer.writeheader()
        writer.writerows(rows)


if not os.path.exists(CSV_FILE):
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=FIXED_FIELDS).writeheader()


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
# WAIT HELPERS
# ==============================

def wait_for_district_page(district_name, timeout=15):
    """Wait until the district breadcrumb confirms this district is loaded."""
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


def wait_for_state_page(timeout=10):
    """Wait until district breadcrumb is cleared (back to state view)."""
    def state_view_loaded(d):
        try:
            return d.execute_script("""
                var els = document.querySelectorAll('[key="district"]');
                for (var i = 0; i < els.length; i++) {
                    if (els[i].innerText.trim() !== '') return false;
                }
                var stEls = document.querySelectorAll('[key="state"]');
                for (var i = 0; i < stEls.length; i++) {
                    if (stEls[i].innerText.trim() !== '') return true;
                }
                return false;
            """)
        except Exception:
            return False

    WebDriverWait(driver, timeout).until(state_view_loaded)
    time.sleep(0.3)


# ==============================
# DISTRICT NAVIGATION
# ==============================

def navigate_to_district(district_code, district_name, state_code, retries=3):
    """Navigate to a district using DistrictData() JS call with retry logic."""
    for attempt in range(retries):
        try:
            if attempt > 0:
                print(f"    [RETRY {attempt}] {district_name}")
                try:
                    safe_js(f"backData('{state_code}')")
                    time.sleep(2)
                except Exception:
                    pass

            wait_for_js_function("DistrictData")
            safe_js(f"DistrictData('{district_code}')")
            wait_for_district_page(district_name, timeout=15)
            return True

        except Exception as e:
            print(f"    [WARN] Attempt {attempt+1} failed for {district_name}: {e}")
            time.sleep(2)

    return False


def back_to_state(state_code, timeout=10):
    """Go back to state view and confirm district breadcrumb is cleared."""
    safe_js(f"backData('{state_code}')")
    wait_for_state_page(timeout=timeout)


# ==============================
# COLLECT DISTRICTS FROM MAP DOTS
# ==============================

def collect_districts():
    """
    Collect districts from the blinking map dot links.
    The HTML uses: <a onclick="DistrictData('638')"><img aria-label="NICOBARS" ...></a>
    This is the correct and complete source for all districts.
    """
    districts = []
    seen = set()

    imgs = driver.find_elements(By.CSS_SELECTOR, "img.map[aria-label]")
    for img in imgs:
        try:
            parent_a = img.find_element(By.XPATH, "..")
            onclick = parent_a.get_attribute("onclick") or ""
            match = re.search(r"DistrictData\('(\d+)'\)", onclick)
            if not match:
                continue
            code = match.group(1)
            name = (img.get_attribute("aria-label") or
                    img.get_attribute("title") or "").strip()
            if code and name and code not in seen:
                districts.append({"name": name, "code": code})
                seen.add(code)
        except Exception:
            continue

    return districts


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


def parse_table():
    try:
        rows = driver.execute_script(PARSE_TABLE_JS)
    except Exception as e:
        print(f"  [WARN] parse_table JS error: {e}")
        return {}

    if not rows:
        print("  [WARN] Could not find Distributed Quantity table")
        return {}

    commodity_data = {}
    for r in rows:
        col = commodity_to_col(r["name"], r["isSub"])
        commodity_data[col] = r["val"]

    return commodity_data


# ==============================
# MAIN
# ==============================

try:
    open_home()
    change_month(TARGET_YEAR, TARGET_MONTH)
    open_states_modal()

    # Collect all states
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

        # Click into the state
        for l in driver.find_elements(By.CSS_SELECTOR, "#myModal11 a"):
            if state["code"] in (l.get_attribute("onclick") or ""):
                l.click()
                break

        wait_for_js_function("liveDistrictdata")
        safe_js(f"liveDistrictdata('{state['code']}')")
        time.sleep(2)

        # Collect districts from map dots using DistrictData()
        districts = collect_districts()
        print(f"Districts: {len(districts)}")

        if not districts:
            print(f"  [WARN] No districts found for {state['name']}, skipping.")
            continue

        for d in districts:
            print(f"  -> {d['name']} (code: {d['code']})")

            success = navigate_to_district(d["code"], d["name"], state["code"])
            if not success:
                print(f"  [SKIP] {d['name']} — failed after retries")
                try:
                    back_to_state(state["code"])
                except Exception:
                    pass
                continue

            try:
                commodities = parse_table()
                print(f"    Columns: {list(commodities.keys())}")
                print(f"    Values:  {commodities}")

                row = {
                    "state": state["name"],
                    "district": d["name"],
                    "district_code": d["code"],
                    "date": f"{TARGET_YEAR}-{TARGET_MONTH:02d}",
                    **commodities
                }

                fill_missing_sub_columns(row)
                upsert_row(row)

            except Exception as e:
                print(f"  [ERROR] Parsing/saving {d['name']}: {e}")

            try:
                back_to_state(state["code"])
            except Exception as e:
                print(f"  [WARN] backData failed after {d['name']}: {e}")
                time.sleep(1)

except Exception:
    import traceback
    traceback.print_exc()

finally:
    print(f"\nDONE → {CSV_FILE}")
    driver.quit()