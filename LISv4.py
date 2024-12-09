import os
import sys
import shutil
import logging
import threading
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET
from flask import Flask, render_template_string, send_file, jsonify, request, make_response
import re
from threading import Event, Lock
from typing import List, Dict, Optional
import pytz
import json
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import concurrent.futures
import fnmatch

# Explicitly import _strptime to avoid issues in frozen environments
import _strptime

# Globals
_config_path = r'C:\Live Incident Status\config.json'
#_config_path = r'gdc_config.json'

# Load configuration from JSON file
def load_config():
    try:
        with open(_config_path, 'r') as config_file:
            config = json.load(config_file)
            required_keys = [
                'source_directory', 'xml_directory', 'log_file_path', 
                'check_interval_source', 'check_interval_xml', 
                'logging_level', 'logo_path', 'excluded_units', 
                'jurisdiction_company_mapping', 'excluded_call_types'
            ]
            for key in required_keys:
                if key not in config:
                    raise ValueError(f"Missing required configuration key: {key}")
            return config
    except FileNotFoundError as e:
        print(f"Configuration file not found: {e}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON configuration: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error loading configuration: {e}")
        sys.exit(1)

# Load configuration
config = load_config()
jurisdiction_company_mapping = config['jurisdiction_company_mapping']

# Set up logging
logging.basicConfig(
    filename=config['log_file_path'], 
    level=getattr(logging, config['logging_level'].upper(), None), 
    format='%(asctime)s - %(levelname)s - %(message)s'
)

app = Flask(__name__)

# Disable Flask's default request logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

class DeduplicationFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self.logged_messages = set()

    def filter(self, record):
        log_entry = record.getMessage()
        if log_entry in self.logged_messages:
            return False
        else:
            self.logged_messages.add(log_entry)
            return True

# Set up logging with the deduplication filter
logging.basicConfig(
    filename=config['log_file_path'], 
    level=getattr(logging, config['logging_level'].upper(), None), 
    format='%(asctime)s - %(levelname)s - %(message)s'
)
dedup_filter = DeduplicationFilter()
logging.getLogger().addFilter(dedup_filter)
logging.info("Program started.")

# Define Eastern timezone
eastern = pytz.timezone('US/Eastern')

# Ensure destination directories exist
try:
    os.makedirs(config['xml_directory'], exist_ok=True)
    os.makedirs(os.path.dirname(config['log_file_path']), exist_ok=True)
except Exception as e:
    print(f"Error creating necessary directories: {e}")
    sys.exit(1)

# XML namespace
namespace = '{http://www.newworldsystems.com/Aegis/CAD/Peripheral/CallExport/2011/02}'

# Create a lock for synchronizing access to call data
data_lock = Lock()
call_display_times = {}
latest_call_files = {}

# Define the fixed path for the logo
logo_path = config['logo_path']

# Route to serve the logo image
@app.route('/logo.png')
def serve_logo():
    try:
        return send_file(logo_path, mimetype='image/png')
    except Exception as e:
        logging.error(f"Error serving logo.png: {str(e)}")
        return str(e)

stop_event = Event()

# Start the monitoring threads
def start_monitoring():
    global monitor_thread, file_checking_thread, cleanup_thread
    monitor_thread = threading.Thread(target=monitor_and_transfer_files, args=(stop_event,))
    monitor_thread.daemon = True
    monitor_thread.start()

    file_checking_thread = threading.Thread(target=check_for_new_files, args=(config['check_interval_xml'], stop_event))
    file_checking_thread.daemon = True
    file_checking_thread.start()
    
    cleanup_thread = threading.Thread(target=cleanup_old_calls, args=(stop_event,))
    cleanup_thread.daemon = True
    cleanup_thread.start()

def stop_monitoring():
    stop_event.set()
    threads = [monitor_thread, file_checking_thread, cleanup_thread]
    for thread in threads:
        thread.join(timeout=5)

def remove_address_numbers(address: str) -> str:
    return re.sub(r'\b\d+\s*(?:apt|apartment|suite|ste|unit|#|lot|rear|room|rm)?\s*\d*\b|\b(?:apt|apartment|suite|ste|unit|#|lot|rear|room|rm)\b', '', address, flags=re.IGNORECASE).strip()

# Monitor the source directory and transfer new XML files to the XML directory
def monitor_and_transfer_files(stop_event: Event):
    while not stop_event.is_set():
        try:
            for filename in os.listdir(config['source_directory']):
                if filename.endswith('.xml'):
                    source_file = os.path.join(config['source_directory'], filename)
                    dest_file = os.path.join(config['xml_directory'], filename)
                    with data_lock:
                        if not os.path.exists(dest_file):
                            shutil.copy2(source_file, dest_file)
                            logging.info(f"Copied file {filename} to {config['xml_directory']}")
        except Exception as e:
            logging.error(f"Error monitoring source directory: {str(e)}")
        stop_event.wait(config['check_interval_source'])

# Check for new XML files and process them
def check_for_new_files(interval_seconds: int, stop_event: Event):
    while not stop_event.is_set():
        try:
            with data_lock:
                xml_files = [os.path.join(config['xml_directory'], f) for f in os.listdir(config['xml_directory']) if f.endswith('.xml')]
                xml_files.sort()  # Ensure files are processed in order
                calls, files_to_delete = process_xml_files(xml_files)

                with app.app_context():
                    render_webpage(calls)

                # Only delete files if they are marked for deletion
                if files_to_delete:
                    delete_files(files_to_delete)
        except Exception as e:
            logging.error(f"Error in file checking thread: {str(e)}")
        stop_event.wait(interval_seconds)

# Extract data from an XML file
def extract_xml_data(file_path):
    try:
        if os.path.getsize(file_path) == 0:
            logging.error(f"File {file_path} is empty.")
            return None, None, None, None, None, None, [], None, None

        tree = ET.parse(file_path)
        root = tree.getroot()
        
        ns = {'nw': 'http://www.newworldsystems.com/Aegis/CAD/Peripheral/CallExport/2011/02'}
        
        call_number = root.findtext('nw:CallNumber', namespaces=ns)
        close_datetime = root.findtext('nw:CloseDateTime', namespaces=ns)
        location = root.findtext('nw:Location/nw:FullAddress', namespaces=ns)
        nature_of_call = root.findtext('nw:AgencyContexts/nw:AgencyContext/nw:CallType', namespaces=ns)

        # Extract latitude and longitude from the FullAddress field
        latitude = None
        longitude = None
        if location:
            lat_match = re.search(r'LAT:\s*([-+]?[0-9]*\.?[0-9]+)', location)
            lon_match = re.search(r'LON:\s*([-+]?[0-9]*\.?[0-9]+)', location)
            if lat_match and lon_match:
                latitude = lat_match.group(1)
                longitude = lon_match.group(1)
                location = remove_address_numbers(location)
                location_parts = location.split(',') #
                location = ', '.join(location_parts[1:]).strip()  # Remove the first part (house, apt, suite numbers) #

        agency_contexts = []
        for context in root.findall('nw:AgencyContexts/nw:AgencyContext', namespaces=ns):
            agency_context = {
                'agency_type': context.findtext('nw:AgencyType', namespaces=ns).lower(),
                'call_type': context.findtext('nw:CallType', namespaces=ns),
                'status': context.findtext('nw:Status', namespaces=ns),            
            }
            agency_contexts.append(agency_context)

        unit_details = []
        primary_unit = None
        for unit in root.findall('nw:AssignedUnits/nw:Unit', namespaces=ns):
            is_primary = unit.findtext('nw:IsPrimary', namespaces=ns) == 'true'
            unit_detail = {
                'unit_id': unit.findtext('nw:UnitNumber', namespaces=ns),
                'unit_type': unit.findtext('nw:Type', namespaces=ns).title(),
                'clear_time': parse_datetime(unit.findtext('nw:ClearDateTime', namespaces=ns)),
                'arrive_time': parse_datetime(unit.findtext('nw:ArriveDateTime', namespaces=ns)),
                'enroute_time': parse_datetime(unit.findtext('nw:EnrouteDateTime', namespaces=ns)),
                'jurisdiction': unit.findtext('nw:Jurisdiction', namespaces=ns),
                'is_primary': is_primary
            }
            if is_primary:
                primary_unit = unit_detail

            if unit_detail['unit_type'].lower() not in config['excluded_units']:
                unit_details.append(unit_detail)

        return call_number, close_datetime, agency_contexts, root, location, nature_of_call, unit_details, primary_unit, latitude, longitude
    except ET.ParseError as e:
        logging.error(f"XML ParseError for file {file_path}: {str(e)}")
        return None, None, None, None, None, None, [], None, None
    except Exception as e:
        logging.error(f"Error extracting XML data from {file_path}: {str(e)}")
        return None, None, None, None, None, None, [], None, None

def parse_datetime(date_str):
    if date_str:
        if isinstance(date_str, datetime):
            return date_str
        try:
            # Try parsing with the full datetime format first
            return datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S%z")
        except ValueError:
            try:
                # Handle case where only time is provided
                today_str = datetime.now(eastern).strftime("%Y-%m-%d")
                date_str_with_today = f"{today_str} {date_str}"
                return datetime.strptime(date_str_with_today, "%Y-%m-%d %H:%M:%S %Z")
            except ValueError as e:
                logging.error(f"Error parsing datetime string '{date_str}': {str(e)}")
    return None

def merge_unit_details(existing_units, new_units):
    existing_units_dict = {unit['unit_id']: unit for unit in existing_units}
    for new_unit in new_units:
        if new_unit['unit_id'] in existing_units_dict:
            existing_unit = existing_units_dict[new_unit['unit_id']]
            # Update existing unit details if new unit details are more recent
            if (new_unit['arrive_time'] and (existing_unit['arrive_time'] is None or new_unit['arrive_time'] > existing_unit['arrive_time'])):
                existing_unit['arrive_time'] = new_unit['arrive_time']
            if (new_unit['enroute_time'] and (existing_unit['enroute_time'] is None or new_unit['enroute_time'] > existing_unit['enroute_time'])):
                existing_unit['enroute_time'] = new_unit['enroute_time']
            if (new_unit['clear_time'] and (existing_unit['clear_time'] is None or new_unit['clear_time'] > existing_unit['clear_time'])):
                existing_unit['clear_time'] = new_unit['clear_time']
        else:
            if new_unit['unit_type'].lower() not in config['excluded_units']:
                existing_units_dict[new_unit['unit_id']] = new_unit
                logging.info(f"Added new unit {new_unit['unit_id']} for a call: {new_unit}")
    return list(existing_units_dict.values())

def convert_utc_to_est_time(utc_dt: datetime) -> str:
    if utc_dt is None:
        return ""
    est_dt = utc_dt.astimezone(eastern)
    return est_dt.strftime("%m-%d-%y %H:%M:%S %Z")

def update_call_display_time(call_number):
    current_time = datetime.now(eastern)
    call_display_times[call_number] = current_time
    logging.info(f"Updated display time for call {call_number} to {current_time}")

def log_display_duration(call_number, display_time):
    current_time = datetime.now(eastern)
    display_duration = current_time - display_time
    
def cleanup_old_calls(stop_event: Event):
    stop_event.wait(5)
    while not stop_event.is_set():
        try:
            current_time = datetime.now(eastern)
            to_remove = []
            with data_lock:
                for call_number, display_time in call_display_times.items():
                    display_duration = (current_time - display_time) // timedelta(minutes=1)  # Calculate duration in minutes
                    if display_duration > 360:  # 6 hour display time limit in minutes
                        to_remove.append(call_number)
                    else:
                        logging.info(f"Call {call_number} has been displayed for {display_duration} minutes.")

                for call_number in to_remove:
                     del call_display_times[call_number]
                     for xml_file in os.listdir(config['xml_directory']):
                         if xml_file.endswith('.xml'):
                             file_path = os.path.join(config['xml_directory'], xml_file)
                             cn, _, _, _, _, _, _, _, _, _ = extract_xml_data(file_path)
                             if cn == call_number:
                                 os.remove(file_path)
                                 logging.info(f"Removed call {call_number} from display due to 6-hour limit.")
        except Exception as e:
            logging.error(f"Error in cleanup_old_calls: {str(e)}")
        stop_event.wait(600)  # Check every 10 minutes

def is_excluded_unit(unit_id: str, excluded_units: List[str]) -> bool:
    for pattern in excluded_units:
        if fnmatch.fnmatch(unit_id.lower(), pattern.lower()):
            return True
    return False

def process_xml_files(xml_files: List[str]) -> List[Dict[str, str]]:
    location_calls = {}
    processed_call_numbers = set()
    latest_call_files = {}
    files_to_delete = []

    closed_call_numbers = set()
    primary_units = {}

    for xml_file in xml_files:
        call_number, close_datetime, agency_contexts, root, location, nature_of_call, unit_details, primary_unit, latitude, longitude = extract_xml_data(xml_file)
        if call_number and close_datetime:
            logging.info(f"Call {call_number} is marked as closed with CloseDateTime {close_datetime}.")
            closed_call_numbers.add(call_number)

    for xml_file in xml_files:
        if not os.path.exists(xml_file):
            logging.error(f"File {xml_file} does not exist.")
            continue

        # CHANGED by GDC - added .xml
        match = re.match(r'^(\d+)_(\d+)\.xml(?:-\d+\.backup)?$', os.path.basename(xml_file))
        if not match:
            logging.error(f"Invalid filename format: {xml_file}")
            continue

        call_number, timestamp = match.groups()
        call_number, close_datetime, agency_contexts, root, location, nature_of_call, unit_details, primary_unit, latitude, longitude = extract_xml_data(xml_file)
        if call_number:
            if call_number in closed_call_numbers:
                files_to_delete.append(xml_file)
                if call_number in call_display_times:
                    del call_display_times[call_number]
                continue

            current_file_number = int(timestamp)
            previous_file_number = int(os.path.splitext(os.path.basename(latest_call_files.get(call_number, '0_0.xml')))[0].split('_')[1])

            if call_number in processed_call_numbers:
                if current_file_number > previous_file_number:
                    files_to_delete.append(latest_call_files[call_number])
                else:
                    continue

            processed_call_numbers.add(call_number)
            latest_call_files[call_number] = xml_file

            try:
                logging.debug(f"Processing call number {call_number}")

                fire_units_dispatched = False
                ems_units_dispatched = False
                police_units_dispatched = False
                has_excluded_unit = False

                for unit in unit_details:
                    unit_type = unit['unit_type']
                    unit_id = unit['unit_id'].lower()

                    if unit_type.lower() in {'engine', 'rescue', 'tanker', 'quint', 'truck', 'lo', 'rit', 'fire police', 'traffic', 'fire tone', 'chief'}:
                        fire_units_dispatched = True
                    elif unit_type.lower() in {'micu', 'ambulance', 'intermediate', 'medic', 'ems tone', 'ems page'}:
                        ems_units_dispatched = True
                    elif unit_type.lower() == 'police officer':
                        police_units_dispatched = True

                    if is_excluded_unit(unit_id, config['excluded_units']):
                        has_excluded_unit = True

                    if unit['is_primary']:
                        primary_unit = unit

                if primary_unit:
                    jurisdiction = primary_unit['jurisdiction']
                    agency_type = jurisdiction_company_mapping.get(jurisdiction, {}).get('agency_type', '')

                    if agency_type == 'EMS':
                        ems_units_dispatched = True
                    elif agency_type == 'Police':
                        police_units_dispatched = True

                    primary_units[call_number] = agency_type

                    logging.info(f"Call {call_number}, Primary Unit: {primary_unit['unit_id']}, Nature of call: {nature_of_call.lower()}, Agency Type: {agency_type}")

                if primary_unit and primary_unit['unit_type'].lower() in config['excluded_unit_types']:
                    logging.info(f"Call {call_number} has excluded Primary Unit Type {primary_unit['unit_type']}. Skipping.")
                    files_to_delete.append(xml_file)
                    continue

                if has_excluded_unit:
                    logging.info(f"Call {call_number} has excluded units. Skipping.")
                    files_to_delete.append(xml_file)
                    continue

                if primary_units.get(call_number) == 'EMS':
                    logging.info(f"Call {call_number} is a EMS call. Skipping.")
                    files_to_delete.append(xml_file)
                    continue

                if primary_units.get(call_number) == 'Police' and not fire_units_dispatched:
                    logging.info(f"Call {call_number} is a Police call. Skipping.")
                    files_to_delete.append(xml_file)
                    continue

                if nature_of_call.lower() in config['excluded_call_types']:
                    logging.info(f"Call {call_number} has an excluded Call Type {nature_of_call}. Skipping.")
                    files_to_delete.append(xml_file)
                    continue

                create_date_time_elem = root.find(f'{namespace}CreateDateTime')
                create_date_time = convert_utc_to_est_time(parse_datetime(create_date_time_elem.text)) if create_date_time_elem is not None else ""

                location_without_numbers = remove_address_numbers(location)
                display_location = location_without_numbers
                if latitude and longitude:
                    display_location = f"{location_without_numbers}, LAT: {latitude}, LON: {longitude}"
                    #display_location = f"US Route 15: LAT: {latitude}, LON: {longitude}"

                if location in location_calls:
                    if call_number not in location_calls[location]['call_numbers']:
                        location_calls[location]['call_numbers'].append(call_number)
                    if nature_of_call not in location_calls[location]['nature_of_call']:
                        location_calls[location]['nature_of_call'] += ", " + nature_of_call

                    logging.info(f"Merging unit details for call number {call_number} at location {location}")

                    merged_unit_details = merge_unit_details(location_calls[location]['unit_details'], unit_details)
                    location_calls[location]['unit_details'] = merged_unit_details
                else:
                    location_calls[location] = {
                        'call_numbers': [call_number],
                        'location': display_location, 
                        'create_date_time': create_date_time,
                        'nature_of_call': nature_of_call,
                        'unit_details': unit_details,
                        'latitude': latitude,
                        'longitude': longitude
                    }

                if call_number not in call_display_times:
                    call_display_times[call_number] = datetime.now(eastern)

            except Exception as e:
                logging.error(f"Error processing file: {xml_file} - {str(e)}")

    for location, call_info in location_calls.items():
        call_info['unit_details'] = [unit for unit in call_info['unit_details'] if unit['clear_time'] is None and unit['unit_type'].lower() not in config['excluded_units']]

    calls = sorted(location_calls.values(), key=lambda x: int(x['call_numbers'][0]), reverse=True)

    for call_info in calls:
        call_info['call_number'] = ', '.join(call_info['call_numbers'])

    logging.info(f"Total calls to be displayed: {len(calls)}") if calls else None

    return calls, files_to_delete

def delete_files(files_to_delete: List[str]):
    # Deleting files after rendering webpage
    logging.info(f"Files to delete: {files_to_delete}")
    for xml_file in files_to_delete:
        if os.path.exists(xml_file):
            os.remove(xml_file)
            logging.info(f"Deleted processed file {xml_file}")

def render_webpage(calls: List[Dict[str, str]]):
    try:
        rendered_page = render_template_string('''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Live Incident Status</title>
    <style>
        body { 
            font-family: Arial, sans-serif; 
            margin: 0; 
            padding: 0; 
            background-color: #e9ecef; 
        }
        .container { 
            max-width: 1200px; 
            margin: 20px auto; 
            padding: 20px; 
            background-color: #fff; 
            box-shadow: 0 0 10px rgba(0, 0, 0, 0.1); 
            border-radius: 8px;
        }
        .header img { 
            width: 100%; 
            height: auto; 
            max-height: 200px; 
            object-fit: contain; 
        }
        h1 { 
            text-align: center; 
            color: #333; 
            margin-top: 20px; 
            font-size: 28px; 
        }
        table { 
            width: 100%; 
            border-collapse: collapse; 
            margin-top: 20px; 
        }
        th, td { 
            padding: 12px; 
            border-bottom: 1px solid #ddd; 
            text-align: left; 
        }
        th { 
            background-color: #343a40; 
            color: #fff; 
            font-weight: bold; 
        }
        tr:nth-child(even) { 
            background-color: #CFE2F3; 
        }
        tr:nth-child(odd) { 
            background-color: #ffffff; 
        }
        .no-calls { 
            text-align: center; 
            color: #666; 
            margin-top: 20px; 
        }
        .refresh-container { 
            text-align: center; 
            font-style: italic; 
            color: #999; 
            margin-top: 10px; 
        }
        .countdown { 
            display: inline; 
            color: #999; 
        }
    </style>
    <script>
        var countdownTime = 2 * 60;  // 2 minutes in seconds
        
        function updateCountdown() {
            var minutes = Math.floor(countdownTime / 60);
            var seconds = countdownTime % 60;
            if (seconds < 10) {
                seconds = "0" + seconds;
            }
            document.getElementById("countdown").innerHTML = minutes + ":" + seconds;
            countdownTime--;
            if (countdownTime < 0) {
                location.reload();
            } else {
                setTimeout(updateCountdown, 1000);
            }
        }
        
        window.onload = function() {
            updateCountdown();
        };

        function sortTable(columnIndex) {
            var table = document.getElementById("callsTable");
            var rows = Array.prototype.slice.call(table.rows, 1);
            rows.sort(function (a, b) {
                return a.cells[columnIndex].innerText.localeCompare(b.cells[columnIndex].innerText);
            });
            rows.forEach(function (row) {
                table.appendChild(row);
            });
        }
    </script>
</head>
<body>
    <div class="container">
        <div class="header">
            <img src="/logo.png" alt="Header Image">
        </div>
        <h1>Live Incident Status</h1>
        <div class="refresh-container">
            <span>This page automatically refreshes in:</span>
            <span class="countdown" id="countdown"></span>
        </div>
        {% if calls %}
        <table id="callsTable">
            <thead>
                <tr>
                    <th onclick="sortTable(0)">Call Numbers</th>
                    <th onclick="sortTable(1)">Location</th>
                    <th onclick="sortTable(2)">Time Created (EST)</th>
                    <th onclick="sortTable(3)">Nature of Call</th>
                    <th>Details</th>
                </tr>
            </thead>
            <tbody>
                {% for call in calls %}
                <tr>
                    <td>{{ call.call_number | e}}</td>
                    <td>{{ call.location | e}}</td>
                    <td>{{ call.create_date_time | e}}</td>
                    <td>{{ call.nature_of_call | e}}</td>
                    <td><a href="/unit_details?call_number={{ call.call_number | e }}">View Details</a></td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        {% else %}
        <p class="no-calls">No calls to display at the moment.</p>
        {% endif %}
    </div>
</body>
</html>
        ''', calls=calls)

        response = make_response(rendered_page)
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "frame-ancestors 'none';"
        return response
    except Exception as e:
        logging.error(f"Error rendering webpage: {str(e)}")
        return "Error rendering the page."

@app.route('/debug-calls')
def debug_calls():
    try:
        with data_lock:
            xml_files = [os.path.join(config['xml_directory'], f) for f in os.listdir(config['xml_directory']) if f.endswith('.xml')]
            calls, _ = process_xml_files(xml_files)
        return jsonify(calls)
    except Exception as e:
        logging.error(f"Error in debug route: {str(e)}")
        return jsonify({"error": str(e)})

@app.route('/unit_details')
def unit_details():
    call_number = request.args.get('call_number')
    if not call_number:
        return "Call number not provided", 400

    try:
        unit_details_list = []
        with data_lock:
            xml_files = [os.path.join(config['xml_directory'], f) for f in os.listdir(config['xml_directory']) if f.endswith('.xml')]
            xml_files.sort()
            for xml_file in xml_files:
                cn, _, _, _, _, _, unit_details, _, _, _ = extract_xml_data(xml_file)
                if cn == call_number:
                    logging.debug(f"Processing call number {call_number}")
                    logging.debug(f"Extracted unit details: {unit_details}")

                    for unit in unit_details:
                        if isinstance(unit['enroute_time'], datetime):
                            unit['enroute_time'] = convert_utc_to_est_time(unit['enroute_time'])
                        if isinstance(unit['arrive_time'], datetime):
                            unit['arrive_time'] = convert_utc_to_est_time(unit['arrive_time'])
                        if isinstance(unit['clear_time'], datetime):
                            unit['clear_time'] = convert_utc_to_est_time(unit['clear_time'])
                    unit_details_list = unit_details
                    break

        if not unit_details_list:
            return "No unit details found for the given call number", 404

        logging.debug(f"Rendering unit details for call number {call_number}: {unit_details_list}")

        # Sort the unit details by unit type
        unit_details_list.sort(key=lambda x: x['unit_type'])

        return render_template_string('''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Unit Details</title>
    <style>
        body { 
            font-family: Arial, sans-serif; 
            margin: 0; 
            padding: 0; 
            background-color: #e9ecef; 
        }
        .container { 
            max-width: 1000px; 
            margin: 20px auto; 
            padding: 20px; 
            background-color: #fff; 
            box-shadow: 0 0 10px rgba(0, 0, 0, 0.1); 
            border-radius: 8px;
        }
        .header img { 
            width: 100%; 
            height: auto; 
            max-height: 200px; 
            object-fit: contain; 
        }
        h1 { 
            text-align: center; 
            color: #333; 
            margin-top: 20px; 
            font-size: 28px; 
        }
        table { 
            width: 100%; 
            border-collapse: collapse; 
            margin-top: 20px; 
        }
        th, td { 
            padding: 12px; 
            border-bottom: 1px solid #ddd; 
            text-align: left; 
        }
        th { 
            background-color: #343a40; 
            color: #fff; 
            font-weight: bold; 
            cursor: pointer; 
        }
        tr:nth-child(even) { 
            background-color: #CFE2F3; 
        }
        tr:nth-child(odd) { 
            background-color: #ffffff; 
        }
        .no-calls { 
            text-align: center; 
            color: #666; 
            margin-top: 20px; 
        }
        .refresh-container { 
            text-align: center; 
            font-style: italic; 
            color: #999; 
            margin-top: 10px; 
        }
        .countdown { 
            display: inline; 
            color: #999; 
        }
        .back-link {
            display: block;
            margin-top: 20px;
            text-align: center;
            font-size: 16px;
            color: #007bff;
            text-decoration: none;
        }
        .back-link:hover {
            text-decoration: underline;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <img src="/logo.png" alt="Header Image">
        </div>
        <h1>Unit Details for Call {{ call_number }}</h1>
        <table>
            <thead>
                <tr>
                    <th>Unit ID</th>
                    <th>Unit Type</th>
                    <th>Enroute Time</th>
                    <th>Arrive Time</th>
                    <th>Clear Time</th>
                </tr>
            </thead>
            <tbody>
                {% for unit in unit_details %}
                <tr>
                    <td>{{ unit.unit_id | e }}</td>
                    <td>{{ unit.unit_type | e }}</td>
                    <td>{{ unit.enroute_time | e }}</td>
                    <td>{{ unit.arrive_time | e }}</td>
                    <td>{{ unit.clear_time | e }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        <a href="/" class="back-link">Go Back</a>
    </div>
</body>
</html>
        ''', call_number=call_number, unit_details=unit_details_list)
    except Exception as e:
        logging.error(f"Error rendering unit details page: {str(e)}")
        return "Error rendering the page."

start_monitoring()

@app.route('/')
def index():
    try:
        with data_lock:
            xml_files = [os.path.join(config['xml_directory'], f) for f in os.listdir(config['xml_directory']) if f.endswith('.xml')]
            calls, _ = process_xml_files(xml_files)
        
        logging.debug(f"Calls data to be rendered: {calls}")
        
        return render_webpage(calls)
    except Exception as e:
        logging.error(f"Error in root route: {str(e)}")
        return "Error loading the page."

def run_flask():
    #app.run(host='0.0.0.0', port=5000)
    app.run(host='192.168.4.195')

# Add tooltips to tkinter widgets
class ToolTip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tooltip_window = None
        self.widget.bind("<Enter>", self.show_tooltip)
        self.widget.bind("<Leave>", self.hide_tooltip)

    def show_tooltip(self, event):
        if self.tooltip_window or not self.text:
            return
        x, y = self.widget.winfo_pointerxy()
        self.tooltip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x+20}+{y+10}")
        label = tk.Label(tw, text=self.text, justify=tk.LEFT,
                         background="#ffffe0", relief=tk.SOLID, borderwidth=1,
                         font=("tahoma", "8", "normal"))
        label.pack(ipadx=1)

    def hide_tooltip(self, event):
        tw = self.tooltip_window
        self.tooltip_window = None
        if tw:
            tw.destroy()

# Define HTMLUpdater class
class HTMLUpdater:
    def __init__(self, logger):
        self.logger = logger

    def update_html(self, html_path: str, data: str):
        self.logger.info(f"Updating HTML at path: {html_path}")
        try:
            with open(html_path, 'w') as f:
                f.write(data)
            self.logger.info(f"HTML file updated successfully.")
        except Exception as e:
            self.logger.error(f"Failed to update HTML file: {e}")

# GUI Implementation
class XMLProcessorGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Live Incident Status Control Panel")
        self.geometry("800x600")
        
        self.info_label = ttk.Label(self, text="Control Panel for XML File Processing", font=("Arial", 16))
        self.info_label.pack(pady=10)
        
        self.main_frame = ttk.Frame(self)
        self.main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        self.call_listbox_label = ttk.Label(self.main_frame, text="List of Active Calls", font=("Arial", 14))
        self.call_listbox_label.grid(row=0, column=0, padx=5, pady=5, sticky="w")
        ToolTip(self.call_listbox_label, "Displays the list of currently active calls.")
        
        self.call_files_listbox_label = ttk.Label(self.main_frame, text="Files Associated with the Selected Call", font=("Arial", 14))
        self.call_files_listbox_label.grid(row=0, column=1, padx=5, pady=5, sticky="w")
        ToolTip(self.call_files_listbox_label, "Displays the files associated with the selected call.")
        
        self.call_listbox = tk.Listbox(self.main_frame, selectmode=tk.SINGLE, font=("Arial", 12))
        self.call_listbox.grid(row=1, column=0, padx=5, pady=5, sticky="nsew")
        self.call_listbox.bind("<<ListboxSelect>>", self.show_call_files)
        ToolTip(self.call_listbox, "Select a call to view associated XML files.")
        
        self.call_files_listbox = tk.Listbox(self.main_frame, selectmode=tk.SINGLE, font=("Arial", 12))
        self.call_files_listbox.grid(row=1, column=1, padx=5, pady=5, sticky="nsew")
        self.call_files_listbox.bind("<<ListboxSelect>>", self.show_call_details)
        ToolTip(self.call_files_listbox, "Select a file to view its details.")
        
        self.details_label = ttk.Label(self.main_frame, text="Details of Selected XML File", font=("Arial", 14))
        self.details_label.grid(row=2, column=0, columnspan=2, padx=5, pady=5, sticky="w")
        ToolTip(self.details_label, "Displays details of the selected XML file.")
        
        self.details_text = scrolledtext.ScrolledText(self.main_frame, height=10, font=("Arial", 12))
        self.details_text.grid(row=3, column=0, columnspan=2, padx=5, pady=5, sticky="nsew")
        ToolTip(self.details_text, "Details of the selected XML file will be displayed here.")
        
        self.button_frame = ttk.Frame(self)
        self.button_frame.pack(pady=10)

        self.refresh_button = ttk.Button(self.button_frame, text="Refresh Calls", command=self.refresh_calls)
        self.refresh_button.grid(row=0, column=0, padx=5)
        ToolTip(self.refresh_button, "Click to refresh the list of active calls.")
        
        self.delete_button = ttk.Button(self.button_frame, text="Delete XML", command=self.delete_selected_call)
        self.delete_button.grid(row=0, column=1, padx=5)
        ToolTip(self.delete_button, "Click to delete the selected XML file.")
        
        self.main_frame.columnconfigure(0, weight=1)
        self.main_frame.columnconfigure(1, weight=1)
        self.main_frame.rowconfigure(1, weight=1)
        self.main_frame.rowconfigure(3, weight=1)
        
        # Set up auto-refresh
        self.auto_refresh_interval = 60000  # 60 seconds
        self.auto_refresh()
        
        # Handle window close event to ensure graceful shutdown
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
    
    def auto_refresh(self):
        self.refresh_calls()
        self.after(self.auto_refresh_interval, self.auto_refresh)
    
    def refresh_calls(self):
        with data_lock:
            xml_files = [os.path.join(config['xml_directory'], f) for f in os.listdir(config['xml_directory']) if f.endswith('.xml')]
            xml_files.sort()  # Ensure files are processed in order
            calls, _ = process_xml_files(xml_files)
        
        self.call_listbox.delete(0, tk.END)
        for call in calls:
            for call_number in call['call_numbers']:
                self.call_listbox.insert(tk.END, call_number)
        
        # Clear the associated files and details if no calls
        if not calls:
            self.call_files_listbox.delete(0, tk.END)
            self.clear_details()
    
    def show_call_files(self, event):
        try:
            selected_call = self.call_listbox.get(self.call_listbox.curselection())
            self.call_files_listbox.delete(0, tk.END)
            
            for xml_file in os.listdir(config['xml_directory']):
                if xml_file.endswith('.xml'):
                    file_path = os.path.join(config['xml_directory'], xml_file)
                    call_number, _, _, _, _, _, _, _, _, _ = extract_xml_data(file_path)
                    if call_number == selected_call:
                        self.call_files_listbox.insert(tk.END, xml_file)
        except tk.TclError:
            pass  # No selection, do nothing
    
    def show_call_details(self, event):
        try:
            selected_file = self.call_files_listbox.get(self.call_files_listbox.curselection())
            file_path = os.path.join(config['xml_directory'], selected_file)
            self.display_file_details(file_path)
        except tk.TclError:
            pass  # No selection, do nothing
    
    def display_file_details(self, file_path):
        call_number, close_datetime, agency_contexts, root, location, nature_of_call, unit_details, primary_unit, latitude, longitude = extract_xml_data(file_path)
        details = f"Call Number: {call_number}\n"
        details += f"Close DateTime: {close_datetime}\n"
        details += f"Location: {location}\n"
        details += f"Latitude: {latitude}\n"
        details += f"Longitude: {longitude}\n"
        details += f"Nature of Call: {nature_of_call}\n"
        details += f"Call Type: {self.get_call_type(agency_contexts) if agency_contexts else 'Unknown'}\n"
        details += "Agency Contexts:\n"
        if agency_contexts:
            for context in agency_contexts:
                details += f"  Agency Type: {context['agency_type']}\n"
        details += "\nUnit Details:\n"
        for unit in unit_details:
            details += f"  Unit ID: {unit['unit_id']}\n"
            details += f"  Unit Type: {unit['unit_type']}\n"
            details += f"  Enroute Time: {unit['enroute_time']}\n"
            details += f"  Arrive Time: {unit['arrive_time']}\n"
            details += f"  Clear Time: {unit['clear_time']}\n"
        self.details_text.delete(1.0, tk.END)
        self.details_text.insert(tk.END, details)
    
    def get_call_type(self, agency_contexts):
        for context in agency_contexts:
            if context['agency_type'] == 'fire':
                return 'Fire'
            elif context['agency_type'] == 'ems':
                return 'EMS'
            elif context['agency_type'] == 'police':
                return 'Police'
        return 'Unknown'
    
    def delete_selected_call(self):
        try:
            selected_file = self.call_files_listbox.get(self.call_files_listbox.curselection())
            file_path = os.path.join(config['xml_directory'], selected_file)
            
            if messagebox.askyesno("Delete XML", f"Are you sure you want to delete the XML file {selected_file}?"):
                os.remove(file_path)
                self.call_files_listbox.delete(self.call_files_listbox.curselection())
                self.refresh_calls()
                self.clear_details()  # Clear details text box
        except tk.TclError:
            pass  # No selection, do nothing
    
    def clear_details(self):
        self.details_text.delete(1.0, tk.END)
    
    def on_closing(self):
        # Gracefully stop the monitoring threads
        stop_monitoring()
        self.destroy()

if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    start_monitoring()
    
    # GUI
    gui = XMLProcessorGUI()
    gui.mainloop()
    
    stop_monitoring()

