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
# options = webdriver.ChromeOptions()
# options.add_argument("--disable-blink-features=AutomationControlled")
# options.add_argument("--start-maximized")

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

    months = wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, ".cal_month a")))
    months[month - 1].click()

    time.sleep(2)


def open_states_modal():
    btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "a.textInfo")))
    btn.click()
    wait.until(EC.visibility_of_element_located((By.ID, "myModal11")))


# ==============================
# TABLE PARSER
# ==============================

def parse_table():
    data = {}

    tables = driver.find_elements(By.TAG_NAME, "table")

    for table in tables:
        try:
            headers = [h.text.strip() for h in table.find_elements(By.TAG_NAME, "th")]
            if "Commodity" not in headers:
                continue

            rows = table.find_elements(By.TAG_NAME, "tr")

            for row in rows:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) < 5:
                    continue

                name = cells[0].text.strip()
                if name.lower() == "total":
                    continue

                val = cells[4].text.strip()
                is_sub = "customRow" in (row.get_attribute("class") or "")

                col = commodity_to_col(name, is_sub)
                data[col] = val

            return data

        except:
            continue

    return data


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

        for d in districts:
            try:
                print(f"  -> {d['name']}")

                wait_for_js_function("stateData")
                safe_js(f"stateData('{d['code']}')")

                time.sleep(2)

                commodities = parse_table()

                row = {
                    "state": state["name"],
                    "district": d["name"],
                    "district_code": d["code"],
                    "date": f"{TARGET_YEAR}-{TARGET_MONTH:02d}",
                    **commodities
                }

                upsert_row(row)

                safe_js(f"backData('{state['code']}')")

            except Exception as e:
                print(f"District error: {e}")
                try:
                    safe_js(f"backData('{state['code']}')")
                except:
                    pass

except Exception:
    import traceback
    traceback.print_exc()

finally:
    print(f"\nDONE → {CSV_FILE}")
    driver.quit()