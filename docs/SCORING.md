# Scoring Engine

The scoring engine (`jobflow/filter.py`) evaluates each job posting against the user's profile using multiple signals. It produces a `FilterResult` with a normalized 0-100% match score.

## Score Calculation

```
raw = keyword_score + synergy_bonus + level_points + experience_score
      + recency_score + location_score + h1b_bonus + senior_penalty

score_pct = min(100, max(0, round(raw / 130 * 100)))
```

`SCORE_MAX_RAW = 130` is the practical ceiling (not every keyword firing at once).

## Scoring Signals

### 1. Keyword Matching (`keyword_score`)

Binary presence match — each keyword scores once regardless of frequency. Organized by category:

| Category | Keywords (weight) |
|----------|------------------|
| **Core** | python(10), c++(6), java(4), sql(5), go(4) |
| **ML/AI** | machine learning(10), deep learning(8), pytorch(8), tensorflow(7), llm(8), rag(7), langchain(6), hugging face(5), computer vision(5), nlp(6), transformers(6) |
| **Backend** | distributed systems(8), rest(4), api(4), fastapi(7), flask(5), microservices(5), grpc(5) |
| **Cloud** | aws(7), gcp(4), azure(3), lambda(3), ec2(3) |
| **DevOps** | docker(5), kubernetes(6), ci/cd(4), terraform(4), linux(4) |
| **Data** | postgresql(5), mongodb(4), redis(5), kafka(5), spark(4), airflow(4), elasticsearch(4) |

### 2. Synergy Combos (`synergy_bonus`)

Extra points when a full tech combo appears together:

| Combo | Bonus |
|-------|-------|
| python + fastapi + aws | +10 |
| python + pytorch + aws | +10 |
| machine learning + python + docker | +8 |
| llm + python + aws | +10 |
| python + docker + kubernetes | +8 |
| python + kafka + distributed systems | +8 |
| postgresql + redis + api | +6 |

### 3. Level Points

Based on `level_tag()` which detects job level from title + description:

| Level | Points | Detection Patterns |
|-------|--------|--------------------|
| New Grad | +20 | "new grad", "university grad", "recent graduate" |
| Entry | +15 | "entry-level", "junior", "associate", "SDE I", "SWE I", "early career" |
| Mid | +5 | "SDE II", "SWE II", "software engineer II", "mid-level" |
| Unknown | +4 | No level signals detected |

### 4. Experience Score

Parsed from JD text (e.g., "2-4 years", "minimum 3 years"):

| Range | Points | Rationale |
|-------|--------|-----------|
| Includes 0-2 years | +10 | Sweet spot for new grad |
| Max <= 1 year | +8 | Intern/very junior |
| Min exactly 3 | +6 | Stretch but possible |
| Min > 3 | 0 | Too senior |
| No data | 0 | Can't determine |

### 5. Recency Score

Based on `first_seen` timestamp (when job was discovered):

| Age | Points |
|-----|--------|
| < 6 hours | +10 |
| 6-12 hours | +8 |
| 12-24 hours | +5 |
| 24-48 hours | +2 |
| > 48 hours | -5 |

### 6. Other Signals

| Signal | Points | Condition |
|--------|--------|-----------|
| US location | +10 | Matches US city/state/remote patterns |
| Non-US location | -10 | Doesn't match US patterns |
| H1B mention | +8 | JD mentions "h1b", "visa sponsorship", "will sponsor" |
| Senior penalty | -30 | 3+ senior phrases AND 0 entry-level signals |

### 7. Competition Estimate (0-10)

Separate from main score — estimates applicant competition:
- Big tech company (Google, Amazon, Meta, etc.): +5
- Posting > 48h old: +5
- Posting 24-48h old: +2

## Hard Disqualifiers

Jobs are instantly rejected (score=0) if they match:
- "no visa sponsorship", "will not sponsor", "cannot sponsor"
- "US citizen required", "security clearance"
- "permanent resident only", "green card required"
- "authorized to work without sponsorship"
- Non-US location (India, UK, Germany, etc.)

## Thresholds

| Threshold | Value | Usage |
|-----------|-------|-------|
| `should_apply` | score_pct >= 30 | Job appears in scan results |
| `recommended` | score_pct >= 25 | Gold star on LinkedIn dashboard |
| Senior reject | 3+ senior phrases, 0 entry signals | -30 penalty |

## Variant Selection

Resume variant selected based on JD keyword counts:
- **ml**: ML_KEYWORDS count > APPDEV_KEYWORDS count AND >= 2 matches
- **appdev**: APPDEV_KEYWORDS > ML_KEYWORDS AND >= 2 matches  
- **se**: Default (software engineering)
