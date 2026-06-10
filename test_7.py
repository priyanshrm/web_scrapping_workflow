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
# WAIT FOR CORRECT DISTRICT PAGE
# ==============================

def wait_for_district_page(district_name, timeout=15):
    """
    Wait until the page is showing the specific district's data.

    The district page has a breadcrumb element:
        <div class="status m_menu" key="district">SOUTH ANDAMANS</div>

    The national/state page does NOT have this element with the district name.
    This is the reliable signal that the correct district data is loaded.
    """
    district_upper = district_name.strip().upper()

    def correct_district_loaded(d):
        try:
            # Look for the district breadcrumb div that shows the district name.
            # On the district page: <div class="status m_menu" key="district">DISTRICT NAME</div>
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
# TABLE PARSER — pure JS, no Selenium element refs
# ==============================

# Extracts all commodity rows from the Distributed Quantity table via JS.
# Specifically targets the table with aria-label="Distributed Quantity(In MT)"
# to avoid accidentally parsing the national-level table.
PARSE_TABLE_JS = """
    var result = [];
    var tables = document.querySelectorAll('table');
    for (var t of tables) {
        // Identify the right table by aria-label (most reliable)
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

            // menu-toggle parent row (e.g. Coarse Grains)
            var btn = nameCell.querySelector('.menu-toggle');
            if (btn) {
                name = btn.innerText.trim();
            }

            if (!name || name.toLowerCase() === 'total') continue;

            var val = cells[4].innerText.trim();
            var isSub = row.className.indexOf('customRow') !== -1;

            result.push({name: name, val: val, isSub: isSub});
        }
        return result;  // found and parsed the right table
    }
    return result;
"""


def parse_table() -> dict:
    """Extract all commodity data from the district Distributed Quantity table via JS."""
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
        print(f"Districts: {len(districts)}")

        for d in districts:
            try:
                print(f"  -> {d['name']} (code: {d['code']})")

                wait_for_js_function("stateData")
                safe_js(f"stateData('{d['code']}')")

                # Wait until the page breadcrumb confirms this specific district is loaded
                wait_for_district_page(d["name"])

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

                safe_js(f"backData('{state['code']}')")
                time.sleep(1)

            except Exception as e:
                print(f"  District error ({d['name']}): {e}")
                try:
                    safe_js(f"backData('{state['code']}')")
                    time.sleep(1)
                except Exception:
                    pass

except Exception:
    import traceback
    traceback.print_exc()

finally:
    print(f"\nDONE → {CSV_FILE}")
    driver.quit()