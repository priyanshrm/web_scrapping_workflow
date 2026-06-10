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

def wait_for_js_function(name, timeout=15):
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
    # Wait for the main dashboard JS to be available
    WebDriverWait(driver, 20).until(
        lambda d: d.execute_script("return typeof stateData === 'function'")
    )


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


def navigate_to_state(state_code):
    """Navigate from home to a state's district map view."""
    open_home()
    change_month(TARGET_YEAR, TARGET_MONTH)

    # Open states modal
    btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "a.textInfo")))
    btn.click()
    wait.until(EC.visibility_of_element_located((By.ID, "myModal11")))

    # Click state link in modal
    clicked = False
    for l in driver.find_elements(By.CSS_SELECTOR, "#myModal11 a"):
        if state_code in (l.get_attribute("onclick") or ""):
            l.click()
            clicked = True
            break
    if not clicked:
        raise Exception(f"State link not found for code {state_code}")

    # Wait for state map to load (stateDefaultDivId hidden, stateDivId shown with district blinkicons)
    WebDriverWait(driver, 15).until(
        lambda d: len(d.find_elements(By.CSS_SELECTOR, ".blink_icon_img")) > 0
    )
    time.sleep(1)


def get_districts_for_state(state_code):
    """
    After calling navigate_to_state(), collect all district links.
    Districts are the blinking icons on the map with onclick="DistrictData('NNN')"
    or stateData calls with img width=12.
    """
    districts = []
    seen = set()

    # The district-level blink icons use DistrictData() or stateData() with img[width=12]
    for a in driver.find_elements(By.TAG_NAME, "a"):
        onclick = a.get_attribute("onclick") or ""

        # Try DistrictData pattern first
        m = re.search(r"DistrictData\('(\d+)'\)", onclick)
        if m:
            code = m.group(1)
            if code not in seen:
                seen.add(code)
                imgs = a.find_elements(By.TAG_NAME, "img")
                name = imgs[0].get_attribute("aria-label") if imgs else a.get_attribute("title") or code
                districts.append({"name": name, "code": code})
            continue

        # Fallback: stateData with small blinking icon
        m = re.search(r"stateData\('(\d+)'\)", onclick)
        if m:
            code = m.group(1)
            if code not in seen:
                imgs = a.find_elements(By.TAG_NAME, "img")
                if imgs and imgs[0].get_attribute("width") == "12":
                    seen.add(code)
                    name = imgs[0].get_attribute("aria-label") or code
                    districts.append({"name": name, "code": code})

    return districts


# ==============================
# WAIT FOR CORRECT DISTRICT PAGE
# ==============================

def wait_for_district_page(district_code, timeout=20):
    """
    Wait until the district data page is loaded for the given district code.

    Strategy: wait for the Distributed Quantity table to appear inside #stateDivId
    AND verify it is NOT inside #stateDefaultDivId (which is the national view).
    We use the back button element (aria-label="Go back") as the reliable signal
    that we are on a district page, not a state or national page.
    """
    def district_loaded(d):
        try:
            # Check for the back button that only appears on district pages
            back_btns = d.find_elements(By.CSS_SELECTOR, "i.fa-hand-o-left")
            if not back_btns:
                return False

            # Also check that the Distributed Quantity table is present
            # and not inside stateDefaultDivId
            result = d.execute_script("""
                var tables = document.querySelectorAll('table[aria-label*="Distributed Quantity"]');
                for (var t of tables) {
                    var parent = t.parentElement;
                    var inDefault = false;
                    while (parent) {
                        if (parent.id === 'stateDefaultDivId') { inDefault = true; break; }
                        parent = parent.parentElement;
                    }
                    if (!inDefault) return true;
                }
                return false;
            """)
            return bool(result)
        except Exception:
            return False

    WebDriverWait(driver, timeout).until(district_loaded)
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

        // Skip tables inside #stateDefaultDivId (national view)
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
# NAVIGATE TO SPECIFIC DISTRICT
# ==============================

def navigate_to_district(state_code, district_code, district_name):
    """
    Navigate fresh to a district page:
    1. Load home
    2. Set month/year
    3. Call stateData(state_code) to get to state view
    4. Call stateData(district_code) to get to district view
    5. Wait for district page to be confirmed loaded
    """
    open_home()
    change_month(TARGET_YEAR, TARGET_MONTH)

    # Navigate to state first
    wait_for_js_function("stateData")
    safe_js(f"stateData('{state_code}')")
    WebDriverWait(driver, 15).until(
        lambda d: len(d.find_elements(By.CSS_SELECTOR, ".blink_icon_img")) > 0
    )
    time.sleep(1)

    # Navigate to district
    wait_for_js_function("stateData")
    safe_js(f"stateData('{district_code}')")

    # Wait for district page to confirm load
    wait_for_district_page(district_code)


# ==============================
# MAIN
# ==============================

try:
    # ---- collect state list ----
    open_home()
    change_month(TARGET_YEAR, TARGET_MONTH)

    btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "a.textInfo")))
    btn.click()
    wait.until(EC.visibility_of_element_located((By.ID, "myModal11")))

    states = []
    for l in driver.find_elements(By.CSS_SELECTOR, "#myModal11 a"):
        onclick = l.get_attribute("onclick")
        match = re.search(r"stateData\('(\d+)'\)", str(onclick))
        if match:
            states.append({"name": l.text.strip(), "code": match.group(1)})

    # Close modal before navigating
    driver.execute_script(
        "var m = document.getElementById('myModal11'); if(m) m.style.display='none';"
    )

    print(f"States found: {len(states)}")

    for state in states[START_STATE:END_STATE]:
        print(f"\nSTATE: {state['name']}")

        # Navigate to state to collect district list
        navigate_to_state(state["code"])
        districts = get_districts_for_state(state["code"])

        # dedupe
        seen = set()
        districts = [d for d in districts if not (d["code"] in seen or seen.add(d["code"]))]
        print(f"Districts: {len(districts)}")

        for d in districts:
            try:
                print(f"  -> {d['name']} (code: {d['code']})")

                # Fresh navigation for every district
                navigate_to_district(state["code"], d["code"], d["name"])

                commodities = parse_table()
                print(f"    Columns: {list(commodities.keys())}")
                print(f"    Values:  {commodities}")

                if not commodities:
                    print(f"  [SKIP] No commodity data for {d['name']}")
                    continue

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
                print(f"  District error ({d['name']}): {e}")
                import traceback
                traceback.print_exc()

except Exception:
    import traceback
    traceback.print_exc()

finally:
    print(f"\nDONE → {CSV_FILE}")
    driver.quit()