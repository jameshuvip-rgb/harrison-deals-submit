# Notion schema note (Submitter)

The auth form always writes `submitter` as the logged-in owner email (not a free-text handle).

On the Harrison Deals Notion database used for Pending intake:

- Keep / ensure a **Submitter** property (text / rich text is fine).
- Store the owner email there when draining `inbox/*.json` (`payload["submitter"]`).
- No other schema change is required for this MVP.
- Optional later: a `local_id` or last inbox event id can be stored if you want to map edits/deletes back to an existing Notion page. Inbox events include `action` (`create` | `update` | `delete`) and `local_id`.
