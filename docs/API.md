# API Reference

All routes are defined in `jobflow/web/__init__.py`.

## Page Routes

| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Redirects to `/linkedin` |
| `/linkedin` | GET | LinkedIn job feed dashboard |
| `/boards` | GET | Job Boards placeholder page |
| `/scan` | GET | Scanner page |
| `/tailor` | GET | Resume tailor page |

## LinkedIn Jobs API

### GET /api/linkedin/jobs

Fetch filtered LinkedIn jobs. Returns HTML table body fragment.

**Query Parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `status` | string | `""` | Filter by status: "Tracking", "Applied", "Not Interested", "Recommended" |
| `level` | string | `""` | Filter by level: "New Grad", "Entry", "Mid" |
| `q` | string | `""` | Text search (company, title, location) |
| `search_term` | string | `""` | Filter by LinkedIn search term that found the job |
| `time` | string | `""` | Time range: "hour", "today", "yesterday", "" (all) |
| `hour` | string | `""` | Specific hour filter (0-23, local hour) |
| `sort` | string | `"last_seen"` | Sort column: "first_seen", "score_pct", "score", "company", "level", "title" |
| `dir` | string | `"desc"` | Sort direction: "asc" or "desc" |
| `tz` | int | `0` | User's timezone offset in minutes from UTC (e.g., 240 for EDT) |

**Response Headers:**

| Header | Content | Description |
|--------|---------|-------------|
| `X-Counts` | JSON | `{"All": N, "Tracking": N, "Applied": N, "Not Interested": N, "Recommended": N}` |
| `X-Level-Counts` | JSON | `{"All": N, "New Grad": N, "Entry": N, "Mid": N, "Unknown": N}` |
| `X-Time-Counts` | JSON | `{"this_hour": N, "today": N, "yesterday": N}` |

### PATCH /api/linkedin/jobs/\<key\>/status

Update a job's status.

**Form Parameters:**
- `status`: One of "Tracking", "Applied", "Not Interested", "" (clear)

**Returns:** Updated HTML row fragment.

### POST /api/linkedin/refresh

Trigger manual git pull + merge of latest scan results.

**Returns:** HTML status message.

## Scanner API

### POST /api/scan/trigger

Start a background scan.

**Form Parameters:**
- `platform`: "lever", "greenhouse", "ashby", "linkedin", "github", or empty for all
- `hours`: Max age in hours (0 = no limit)
- `new_only`: "true"/"on" to dedup against seen jobs

### GET /api/scan/status

Poll scan progress. Returns HTML fragment showing:
- Running state with spinner
- Error with message
- Results table with Track buttons

### POST /api/scan/track

Add a scanned job to the CSV tracker.

**Form Parameters:**
- `company`, `role`, `url`, `score`, `variant`, `source`

## Tailor API

### POST /api/tailor/generate

Start resume tailoring session.

**Form Parameters:**
- `jd_text`: Full job description text (required)
- `model`: "sonnet" (default), "opus", "haiku"
- `effort`: "low" (default), "medium", "high"

**Returns:** HTML status fragment with session ID for polling.

### GET /api/tailor/status/\<session_id\>

Poll tailoring progress.

**Returns:** HTML fragment — running spinner, error, or completed with PDF preview + refine form.

### POST /api/tailor/refine/\<session_id\>

Refine the tailored resume.

**Form Parameters:**
- `feedback`: Description of changes to make

### POST /api/tailor/cancel/\<session_id\>

Cancel a running tailoring session.

### GET /api/tailor/pdf/\<session_id\>

View the generated PDF inline.

### GET /api/tailor/download/\<session_id\>

Download the PDF as an attachment.

## Stats API

### GET /api/stats

**Returns:** JSON with application statistics.

```json
{
  "status_counts": {"Pending": 5, "Applied": 3, ...},
  "week_labels": ["Mar 3", "Mar 10", ...],
  "week_values": [2, 5, ...],
  "total": 15
}
```
