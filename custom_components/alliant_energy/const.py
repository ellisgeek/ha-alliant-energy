"""Integration constants."""

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

# Optional limits (integration options): how much hourly history to import and
# how often to update. Changing the history limit rebuilds every meter once
# (ADR 0011); the interval floor keeps the load on the account low (ADR 0007).
CONF_MAX_HISTORY_DAYS = "max_history_days"
CONF_UPDATE_INTERVAL_HOURS = "update_interval_hours"
MIN_HISTORY_DAYS = 30
DEFAULT_MAX_HISTORY_DAYS = 3650  # also the largest allowed
MIN_UPDATE_INTERVAL_HOURS = 3
MAX_UPDATE_INTERVAL_HOURS = 24
# Hourly data lags a day or two, so updating more than daily gains little.
DEFAULT_UPDATE_INTERVAL_HOURS = 24

# Alliant bills in US dollars; electric is kWh and gas is therms.
CURRENCY_USD = "USD"

# Pause between consecutive usage requests, to keep the load gentle (ADR 0007).
REQUEST_PAUSE_SECONDS = 0.5

# Long-term statistics (Energy dashboard) settings.
# A meter's first import walks back through the API in chunks until this many
# chunks in a row come back empty (the start of its history), but never further
# back than the history limit (CONF_MAX_HISTORY_DAYS).
STATISTICS_CHUNK_DAYS = 30
STATISTICS_EMPTY_CHUNKS = 2
# AMI hourly data lags a couple of days; don't bother requesting the newest days.
STATISTICS_LAG_DAYS = 1
