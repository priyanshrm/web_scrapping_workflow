import re
import time
import csv
import os

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

TARGET_YEAR = 2026
TARGET_MONTH = 4
START_STATE = 0
END_STATE = None

CSV_FILE = f"{TARGET_YEAR}_{TARGET_MONTH}_district_data.csv"

# Fixed columns that always appear first
FIXED_FIELDS = ["state", "district", "district_code", "date"]

# =========================================
# CSV HELPERS — dynamic column support
# =========================================

def commodity_to_col(commodity_name: str, is_sub_row: bool) -> str:
    """
    Convert a commodity display name to a CSV column key.
    Sub-rows get a leading dash: -barley, -bajra, etc.
    Parent rows: wheat, rice, coarse_grains, etc.
    """
    col = commodity_name.strip().lower().replace(" ", "_")
    if is_sub_row:
        col = f"-{col}"
    return col


def get_current_fieldnames() -> list:
    """Read the header row from the CSV to get current column list."""
    if not os.path.exists(CSV_FILE):
        return list(FIXED_FIELDS)
    with open(CSV_FILE, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
            return header
        except StopIteration:
            return list(FIXED_FIELDS)


def ensure_columns(new_cols: list):
    """
    If any column in new_cols doesn't exist in the CSV yet,
    rewrite the entire CSV with the new columns appended (existing
    rows get empty string for the new columns).
    """
    current_fields = get_current_fieldnames()
    added = [c for c in new_cols if c not in current_fields]
    if not added:
        return  # nothing to do

    updated_fields = current_fields + added

    # Read all existing rows
    existing_rows = []
    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                existing_rows.append(row)

    # Rewrite with expanded header
    # restval="" fills any missing column with empty string for old rows
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=updated_fields, extrasaction="ignore", restval="")
        writer.writeheader()
        for row in existing_rows:
            writer.writerow(row)

    print(f"  [CSV] Added new column(s): {added}")


def append_row(row: dict):
    """
    Append a row to the CSV. Automatically expands columns if the row
    contains keys not yet in the header.
    """
    # Ensure any new commodity columns exist before writing
    ensure_columns(list(row.keys()))

    fieldnames = get_current_fieldnames()
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        # restval="" fills any commodity column absent for this district with empty string
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore", restval="")
        writer.writerow(row)


# Initialise CSV with fixed fields if it doesn't exist yet
if not os.path.exists(CSV_FILE):
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIXED_FIELDS)
        writer.writeheader()

# =========================================
# TABLE PARSER
# =========================================

def parse_distributed_qty_table(driver) -> dict:
    """
    Locate the 'Distributed Quantity(In MT)' table and extract
    Commodity → Total value for every row (thead/tbody/tfoot).

    Sub-rows are identified by the 'customRow' CSS class on the <tr>.
    The tfoot 'Total' row is skipped (it's the grand total, not a commodity).

    Returns a dict like:
        { "wheat": "42.866", "rice": "291.457", "-barley": "0", ... }
    """
    commodity_data = {}

    tables = driver.find_elements(By.TAG_NAME, "table")
    target_table = None

    for table in tables:
        try:
            # Identify the right table by its aria-label or a header cell
            aria = table.get_attribute("aria-label") or ""
            if "Distributed Quantity" in aria:
                target_table = table
                break
            # Fallback: look for the header text inside the table
            headers = table.find_elements(By.TAG_NAME, "th")
            header_texts = [h.text.strip() for h in headers]
            if "Commodity" in header_texts and "Total" in header_texts:
                target_table = table
                break
        except Exception:
            pass

    if not target_table:
        print("  [WARN] Could not find Distributed Quantity table")
        return commodity_data

    rows = target_table.find_elements(By.TAG_NAME, "tr")

    for row in rows:
        try:
            cells = row.find_elements(By.TAG_NAME, "td")
            if len(cells) < 5:
                continue  # header or malformed row

            commodity_raw = cells[0].text.strip()

            # Skip the grand-total footer row
            if commodity_raw.lower() == "total":
                continue

            # Total value is always the 5th cell (index 4)
            total_val = cells[4].text.strip()

            # Detect sub-row via CSS class on the <tr>
            row_classes = row.get_attribute("class") or ""
            is_sub = "customRow" in row_classes

            col_name = commodity_to_col(commodity_raw, is_sub)
            commodity_data[col_name] = total_val

        except Exception as row_err:
            print(f"  [WARN] Row parse error: {row_err}")
            continue

    return commodity_data


# =========================================
# DRIVER
# =========================================

driver = webdriver.Chrome()
driver.maximize_window()
wait = WebDriverWait(driver, 20)


def change_month(year, month_num):
    calendar_btn = wait.until(
        EC.element_to_be_clickable((By.ID, "calModal"))
    )
    driver.execute_script("arguments[0].click();", calendar_btn)
    time.sleep(1)

    year_dropdown = wait.until(
        EC.presence_of_element_located((By.ID, "selectedyear"))
    )
    driver.execute_script("arguments[0].value = arguments[1]", year_dropdown, str(year))
    driver.execute_script("arguments[0].dispatchEvent(new Event('change'))", year_dropdown)
    time.sleep(1)

    old_page = driver.find_element(By.TAG_NAME, "body").text
    months = driver.find_elements(By.CSS_SELECTOR, ".cal_month a")
    driver.execute_script("arguments[0].click();", months[month_num - 1])
    wait.until(lambda d: d.find_element(By.TAG_NAME, "body").text != old_page)
    time.sleep(3)


try:

    # =====================================
    # OPEN SITE
    # =====================================

    driver.get("https://impds.nic.in/sale/")
    change_month(TARGET_YEAR, TARGET_MONTH)

    # =====================================
    # CLICK STATES BUTTON + WAIT FOR MODAL
    # =====================================

    states_btn = wait.until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "a.textInfo"))
    )
    driver.execute_script("arguments[0].click();", states_btn)
    wait.until(EC.visibility_of_element_located((By.ID, "myModal11")))

    # =====================================
    # GET STATES
    # =====================================

    states = driver.find_elements(By.CSS_SELECTOR, "#myModal11 a")
    print(f"Found {len(states)} states")

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

    print(f"Parsed {len(state_data)} states")

    # =====================================
    # STATES LOOP
    # =====================================

    for state in state_data[START_STATE:END_STATE]:

        state_name = state["name"]
        state_code = state["code"]

        print(f"\n{'#' * 60}")
        print(f"STATE: {state_name} | CODE: {state_code}")
        print(f"{'#' * 60}")

        try:

            # always start fresh from home page
            driver.get("https://impds.nic.in/sale/")
            wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "a.textInfo")))
            time.sleep(2)

            # open modal + force show
            states_btn = driver.find_element(By.CSS_SELECTOR, "a.textInfo")
            driver.execute_script("arguments[0].click();", states_btn)
            driver.execute_script("""
                var modal = document.getElementById('myModal11');
                modal.style.display = 'block';
                modal.classList.add('show');
            """)
            time.sleep(1)

            # find target state link
            state_links = driver.find_elements(By.CSS_SELECTOR, "#myModal11 a")
            target_state = None
            for link in state_links:
                try:
                    onclick = link.get_attribute("onclick")
                    if onclick and f"'{state_code}'" in onclick:
                        target_state = link
                        break
                except:
                    pass

            if not target_state:
                print(f"Could not find link for {state_name}, skipping")
                continue

            driver.execute_script("arguments[0].click();", target_state)
            time.sleep(2)

            driver.execute_script(f"liveDistrictdata('{state_code}')")
            time.sleep(3)

            # =====================================
            # GET DISTRICTS
            # =====================================

            all_links = driver.find_elements(By.TAG_NAME, "a")
            districts = []

            for link in all_links:
                try:
                    onclick = link.get_attribute("onclick")
                    if onclick and "stateData(" in onclick:
                        match = re.search(r"stateData\('(\d+)'\)", onclick)
                        if match:
                            district_code = match.group(1)
                            imgs = link.find_elements(By.TAG_NAME, "img")
                            if imgs:
                                img = imgs[0]
                                if img.get_attribute("width") == "12":
                                    district_name = img.get_attribute("aria-label").strip()
                                    districts.append({
                                        "name": district_name,
                                        "code": district_code
                                    })
                except:
                    pass

            seen = set()
            unique_districts = []
            for d in districts:
                if d["code"] not in seen:
                    seen.add(d["code"])
                    unique_districts.append(d)
            districts = unique_districts

            print(f"Total districts: {len(districts)}")

            # =====================================
            # DISTRICTS LOOP
            # =====================================

            for district in districts:

                try:

                    print("\n" + "=" * 60)
                    district_name = district["name"]
                    district_code = district["code"]
                    print(f"District: {district_name} | Code: {district_code}")

                    driver.execute_script(f"stateData('{district_code}')")
                    time.sleep(4)

                    # Parse all commodities from the table
                    commodity_data = parse_distributed_qty_table(driver)
                    print(f"  Commodities scraped: {list(commodity_data.keys())}")

                    # Build the full row: fixed fields + all commodity columns
                    row = {
                        "state": state_name,
                        "district": district_name,
                        "district_code": district_code,
                        "date": f"{TARGET_YEAR}-{TARGET_MONTH:02d}",
                        **commodity_data
                    }

                    append_row(row)

                    print("Going back...")
                    driver.execute_script(f"backData('{state_code}')")
                    time.sleep(3)

                except Exception as district_error:
                    print(f"\nDistrict Error ({district_name}): {district_error}")
                    try:
                        driver.execute_script(f"backData('{state_code}')")
                        time.sleep(3)
                    except:
                        pass

        except Exception as state_error:
            import traceback
            print(f"\nState Error ({state_name}):")
            traceback.print_exc()


except Exception as e:
    import traceback
    print(f"\nMain Error:")
    traceback.print_exc()


finally:
    print(f"\nDONE — data saved to {CSV_FILE}")
    driver.quit()