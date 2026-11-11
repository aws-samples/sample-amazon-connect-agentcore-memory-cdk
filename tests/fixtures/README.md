# Test fixtures

`cw_log_events.json` and `trace_events.json` follow the shape of Amazon Connect AI agent
trace events in CloudWatch Logs: field names, encodings (for example `values` as a JSON
string and `span` as a string) and relationships such as `tool_use_id` matching. All
identifiers and content are synthetic (for example `123456789012` and
`aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee`). Only the tests read these files.
