import re
import time
import os
import sqlite3
import json
import calendar

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
parser.add_argument(
    "--resume-state",
    type=int,
    default=None,
    help="Index into the states list to resume from (overrides --start for this run's starting point).",
)
args = parser.parse_args()

TARGET_YEAR = args.year
TARGET_MONTH = args.month
START_STATE = args.start
END_STATE = args.end if args.end != 99 else None
SCRAPE_MODE = args.mode  # "all" or "one"

BASE_URL = "https://impds.nic.in/sale/"
DB_FILE = f"{TARGET_YEAR}_{TARGET_MONTH}_{START_STATE}_{END_STATE}_{SCRAPE_MODE}.db"
PROGRESS_FILE = f"{TARGET_YEAR}_{TARGET_MONTH}_{START_STATE}_{END_STATE}_{SCRAPE_MODE}.progress.json"

FIXED_FIELDS = ["state", "district", "district_code", "date"]
KEY_FIELDS = ["state", "district_code", "date"]

# Retry tuning
MAX_DISTRICT_RETRIES = 5      # district-level retries (re-click + re-wait + re-parse) PER browser session
MAX_STATE_RETRIES = 3         # state-level retries (re-open state, re-collect districts) PER browser session

# Per-scope restart budgets. Each state gets its own fresh budget for
# collect_districts_for_state, and each district gets its own fresh budget
# inside scrape_district. This way one bad state burning many restarts
# doesn't starve a later, otherwise-fine state of its own restarts.
MAX_BROWSER_RESTARTS_PER_STATE = 8
MAX_BROWSER_RESTARTS_PER_DISTRICT = 5

TABLE_POLL_TIMEOUT = 20       # seconds to wait for a *verified* table per attempt
TABLE_POLL_INTERVAL = 0.4     # seconds between verification polls
MODAL_VERIFY_TIMEOUT = 8      # seconds to confirm #myModal11 actually appeared
MONTH_VERIFY_TIMEOUT = 8      # seconds to confirm a.done text reflects the selected month/year

DISTRICTS_BEFORE_PROACTIVE_RESTART = 60  # periodic restart to dodge memory creep

# Global counter kept ONLY for logging/visibility — it is no longer used as
# a hard stop anywhere. Per-scope budgets above are what actually bound
# retry behavior, per your requirement that any failing step should
# relaunch Chrome and resume from where it stopped, regardless of how many
# restarts have happened elsewhere in the run.
browser_restart_count = 0


# ==============================
# DRIVER (re-creatable)
# ==============================

def build_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--start-maximized")
    options.add_argument("--incognito")
    d = webdriver.Chrome(options=options)
    w = WebDriverWait(d, 20)
    return d, w


driver, wait = build_driver()


def restart_browser(reason="periodic", _depth=0):
    """
    Quit the current driver and launch a brand new one in incognito mode,
    then re-navigate home and re-select TARGET_YEAR/TARGET_MONTH, verifying
    the selection actually stuck via the 'a.done' label.

    If month verification itself fails post-relaunch (e.g. the relaunch
    landed on another slow/broken page load), this retries the whole
    restart up to 3 times before giving up — consistent with the rest of
    the script's "never just give up after one shot" approach. _depth
    guards against infinite recursion.
    """
    global driver, wait, browser_restart_count
    browser_restart_count += 1
    print(f"  [BROWSER RESTART #{browser_restart_count} total-this-run] reason={reason}")
    try:
        driver.quit()
    except Exception:
        pass
    driver, wait = build_driver()
    open_home()
    try:
        change_month(TARGET_YEAR, TARGET_MONTH)
    except RuntimeError as e:
        if _depth >= 2:
            raise RuntimeError(
                f"restart_browser failed to (re)select month after {_depth + 1} "
                f"full relaunch attempts. Last error: {e}"
            ) from e
        print(f"  [RESTART RETRY] month verification failed post-relaunch, "
              f"relaunching again (depth {_depth + 1}/2) — {e}")
        restart_browser(reason=f"{reason} (retry after month-verify failure)", _depth=_depth + 1)


# ==============================
# PROGRESS TRACKING (so a restart resumes, not starts over)
# ==============================

def save_progress(state_index, state_name, district_index=None, district_name=None):
    try:
        payload = {"state_index": state_index, "state_name": state_name}
        if district_index is not None:
            payload["district_index"] = district_index
            payload["district_name"] = district_name
        with open(PROGRESS_FILE, "w") as f:
            json.dump(payload, f)
    except Exception:
        pass


def load_progress():
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE) as f:
                return json.load(f)
        except Exception:
            return None
    return None


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
    # Let page JS/listeners finish binding before interacting. Cold loads
    # of the homepage were the most common trigger for blank
    # TimeoutExceptions in open_states_modal() immediately afterwards.
    time.sleep(1.5)


def _expected_month_label(year, month):
    """e.g. year=2023, month=8 -> 'August-2023' (matches a.done's rendered text)."""
    return f"{calendar.month_name[month]}-{year}"


def _read_done_label():
    """
    Read the current text of the calendar 'done' link that shows the
    active month/year, e.g.:
      <a class="done" data-bs-target="#myModal10">August-2023</a>
    There can be MULTIPLE elements with class "done" on this page (one of
    them is an unrelated nav link, observed to read "DASHBOARD") so we
    must scope the selector to the one tied to the calendar modal
    (#myModal10) specifically, not just `a.done`.
    Returns the stripped text, or "" if the element can't be found/read.
    """
    try:
        el = driver.find_element(By.CSS_SELECTOR, 'a.done[data-bs-target="#myModal10"]')
        return el.text.strip()
    except Exception:
        # Fallback: some renders may use data-target instead of data-bs-target
        # depending on the Bootstrap version loaded.
        try:
            el = driver.find_element(By.CSS_SELECTOR, 'a.done[data-target="#myModal10"]')
            return el.text.strip()
        except Exception:
            return ""


def _verify_month_selected(year, month, timeout=MONTH_VERIFY_TIMEOUT):
    """
    Poll a.done's text until it contains the expected 'Month-Year' label,
    confirming the calendar selection actually took effect on the page
    (not just that the clicks were dispatched). Raises TimeoutError if it
    never matches in time.
    """
    expected = _expected_month_label(year, month)

    def label_matches(d):
        label = _read_done_label()
        return expected.lower() in label.lower()

    try:
        WebDriverWait(driver, timeout).until(label_matches)
    except Exception as e:
        actual = _read_done_label()
        raise TimeoutError(
            f"a.done label never matched '{expected}' within {timeout}s "
            f"(last seen: '{actual}')"
        ) from e


def _change_month_once(year, month):
    """One attempt at selecting the month/year and verifying it stuck."""
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
    time.sleep(1)
    _verify_month_selected(year, month)


def change_month(year, month, max_attempts=3):
    """
    Open the calendar modal, select the given year/month, and VERIFY via
    the 'a.done' label (e.g. 'August-2023') that the selection actually
    rendered on the page before returning. Retries the click sequence
    in-session up to max_attempts times if verification fails — a flaky
    click or a slow-to-update label shouldn't immediately require a full
    browser restart. Raises RuntimeError if it still hasn't stuck after
    all in-session attempts (caller — restart_browser/main — treats that
    as a hard failure the same as any other unrecoverable navigation step).
    """
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            _change_month_once(year, month)
            print(f"  [MONTH OK] Confirmed '{_expected_month_label(year, month)}' selected")
            return
        except Exception as e:
            last_exc = e
            print(f"  [MONTH RETRY {attempt}/{max_attempts}] {type(e).__name__}: {e}")
            time.sleep(1.5 * attempt)

    raise RuntimeError(
        f"Failed to select/verify month '{_expected_month_label(year, month)}' "
        f"after {max_attempts} attempts. Last error: {last_exc}"
    )


def open_states_modal():
    """
    Click the '33 states' link (liveStatesdata()) and VERIFY the modal
    actually became visible. Each wait is isolated with its own try/except
    so failures are diagnosable instead of collapsing into a single blank
    TimeoutException — you can now tell whether the trigger link never
    became clickable, or the modal link was clicked but never opened.
    """
    try:
        btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "a.textInfo")))
        btn.click()
    except Exception as e:
        raise RuntimeError(f"a.textInfo trigger not clickable: {type(e).__name__}") from e

    try:
        WebDriverWait(driver, MODAL_VERIFY_TIMEOUT).until(
            EC.visibility_of_element_located((By.ID, "myModal11"))
        )
    except Exception as e:
        raise RuntimeError(f"myModal11 never became visible after click: {type(e).__name__}") from e


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


def go_back(state_code, from_timeout=15):
    """
    Click the 'Go back' (fa-hand-o-left) button, which calls backData(state_code).
    This single button is used BOTH to go from district -> state view AND from
    state -> home view, always keyed on the current state's code.
    Returns True if the back-navigation is confirmed, False otherwise
    (caller decides whether that warrants a harder recovery step).
    """
    try:
        safe_js(f"backData('{state_code}')")
        wait_for_state_page(timeout=from_timeout)
        return True
    except Exception as e:
        print(f"  [BACK FAILED] backData('{state_code}') — {type(e).__name__}: {e}")
        return False


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
# or raises after exhausting retries AND exhausting browser restarts.
# ==============================

def _scrape_district_attempts(d, state):
    """
    One full pass of MAX_DISTRICT_RETRIES attempts at a single district,
    using the CURRENT browser session. Does not restart the browser itself.
    Returns silently on verified success. Raises RuntimeError if every
    attempt in this pass fails (caller decides whether to restart and
    try another pass, or give up on the district).
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
            # Navigate back to state view before the next attempt. If that
            # also fails, the page is corrupted beyond what an in-session
            # retry can fix — bail out of this pass immediately so the
            # caller can restart the browser rather than burning remaining
            # attempts against a broken DOM.
            if not go_back(state["code"]):
                raise RuntimeError(
                    f"backData failed mid-retry for '{d['name']}' "
                    f"(attempt {attempt}/{MAX_DISTRICT_RETRIES})"
                ) from e
            time.sleep(1)

    raise RuntimeError(
        f"District '{d['name']}' (code: {d['code']}) failed after "
        f"{MAX_DISTRICT_RETRIES} attempts. Last error: {last_exc}"
    )


def scrape_district(d, state):
    """
    Scrape a single district, with full incognito-browser-restart recovery.

    Behavior:
      1. Try a full pass of MAX_DISTRICT_RETRIES attempts in the current
         browser session (_scrape_district_attempts).
      2. If that pass fails for ANY reason (table never loaded, backData
         broke, etc.) — relaunch Chrome fresh in incognito mode, navigate
         back to this exact state's district list, and try the SAME
         district again from where it stopped. Do not skip ahead.
      3. Repeat up to MAX_BROWSER_RESTARTS_PER_DISTRICT times for this
         district specifically (each district gets its own fresh budget).
      4. Only if every pass across every restart fails does this raise
         RuntimeError, at which point the caller logs it as a genuine
         [FAILED] and moves on to the next district.

    Returns silently on verified success (matches previous contract).
    """
    restarts_used = 0
    last_exc = None

    while True:
        try:
            _scrape_district_attempts(d, state)
            return  # verified success
        except RuntimeError as e:
            last_exc = e
            restarts_used += 1
            print(f"  [DISTRICT RETRIES EXHAUSTED] {d['name']} — {e}")

            if restarts_used > MAX_BROWSER_RESTARTS_PER_DISTRICT:
                break  # give up on this district entirely

            print(f"  [RELAUNCH] Restarting Chrome (incognito) to retry "
                  f"'{d['name']}' — restart {restarts_used}/{MAX_BROWSER_RESTARTS_PER_DISTRICT}")
            restart_browser(reason=f"district retries exhausted ({d['name']})")

            # After a restart we're back at the home/month-select page, not
            # this state's district list — re-enter the state before
            # retrying the same district, so we "pick up where it stopped"
            # rather than skipping to the next district.
            try:
                collect_districts_for_state(state)
            except RuntimeError as state_exc:
                # Couldn't even get back into the state after restarting —
                # no point hammering this district further.
                last_exc = state_exc
                print(f"  [STATE RE-ENTRY FAILED] {state['name']} — {state_exc}")
                break
            # Loop continues: try the same district `d` again, fresh.

    raise RuntimeError(
        f"District '{d['name']}' (code: {d['code']}) failed even after "
        f"{restarts_used - 1} browser restart(s). Last error: {last_exc}"
    )


# ==============================
# STATE-LEVEL COLLECTION (isolated so one bad state doesn't kill the run)
# ==============================

def collect_districts_for_state(state):
    """
    Navigate to a state's district list and return the list of districts.

    Retries MAX_STATE_RETRIES times per browser session. If a full
    in-session pass is exhausted (whatever the reason — modal not opening,
    state link not found, districts not rendering, etc.) this RESTARTS
    Chrome fresh in incognito mode and tries another full pass, up to
    MAX_BROWSER_RESTARTS_PER_STATE restarts, before finally giving up.

    This matches the same "retry, and if retries are exhausted relaunch
    Chrome and resume from where it stopped" contract used everywhere else
    in this script. Previously this function gave up after a single
    3-attempt pass with no restart, which is what caused full states to be
    skipped entirely even though the site was just slow to load.
    """
    restarts_used = 0
    last_exc = None

    while True:
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

                return districts  # verified success

            except Exception as e:
                last_exc = e
                print(f"  [STATE RETRY {attempt}/{MAX_STATE_RETRIES}] {state['name']} — {type(e).__name__}: {e}")
                time.sleep(2 * attempt)  # gentle backoff: 2s, 4s, 6s

        # Full in-session pass exhausted — this is the key fix: relaunch
        # Chrome and try another full pass instead of raising immediately.
        restarts_used += 1
        if restarts_used > MAX_BROWSER_RESTARTS_PER_STATE:
            break

        print(f"  [RELAUNCH] Restarting Chrome (incognito) to retry state "
              f"'{state['name']}' — restart {restarts_used}/{MAX_BROWSER_RESTARTS_PER_STATE}")
        restart_browser(reason=f"state retries exhausted ({state['name']})")
        # Loop continues: fresh MAX_STATE_RETRIES attempts in the new session.

    raise RuntimeError(
        f"State '{state['name']}' (code: {state['code']}) failed to load districts "
        f"even after {restarts_used - 1} browser restart(s). Last error: {last_exc}"
    )


# ==============================
# PER-STATE DISTRICT SCRAPING — mode aware ("all" vs "one")
# ==============================

def scrape_districts_for_state(districts, state, mode):
    """
    Drive district scraping for a single state according to `mode`.

    `scrape_district` now owns its own full recovery loop internally:
    it retries MAX_DISTRICT_RETRIES times, and if that's exhausted it
    relaunches Chrome in incognito, re-enters this exact state, and
    retries the SAME district again — up to MAX_BROWSER_RESTARTS_PER_DISTRICT
    times — before ever raising. So by the time scrape_district raises here,
    the district has been genuinely exhausted and is logged + skipped.

    This function also tracks a running count of districts scraped since
    the last browser restart, and proactively restarts (fresh incognito
    session) after DISTRICTS_BEFORE_PROACTIVE_RESTART districts to dodge
    the slow memory/DOM creep that caused cascading timeouts late in a run.
    A proactive restart re-enters the state and resumes from the NEXT
    district in the list (the current one already succeeded or was
    genuinely exhausted before we got here).

    If re-entering the state after ANY restart in this function fails,
    that failure is itself retried with restarts (via collect_districts_for_state's
    own internal restart loop) rather than silently dropping the rest of
    the district list for this state.
    """
    global districts_since_restart

    got_one = False
    i = 0
    while i < len(districts):
        d = districts[i]
        save_progress(STATE_INDEX_FOR_PROGRESS[0], state["name"], i, d["name"])
        try:
            scrape_district(d, state)
            got_one = True
        except RuntimeError as e:
            # Genuinely exhausted: retries AND restarts both ran out.
            print(f"  [FAILED] {d['name']} — exhausted retries and restarts — {e}")

        districts_since_restart += 1

        # Always try to land back on the state's district view before
        # moving to the next district. If even this fails, restart and
        # re-enter — collect_districts_for_state already retries+restarts
        # internally, so this is the same "never just give up" contract.
        if not go_back(state["code"]):
            print(f"  [BACK FAILED post-district] {state['name']} — restarting and re-entering state")
            restart_browser(reason="backData failed after district loop")
            try:
                districts = collect_districts_for_state(state)
            except RuntimeError as e:
                print(f"  [STATE RE-ENTRY FAILED, aborting rest of state] {state['name']} — {e}")
                return

        if districts_since_restart >= DISTRICTS_BEFORE_PROACTIVE_RESTART:
            districts_since_restart = 0
            restart_browser(reason="proactive periodic restart")
            try:
                districts = collect_districts_for_state(state)  # refresh district list/session
            except RuntimeError as e:
                print(f"  [STATE RE-ENTRY FAILED after proactive restart, aborting rest of state] "
                      f"{state['name']} — {e}")
                return
            # Resume from the NEXT district — the one we just finished
            # (success or genuine failure) is already accounted for.
            i += 1
            continue

        if mode == "one" and got_one:
            break  # we have our one verified district for this state

        i += 1

    if mode == "one" and not got_one:
        print(f"  [STATE FAILED] {state['name']} — no district could be scraped "
              f"(tried all {len(districts)} districts)")


districts_since_restart = 0
# Mutable single-element holder so scrape_districts_for_state can read the
# current state index for progress logging without needing a global rebind.
STATE_INDEX_FOR_PROGRESS = [0]


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

    # Resume support: if a --resume-state index is given, start there instead.
    effective_start = args.resume_state if args.resume_state is not None else START_STATE

    state_index = effective_start
    state_list = states[effective_start:END_STATE]

    for offset, state in enumerate(state_list):
        state_index = effective_start + offset
        STATE_INDEX_FOR_PROGRESS[0] = state_index
        print(f"\n{'='*50}\n[STATE] {state['name']} (index {state_index})")
        save_progress(state_index, state['name'])

        try:
            districts = collect_districts_for_state(state)
        except RuntimeError as e:
            print(f"  [STATE FAILED] {state['name']} — {e}")
            continue

        print(f"[INFO] {len(districts)} districts found")

        # scrape_district (called within) owns all retry/restart recovery
        # internally now, so this never raises mid-state — it always runs
        # to completion for this state's district list.
        scrape_districts_for_state(districts, state, SCRAPE_MODE)

except Exception:
    import traceback
    traceback.print_exc()

finally:
    print(f"\n{'='*50}\n[DONE] Data saved to {DB_FILE}")
    try:
        driver.quit()
    except Exception:
        pass