# QuickFile invoice audit connection

The ESCS service reads `QUICKFILE_API_TOKEN` from its Render environment. Never
commit the value. The current pilot token expires on 30 November 2026; renew it
in QuickFile and replace the Render value before expiry.

All routes require the existing `X-Connector-Key` header:

- `GET /quickfile/status`: perform a live one-invoice search and report connection verification, without returning invoice content.
- `GET /quickfile/invoices`: search by `invoice_number`, `date_from`, `date_to`, `status`, `offset` and `limit` (1–100).
- `GET /quickfile/invoices/{invoice_id}`: read invoice details including notes and line items. The ID is QuickFile's internal invoice ID, not the printed invoice number.

Search responses retain QuickFile's `count`, `paging` and `data`. Follow paging
until exhausted before claiming a complete audit. Date filters are issue dates,
not conveyance dates. No background polling, invoice creation, editing, deletion,
sending or payment operations are implemented. The adapter can only GET fixed
invoice paths at the official HTTPS API origin, with redirects disabled.

Do not register these financial reads on the existing unauthenticated MCP
endpoint. A separately authenticated tool connection is required for chat access.
The REST API currently does not document a read endpoint for invoice attachments;
booking-form attachment verification remains a separate check.

Run validation: `python -m unittest test_quickfile_read -v`.
