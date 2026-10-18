"""Integration constants."""

from datetime import timedelta

DOMAIN = "alliant_energy"

CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_ACCOUNT_ID = "account_id"
CONF_PREMISE_ID = "premise_id"

# Optional static price overrides (integration options). When unset, cost is
# derived from an effective rate per billing period (bill amount / consumption)
# for both electricity and gas.
CONF_ELECTRICITY_RATE = "electricity_rate"  # $/kWh
CONF_GAS_RATE = "gas_rate"  # $/therm

# Alliant bills in US dollars; electric is kWh and gas is therms.
CURRENCY_USD = "USD"

UPDATE_INTERVAL = timedelta(hours=6)

# Long-term statistics (Energy dashboard) settings.
# Hourly history to backfill on first run, fetched in chunks to stay gentle.
STATISTICS_BACKFILL_DAYS = 365
STATISTICS_CHUNK_DAYS = 30
# AMI hourly data lags a couple of days; don't bother requesting the newest days.
STATISTICS_LAG_DAYS = 1
