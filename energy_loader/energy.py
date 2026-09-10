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

# 1stenergy mobile-app API, behind Azure Front Door
MOBILE_API = "https://endpoint-firstenergy-mobileapp-prod-dchufubea3frdfhc.a01.azurefd.net"


# session for the mobile API, so the load balancer cookies are kept across calls
def energy_mobile_session():
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})
    return session


# get the short-lived Azure bearer that the mobile API login call requires
def energy_get_bff_token(session):
    response = session.post("https://myaccount.1stenergy.com.au/api/GetBffToken")
    response.raise_for_status()
    return response.text.strip()


# log in to the mobile API, returns the access and refresh tokens
def energy_mobile_login(session, bff_token, login):
    headers = {
        "Authorization": f"Bearer {bff_token}",
        "Content-Type": "application/json"
    }
    response = session.post(f"{MOBILE_API}/v1/auth/login", json=login, headers=headers)
    response.raise_for_status()
    result = response.json()
    return result["access_token"], result["refresh_token"]


# hold both tokens together: the Azure bearer lives 3900s and the access token 86400s,
# so a multi-year backfill outlives the bearer and has to re-mint it mid-run
BFF_REFRESH_AFTER = 3000


def energy_authenticate(session, login):
    bff_token = energy_get_bff_token(session)
    logger.info("Got BFF token, logging in to the mobile API")
    access_token, refresh_token = energy_mobile_login(session, bff_token, login)
    return {
        "bff_token": bff_token,
        "bff_minted": time.time(),
        "access_token": access_token,
        "refresh_token": refresh_token
    }


# every data call needs the Azure bearer AND the access token, in separate headers;
# either one alone is rejected
def mobile_headers(session, auth):
    if time.time() - auth["bff_minted"] > BFF_REFRESH_AFTER:
        logger.info("BFF token is near expiry, minting a replacement")
        auth["bff_token"] = energy_get_bff_token(session)
        auth["bff_minted"] = time.time()
    return {
        "Authorization": f"Bearer {auth['bff_token']}",
        "Adaptor-Authorization": auth["access_token"]
    }


# get the 1stenergy accounts from the mobile API
def energy_get_accounts(session, auth, fuel_type="ELECTRICITY"):
    response = session.get(
        f"{MOBILE_API}/v1/energy/accounts",
        headers=mobile_headers(session, auth),
        params={"fuel-type": fuel_type}
    )
    response.raise_for_status()
    return response.json()["data"]["accounts"]


# get one account in full, including its plan and rate history
def energy_get_account_detail(session, auth, account_id):
    response = session.get(
        f"{MOBILE_API}/v1/accounts/{account_id}",
        headers=mobile_headers(session, auth)
    )
    response.raise_for_status()
    return response.json()["data"]


# pick the account to load, preferring the open one, newest first
def energy_select_account(accounts, account_id=None):
    if account_id:
        return next(a for a in accounts if a["accountId"] == account_id)
    open_accounts = [a for a in accounts if a["openStatus"] == "OPEN"]
    return max(open_accounts or accounts, key=lambda a: a["creationDate"])


# get the meter and register detail for one service point
def energy_get_service_point(session, auth, service_point_id):
    response = session.get(
        f"{MOBILE_API}/v1/electricity/servicepoints/{service_point_id}",
        headers=mobile_headers(session, auth)
    )
    response.raise_for_status()
    return response.json()["data"]


# get one window of interval usage; monthly gives a calendar month, otherwise a Mon-Sun week.
# costed swaps every value from kWh to dollars, so both passes are needed for a full picture
def energy_get_usage(session, auth, account_id, service_point_id, date,
                     monthly=True, costed=False):
    response = session.get(
        f"{MOBILE_API}/v1/electricity/account/{account_id}/usage/{service_point_id}/{date}",
        headers=mobile_headers(session, auth),
        params={"monthly": str(bool(monthly)).lower(), "costed": str(bool(costed)).lower()}
    )
    response.raise_for_status()
    return response.json()


# the window is nested under current_month or current_week depending on the monthly flag
def usage_window(payload):
    return payload.get("current_month") or payload.get("current_week") or {}


# interval markers are AEST all year, since NEM interval data does not observe daylight saving
def interval_time(marker):
    day = marker["yyyymmdd"]
    return f"{day[:4]}-{day[4:6]}-{day[6:]}T{marker.get('time', '00:00')}:00+10:00"


# the one non-solar key in categorised_total names the band the interval billed at
def interval_band(data):
    for name, entry in (data.get("categorised_total") or {}).items():
        if not entry.get("solar"):
            return name
    return None


# InfluxDB discards empty tag values, so leave them out rather than writing blanks
def clean_tags(tags):
    return {key: value for key, value in tags.items() if value}


# tags identifying which account and site a reading belongs to, so it can be filtered
def account_tags(account, service_point):
    return clean_tags({
        "account": account["accountNumber"],
        "servicepoint": service_point["servicePointId"],
        "site": service_point["siteAddress"]["addressLine1"]
    })


# the days to load for a service point, clamped to ENERGY_START and its own dates
def energy_account_window(service_point):
    window_start = max(datetime.strptime(service_point["startDate"], "%Y-%m-%d"), start_date)
    last_full_day = datetime.now().date() - timedelta(days=1)
    if service_point.get("endDate"):
        window_end = min(datetime.strptime(service_point["endDate"], "%Y-%m-%d").date(), last_full_day)
    else:
        window_end = last_full_day
    return window_start, window_end


# Convert a matched pair of usage windows to InfluxDB points. The uncosted pass carries
# kWh and the costed pass dollars, for the same intervals, so they are merged by slot.
def usage_to_points(uncosted, costed, tags):
    data_points = []
    costed_days = usage_window(costed).get("intervals") or {}

    for index, day in (usage_window(uncosted).get("intervals") or {}).items():
        costed_day = costed_days.get(index) or {}
        costed_slots = costed_day.get("intervals") or {}

        for slot, interval in (day.get("intervals") or {}).items():
            usage = interval.get("data") or {}
            spend = ((costed_slots.get(slot) or {}).get("data")) or {}
            data_points.append({
                "measurement": "electricity",
                "tags": clean_tags({**tags, "timeofuse": interval_band(usage)}),
                "fields": {
                    "consumption": float(usage.get("consumption") or 0),
                    "export": float(usage.get("export") or 0),
                    "cost": float(spend.get("consumption") or 0),
                    # export reads as a credit once costed, so store it as a positive amount
                    "credit": abs(float(spend.get("export") or 0))
                },
                "time": interval_time(interval["interval_range"]["starts_at"])
            })

        # days beyond the meter reads come back with null totals and no intervals
        totals = day.get("data") or {}
        if totals.get("total_spend") is None:
            continue
        costed_totals = costed_day.get("data") or {}
        data_points.append({
            "measurement": "daily",
            "tags": tags,
            "fields": {
                # solar_input and grid_spend are kWh uncosted and dollars costed
                "consumption": float(totals.get("grid_spend") or 0),
                "export": float(totals.get("solar_input") or 0),
                "cost": float(costed_totals.get("grid_spend") or 0),
                "credit": abs(float(costed_totals.get("solar_input") or 0)),
                "servicecharge": float(costed_totals.get("service_charge") or 0),
                "temperature": float(totals.get("temperature") or 0),
                "quality": (day.get("metadata") or {}).get("quality_flag") or ""
            },
            "time": interval_time(day["interval_range"]["starts_at"])
        })

    return data_points


# Convert service point metadata and its meter registers to InfluxDB points
def service_point_to_points(detail, tags):
    data_points = []
    valid_from = detail["validFromDate"]
    participants = {p["role"]: p["party"] for p in detail.get("relatedParticipants", [])}
    loss = detail.get("distributionLossFactor") or {}
    profile = detail.get("consumerProfile") or {}

    data_points.append({
        "measurement": "servicepoint",
        "tags": clean_tags({
            **tags,
            "nmi": detail["nationalMeteringId"],
            "jurisdiction": detail.get("jurisdictionCode"),
            "classification": detail.get("servicePointClassification"),
            "consumerclass": profile.get("classification")
        }),
        "fields": {
            "status": detail.get("servicePointStatus", ""),
            "threshold": profile.get("threshold", ""),
            "isgenerator": bool(detail.get("isGenerator")),
            "lossfactor": float(loss.get("lossValue", 0) or 0),
            "lossfactorcode": loss.get("code", ""),
            "lnsp": participants.get("LNSP", ""),
            "frmp": participants.get("FRMP", "")
        },
        "time": f"{valid_from}T00:00:00+10:00"
    })

    # one point per register, so the network tariff each one bills against is queryable
    for meter in detail.get("meters", []):
        spec = meter.get("specifications") or {}
        effective = meter.get("fromDate") or valid_from
        for register in meter.get("registers", []):
            data_points.append({
                "measurement": "meter",
                "tags": clean_tags({
                    **tags,
                    "meter": meter["meterId"],
                    "register": register["registerId"],
                    "suffix": register["registerSuffix"],
                    "networktariff": register.get("networkTariffCode"),
                    "consumptiontype": register.get("registerConsumptionType")
                }),
                "fields": {
                    "averagedailyload": float(register.get("averagedDailyLoad", 0) or 0),
                    "multiplier": float(register.get("multiplier", 0) or 0),
                    "meterstatus": spec.get("status", ""),
                    "registerstatus": register.get("status", ""),
                    "readtype": spec.get("readType", ""),
                    "installationtype": spec.get("installationType", ""),
                    "timeofday": register.get("timeOfDay", ""),
                    "controlledload": bool(register.get("controlledLoad"))
                },
                "time": f"{effective}T00:00:00+10:00"
            })
    return data_points


# tariffPeriod carries MM-DD only, so the year comes from the plan that contains it
def resolve_period_date(month_day, plan_start, plan_end):
    for year in (plan_start.year, plan_end.year):
        period_date = datetime.strptime(f"{year}-{month_day}", "%Y-%m-%d")
        if plan_start <= period_date <= plan_end:
            return period_date
    return None


# a rate point per band, effective from the start of the period. A block holds its rates
# under timeOfUseRates, singleRate or timeVaryingTariffs depending on the tariff shape,
# so take whichever is present rather than assuming time of use.
RATE_BLOCK_KEYS = ("timeOfUseRates", "singleRate", "timeVaryingTariffs")


def rate_points(block, effective, tags, default_band=None):
    data_points = []
    bands = []
    for key in RATE_BLOCK_KEYS:
        found = block.get(key)
        if found:
            bands = found if isinstance(found, list) else [found]
            break
    for band in bands:
        label = band.get("displayName") or block.get("displayName")
        for rate in band.get("rates") or []:
            if rate.get("unitPrice") is None:
                continue
            # a feed-in tariff is quoted negative; store the magnitude so it reads
            # alongside the usage rates, with tariffclass telling them apart
            price = abs(float(rate["unitPrice"]))
            stamp = f"{effective.strftime('%Y-%m-%d')}T00:00:00+10:00"

            # measureUnit is the only dependable divider here: one plan quotes its
            # supply charge as a rate block of its own rather than a dailySupplyCharge
            # attribute, and it is per DAYS where every usage and feed-in rate is per KWH
            if (rate.get("measureUnit") or "").upper() == "DAYS":
                data_points.append({
                    "measurement": "supply",
                    "tags": clean_tags({**tags, "tariff": label}),
                    "fields": {"dailycharge": price},
                    "time": stamp
                })
                continue

            data_points.append({
                "measurement": "rate",
                "tags": clean_tags({
                    **tags,
                    "tariff": label,
                    # a single rate carries no band, so fall back to the caller's label
                    "timeofuse": band.get("type") or default_band
                }),
                "fields": {"unitprice": price},
                "time": stamp
            })
    return data_points


# Convert a plan's rate history to InfluxDB points, clamped to the service point window
def plan_to_points(plan, tags, window_start, window_end):
    data_points = []
    overview = plan["planOverview"]
    plan_start = datetime.strptime(overview["startDate"], "%Y-%m-%d")
    plan_end = datetime.strptime(overview["endDate"], "%Y-%m-%d")
    contract = plan["planDetail"]["electricityContract"]
    plan_tags = {
        **tags,
        "plan": overview["displayName"],
        "pricingmodel": contract["pricingModel"]
    }

    # only keep the price applicable from the first day we hold usage for
    def effective_from(start, end):
        if end is not None and end.date() < window_start.date(): return None
        if start.date() > window_end: return None
        return max(start, window_start)

    # retail service, the tariff periods that also carry the daily supply charge
    for period in contract.get("tariffPeriod") or []:
        start = resolve_period_date(period["startDate"], plan_start, plan_end)
        end = resolve_period_date(period["endDate"], plan_start, plan_end)
        if start is None: continue
        effective = effective_from(start, end)
        if effective is None: continue
        period_tags = {**plan_tags, "tariffclass": period.get("type")}
        data_points += rate_points(period, effective, period_tags)
        # only some periods quote a supply charge, and one plan puts it on a period of
        # its own with no usage rate at all
        if period.get("dailySupplyCharge") is not None:
            data_points.append({
                "measurement": "supply",
                "tags": clean_tags({**period_tags, "tariff": period.get("displayName")}),
                "fields": {"dailycharge": float(period["dailySupplyCharge"])},
                "time": f"{effective.strftime('%Y-%m-%d')}T00:00:00+10:00"
            })

    # controlled load, whose periods carry full dates rather than MM-DD
    for load in contract.get("controlledLoad") or []:
        start = datetime.strptime(load["startDate"], "%Y-%m-%d")
        end = datetime.strptime(load["endDate"], "%Y-%m-%d") if load.get("endDate") else None
        effective = effective_from(start, end)
        if effective is None: continue
        data_points += rate_points(load, effective, {**plan_tags, "tariffclass": "CONTROLLED_LOAD"})

    # solar feed-in, quoted with full dates like controlled load
    for feedin in contract.get("solarFeedInTariff") or []:
        start = datetime.strptime(feedin["startDate"], "%Y-%m-%d")
        end = datetime.strptime(feedin["endDate"], "%Y-%m-%d") if feedin.get("endDate") else None
        effective = effective_from(start, end)
        if effective is None: continue
        data_points += rate_points(
            feedin, effective, {**plan_tags, "tariffclass": "SOLAR_FEED_IN"}
        )

    # discounts apply across the whole plan, and the key can be present but null
    for discount in contract.get("discounts") or []:
        if discount.get("methodUType") != "percentOfUse": continue
        effective = effective_from(plan_start, plan_end)
        if effective is None: continue
        data_points.append({
            "measurement": "discount",
            "tags": {
                **plan_tags,
                "description": discount["displayName"],
                "type": discount["type"]
            },
            "fields": {"percentofuse": float(discount["percentOfUse"]["rate"])},
            "time": f"{effective.strftime('%Y-%m-%d')}T00:00:00+10:00"
        })

    return data_points


# get oldest last record from all tables based on measurement, for one account
def influx_get_last(influx_client, measurement, account=None):
    query_api = influx_client.query_api()
    query = f'from(bucket: "{bucket}") |> range(start: 0) |> filter(fn: (r) => r._measurement == "{measurement}")'
    if account:
        query += f' |> filter(fn: (r) => r.account == "{account}")'
    query += ' |> last()'
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

    mobile_session = energy_mobile_session()
    auth = energy_authenticate(mobile_session, energy_login)
    logger.info("Mobile API login succeeded")

    accounts = energy_get_accounts(mobile_session, auth)
    if os.getenv("ENERGY_ACCOUNT"):
        accounts = [energy_select_account(accounts, os.getenv("ENERGY_ACCOUNT"))]
    logger.info(f"Found {len(accounts)} account(s) to load")

    # load every account, tagged so each can be filtered on its own
    for account in accounts:
        detail = energy_get_account_detail(mobile_session, auth, account["accountId"])
        for service_point in detail["servicePoints"]:
            sync_meters(write_api, mobile_session, auth, detail, service_point)
            sync_plans(write_api, detail, service_point)
            sync_usage(influx_client, write_api, mobile_session, auth, detail, service_point)

    logger.info("Sync task complete.")


# write the service point metadata and its meter registers
def sync_meters(write_api, session, auth, account, service_point):
    tags = account_tags(account, service_point)
    detail = energy_get_service_point(session, auth, service_point["servicePointId"])
    data_points = service_point_to_points(detail, tags)
    logger.info(
        f"NMI {detail['nationalMeteringId']}, "
        f"{len(detail.get('meters', []))} meter(s), {len(data_points) - 1} register(s)"
    )
    write_api.write(org=org, bucket=bucket, record=data_points)


# write the rate history for every plan that covers this service point
def sync_plans(write_api, account, service_point):
    tags = account_tags(account, service_point)
    window_start, window_end = energy_account_window(service_point)
    data_points = []
    for plan in account.get("plans", []):
        if service_point["servicePointId"] not in plan.get("servicePointIds", []):
            continue
        data_points += plan_to_points(plan, tags, window_start, window_end)
    logger.info(f"Writing {len(data_points)} rate history points")
    if data_points:
        write_api.write(org=org, bucket=bucket, record=data_points)


# 1stenergy serves a hard 500 for the occasional month (2024-06 on service point 530720),
# and a transient 5xx is always possible, so retry briefly and then move on without it
USAGE_ATTEMPTS = 3


# fetch the costed and uncosted passes for one window, or None if it cannot be had
def energy_get_usage_pair(session, auth, account_id, service_point_id, date):
    for attempt in range(1, USAGE_ATTEMPTS + 1):
        try:
            return (
                energy_get_usage(session, auth, account_id, service_point_id, date,
                                 monthly=True, costed=False),
                energy_get_usage(session, auth, account_id, service_point_id, date,
                                 monthly=True, costed=True)
            )
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else None
            # a window outside the service point's life is a definite no, not a fault
            if status == 403:
                logger.warning(f"Usage forbidden for {date}, outside this service point")
                return None
            if status is None or status < 500:
                raise
            if attempt < USAGE_ATTEMPTS:
                logger.warning(f"Server error {status} for {date}, retrying ({attempt})")
                time.sleep(5 * attempt)
                continue
            logger.error(f"Server error {status} for {date} after {attempt} tries, skipping")
            return None


# the first of the month after the one the given date falls in
def next_month(date):
    return (date.replace(day=28) + timedelta(days=4)).replace(day=1)


# load one service point's interval usage, picking up where its own tags left off
def sync_usage(influx_client, write_api, session, auth, account, service_point):
    tags = account_tags(account, service_point)
    window_start, window_end = energy_account_window(service_point)
    logger.info(
        f"Account {tags['account']} / service point {tags['servicepoint']} ({tags['site']}), "
        f"window {window_start.strftime('%Y-%m-%d')} to {window_end.strftime('%Y-%m-%d')}"
    )

    last_time = influx_get_last(influx_client, "electricity", tags["account"])
    if last_time is None:
        resume = window_start
        logger.info(f"No last date found, using window start: {resume.strftime('%Y-%m-%d')}")
    else:
        # InfluxDB hands back an aware UTC time; shift it to AEST wall clock and drop
        # the zone so it compares with the naive window dates
        resume = (last_time + timedelta(hours=10)).replace(tzinfo=None)
        logger.info(f"Last date found, starting at: {resume.strftime('%Y-%m-%d')}")

    # a monthly window returns a whole calendar month of half-hourly intervals in one
    # call, so refetch from the first of the resume month and let the writes overwrite
    skipped = []
    month = max(resume, window_start).replace(day=1)
    while month.date() <= window_end:
        # a date earlier than the service point's own start is rejected outright, so
        # never ask before the window start even when rewinding to the first of a month.
        # The reply still covers the whole calendar month the date falls in.
        date = max(month, window_start).strftime("%Y-%m-%d")
        logger.info(f"Getting usage for: {month.strftime('%Y-%m')}")
        pair = energy_get_usage_pair(
            session, auth, account["accountId"], service_point["servicePointId"], date
        )
        if pair is None:
            skipped.append(month.strftime("%Y-%m"))
        elif not usage_window(pair[0]).get("metadata", {}).get("has_data"):
            logger.info("No data in this window")
        else:
            data_points = usage_to_points(pair[0], pair[1], tags)
            logger.info(f"Writing {len(data_points)} usage points")
            write_api.write(org=org, bucket=bucket, record=data_points)
        month = next_month(month)

    if skipped:
        logger.warning(f"Months the API would not serve: {', '.join(skipped)}")


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
