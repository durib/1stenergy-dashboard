import os
import time
import logging
import schedule
import requests
from datetime import datetime, timedelta
from influxdb_client import InfluxDBClient
from influxdb_client.client.write_api import SYNCHRONOUS

# Convert the string to a boolean
def str_to_bool(value):
    return value.lower() in ('true', '1', 't', 'y', 'yes')

# Determine if the given date and time is during peak or off-peak hours.
def get_peak_status(date_time):
    # Convert input to datetime object if it's not already
    if isinstance(date_time, str):
        date_time = datetime.strptime(date_time, '%Y-%m-%d %H:%M:%S')
    
    day_of_week = date_time.weekday()
    hour = date_time.hour
    
    if day_of_week >= 5: return "Off-peak"
    if (7 <= hour < 10) or (16 <= hour < 21): return "Peak"
    return "Off-peak"

# get 1stenergy token using login
def energy_get_token(login):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://portal.1stenergy.com.au/",
        "Referer": "https://portal.1stenergy.com.au/"
    }
    response = requests.post(
        "https://portal-api.1stenergy.com.au/api/users/validate-user",
        json=login,
        headers=headers
    )
    response.raise_for_status()
    return response.json()["result"]["token"]


# get the first 1stenergy utilityAccountId
def energy_get_account(token):
    headers = {
        "Authorization": f"Bearer {token}",
        "Brand-Code": "FIRST"
    }
    response = requests.get(
        "https://portal-api.1stenergy.com.au/api/users/accounts",
        headers=headers
    )
    response.raise_for_status()
    return response.json()[0]["properties"][0]["utilityServices"][0]["utilityAccountId"]


# get 1stenergy usage data
def energy_get_data(account, token, date):
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "year": date.year,
        "month": date.month,
        "day": date.day,
        "productType": "POWER",
        "viewInterval": "day",
        "viewMode": "USAGE"
    }
    url = f"https://portal-api.1stenergy.com.au/api/utility/{account}/usage-chart?"
    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status()
    return response.json()

# get 1stenergy usage data
def energy_get_offerings(account, token):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://portal-api.1stenergy.com.au/api/product-offerings/{account}?"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

# Convert JSON data to InfluxDB energy points
def energy_to_points(data):
    data_points = []
    for series in data["series"]:
        for reading in series["data"]:
            reading_time = datetime.strptime(f"{data['date']}T{reading['category']}:00+10:00", '%Y-%m-%dT%H:%M:%S%z')
            point = {
                "measurement": "electricity",
                "tags": {"tariff": series["name"], "timeofuse": get_peak_status(reading_time)},
                "fields": {"value": reading["value"]},
                "time": reading_time
            }
            data_points.append(point)
    return data_points

# Convert JSON data to InfluxDB offerings points
def offerings_to_points(data):
    data_points = []
    for rate in data["rates"]:
        for h in range(24):
            point = {
                "measurement": "cost",
                "tags": {"description": rate["description"]},
                "fields": {"value": float(rate["rate"])/2400},
                "time": f"{data['date'] + timedelta(hours=h)}+10:00"
            }
            data_points.append(point)
    return data_points


# get oldest last record from all tables based on measurement
def influx_get_last(influx_client,measurement):
    query_api = influx_client.query_api()
    query = f'from(bucket: "{bucket}") |> range(start: 0) |> filter(fn: (r) => r._measurement == "{measurement}") |> last()'
    tables = query_api.query(query, org=org)
    return min((record.get_time() for table in tables for record in table.records), default=None)


def job():
    logger.info("Sync task is starting...")

    # Setup InfluxDB client
    influx_client = InfluxDBClient(
        url=os.getenv("INFLUXDB_URL","http://influxdb:8086"),
        token=os.getenv("INFLUXDB_TOKEN"),
        org=org,
        debug=str_to_bool(os.getenv("DEBUG", 'false'))
    )
    write_api = influx_client.write_api(write_options=SYNCHRONOUS)
    logger.info("Connecting to InfluxDB v2 on " + influx_client.url)

    energy_token = energy_get_token(energy_login)
    energy_account = energy_get_account(energy_token)
    last_time = influx_get_last(influx_client, "electricity")

    if last_time is None:
        next_date = start_date
        logger.info(f"No last date found, using start_date: {next_date.strftime('%Y-%m-%d')}")
    else:
        next_date = last_time + timedelta(hours=10) + timedelta(minutes=5)
        logger.info(f"Last date found, starting at: {next_date.strftime('%Y-%m-%d')}")

    # get data from 1stenergy
    while next_date.date() < datetime.now().date():
        logger.info(f"Getting energy data for: {next_date.date().strftime('%Y-%m-%d')}")
        energy_data = energy_get_data(energy_account, energy_token, next_date)
        energy_data["date"] = next_date.strftime("%Y-%m-%d")
        data_points = energy_to_points(energy_data)
        write_api.write(org=org, bucket=bucket, record=data_points)

        logger.info(f"Getting offerings data")
        offerings = energy_get_offerings(energy_account, energy_token)
        offerings["date"] = next_date
        data_points = offerings_to_points(offerings)
        write_api.write(org=org, bucket=bucket, record=data_points)

        next_date += timedelta(days=1)

    logger.info("Sync task complete.")


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler()  # Log to stdout
    ]
)
logger = logging.getLogger(__name__)
logger.info("Starting energy scraper...")

# Get environment variables
start_date = datetime.strptime(os.getenv("ENERGY_START"), "%Y-%m-%d")
bucket = os.getenv("INFLUXDB_BUCKET")
org = os.getenv("INFLUXDB_ORG")

# 1stenergy login credentials
energy_login = {
    "userName": os.getenv("ENERGY_USER"),
    "password": os.getenv("ENERGY_PASSWORD")
}
logger.info("Usign login details for " + os.getenv("ENERGY_USER"))

# Run job at startup
job()

# Schedule the job to run daily at 05:00
schedule.every().day.at("05:00").do(job)
while True:
    schedule.run_pending()
    logger.info("Sleeping for 1h")
    time.sleep(3600)
