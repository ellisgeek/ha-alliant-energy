# Test fixtures

## `bill_2026_10.pdf` and `bill_2026_10.txt`

A real Alliant bill (October 2026, electric and gas), made safe to publish:

- Personal details are replaced: name, address, email, account and meter
  numbers, the payment barcode, and the document id.
- Usage, meter readings, rates, and dates are made up. Dates moved +117 days,
  so the bill is dated Feb 07, 2027 and its short electric section crosses the
  new year. Every charge, tax base, and total is recomputed from the fake
  values. The state (WI), the temperature, and the degree days are unchanged.
- The PDF has all images removed (logos, QR code, usage charts), the five
  image-only insert pages dropped, and its metadata cleared.

`bill_2026_10.txt` is the text pdfplumber extracted from the original PDF,
changed the same way. pdfplumber orders a few blocks of the cleaned PDF
differently, so the two files give the parser two real layouts, and
`test_pdf_end_to_end_matches_the_text_layer` checks both parse to the same
bill.

## Making a new bill fixture

The tool used for this is not in the repo, because its replacement table holds
real personal data. To make another fixture:

1. Download the bill PDF (see `docs/backend-api.md`, Bills) and keep it outside
   the repo.
2. With PyMuPDF (`uv run --with pymupdf`, not a project dependency), for each
   page: delete every image, then search for each value to replace and add a
   redaction annotation over each match. Process longer strings first, and skip
   matches inside an area already redacted (the account number also appears
   inside the payment barcode). Limit short numbers (such as a usage of `93`)
   to their table row: find the row's text, and search only a full-width band
   at that height.
3. Apply the redactions, then write each replacement with `insert_text` at the
   redacted spot. Text passed to the redaction itself is dropped silently when
   it does not fit, and a wider box erases neighbouring text. Do not write a
   replacement for vertical text, such as the document id on the page edge.
4. Drop image-only pages, clear the metadata (`set_metadata({})`,
   `del_xml_metadata()`), and save with `garbage=4, deflate=True, clean=True`
   so no deleted text or images stay in the file.
5. Check the result: search the pdfplumber text and every decompressed object
   and stream for each original value, and render every page and look at it.
   Personal text drawn as vector outlines would pass the text checks.
