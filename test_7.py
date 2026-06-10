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

DEBUG_DIR = "debug"
os.makedirs(DEBUG_DIR, exist_ok=True)

FIXED_FIELDS = ["state", "district", "district_code", "date"]
KEY_FIELDS = ["state", "district_code", "date"]

# ==============================
# DRIVER
# ==============================

options = webdriver.ChromeOptions()
options.add_argument("--headless=new")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")

driver = webdriver.Chrome(options=options)
wait = WebDriverWait(driver, 20)

# ==============================
# DEBUG HELPERS
# ==============================

def save_debug(name):
    ts = int(time.time())
    png = f"{DEBUG_DIR}/{name}_{ts}.png"
    html = f"{DEBUG_DIR}/{name}_{ts}.html"

    driver.save_screenshot(png)
    with open(html, "w", encoding="utf-8") as f:
        f.write(driver.page_source)

    print(f"[DEBUG] Saved: {png}, {html}")

# ==============================
# CSV FUNCTIONS
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
    wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "a.textInfo"))).click()
    wait.until(EC.visibility_of_element_located((By.ID, "myModal11")))

# ==============================
# WAIT SYSTEM
# ==============================

def wait_for_districts():
    WebDriverWait(driver, 20).until(
        lambda d: d.execute_script("""
            return document.querySelectorAll('a[onclick*="DistrictData"]').length;
        """) > 0
    )

def wait_for_district(name):
    target = name.upper().strip()
    WebDriverWait(driver, 15).until(
        lambda d: d.execute_script("""
            var els = document.querySelectorAll('[key="district"]');
            for (var e of els) {
                var t = e.innerText.replace(/\\s+/g,' ').trim().toUpperCase();
                if (t.includes(arguments[0])) return true;
            }
            return false;
        """, target)
    )

def load_state_with_retry(code, retries=3):
    for i in range(retries):
        print(f"[Retry {i+1}] Loading state {code}")
        driver.execute_script(f"stateData('{code}')")
        try:
            wait_for_districts()
            return True
        except:
            save_debug(f"state_fail_{code}")
            time.sleep(2)
    return False

# ==============================
# JS HELPERS
# ==============================

GET_STATES_JS = """
var result = [];
document.querySelectorAll('#myModal11 a').forEach(a => {
    var m = a.getAttribute("onclick")?.match(/stateData\\('(\\d+)'\\)/);
    if (m) result.push({name: a.innerText.trim(), code: m[1]});
});
return result;
"""

GET_DISTRICTS_JS = """
var result = [];
document.querySelectorAll('a[onclick*="DistrictData"]').forEach(a => {
    var m = a.getAttribute("onclick").match(/DistrictData\\('(\\d+)'\\)/);
    if (m) {
        result.push({
            name: a.getAttribute("aria-label"),
            code: m[1]
        });
    }
});
return result;
"""

PARSE_TABLE_JS = """
var result = [];
var t = document.querySelector('table[aria-label*="Distributed Quantity"]');
if (!t) return [];

t.querySelectorAll('tr').forEach(r => {
    var td = r.querySelectorAll('td');
    if (td.length < 5) return;

    var name = td[0].innerText.trim();
    if (!name || name.toLowerCase() === 'total') return;

    var val = td[4].innerText.trim();
    var isSub = r.className.includes('customRow');

    result.push({name, val, isSub});
});
return result;
"""

# ==============================
# MAIN
# ==============================

try:
    open_home()
    change_month(TARGET_YEAR, TARGET_MONTH)
    open_states_modal()

    states = driver.execute_script(GET_STATES_JS)
    print(f"States found: {len(states)}")

    for state in states[START_STATE:END_STATE]:

        print(f"\nSTATE: {state['name']}")

        open_home()
        open_states_modal()
        time.sleep(1)

        if not load_state_with_retry(state["code"]):
            print("❌ Failed to load state")
            continue

        districts = driver.execute_script(GET_DISTRICTS_JS)
        print(f"Districts: {len(districts)}")

        if len(districts) == 0:
            save_debug("no_districts")
            continue

        for d in districts:
            try:
                print(f"  -> {d['name']}")

                driver.execute_script(f"DistrictData('{d['code']}')")
                wait_for_district(d["name"])

                rows = driver.execute_script(PARSE_TABLE_JS)

                if not rows:
                    save_debug(f"no_table_{d['code']}")

                commodities = {}
                for r in rows:
                    col = commodity_to_col(r["name"], r["isSub"])
                    commodities[col] = r["val"]

                row = {
                    "state": state["name"],
                    "district": d["name"],
                    "district_code": d["code"],
                    "date": f"{TARGET_YEAR}-{TARGET_MONTH:02d}",
                    **commodities
                }

                upsert_row(row)

                load_state_with_retry(state["code"])

            except Exception as e:
                print(f"❌ ERROR: {d['name']} -> {e}")
                save_debug(f"district_fail_{d['code']}")
                load_state_with_retry(state["code"])

finally:
    print(f"\nDONE → {CSV_FILE}")
    driver.quit()