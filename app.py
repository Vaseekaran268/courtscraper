import os
import time
import datetime
import base64
import requests
import sqlite3
import streamlit as st
from pathlib import Path
import io
import re

# Import with error handling for optional dependencies
try:
    from bs4 import BeautifulSoup
    BEAUTIFULSOUP_AVAILABLE = True
except ImportError:
    BEAUTIFULSOUP_AVAILABLE = False
    st.error("BeautifulSoup4 is not installed. Please install it with: pip install beautifulsoup4")

try:
    from urllib.parse import urljoin, urlparse
    from dateutil import parser as dateparser
    DATEUTIL_AVAILABLE = True
except ImportError:
    DATEUTIL_AVAILABLE = False
    st.error("python-dateutil is not installed. Please install it with: pip install python-dateutil")

try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False
    st.error("Pandas is not installed. Please install it with: pip install pandas")

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.chrome.options import Options
    from selenium.common.exceptions import TimeoutException, NoSuchElementException
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False
    st.error("Selenium is not installed. Please install it with: pip install selenium")

# ----------------- Configuration -----------------
ECOURTS_URL = "https://services.ecourts.gov.in/ecourtindia_v6/?p=cause_list/index&app_token=999af70e3228e4c73736b14e53143cc8215edf44df7868a06331996cdf179d97#"
DOWNLOAD_DIR = os.path.join(os.getcwd(), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Check if all required dependencies are available
ALL_DEPS_AVAILABLE = all([
    BEAUTIFULSOUP_AVAILABLE,
    DATEUTIL_AVAILABLE,
    PANDAS_AVAILABLE,
    SELENIUM_AVAILABLE
])

# ----------------- Database Setup -----------------
def init_db():
    """Initialize SQLite database for storing PDFs and case data"""
    conn = sqlite3.connect('ecourts_data.db', check_same_thread=False)
    cursor = conn.cursor()
    
    # Table for case details
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            serial_number TEXT,
            cnr_number TEXT,
            case_type TEXT,
            court_info TEXT,
            filing_number TEXT,
            registration_number TEXT,
            court_name TEXT,
            next_hearing_date TEXT,
            captured_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            pdf_path TEXT,
            additional_pdfs TEXT
        )
    ''')
    
    # Table for PDF files (BLOB storage)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pdf_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER,
            filename TEXT,
            file_data BLOB,
            file_type TEXT,
            uploaded_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (case_id) REFERENCES cases (id)
        )
    ''')
    
    conn.commit()
    conn.close()

def save_case_to_db(case_data, pdf_path=None, additional_pdfs=None):
    """Save case details to database"""
    conn = sqlite3.connect('ecourts_data.db', check_same_thread=False)
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT INTO cases (
            serial_number, cnr_number, case_type, court_info, 
            filing_number, registration_number, court_name, 
            next_hearing_date, pdf_path, additional_pdfs
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        case_data.get('Serial'),
        case_data.get('CNR Number'),
        case_data.get('Case Type'),
        case_data.get('Court Number and Judge'),
        case_data.get('Filing Number'),
        case_data.get('Registration Number'),
        case_data.get('court_name'),
        str(case_data.get('next_hearing_date')) if case_data.get('next_hearing_date') else None,
        pdf_path,
        ', '.join(additional_pdfs) if additional_pdfs else None
    ))
    
    case_id = cursor.lastrowid
    
    # Save PDF files to database
    if pdf_path and os.path.exists(pdf_path):
        try:
            with open(pdf_path, 'rb') as f:
                pdf_data = f.read()
            cursor.execute('''
                INSERT INTO pdf_files (case_id, filename, file_data, file_type)
                VALUES (?, ?, ?, ?)
            ''', (case_id, os.path.basename(pdf_path), pdf_data, 'main_pdf'))
        except Exception as e:
            st.error(f"Error saving main PDF to database: {e}")
    
    if additional_pdfs:
        for pdf_file in additional_pdfs:
            if os.path.exists(pdf_file):
                try:
                    with open(pdf_file, 'rb') as f:
                        pdf_data = f.read()
                    cursor.execute('''
                        INSERT INTO pdf_files (case_id, filename, file_data, file_type)
                        VALUES (?, ?, ?, ?)
                    ''', (case_id, os.path.basename(pdf_file), pdf_data, 'additional_pdf'))
                except Exception as e:
                    st.error(f"Error saving additional PDF to database: {e}")
    
    conn.commit()
    conn.close()
    return case_id

def get_all_cases():
    """Retrieve all cases from database"""
    conn = sqlite3.connect('ecourts_data.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT * FROM cases ORDER BY captured_date DESC
    ''')
    cases = cursor.fetchall()
    conn.close()
    return cases

def get_pdf_from_db(case_id, file_type='main_pdf'):
    """Retrieve PDF file from database"""
    conn = sqlite3.connect('ecourts_data.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT filename, file_data FROM pdf_files 
        WHERE case_id = ? AND file_type = ?
    ''', (case_id, file_type))
    result = cursor.fetchone()
    conn.close()
    return result

# ----------------- Scraper Functions -----------------
def setup_driver():
    """Setup Chrome driver with options"""
    if not SELENIUM_AVAILABLE:
        st.error("Selenium is not available. Cannot setup browser driver.")
        return None
        
    chrome_options = Options()
    chrome_options.add_argument("--start-maximized")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_experimental_option('prefs', {
        "download.default_directory": DOWNLOAD_DIR,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True
    })
    
    # Check if we're in Streamlit cloud and set appropriate options
    if os.environ.get('STREAMLIT_SHARING_MODE') or os.environ.get('STREAMLIT_SERVER_HEADLESS'):
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--remote-debugging-port=9222")
    
    # Try to find Chrome driver automatically
    try:
        driver = webdriver.Chrome(options=chrome_options)
        driver.implicitly_wait(10)
        return driver
    except Exception as e:
        st.error(f"Failed to initialize Chrome driver: {e}")
        st.info("""
        **Troubleshooting tips:**
        1. Make sure Chrome browser is installed
        2. Download ChromeDriver from https://chromedriver.chromium.org/
        3. Add ChromeDriver to your PATH or place it in the same directory
        """)
        return None

def save_captcha_image(driver, save_path="captcha.png"):
    if not SELENIUM_AVAILABLE:
        return None
        
    try:
        captcha_img = driver.find_element(By.XPATH, "//img[contains(@src,'captcha') or contains(@id,'imgCaptcha') or @alt='Captcha']")
        src = captcha_img.get_attribute("src")
        if src and src.startswith("data:"):
            captcha_img.screenshot(save_path)
            return save_path
        cookies = {c['name']: c['value'] for c in driver.get_cookies()}
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(src, headers=headers, cookies=cookies, stream=True, timeout=15)
        if r.status_code == 200:
            with open(save_path, "wb") as f:
                for chunk in r.iter_content(1024):
                    f.write(chunk)
            return save_path
    except Exception as e:
        st.error(f"❌ Captcha image not found: {e}")
    return None

def download_file(url, dst_folder=DOWNLOAD_DIR):
    try:
        os.makedirs(dst_folder, exist_ok=True)
        local_name = os.path.join(dst_folder, os.path.basename(urlparse(url).path) or "file.pdf")
        with requests.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            with open(local_name, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        return local_name
    except Exception as e:
        st.error(f"❌ Download failed: {e}")
        return None

def parse_date_nullable(text):
    if not DATEUTIL_AVAILABLE:
        return None
    try:
        return dateparser.parse(text, dayfirst=True).date()
    except Exception:
        return None

def save_fullpage_pdf(driver, output_path):
    """Capture the full currently loaded page as a PDF file."""
    if not SELENIUM_AVAILABLE:
        return False
        
    try:
        result = driver.execute_cdp_cmd("Page.printToPDF", {"printBackground": True})
        data = base64.b64decode(result['data'])
        Path(output_path).write_bytes(data)
        return True
    except Exception as e:
        st.error(f"❌ Failed to save page as PDF: {e}")
        return False

def extract_case_details(driver):
    """Extract key case details including correct 16-digit CNR Number."""
    if not BEAUTIFULSOUP_AVAILABLE or not SELENIUM_AVAILABLE:
        return {}
        
    soup = BeautifulSoup(driver.page_source, "html.parser")

    details = {
        "CNR Number": None,
        "Case Type": None,
        "Court Number and Judge": None,
        "Filing Number": None,
        "Registration Number": None,
    }

    text = soup.get_text(" ", strip=True)

    # Extract correct CNR Number
    cnr_match = re.search(r'\b([A-Z0-9]{16})\s*\(Note the CNR number', text, re.IGNORECASE)
    if cnr_match:
        details["CNR Number"] = cnr_match.group(1).strip()
    else:
        fallback = re.search(r'\b[A-Z0-9]{16}\b', text)
        if fallback:
            details["CNR Number"] = fallback.group(0).strip()

    # Extract other details
    for key in [k for k in details.keys() if k != "CNR Number"]:
        pattern = re.compile(rf"{key}[:\-\s]*([A-Za-z0-9\/\.\-\s]+)", re.IGNORECASE)
        m = pattern.search(text)
        if m:
            details[key] = m.group(1).strip()

    return details

def extract_cases_from_soup(soup_obj):
    if not BEAUTIFULSOUP_AVAILABLE:
        return []
        
    cases = []
    table = soup_obj.find("table")
    
    if not table:
        return cases
        
    rows = table.find_all("tr")
    h = soup_obj.find(["h1", "h2", "h3"])
    court_name = h.get_text(strip=True) if h else "Unknown Court"

    DATE_LABEL_REGEX = re.compile(
        r"(Next\s+Hearing\s+Date|Next\s+Date|Next\s+Hearing|NextDate)[:\-\s]*",
        flags=re.IGNORECASE,
    )

    for tr in rows[1:]:  # Skip header row
        cols = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if not cols:
            continue

        serial = cols[0] if cols else ""
        row_text = tr.get_text(" ", strip=True)
        next_hearing_date = None

        m = DATE_LABEL_REGEX.search(row_text)
        if m:
            after = row_text[m.end():].strip()
            token = re.findall(r"\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{1,2}\s+\w+\s+\d{4}", after)
            if token:
                next_hearing_date = parse_date_nullable(token[0])

        if not next_hearing_date:
            token = re.findall(r"\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}", row_text)
            if token and "Next" in row_text:
                next_hearing_date = parse_date_nullable(token[0])

        cases.append({
            "serial": serial.strip(),
            "cols": cols,
            "court_name": court_name,
            "next_hearing_date": next_hearing_date,
        })
    return cases

def find_and_click_view_button(driver, serial_number):
    """Find and click the View button for a specific serial number with multiple strategies"""
    try:
        # Wait for the page to load completely
        time.sleep(2)
        
        # Strategy 1: Look for View links in the entire page and check if they're in the same row as serial
        view_links = driver.find_elements(By.XPATH, "//a[contains(., 'View') or contains(., 'VIEW')]")
        
        for link in view_links:
            # Get the parent row of this link
            try:
                row = link.find_element(By.XPATH, "./ancestor::tr[1]")
                # Check if this row contains the serial number
                if serial_number in row.text:
                    driver.execute_script("arguments[0].click();", link)
                    time.sleep(3)
                    return True
            except:
                continue
        
        # Strategy 2: Find the row with serial number and then find View link in that row
        try:
            serial_element = driver.find_element(By.XPATH, f"//td[contains(., '{serial_number}')]")
            row = serial_element.find_element(By.XPATH, "./..")
            view_links_in_row = row.find_elements(By.XPATH, ".//a[contains(., 'View') or contains(., 'VIEW')]")
            
            if view_links_in_row:
                driver.execute_script("arguments[0].click();", view_links_in_row[0])
                time.sleep(3)
                return True
        except:
            pass
        
        # Strategy 3: Look for any clickable element in the same row as serial
        try:
            serial_element = driver.find_element(By.XPATH, f"//td[contains(., '{serial_number}')]")
            row = serial_element.find_element(By.XPATH, "./..")
            # Find all links in this row
            all_links = row.find_elements(By.TAG_NAME, "a")
            if all_links:
                # Click the first link that's not empty
                for link in all_links:
                    if link.text.strip():
                        driver.execute_script("arguments[0].click();", link)
                        time.sleep(3)
                        return True
        except:
            pass
        
        return False
        
    except Exception as e:
        st.error(f"Error clicking View button for serial {serial_number}: {e}")
        return False

def click_back_button(driver):
    """Click the back button to return to the main list"""
    try:
        # Try multiple strategies to find and click back button
        back_selectors = [
            "//a[contains(., 'Back')]",
            "//button[contains(., 'Back')]",
            "//input[@value='Back']",
            "//a[contains(@href, 'javascript:history.back()')]",
            "//a[contains(@onclick, 'back')]"
        ]
        
        for selector in back_selectors:
            try:
                back_btn = driver.find_element(By.XPATH, selector)
                driver.execute_script("arguments[0].click();", back_btn)
                time.sleep(2)
                return True
            except:
                continue
        
        # If no back button found, use browser back
        driver.back()
        time.sleep(2)
        return True
        
    except Exception as e:
        st.error(f"Error clicking back button: {e}")
        return False

def capture_case_details_automated(driver, case, status_placeholder):
    """Automatically capture case details by clicking View button with proper navigation"""
    serial = case['serial']
    
    # Update status
    status_placeholder.info(f"🔄 Processing Serial {serial}...")
    
    # Click the View button for this serial
    if find_and_click_view_button(driver, serial):
        # Wait for details page to load
        time.sleep(3)
        
        # Update status
        status_placeholder.info(f"📄 Serial {serial}: View page loaded, extracting details...")
        
        # Save full page as PDF
        pdf_path = os.path.join(DOWNLOAD_DIR, f"serial_{serial}.pdf")
        pdf_saved = save_fullpage_pdf(driver, pdf_path)
        
        if pdf_saved:
            status_placeholder.info(f"✅ Serial {serial}: PDF saved successfully")
        else:
            status_placeholder.warning(f"❌ Serial {serial}: Failed to save PDF")
        
        # Extract case details
        details = extract_case_details(driver)
        
        # Update status
        status_placeholder.info(f"📊 Serial {serial}: Extracting case information...")
        
        # Download linked PDFs
        soup_now = BeautifulSoup(driver.page_source, "html.parser")
        pdfs = []
        for a in soup_now.find_all("a", href=True):
            if a['href'].lower().endswith(".pdf"):
                href = urljoin(driver.current_url, a['href'])
                dl = download_file(href, dst_folder=DOWNLOAD_DIR)
                if dl:
                    pdfs.append(dl)
        
        # Prepare case data
        case_data = {
            "Serial Number": serial,
            "CNR Number": details.get('CNR Number'),
            "Case Type": details.get('Case Type'),
            "Court Number and Judge": details.get('Court Number and Judge'),
            "Filing Number": details.get('Filing Number'),
            "Registration Number": details.get('Registration Number'),
            "Court Name": case['court_name'],
            "Next Hearing Date": case['next_hearing_date'],
            "PDF Saved": "✅" if pdf_saved else "❌",
            "Additional PDFs": len(pdfs),
            "Status": "✅ Completed"
        }
        
        # Save to database
        case_id = save_case_to_db({
            "Serial": serial,
            "court_name": case['court_name'],
            "next_hearing_date": case['next_hearing_date'],
            **details
        }, pdf_path if pdf_saved else None, pdfs)
        
        # Update status
        status_placeholder.success(f"✅ Serial {serial}: Successfully captured! (ID: {case_id})")
        
        # Go back to the main list using back button
        if not click_back_button(driver):
            # If back button fails, use browser back
            driver.back()
            time.sleep(2)
        
        return case_data
    else:
        status_placeholder.error(f"❌ Serial {serial}: Could not find View button")
        
        # Return partial data for tracking
        return {
            "Serial Number": serial,
            "CNR Number": None,
            "Case Type": None,
            "Court Number and Judge": None,
            "Filing Number": None,
            "Registration Number": None,
            "Court Name": case['court_name'],
            "Next Hearing Date": case['next_hearing_date'],
            "PDF Saved": "❌",
            "Additional PDFs": 0,
            "Status": "❌ Failed - No View Button"
        }

# ----------------- Streamlit UI -----------------
def main():
    st.set_page_config(
        page_title="eCourts Case Scraper",
        page_icon="⚖️",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    
    # Check dependencies first
    if not ALL_DEPS_AVAILABLE:
        st.error("""
        ⚠️ **Missing Dependencies**
        
        Some required packages are not installed. Please install all dependencies using:
        ```
        pip install -r requirements.txt
        ```
        
        Or install manually:
        ```
        pip install streamlit selenium beautifulsoup4 pandas requests python-dateutil lxml openpyxl
        ```
        """)
        return
    
    # Initialize database
    init_db()
    
    # Initialize session state
    if 'current_step' not in st.session_state:
        st.session_state.current_step = 1
    if 'captured_cases' not in st.session_state:
        st.session_state.captured_cases = []
    if 'matches' not in st.session_state:
        st.session_state.matches = []
    if 'driver' not in st.session_state:
        st.session_state.driver = None
    if 'capture_in_progress' not in st.session_state:
        st.session_state.capture_in_progress = False
    if 'current_case_index' not in st.session_state:
        st.session_state.current_case_index = 0
    
    st.title("⚖️ eCourts Case Scraper")
    st.markdown("---")
    
    # Sidebar for navigation
    st.sidebar.title("Navigation")
    app_mode = st.sidebar.selectbox(
        "Choose Mode",
        ["Scrape Cases", "View Database", "Settings", "Installation Guide"]
    )
    
    if app_mode == "Scrape Cases":
        scrape_cases_ui()
    elif app_mode == "View Database":
        view_database_ui()
    elif app_mode == "Settings":
        settings_ui()
    elif app_mode == "Installation Guide":
        installation_guide_ui()

def scrape_cases_ui():
    st.header("Scrape Cases from eCourts")
    
    # Step 1: Initialize Browser
    if st.session_state.current_step == 1:
        st.subheader("Step 1: Initialize Browser Session")
        
        if st.button("Initialize Browser Session"):
            with st.spinner("Starting browser session..."):
                driver = setup_driver()
                if driver:
                    st.session_state.driver = driver
                    try:
                        driver.get(ECOURTS_URL)
                        st.session_state.current_step = 2
                        st.success("Browser session started successfully!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error loading eCourts website: {e}")
    
    # Step 2: CAPTCHA Handling
    elif st.session_state.current_step == 2:
        st.subheader("Step 2: Enter CAPTCHA")
        
        if st.session_state.driver:
            # Save and display captcha
            captcha_file = save_captcha_image(st.session_state.driver)
            if captcha_file:
                st.image(captcha_file, caption="CAPTCHA Image", use_column_width=True)
            
            captcha_value = st.text_input("Enter CAPTCHA value:")
            
            col1, col2 = st.columns(2)
            with col1:
                if st.button("Submit CAPTCHA and Scrape"):
                    if captcha_value:
                        st.session_state.current_step = 3
                        st.session_state.captcha_value = captcha_value
                        st.rerun()
                    else:
                        st.error("Please enter CAPTCHA value")
            
            with col2:
                if st.button("Refresh CAPTCHA"):
                    captcha_file = save_captcha_image(st.session_state.driver, "captcha_refresh.png")
                    if captcha_file:
                        st.rerun()
    
    # Step 3: Scraping Cases
    elif st.session_state.current_step == 3:
        st.subheader("Step 3: Scraping Cases")
        
        if not st.session_state.matches:
            # Perform scraping
            process_scraping()
        else:
            # Show already scraped cases
            display_scraped_cases()
    
    # Step 4: Capture Cases
    elif st.session_state.current_step == 4:
        st.subheader("Step 4: Capture Case Details")
        capture_cases_ui()

def process_scraping():
    """Process the scraping of cases"""
    driver = st.session_state.driver
    captcha_value = st.session_state.captcha_value
    
    status_placeholder = st.empty()
    progress_bar = st.progress(0)
    
    try:
        # Fill captcha
        status_placeholder.info("Filling CAPTCHA...")
        captcha_input = driver.find_element(By.XPATH, "//input[contains(@id,'captcha') or contains(@name,'captcha')]")
        captcha_input.clear()
        captcha_input.send_keys(captcha_value)
        
        # Try to click Civil or Criminal button
        status_placeholder.info("Selecting case type...")
        for btn_text in ["Civil", "Criminal"]:
            try:
                btn = driver.find_element(By.XPATH, f"//button[contains(.,'{btn_text}') or //input[@value='{btn_text}']]")
                btn.click()
                break
            except Exception:
                continue
        
        time.sleep(2)
        
        # Extract cases
        status_placeholder.info("Scraping cases from pages...")
        all_cases = []
        page_index = 1
        max_pages = 10  # Safety limit
        
        while page_index <= max_pages:
            status_placeholder.info(f"Scraping page {page_index}...")
            soup = BeautifulSoup(driver.page_source, "html.parser")
            cases = extract_cases_from_soup(soup)
            all_cases.extend(cases)
            
            # Update progress (0.0 to 1.0)
            progress_bar.progress(page_index / max_pages)
            
            try:
                next_btn = driver.find_element(By.LINK_TEXT, "Next")
                if next_btn.is_enabled():
                    next_btn.click()
                    page_index += 1
                    time.sleep(1.5)
                    continue
            except:
                try:
                    next_btn = driver.find_element(By.XPATH, "//a[contains(@class,'next') or contains(@aria-label,'Next')]")
                    next_btn.click()
                    page_index += 1
                    time.sleep(1.5)
                    continue
                except:
                    break
            break
        
        progress_bar.progress(1.0)
        
        # Filter cases for today and tomorrow
        today = datetime.date.today()
        tomorrow = today + datetime.timedelta(days=1)
        matches = []
        
        for c in all_cases:
            listed = c.get("next_hearing_date")
            if listed and (listed == today or listed == tomorrow):
                matches.append(c)
        
        st.session_state.matches = matches
        st.session_state.all_cases = all_cases
        
        if matches:
            status_placeholder.success(f"Found {len(matches)} cases with next hearing today or tomorrow")
            st.session_state.current_step = 4
            st.rerun()
        else:
            status_placeholder.warning("No cases found with next hearing today or tomorrow")
            st.session_state.current_step = 1
            
    except Exception as e:
        status_placeholder.error(f"Error during scraping: {e}")

def display_scraped_cases():
    """Display the scraped cases and provide option to capture"""
    matches = st.session_state.matches
    
    st.success(f"Found {len(matches)} cases with next hearing today or tomorrow")
    
    # Display matches in a table
    display_data = []
    for case in matches:
        display_data.append({
            "Serial": case['serial'],
            "Court": case['court_name'],
            "Next Hearing": case['next_hearing_date'],
            "Status": "⏳ Pending Capture"
        })
    
    df_display = pd.DataFrame(display_data)
    st.dataframe(df_display)
    
    # Start automatic capture
    if st.button("🚀 Start Automatic Capture of All Cases"):
        st.session_state.capture_in_progress = True
        st.session_state.current_case_index = 0
        st.session_state.captured_cases = []
        st.rerun()

def capture_cases_ui():
    """UI for capturing cases with live updates"""
    if st.session_state.capture_in_progress:
        perform_capture()
    else:
        display_scraped_cases()

def perform_capture():
    """Perform the actual capture of cases with live updates"""
    driver = st.session_state.driver
    matches = st.session_state.matches
    
    # Create UI elements for live updates
    progress_bar = st.progress(0)
    status_placeholder = st.empty()
    table_placeholder = st.empty()
    
    current_index = st.session_state.current_case_index
    
    if current_index < len(matches):
        case = matches[current_index]
        
        # Update progress (0.0 to 1.0) - FIXED: Use proper fraction
        progress_fraction = current_index / len(matches)
        progress_bar.progress(progress_fraction)
        
        # Show current status
        status_placeholder.info(f"🔄 Processing Case {current_index + 1} of {len(matches)}: Serial {case['serial']}")
        
        # Capture case details
        captured_data = capture_case_details_automated(driver, case, status_placeholder)
        
        # Add to captured cases
        if captured_data:
            st.session_state.captured_cases.append(captured_data)
        
        # Update the table
        if st.session_state.captured_cases:
            df_live = pd.DataFrame(st.session_state.captured_cases)
            with table_placeholder.container():
                st.subheader("📊 Live Capture Progress")
                st.dataframe(df_live, use_container_width=True)
        
        # Move to next case
        st.session_state.current_case_index += 1
        
        # Rerun to update UI
        time.sleep(1)
        st.rerun()
    
    else:
        # Capture completed
        progress_bar.progress(1.0)
        status_placeholder.success(f"✅ Automatic capture completed! Processed {len(st.session_state.captured_cases)} out of {len(matches)} cases")
        st.session_state.capture_in_progress = False
        
        # Save all captured data to Excel
        if st.session_state.captured_cases:
            df_excel = pd.DataFrame(st.session_state.captured_cases)
            
            # Download Excel button
            excel_buffer = io.BytesIO()
            with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                df_excel.to_excel(writer, index=False, sheet_name='Case Details')
            
            st.download_button(
                label="📥 Download All Case Details as Excel",
                data=excel_buffer.getvalue(),
                file_name=f"case_details_{datetime.date.today()}.xlsx",
                mime="application/vnd.ms-excel"
            )
            
            # Show final summary
            st.subheader("🎯 Final Capture Summary")
            st.dataframe(df_excel, use_container_width=True)

def view_database_ui():
    st.header("Stored Cases Database")
    
    cases = get_all_cases()
    
    if cases:
        # Convert to DataFrame for display
        df = pd.DataFrame(cases, columns=[
            'ID', 'Serial', 'CNR', 'Case Type', 'Court Info', 
            'Filing Number', 'Registration Number', 'Court Name',
            'Next Hearing', 'Captured Date', 'PDF Path', 'Additional PDFs'
        ])
        
        st.dataframe(df)
        
        # Search and filter
        st.subheader("Search Cases")
        search_term = st.text_input("Search by CNR, Serial, or Case Type:")
        
        if search_term:
            filtered_df = df[df.apply(lambda row: row.astype(str).str.contains(search_term, case=False).any(), axis=1)]
            st.dataframe(filtered_df)
        
        # Download full database
        excel_buffer = io.BytesIO()
        df.to_excel(excel_buffer, index=False)
        st.download_button(
            label="Download Full Database as Excel",
            data=excel_buffer.getvalue(),
            file_name=f"ecourts_database_{datetime.date.today()}.xlsx",
            mime="application/vnd.ms-excel"
        )
    
    else:
        st.info("No cases stored in database yet.")

def settings_ui():
    st.header("Settings")
    
    st.subheader("Download Directory")
    st.write(f"Current download directory: `{DOWNLOAD_DIR}`")
    
    st.subheader("Database Information")
    conn = sqlite3.connect('ecourts_data.db', check_same_thread=False)
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM cases")
    case_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM pdf_files")
    pdf_count = cursor.fetchone()[0]
    
    conn.close()
    
    st.write(f"Total cases in database: {case_count}")
    st.write(f"Total PDF files stored: {pdf_count}")
    
    # Clear database option
    if st.button("Clear Database (Dangerous!)"):
        conn = sqlite3.connect('ecourts_data.db', check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM cases")
        cursor.execute("DELETE FROM pdf_files")
        conn.commit()
        conn.close()
        st.success("Database cleared!")
        st.rerun()

def installation_guide_ui():
    st.header("Installation Guide")
    
    st.subheader("Step 1: Install Dependencies")
    st.code("""
pip install streamlit==1.28.0 selenium==4.15.0 beautifulsoup4==4.12.2 
pandas==2.0.3 requests==2.31.0 python-dateutil==2.8.2 lxml==4.9.3 openpyxl==3.1.2
""", language="bash")
    
    st.subheader("Step 2: Install Chrome Driver")
    st.write("""
    1. Download ChromeDriver from [https://chromedriver.chromium.org/](https://chromedriver.chromium.org/)
    2. Make sure it matches your Chrome browser version
    3. Add ChromeDriver to your system PATH or place it in the same directory as this script
    """)
    
    st.subheader("Step 3: Run the Application")
    st.code("streamlit run app.py", language="bash")

if __name__ == "__main__":
    main()