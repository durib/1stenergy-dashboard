import os
import time
import schedule
import requests
from datetime import datetime, timedelta
from influxdb_client import InfluxDBClient
from influxdb_client.client.write_api import SYNCHRONOUS

# Get environment variables
start_date = datetime.strptime(os.environ["ENERGY_START"], "%Y-%m-%d")
bucket = os.environ["INFLUXDB_BUCKET"]
org = os.environ["INFLUXDB_ORG"]

# Setup InfluxDB client
influx_client = InfluxDBClient(
    url=os.environ["INFLUXDB_URL"],
    token=os.environ["INFLUXDB_TOKEN"],
    org=org,
    debug=os.environ["DEBUG"]
)
write_api = influx_client.write_api(write_options=SYNCHRONOUS)

# 1stenergy login credentials
energy_login = {
    "userName": os.environ["ENERGY_USER"],
    "password": os.environ["ENERGY_PASSWORD"]
}


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


# Convert JSON data to InfluxDB data points
def convert_to_data_points(data):
    data_points = []
    for series in data["series"]:
        for reading in series["data"]:
            point = {
                "measurement": "electricity",
                "tags": {"tariff": series["name"]},
                "fields": {"value": reading["value"]},
                "time": f"{data['date']}T{reading['category']}:00+10:00"
            }
            data_points.append(point)
    return data_points


# get oldest last record from all tables based on measurement
def influx_get_last(measurement):
    query_api = influx_client.query_api()
    query = f'from(bucket: "{bucket}") |> range(start: 0) |> filter(fn: (r) => r._measurement == "{measurement}") |> last()'
    tables = query_api.query(query, org=org)
    return min((record.get_time() for table in tables for record in table.records), default=None)


def job():
    print("Task is running...")
    energy_token = energy_get_token(energy_login)
    energy_account = energy_get_account(energy_token)
    last_time = influx_get_last("electricity")

    if last_time is None:
        next_date = start_date
        print(f"No last date found, using start_date: {next_date.strftime('%Y-%m-%d')}")
    else:
        next_date = last_time + timedelta(hours=10) + timedelta(minutes=5)
        print(f"Last date found, starting at: {next_date.strftime('%Y-%m-%d')}")

    # get data from 1stenergy
    while next_date.date() < datetime.now().date():
        print(f"Getting energy data for: {next_date.date().strftime('%Y-%m-%d')}")
        energy_data = energy_get_data(energy_account, energy_token, next_date)
        energy_data["date"] = next_date.strftime("%Y-%m-%d")
        data_points = convert_to_data_points(energy_data)
        write_api.write(org=org, bucket=bucket, record=data_points)
        next_date += timedelta(days=1)


# Run job at startup
job()

# Schedule the job to run daily at 05:00
schedule.every().day.at("05:00").do(job)
while True:
    schedule.run_pending()
    time.sleep(3600)
