# Alliant Energy

Terms used when reading electricity and gas usage, cost, and bills from an
Alliant Energy customer account and presenting them in Home Assistant.

## Customer and site

**Login**:
The username and password a person uses to sign in to the Alliant customer
portal. One login can reach several accounts.
_Avoid_: User, account (for the credentials)

**Account**:
An Alliant billing relationship, identified by an account number. Bills are
issued per account and combine every service on it.
_Avoid_: Customer, user

**Premise**:
The physical service address an account is billed for, identified by a premise
number. Usage is always requested for an account at a premise.
_Avoid_: Address, location, site

**Meter**:
A metering point at a premise that measures one service. A premise usually has
one electric meter and, optionally, one gas meter.

**Service**:
The kind of energy a meter measures: electric or gas.
_Avoid_: Fuel, commodity, utility type

## Usage

**Usage reading**:
The amount of a service consumed over one interval, in the service's native
unit (kWh for electric, therms for gas).
_Avoid_: Consumption record, sample

**Periodicity**:
The interval length of usage readings: hourly, daily, or billing cycle.
_Avoid_: Granularity, resolution

**Billing cycle**:
The span of days one bill covers for a service. Billing cycles do not align
with calendar months.
_Avoid_: Month, billing period (use "billing cycle")

**Therm**:
The unit Alliant bills gas in, equal to 29.3001 kWh of energy. Gas meters
measure volume (CCF); a heat factor on the bill converts it to therms.

**Data lag**:
The delay, about two days, between when energy is used and when its hourly
usage readings become available.

## Bills and cost

**Bill**:
The statement Alliant issues to an account once per billing cycle, with an
amount due, a due date, and itemized charges for every service.
_Avoid_: Statement

**Invoice**:
The identifier of one bill, used to retrieve its itemized PDF.

**Bill summary**:
The headline figures of the most recent bill: amount due, current charges,
past due amount, previous balance, due date, and invoice date.

**Itemized bill**:
What a bill's PDF says: each service's charges, rates, billing cycle, and
meter readings, plus the next meter reading window and the weather. A bill has
a bill summary and, when its PDF can be read, an itemized bill.
_Avoid_: Bill PDF (for the parsed content), statement

**Rate schedule**:
The published tariff a service is billed under (for example RG-1 for
residential electric, GG-1 for residential gas).
_Avoid_: Plan, tariff code

**Customer charge**:
A fixed daily charge for a service that does not depend on usage.
_Avoid_: Basic charge, service fee

**Marginal rate**:
The price of one more unit of a service: the sum of the usage-proportional
charges on a bill (for electric, energy charge plus fuel adjustment; for gas,
distribution plus supply charges). Excludes customer charges, fees, and taxes.
_Avoid_: Energy rate, unit price

**Effective rate**:
The amount billed for a service in a billing cycle divided by the usage in that
cycle. Includes fixed charges, so it is higher than the marginal rate.
_Avoid_: Blended rate, average rate

**Static rate**:
A price per unit the person sets themselves, overriding both the marginal and
the effective rate.
_Avoid_: Fixed rate, manual rate

**Rate card**:
The set of marginal rates and customer charges read from one bill.

**Cost rate**:
The rate that prices one hour of a cost statistic, chosen from the static,
marginal, and effective rate. A static rate wins for every hour; otherwise a
bill's marginal rate covers its billing cycle and later hours, and older hours
use their own billing cycle's effective rate.
_Avoid_: Price, tariff

## Home Assistant presentation

**Usage statistic**:
The hourly history of one meter's usage kept in Home Assistant's long-term
statistics, which the Energy dashboard reads.
_Avoid_: Energy sensor, history

**Cost statistic**:
The hourly history of one meter's estimated cost, paired with its usage
statistic.

**Backfill**:
The first import of all the hourly usage history Alliant has for a meter.
_Avoid_: Initial sync, history load

**Two-factor challenge**:
A one-time code Alliant sends by text or email to confirm a sign-in.
_Avoid_: MFA, TFA, OTP (in prose)
