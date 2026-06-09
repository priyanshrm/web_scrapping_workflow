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
    if is_sub_row: col = f"-{col}"
    return col

def get_current_fieldnames() -> list:
    if not os.path.exists(CSV_FILE): return list(FIXED_FIELDS)
    with open(CSV_FILE, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        try: return next(reader)
        except StopIteration: return list(FIXED_FIELDS)

def ensure_columns(new_cols: list):
    current_fields = get_current_fieldnames()
    added = [c for c in new_cols if c not in current_fields]
    if not added: return

    updated_fields = current_fields + added
    existing_rows = []
    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader: existing_rows.append(row)

    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=updated_fields, extrasaction="ignore", restval="")
        writer.writeheader()
        for row in existing_rows: writer.writerow(row)

def append_row(row: dict):
    ensure_columns(list(row.keys()))
    fieldnames = get_current_fieldnames()
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore", restval="")
        writer.writerow(row)

if not os.path.exists(CSV_FILE):
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIXED_FIELDS)
        writer.writeheader()


# =========================================
# TABLE PARSER (With Dynamic Wait Integration)
# =========================================
def parse_distributed_qty_table(driver) -> dict:
    commodity_data = {}
    
    # Dynamically wait up to 10 seconds for the table structure to show content
    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, "//table[contains(@aria-label, 'Distributed Quantity') or .//th[text()='Commodity']]"))
        )
    except:
        print("  [WARN] Table presence timeout. Attempting immediate extraction fallback...")

    tables = driver.find_elements(By.TAG_NAME, "table")
    target_table = None

    for table in tables:
        try:
            aria = table.get_attribute("aria-label") or ""
            if "Distributed Quantity" in aria:
                target_table = table
                break
            headers = table.find_elements(By.TAG_NAME, "th")
            header_texts = [h.text.strip() for h in headers]
            if "Commodity" in header_texts and "Total" in header_texts:
                target_table = table
                break
        except:
            pass

    if not target_table:
        print("  [WARN] Could not find Distributed Quantity table")
        return commodity_data

    rows = target_table.find_elements(By.TAG_NAME, "tr")
    for row in rows:
        try:
            cells = row.find_elements(By.TAG_NAME, "td")
            if len(cells) < 5: continue

            commodity_raw = cells[0].text.strip()
            if commodity_raw.lower() == "total" or not commodity_raw: continue

            total_val = cells[4].text.strip()
            row_classes = row.get_attribute("class") or ""
            is_sub = "customRow" in row_classes

            col_name = commodity_to_col(commodity_raw, is_sub)
            commodity_data[col_name] = total_val
        except:
            continue

    return commodity_data


# =========================================
# DRIVER ENGINE SETUP
# =========================================
options = Options()
options.add_argument("--headless=new")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")
options.add_argument("--disable-gpu")
options.add_argument("--window-size=1920,1080")

driver = webdriver.Chrome(options=options)
wait = WebDriverWait(driver, 15)

def change_month(year, month_num):
    calendar_btn = wait.until(EC.element_to_be_clickable((By.ID, "calModal")))
    driver.execute_script("arguments[0].click();", calendar_btn)
    time.sleep(1)

    year_dropdown = wait.until(EC.presence_of_element_located((By.ID, "selectedyear")))
    driver.execute_script("arguments[0].value = arguments[1]", year_dropdown, str(year))
    driver.execute_script("arguments[0].dispatchEvent(new Event('change'))", year_dropdown)
    time.sleep(1)

    old_page = driver.find_element(By.TAG_NAME, "body").text
    months = driver.find_elements(By.CSS_SELECTOR, ".cal_month a")
    driver.execute_script("arguments[0].click();", months[month_num - 1])
    wait.until(lambda d: d.find_element(By.TAG_NAME, "body").text != old_page)
    time.sleep(2)


# =========================================
# EXTRACTION SCRAPER EXECUTOR
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
        try:
            onclick = s.get_attribute("onclick")
            match = re.search(r"stateData\('(\d+)'\)", onclick)
            if match:
                state_data.append({
                    "name": s.get_attribute("innerHTML").strip(),
                    "code": match.group(1)
                })
        except:
            pass

    print(f"Found and Parsed {len(state_data)} states configuration chains.")

    for state in state_data[START_STATE:END_STATE]:
        state_name = state["name"]
        state_code = state["code"]

        print(f"\n{'#' * 60}\nSTATE: {state_name} | CODE: {state_code}\n{'#' * 60}")

        try:
            # Absolute hard reset of DOM states to fix missing javascript environments
            driver.get("https://impds.nic.in/sale/")
            change_month(TARGET_YEAR, TARGET_MONTH)
            
            wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "a.textInfo")))
            states_btn = driver.find_element(By.CSS_SELECTOR, "a.textInfo")
            driver.execute_script("arguments[0].click();", states_btn)
            
            # Explicitly force the modal layer display engine parameters via Injection
            driver.execute_script("""
                var modal = document.getElementById('myModal11');
                if(modal) {
                    modal.style.display = 'block';
                    modal.classList.add('show');
                }
            """)
            time.sleep(1.5)

            state_links = driver.find_elements(By.CSS_SELECTOR, "#myModal11 a")
            target_state = None
            for link in state_links:
                onclick = link.get_attribute("onclick") or ""
                if f"'{state_code}'" in onclick:
                    target_state = link
                    break

            if not target_state:
                print(f"Target UI parameters unresolvable for {state_name}, bypassing state branch.")
                continue

            driver.execute_script("arguments[0].click();", target_state)
            time.sleep(2)

            # Ensure execution occurs only when DOM environment is ready
            wait.until(lambda d: d.execute_script("return typeof liveDistrictdata === 'function'"))
            driver.execute_script(f"liveDistrictdata('{state_code}')")
            time.sleep(2)

            all_links = driver.find_elements(By.TAG_NAME, "a")
            districts = []
            for link in all_links:
                try:
                    onclick = link.get_attribute("onclick") or ""
                    if "stateData(" in onclick:
                        match = re.search(r"stateData\('(\d+)'\)", onclick)
                        if match:
                            d_code = match.group(1)
                            imgs = link.find_elements(By.TAG_NAME, "img")
                            if imgs and imgs[0].get_attribute("width") == "12":
                                districts.append({"name": imgs[0].get_attribute("aria-label").strip(), "code": d_code})
                except:
                    pass

            seen = set()
            districts = [d for d in districts if not (d["code"] in seen or seen.add(d["code"]))]
            print(f"Total structured districts located: {len(districts)}")

            for district in districts:
                district_name = district["name"]
                district_code = district["code"]
                print(f"\nDistrict: {district_name} | Code: {district_code}")

                try:
                    # Sync wait verification check before JavaScript injection executions
                    wait.until(lambda d: d.execute_script("return typeof stateData === 'function'"))
                    driver.execute_script(f"stateData('{district_code}')")
                    
                    commodity_data = parse_distributed_qty_table(driver)
                    print(f"  Commodities parsed successfully: {list(commodity_data.keys())}")

                    if commodity_data and not (len(commodity_data) == 1 and '-' in commodity_data):
                        row = {
                            "state": state_name,
                            "district": district_name,
                            "district_code": district_code,
                            "date": f"{TARGET_YEAR}-{TARGET_MONTH:02d}",
                            **commodity_data
                        }
                        append_row(row)
                    else:
                        print("  [INFO] Extracted data empty or invalid row signature skipped.")

                    # Return safely to state dashboard context frame
                    wait.until(lambda d: d.execute_script("return typeof backData === 'function'"))
                    driver.execute_script(f"backData('{state_code}')")
                    time.sleep(1.5)

                except Exception as district_error:
                    print(f"  [District Loop Exception Block Handler Triggered]: {district_error}")
                    try: 
                        driver.get("https://impds.nic.in/sale/")
                        change_month(TARGET_YEAR, TARGET_MONTH)
                    except: pass

        except Exception as state_error:
            print(f"\n[CRITICAL] Broken State Thread Pipeline context skip on ({state_name}): {state_error}")

except Exception as main_e:
    print(f"\n[FATAL RUNTIME TERMINATION EVENT]: {main_e}")

finally:
    print(f"\nData extraction iteration execution cycles terminated — target reference stream: {CSV_FILE}")
    driver.quit()