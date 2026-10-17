# Price hourly cost with a marginal rate parsed from the bill PDF

The usage API gives cost only per billing cycle, rounded to whole dollars, and
reports hourly and daily cost as zero. To produce a cost statistic, the
integration multiplies hourly usage by one rate per service, picked in this
order:

1. a **static rate** the person sets in the options;
2. the **marginal rate** parsed from the latest bill PDF (`bill/GetBillPdf`,
   parsed with `pdfplumber`, cached per invoice);
3. the **effective rate** per billing cycle (bill amount divided by usage).

The marginal rate is preferred because it is what an extra kWh actually costs.
The effective rate spreads fixed customer charges and fees over usage, so it
overstates the cost of each hour (about $0.22/kWh effective against $0.17/kWh
marginal on a residential RG-1 bill).

## Consequences

The cost statistic is lower than the bill total because fixed charges are left
out. The bill total is shown by the bill summary sensors instead. PDF parsing
depends on the layout and is best-effort: on any failure it falls back to the
effective rate and never fails the import.
