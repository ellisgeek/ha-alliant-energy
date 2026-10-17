# Keep logins rare and requests gentle

The portal locks an IP after repeated failed logins (`SetIpLockCount`), and
every successful login is recorded as a device on the account. The integration
logs in once per Home Assistant run, then renews the session with the refresh
token on HTTP 401. It polls every 6 hours, and on first run it backfills
history in chunks with a delay between requests. After a failed login it goes
to reauthentication and does not retry the password.

## Consequences

Lowering `UPDATE_INTERVAL` or removing the backfill delay increases the chance
that the account gets locked or flagged. Hourly data is about two days behind,
so polling more often gives no new data.
