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
# TABLE HELPERS
# ==============================

def wait_for_table(old_first_val=None):
    """
    Wait until the Distributed Quantity table is present with fresh data.
    If old_first_val is given, waits for the table's first data cell to change
    from that value — ensuring stale DOM from previous district is gone.
    """
    def table_is_fresh(d):
        tables = d.find_elements(By.TAG_NAME, "table")
        for t in tables:
            headers = [h.text.strip() for h in t.find_elements(By.TAG_NAME, "th")]
            if "Commodity" not in headers or "Total" not in headers:
                continue
            rows = t.find_elements(By.TAG_NAME, "tr")
            for row in rows:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) >= 5:
                    first_val = cells[4].text.strip()
                    # If we have a stale reference, wait until it changes
                    if old_first_val is not None:
                        return first_val != old_first_val and first_val != ""
                    return first_val != ""
        return False

    WebDriverWait(driver, 15).until(table_is_fresh)
    time.sleep(0.5)  # small buffer for full render


def get_first_table_val():
    """Get the first data cell value from the Distributed Quantity table (used as stale marker)."""
    try:
        for table in driver.find_elements(By.TAG_NAME, "table"):
            headers = [h.text.strip() for h in table.find_elements(By.TAG_NAME, "th")]
            if "Commodity" not in headers or "Total" not in headers:
                continue
            for row in table.find_elements(By.TAG_NAME, "tr"):
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) >= 5:
                    return cells[4].text.strip()
    except Exception:
        pass
    return None


def get_cell_text(cell):
    """Get text from a cell, including hidden elements."""
    text = cell.text.strip()
    if not text:
        # fallback for hidden rows in headless
        text = driver.execute_script("return arguments[0].innerText;", cell).strip()
    return text


# ==============================
# TABLE PARSER
# ==============================

def parse_table() -> dict:
    commodity_data = {}

    for table in driver.find_elements(By.TAG_NAME, "table"):
        try:
            headers = [h.text.strip() for h in table.find_elements(By.TAG_NAME, "th")]
            if "Commodity" not in headers or "Total" not in headers:
                continue

            for row in table.find_elements(By.TAG_NAME, "tr"):
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) < 5:
                    continue

                # Use innerText fallback for hidden rows
                name_raw = get_cell_text(cells[0])

                # Strip button text if it's a menu-toggle parent row
                btn = cells[0].find_elements(By.CLASS_NAME, "menu-toggle")
                if btn:
                    name_raw = driver.execute_script(
                        "return arguments[0].innerText;", btn[0]
                    ).strip()

                if not name_raw or name_raw.lower() == "total":
                    continue

                val = get_cell_text(cells[4])
                row_classes = row.get_attribute("class") or ""
                is_sub = "customRow" in row_classes

                commodity_data[commodity_to_col(name_raw, is_sub)] = val

            return commodity_data

        except Exception as e:
            print(f"Table parse error: {e}")

    print("  [WARN] Could not find Distributed Quantity table")
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

        # districts
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

        old_val = None  # stale marker for first district (no previous table)

        for d in districts:
            try:
                print(f"  -> {d['name']}")

                wait_for_js_function("stateData")
                safe_js(f"stateData('{d['code']}')")

                # Wait for new table to load, ensuring stale table is gone
                wait_for_table(old_first_val=old_val)

                commodities = parse_table()
                print(f"    Columns: {list(commodities.keys())}")

                # Capture current table's first value as stale marker for next iteration
                old_val = get_first_table_val()

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
                print(f"District error: {e}")
                old_val = None  # reset stale marker on error
                try:
                    safe_js(f"backData('{state['code']}')")
                    time.sleep(1)
                except:
                    pass

except Exception:
    import traceback
    traceback.print_exc()

finally:
    print(f"\nDONE → {CSV_FILE}")
    driver.quit()