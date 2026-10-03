# Mobility Business Analyst Agent

A single analyst helps operations teams investigate market changes and prioritize zone/time-band experiments. It does not book trips, dispatch drivers, change incentives, import files, or contact people.

## Use the dashboard

Start the server, open the **Business Analyst Agent** tab (`业务分析 Agent`), and choose an end month, services, and an optional pickup borough. These are independent of the original dashboard filters. The analysis uses three consecutive calendar months ending in the selected month.

Two modes are intentionally separate:

- **Fixed decision brief:** local SQL and a deterministic report template. No model, API key, or custom-question interpretation. Works immediately after importing data.
- **AI business questions:** one model chooses evidence tools and writes an answer to a specific business question. Requires the server-side configuration below. The UI disables this mode's Run button when configuration is missing. Missing or inconsistent data stops analysis before any model request.

The header language selector switches between Chinese and English and remembers your preference. Fixed briefs are available in both languages; new AI answers follow the selected language. Existing AI answers retain their original language when switching, without another paid model call. Filters and drafts are retained during the language-switch reload.

Both modes expose aggregate evidence, methodology, source SQL and parameters. Download the report with its evidence as JSON.

## Enable AI questions

Set these environment variables **in the terminal that starts the server**:

```bash
source .venv/bin/activate
# Prompt for the key without displaying it or adding it to shell history:
read -s OPENAI_API_KEY
export OPENAI_API_KEY
# Replace this placeholder with a Responses API tool-capable model available to your API project.
export TAXI_AGENT_MODEL='your-model-id'
TAXI_MEMORY_LIMIT=8GB TAXI_THREADS=4 python -m taxi serve --port 8001
```

Stop any existing server using the same database before starting this process. `.env.example` documents configuration; `.env` files are ignored by Git but are **not loaded automatically**. Do not put a key in the browser, source code, report, or Git.

Requests go to the official `https://api.openai.com/v1/responses` endpoint using `httpx`. The application has no proxy/provider setting. Only the user's question, selected scope, and aggregate evidence are sent to OpenAI. Raw trip records are not sent. Responses use `store=false`; this is not a claim of zero provider-side retention. API usage may incur charges under the configured account.

The request format follows [OpenAI's function calling documentation](https://developers.openai.com/api/docs/guides/function-calling). The model identifier is explicitly configured rather than silently choosing a paid model.

## Tools and evidence

| Tool | Evidence | Purpose |
| --- | --- | --- |
| `check_data_quality` | E1 | Imported files, retained row counts, observed calendar days, and exclusions |
| `compare_monthly_market` | E2 | Monthly service counts, trips per calendar day, daily-volume changes, and selected-service shares |
| `find_operating_opportunities` | E3 | Largest positive changes by service / pickup zone / weekday or weekend / six-hour band |
| `investigate_declines` | E4 | Largest negative changes at the same granularity |

The application forces the first model tool call to check data quality. Tool names and empty argument objects are validated by the server. All analysis scope comes from validated request fields. The agent cannot generate arbitrary SQL or access filesystem/network tools. SQL is fixed, with bound parameters.

One request runs at a time, with at most five model rounds and eight tool calls. SQL queries have a 25-second interrupt timer on their own cursor. Model requests have network timeouts, a turn deadline check, and a 2,200-output-token limit per round. These bounds are not a guaranteed wall-clock SLA. Model errors are surfaced without forwarding provider bodies or credentials. Failed AI calls never silently masquerade as successful model answers.

The evidence queries run in a single read transaction for a consistent MVCC snapshot. Up to four evidence packs are cached by scope and database write version. Successful imports invalidate that cache. The UI renders model text as text, not executable HTML.

## Decision methodology

- Require imported, nonempty files for every selected service in all three months.
- Check every calendar date has at least one retained trip at service level and manifest row counts match actual counts. This detects gaps, but does not prove source completeness.
- Normalize monthly counts by calendar days, including zero-trip dates. A shorter February is not automatically a business decline.
- For zone/time-band changes, divide by the corresponding count of weekdays or weekend days in each month. The unit is trips **per matching day within that six-hour band**, not trips per hour.
- Compare the latest month with the previous month; expose the first month's daily level for context.
- Rank by absolute daily-volume change, requiring at least 100 trips in both comparison months. Return up to ten positive and ten negative groups. New, vanished, low-volume, and unknown-zone groups are excluded from ranking.
- Filter wait-time averages to 0–180 minutes. Missing wait data remains null.
- Proposed experiments and validation KPIs are suggestions, not measured outcomes. There is no profitability ranking, significance test, causal identification, or forecast.

An empty ranking is a valid result. The system never fabricates three opportunities to fill a template.

## Boundaries

Public completed trips do not reveal all requests, rejected/cancelled rides, available drivers, or unmet demand. Passenger payments are not platform revenue or profit. Service shares describe only the selected imported data, not the entire transport market. The tool does not yet provide individual-company comparisons, weather/event joins, custom hours, or arbitrary geography. Company-specific decisions need internal conversion, cost, margin, and driver-supply data.

AI answers require evidence citations and only known evidence IDs are accepted. This validates citation provenance, **not every sentence or numeric claim**. Human review and a live-model evaluation set are still needed before operational use. The existing application is intended for local use; it does not include production authentication or user billing controls.

## Validation

```bash
pytest -q
```

Tests cover calendar normalization, opportunity/decline direction, borough filters, missing months/dates, invalid scope, cache invalidation, concurrency gating, model tool execution and replayed reasoning items, unauthorized tools/arguments, citation checks, bounded loops, configuration errors, and secret non-disclosure. Model responses are mocked for offline tests; they are not evidence of a live API call.
