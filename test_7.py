import re
import time
import csv
import os

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options

TARGET_YEAR = 2026
TARGET_MONTH = 4
START_STATE = 0
END_STATE = 1

CSV_FILE = f"{TARGET_YEAR}_{TARGET_MONTH}_district_data.csv"
FIXED_FIELDS = ["state", "district", "district_code", "date"]

# =========================================
# CSV HELPERS
# =========================================
def commodity_to_col(commodity_name: str, is_sub_row: bool) -> str:
    col = commodity_name.strip().lower().replace(" ", "_")
    if is_sub_row:
        col = f"-{col}"
    return col

def get_current_fieldnames():
    if not os.path.exists(CSV_FILE):
        return list(FIXED_FIELDS)
    with open(CSV_FILE, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            return next(reader)
        except StopIteration:
            return list(FIXED_FIELDS)

def ensure_columns(new_cols):
    current_fields = get_current_fieldnames()
    added = [c for c in new_cols if c not in current_fields]
    if not added:
        return

    updated_fields = current_fields + added
    existing_rows = []

    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            existing_rows = list(reader)

    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=updated_fields, restval="")
        writer.writeheader()
        for row in existing_rows:
            writer.writerow(row)

    print(f"[CSV] Added columns: {added}")

def append_row(row):
    ensure_columns(list(row.keys()))
    fieldnames = get_current_fieldnames()

    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, restval="")
        writer.writerow(row)

if not os.path.exists(CSV_FILE):
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIXED_FIELDS)
        writer.writeheader()

# =========================================
# FIXED TABLE PARSER
# =========================================
def parse_distributed_qty_table(driver):
    commodity_data = {}

    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, "//table[contains(@aria-label, 'Distributed Quantity')]"))
        )
    except:
        return commodity_data

    tables = driver.find_elements(By.TAG_NAME, "table")
    target_table = None

    for table in tables:
        aria = table.get_attribute("aria-label") or ""
        if "Distributed Quantity" in aria:
            target_table = table
            break

    if not target_table:
        return commodity_data

    rows = target_table.find_elements(By.TAG_NAME, "tr")

    for row in rows:
        try:
            cells = row.find_elements(By.TAG_NAME, "td")
            if len(cells) < 5:
                continue

            commodity_raw = cells[0].text.strip()
            if not commodity_raw or commodity_raw.lower() == "total":
                continue

            total_val = cells[4].text.strip()

            # ✅ ROBUST SUB-ROW DETECTION
            try:
                html = cells[0].get_attribute("innerHTML")
                is_sub = ("&nbsp;" in html) or ("padding-left" in html.lower())
            except:
                is_sub = False

            col_name = commodity_to_col(commodity_raw, is_sub)
            commodity_data[col_name] = total_val

        except:
            continue

    return commodity_data

# =========================================
# DRIVER SETUP (CI SAFE)
# =========================================
options = Options()
options.add_argument("--headless=new")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")
options.add_argument("--disable-gpu")
options.add_argument("--window-size=1920,1080")
options.add_argument("--disable-blink-features=AutomationControlled")

driver = webdriver.Chrome(options=options)
wait = WebDriverWait(driver, 20)

def change_month(year, month_num):
    calendar_btn = wait.until(EC.element_to_be_clickable((By.ID, "calModal")))
    driver.execute_script("arguments[0].click();", calendar_btn)

    year_dropdown = wait.until(EC.presence_of_element_located((By.ID, "selectedyear")))
    driver.execute_script("arguments[0].value = arguments[1]", year_dropdown, str(year))
    driver.execute_script("arguments[0].dispatchEvent(new Event('change'))", year_dropdown)

    old_page = driver.find_element(By.TAG_NAME, "body").text
    months = driver.find_elements(By.CSS_SELECTOR, ".cal_month a")

    driver.execute_script("arguments[0].click();", months[month_num - 1])

    wait.until(lambda d: d.find_element(By.TAG_NAME, "body").text != old_page)

# =========================================
# MAIN LOOP
# =========================================
try:
    driver.get("https://impds.nic.in/sale/")
    change_month(TARGET_YEAR, TARGET_MONTH)

    states_btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "a.textInfo")))
    driver.execute_script("arguments[0].click();", states_btn)

    wait.until(EC.visibility_of_element_located((By.ID, "myModal11")))

    states = driver.find_elements(By.CSS_SELECTOR, "#myModal11 a")

    state_data = []
    for s in states:
        onclick = s.get_attribute("onclick") or ""
        match = re.search(r"stateData\('(\d+)'\)", onclick)
        if match:
            state_data.append({
                "name": s.get_attribute("innerHTML").strip(),
                "code": match.group(1)
            })

    for state in state_data[START_STATE:END_STATE]:
        state_name = state["name"]
        state_code = state["code"]

        print(f"\nSTATE: {state_name}")

        driver.get("https://impds.nic.in/sale/")
        change_month(TARGET_YEAR, TARGET_MONTH)

        states_btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "a.textInfo")))
        driver.execute_script("arguments[0].click();", states_btn)

        driver.execute_script(f"liveDistrictdata('{state_code}')")
        time.sleep(2)

        links = driver.find_elements(By.TAG_NAME, "a")
        districts = []

        for link in links:
            onclick = link.get_attribute("onclick") or ""
            match = re.search(r"stateData\('(\d+)'\)", onclick)
            if match:
                imgs = link.find_elements(By.TAG_NAME, "img")
                if imgs and imgs[0].get_attribute("width") == "12":
                    districts.append({
                        "name": imgs[0].get_attribute("aria-label").strip(),
                        "code": match.group(1)
                    })

        seen = set()
        districts = [d for d in districts if not (d["code"] in seen or seen.add(d["code"]))]

        for d in districts:
            district_name = d["name"]
            district_code = d["code"]

            print(f"District: {district_name}")

            old_text = driver.find_element(By.TAG_NAME, "body").text

            driver.execute_script(f"stateData('{district_code}')")

            wait.until(lambda drv: drv.find_element(By.TAG_NAME, "body").text != old_text)

            commodity_data = parse_distributed_qty_table(driver)

            print("  ->", commodity_data.keys())

            row = {
                "state": state_name,
                "district": district_name,
                "district_code": district_code,
                "date": f"{TARGET_YEAR}-{TARGET_MONTH:02d}",
                **commodity_data
            }

            append_row(row)

            driver.execute_script(f"backData('{state_code}')")
            time.sleep(1)

except Exception as e:
    print("ERROR:", e)

finally:
    driver.quit()
    print("DONE")