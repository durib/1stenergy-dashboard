import os
import time
import pandas as pd
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS
from datetime import datetime, timezone, timedelta

# ==== CONFIGURATION ====
excel_file = os.getenv("EXCEL_FILE", "SolarEstimate.xlsx")
value_column = "AC System Output (W)"
day_col = "Day"
month_col = "Month"
hour_col = "Hour"
measurement_name = "solar_output"

# ==== ENV VARIABLES ====
energy_start = os.getenv("ENERGY_START", "2024-01-01")
start_date = datetime.strptime(energy_start, "%Y-%m-%d")

url = os.getenv("INFLUXDB_URL", "http://influxdb:8086")
org = os.getenv("INFLUXDB_ORG", "default-org")
bucket = os.getenv("INFLUXDB_BUCKET", "solar-estimate")
token = os.getenv("INFLUXDB_TOKEN")

print(f"⚡ ENERGY_START = {start_date.date()}")
print(f"🌍 InfluxDB URL: {url}, Org: {org}, Bucket: {bucket}")

if not token:
    raise RuntimeError("❌ INFLUXDB_TOKEN not set in environment")

# ==== WAIT FOR INFLUXDB TO BE READY ====
for attempt in range(10):
    try:
        client = InfluxDBClient(url=url, token=token, org=org)
        if client.ping():
            print("✅ Connected to InfluxDB")
            break
    except Exception:
        print("⏳ Waiting for InfluxDB to be ready...")
        time.sleep(3)
else:
    raise RuntimeError("❌ Cannot connect to InfluxDB after multiple attempts")

write_api = client.write_api(write_options=SYNCHRONOUS)

# ==== LOAD ALL SHEETS FROM ROW 32 ====
dfs = pd.read_excel(excel_file, sheet_name=None, skiprows=31)

total_points = 0

# ==== PROCESS EACH SHEET ====
for sheet_name, df in dfs.items():
    print(f"Processing sheet: {sheet_name}")

    # Ensure numeric values
    df[value_column] = pd.to_numeric(df[value_column], errors="coerce")
    df[day_col] = pd.to_numeric(df[day_col], errors="coerce")
    df[month_col] = pd.to_numeric(df[month_col], errors="coerce")
    df[hour_col] = pd.to_numeric(df[hour_col], errors="coerce")

    # Drop rows with NaN in important columns
    df = df.dropna(subset=[value_column, day_col, month_col, hour_col])

    # ---- TIMEZONE HANDLING ----
    # Assuming data is in AEST (UTC+10)
    aest_fixed = timezone(timedelta(hours=10))

    points = []

    for _, row in df.iterrows():
        try:
            month = int(row[month_col])
            day = int(row[day_col])
            hour = int(row[hour_col])
            ac_output = float(row[value_column])

            # ---- YEAR CALCULATION ----
            year = start_date.year
            if (month, day) < (start_date.month, start_date.day):
                year += 1

            timestamp = datetime(year=year, month=month, day=day, hour=hour, tzinfo=aest_fixed)
            timestamp = timestamp.astimezone(timezone.utc)

            point = (
                Point(measurement_name)
                .tag("sheet", sheet_name)
                .field("ac_output_watts", ac_output)
                .time(timestamp, WritePrecision.S)
            )
            points.append(point)

        except Exception as e:
            print(f"Skipping row {row.to_dict()} due to error: {e}")

    if points:
        write_api.write(bucket=bucket, org=org, record=points)
        total_points += len(points)

print(f"✅ Wrote {total_points} points to InfluxDB (across {len(dfs)} sheets)")
