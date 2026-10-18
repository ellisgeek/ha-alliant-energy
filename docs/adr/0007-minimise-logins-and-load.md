# Keep logins rare and requests gentle

The portal locks an IP after repeated failed logins (`SetIpLockCount`), and
every successful login is recorded as a device on the account. The integration
logs in once per Home Assistant run, then renews the session with the refresh
token on HTTP 401. It polls once a day by default; an option allows 3 to 24
hours. After the first import, updates fetch only new hours, and the first
import fetches history in chunks with a delay between requests. After a failed
login it goes to reauthentication and does not retry the password.

## Consequences

The update interval cannot go below 3 hours, and removing the backfill delay
increases the chance that the account gets locked or flagged. Hourly data is a
day or two behind, so polling more often gives no new usage data, only fresher
bill sensors.
