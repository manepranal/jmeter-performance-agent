# JMeter Performance Test Agent

An AI-powered agent that automatically sets up and runs JMeter performance tests.
You provide API requests in any format — the agent configures everything.
You only pick an environment, a profile, and hit Run.

---

## Features

| Feature | Description |
|---|---|
| **Any input format** | Paste cURL commands, Postman collection JSON, or plain text |
| **Environment switcher** | team1 / team2 / team3 / team4 / play / stage |
| **Test profiles** | smoke / load / stress / soak — pre-configured thread settings |
| **Auto token extraction** | Extracts `accessToken` from signin and injects into all requests |
| **CSV data feed** | Cycles through multiple users to avoid rate limiting |
| **Threshold assertions** | Fails samples exceeding 2000ms response time |
| **HTML report** | Auto-generated and opened in browser after headless runs |
| **Headless / CLI mode** | No GUI — run from terminal or CI/CD pipelines |
| **Error resolution** | Paste a JMeter error → AI fixes the JMX → reopens JMeter |
| **Results comparison** | Compare two test runs side-by-side with delta metrics |

---

## Requirements

- macOS (Homebrew is used to install JMeter automatically)
- Python 3.8+
- An [Anthropic API key](https://console.anthropic.com)

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/manepranal/jmeter-performance-agent.git
cd jmeter-performance-agent
```

### 2. Install Python dependencies

```bash
pip3 install -r requirements.txt
```

### 3. Set your Anthropic API key

```bash
export ANTHROPIC_API_KEY=your_key_here
```

To make it permanent, add the line above to your `~/.zshrc` or `~/.bash_profile`.

### 4. JMeter (auto-installed)

JMeter is installed automatically via Homebrew on first run. If you prefer to install it manually:

```bash
brew install jmeter
```

---

## Usage

```bash
python3 jmeter_agent.py
```

You'll be shown a menu:

```
Select mode:
  1. Create new test plan  (GUI)
  2. Create new test plan  (Headless / CLI)
  3. Fix error from last run
  4. Generate HTML report from last results
  5. Compare two test runs
```

---

## Modes

### Mode 1 — Create test plan (GUI)

1. Pick an environment
2. Pick a test profile
3. Paste your API request(s)
4. Enter a test plan name
5. JMeter opens pre-loaded — set thread count and press ▶ Run

### Mode 2 — Headless / CLI

Same as Mode 1, but runs without opening the JMeter GUI.
Results are saved as a `.jtl` file and an HTML report is auto-generated and opened in your browser.

### Mode 3 — Fix error

1. Run this mode after a test fails
2. Paste the error message or stack trace
3. AI diagnoses the issue, fixes the JMX file, and reopens JMeter

### Mode 4 — HTML report

Generates a full JMeter HTML dashboard report from the most recent `.jtl` results file and opens it in the browser.

### Mode 5 — Compare runs

Lists available result files and lets you pick two to compare:

```
──────────────────────────────────────────────────────────────────
  Metric                  Run 1      Run 2      Delta    OK?
──────────────────────────────────────────────────────────────────
  Avg Response            245ms      310ms      ▲65ms    ✗
  95th Percentile         890ms      740ms      ▼150ms   ✓
  99th Percentile        1200ms     1050ms      ▼150ms   ✓
  Max Response           2100ms     1900ms      ▼200ms   ✓
  Min Response             80ms       75ms        ▼5ms   ✓
  Error Rate               0.5%       0.2%       ▼0.3%
  Total Samples            1000       1000          =
──────────────────────────────────────────────────────────────────
```

---

## Environments

| Name | URL |
|---|---|
| team1 | keymaker.team1realbrokerage.com |
| team2 | keymaker.team2realbrokerage.com |
| team3 | keymaker.team3realbrokerage.com |
| team4 | keymaker.team4realbrokerage.com |
| play  | keymaker.playrealbrokerage.com  |
| stage | keymaker.stagerealbrokerage.com |

---

## Test Profiles

| Profile | Threads | Ramp-Up | Duration |
|---|---|---|---|
| smoke  | 5    | 10s | 1 loop      |
| load   | 100  | 60s | 10 loops    |
| stress | 500  | 30s | 10 loops    |
| soak   | 50   | 60s | 30 minutes  |

---

## Supported Input Formats

### cURL command

```bash
curl -X POST https://keymaker.team1realbrokerage.com/api/v1/auth/signin \
  -H "Content-Type: application/json" \
  -d '{"email": "user@example.com", "password": "secret"}'
```

### Postman collection JSON

Export a collection from Postman and paste the JSON directly.

### Plain text description

```
POST /api/v1/auth/signin with email and password in body
GET /api/v1/users with Bearer token auth
```

---

## Auto Token Extraction

When a signin / login / auth endpoint is detected, the agent automatically:

1. Adds a **RegexExtractor** to capture `accessToken` from the response
2. Stores it as a JMeter variable `${access_token}`
3. Injects `Bearer ${access_token}` into all subsequent requests

No manual copy-paste of tokens needed.

---

## CSV Data Feed

When an auth endpoint is detected, the agent creates:

```
~/jmeter-tests/data/users.csv
```

```csv
email,password
user1@therealbrokerage.com,P@ssw0rd1234
user2@therealbrokerage.com,P@ssw0rd1234
user3@therealbrokerage.com,P@ssw0rd1234
```

JMeter cycles through these users across threads — preventing rate limiting during load and stress tests.
Add as many rows as needed.

---

## Thresholds

Every sampler includes a **Duration Assertion** that marks a sample as failed if the response time exceeds **2000ms**.

To change the threshold, edit this in `jmeter_agent.py`:

```python
THRESHOLDS = {
    "response_time_ms": 2000,   # change this value
    "error_rate_pct":   1.0,
}
```

---

## Output Files

All generated files are saved under `~/jmeter-tests/`:

```
~/jmeter-tests/
├── MyTest_team1_load.jmx          ← generated test plans
├── MyTest_team1_load.bak.jmx      ← backup before error fix
├── data/
│   └── users.csv                  ← user credentials for CSV feed
├── results/
│   └── MyTest_team1_load_*.jtl    ← raw test results (headless mode)
└── reports/
    └── 20260327_143000/
        └── index.html             ← HTML dashboard report
```

---

## Project Structure

```
jmeter-performance-agent/
├── jmeter_agent.py     ← main agent script
├── requirements.txt    ← Python dependencies
├── setup.sh            ← one-time setup helper
└── README.md
```

---

## Troubleshooting

| Error | Fix |
|---|---|
| `ANTHROPIC_API_KEY not set` | Run `export ANTHROPIC_API_KEY=your_key` |
| `JMeter not found` | Run `brew install jmeter` |
| `TOKEN_NOT_FOUND` in requests | Signin endpoint not detected — check path contains `signin`/`login`/`auth` |
| `401 Unauthorized` | Use Mode 3 (fix error) and paste the error |
| HTML report empty | Make sure `.jtl` file is not empty — rerun headless test |
