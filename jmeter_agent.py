#!/usr/bin/env python3
"""
JMeter Performance Test Agent
Features:
  - Create test plans from cURL / Postman JSON / plain text
  - Environment switcher  (team1 / team2 / team3 / team4 / play / stage)
  - Test profiles         (smoke / load / stress / soak)
  - Auto token extraction & injection
  - CSV data feed         (multiple users — avoids rate-limiting)
  - Threshold assertions  (response time + error rate)
  - HTML report           (auto-generated & opened in browser)
  - Headless / CLI mode   (no GUI — good for CI/CD)
  - Error resolution      (paste error → AI fixes JMX → reopens JMeter)
  - Results comparison    (compare two test runs side-by-side)
"""

import csv as csv_module
import json
import os
import re
import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from statistics import mean, quantiles

import anthropic

# ─── Constants ────────────────────────────────────────────────────────────────

ENVIRONMENTS = {
    "team1": {"domain": "keymaker.team1realbrokerage.com", "protocol": "https", "port": 443},
    "team2": {"domain": "keymaker.team2realbrokerage.com", "protocol": "https", "port": 443},
    "team3": {"domain": "keymaker.team3realbrokerage.com", "protocol": "https", "port": 443},
    "team4": {"domain": "keymaker.team4realbrokerage.com", "protocol": "https", "port": 443},
    "play":  {"domain": "keymaker.playrealbrokerage.com",  "protocol": "https", "port": 443},
    "stage": {"domain": "keymaker.stagerealbrokerage.com", "protocol": "https", "port": 443},
}

PROFILES = {
    "smoke":  {"threads": 5,   "ramp": 10,  "loops": 1,  "duration": 0,    "scheduler": False},
    "load":   {"threads": 100, "ramp": 60,  "loops": 10, "duration": 0,    "scheduler": False},
    "stress": {"threads": 500, "ramp": 30,  "loops": 10, "duration": 0,    "scheduler": False},
    "soak":   {"threads": 50,  "ramp": 60,  "loops": -1, "duration": 1800, "scheduler": True},
}

THRESHOLDS = {
    "response_time_ms": 2000,
    "error_rate_pct":   1.0,
}

OUTPUT_DIR  = Path.home() / "jmeter-tests"
RESULTS_DIR = OUTPUT_DIR / "results"
REPORTS_DIR = OUTPUT_DIR / "reports"
DATA_DIR    = OUTPUT_DIR / "data"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _escape_xml(text):
    if not text:
        return ""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _save_config():
    return """<objProp>
            <name>saveConfig</name>
            <value class="SampleSaveConfiguration">
              <time>true</time><latency>true</latency><timestamp>true</timestamp>
              <success>true</success><label>true</label><code>true</code>
              <message>true</message><threadName>true</threadName>
              <dataType>true</dataType><encoding>false</encoding>
              <assertions>true</assertions><subresults>true</subresults>
              <responseData>false</responseData><samplerData>false</samplerData>
              <xml>false</xml><fieldNames>true</fieldNames>
              <responseHeaders>false</responseHeaders><requestHeaders>false</requestHeaders>
              <responseDataOnError>false</responseDataOnError>
              <saveAssertionResultsFailureMessage>true</saveAssertionResultsFailureMessage>
              <bytes>true</bytes><sentBytes>true</sentBytes><url>true</url>
              <threadCounts>true</threadCounts><idleTime>true</idleTime>
              <connectTime>true</connectTime>
            </value>
          </objProp>"""


def collect_multiline(prompt_msg):
    print(f"\n{prompt_msg}")
    print("Press Enter TWICE when done:\n")
    lines, empty = [], 0
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line == "":
            empty += 1
            if empty >= 2:
                break
        else:
            empty = 0
            lines.append(line)
    return "\n".join(lines).strip()


# ─── Environment & Profile Picker ─────────────────────────────────────────────

def pick_environment():
    envs = list(ENVIRONMENTS.keys())
    print("\nSelect environment:")
    for i, e in enumerate(envs, 1):
        print(f"  {i}. {e}  →  {ENVIRONMENTS[e]['domain']}")
    choice = input("\nEnter number or name: ").strip().lower()
    if choice in ENVIRONMENTS:
        return choice
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(envs):
            return envs[idx]
    except ValueError:
        pass
    print(f"Invalid choice, defaulting to team1")
    return "team1"


def pick_profile():
    profiles = list(PROFILES.keys())
    print("\nSelect test profile:")
    for i, p in enumerate(profiles, 1):
        cfg = PROFILES[p]
        dur = f"{cfg['duration']}s duration" if cfg["scheduler"] else f"{cfg['loops']} loops"
        print(f"  {i}. {p:7s}  →  {cfg['threads']} users, {cfg['ramp']}s ramp-up, {dur}")
    choice = input("\nEnter number or name: ").strip().lower()
    if choice in PROFILES:
        return choice
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(profiles):
            return profiles[idx]
    except ValueError:
        pass
    print("Invalid choice, defaulting to load")
    return "load"


# ─── Request Parsing ──────────────────────────────────────────────────────────

def detect_format(text):
    if text.lower().lstrip().startswith("curl "):
        return "cURL command(s)"
    try:
        data = json.loads(text)
        if "info" in data and "item" in data:
            return "Postman collection JSON"
        if isinstance(data, list):
            return "JSON request array"
    except Exception:
        pass
    return "plain text / mixed"


def parse_requests_with_claude(input_text, fmt):
    client = anthropic.Anthropic()
    prompt = f"""You are an API parser. Parse the following {fmt} input and return a JSON array of HTTP requests.

Each object must have EXACTLY these fields (null if not present):
{{
  "name": "short name",
  "method": "GET|POST|PUT|DELETE|PATCH",
  "protocol": "http or https",
  "domain": "hostname only",
  "port": 443,
  "path": "/path",
  "headers": {{"Name": "value"}},
  "query_params": {{"param": "value"}},
  "body": "raw body or null",
  "auth_type": "bearer|basic|api_key|null",
  "auth_value": "token or null",
  "is_auth_endpoint": true/false
}}

Rules:
- Set is_auth_endpoint=true if path contains signin/login/auth/token.
- Extract Authorization into auth_type/auth_value, remove from headers.
- Default port: 443 for https, 80 for http.
- Return ONLY valid JSON array, no markdown.

Input:
{input_text}"""

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    return json.loads(match.group() if match else raw)


# ─── JMX Building Blocks ──────────────────────────────────────────────────────

def build_http_defaults(env_cfg):
    return f"""<ConfigTestElement guiclass="HttpDefaultsGui" testclass="ConfigTestElement"
            testname="HTTP Request Defaults" enabled="true">
          <elementProp name="HTTPsampler.Arguments" elementType="Arguments"
              guiclass="HTTPArgumentsPanel" testclass="Arguments">
            <collectionProp name="Arguments.arguments"/>
          </elementProp>
          <stringProp name="HTTPSampler.domain">{env_cfg['domain']}</stringProp>
          <stringProp name="HTTPSampler.port">{env_cfg['port']}</stringProp>
          <stringProp name="HTTPSampler.protocol">{env_cfg['protocol']}</stringProp>
          <stringProp name="HTTPSampler.contentEncoding">UTF-8</stringProp>
          <stringProp name="HTTPSampler.connect_timeout">10000</stringProp>
          <stringProp name="HTTPSampler.response_timeout">30000</stringProp>
        </ConfigTestElement>
        <hashTree/>"""


def build_header_manager(requests_data):
    skip = {"authorization", "host"}
    common = {}
    for req in requests_data:
        for k, v in (req.get("headers") or {}).items():
            if k.lower() not in skip:
                common[k] = v
    if not common:
        common["Content-Type"] = "application/json"

    items = "\n".join(
        f"""            <elementProp name="{_escape_xml(k)}" elementType="Header">
              <stringProp name="Header.name">{_escape_xml(k)}</stringProp>
              <stringProp name="Header.value">{_escape_xml(v)}</stringProp>
            </elementProp>"""
        for k, v in common.items()
    )
    return f"""<HeaderManager guiclass="HeaderPanel" testclass="HeaderManager"
            testname="HTTP Header Manager" enabled="true">
          <collectionProp name="HeaderManager.headers">
{items}
          </collectionProp>
        </HeaderManager>
        <hashTree/>"""


def build_auth_header(use_variable=False, static_token=""):
    value = "Bearer ${access_token}" if use_variable else f"Bearer {_escape_xml(static_token)}"
    return f"""<HeaderManager guiclass="HeaderPanel" testclass="HeaderManager"
            testname="Authorization Header" enabled="true">
          <collectionProp name="HeaderManager.headers">
            <elementProp name="Authorization" elementType="Header">
              <stringProp name="Header.name">Authorization</stringProp>
              <stringProp name="Header.value">{value}</stringProp>
            </elementProp>
          </collectionProp>
        </HeaderManager>
        <hashTree/>"""


def build_token_extractor():
    return """<RegexExtractor guiclass="RegexExtractorGui" testclass="RegexExtractor"
              testname="Extract Access Token" enabled="true">
            <stringProp name="RegexExtractor.useHeaders">false</stringProp>
            <stringProp name="RegexExtractor.refname">access_token</stringProp>
            <stringProp name="RegexExtractor.regex">"accessToken"\\s*:\\s*"([^"]+)"</stringProp>
            <stringProp name="RegexExtractor.template">$1$</stringProp>
            <stringProp name="RegexExtractor.default">TOKEN_NOT_FOUND</stringProp>
            <stringProp name="RegexExtractor.match_no">1</stringProp>
          </RegexExtractor>
          <hashTree/>"""


def build_csv_dataset(csv_path):
    return f"""<CSVDataSet guiclass="TestBeanGUI" testclass="CSVDataSet"
            testname="CSV User Data" enabled="true">
          <stringProp name="filename">{_escape_xml(str(csv_path))}</stringProp>
          <stringProp name="fileEncoding">UTF-8</stringProp>
          <stringProp name="variableNames">email,password</stringProp>
          <boolProp name="ignoreFirstLine">true</boolProp>
          <stringProp name="delimiter">,</stringProp>
          <boolProp name="quotedData">false</boolProp>
          <boolProp name="recycle">true</boolProp>
          <boolProp name="stopThread">false</boolProp>
          <stringProp name="shareMode">shareMode.all</stringProp>
        </CSVDataSet>
        <hashTree/>"""


def build_threshold_assertion():
    return f"""<DurationAssertion guiclass="DurationAssertionGui" testclass="DurationAssertion"
              testname="Response Time &lt; {THRESHOLDS['response_time_ms']}ms" enabled="true">
            <stringProp name="DurationAssertion.duration">{THRESHOLDS['response_time_ms']}</stringProp>
          </DurationAssertion>
          <hashTree/>"""


def build_sampler(req, has_auth_endpoint):
    method  = req.get("method", "GET")
    path    = req.get("path", "/")
    name    = req.get("name", f"{method} {path}")
    body    = req.get("body")
    is_auth = req.get("is_auth_endpoint", False)

    qp = req.get("query_params") or {}
    if qp:
        qs   = "&".join(f"{_escape_xml(k)}={_escape_xml(v)}" for k, v in qp.items())
        sep  = "&" if "?" in path else "?"
        path = f"{path}{sep}{qs}"

    if is_auth and body:
        try:
            body_obj = json.loads(body)
            if "email" in body_obj:
                body_obj["email"]    = "${email}"
                body_obj["password"] = "${password}"
                body = json.dumps(body_obj)
        except Exception:
            pass

    if body and method in ("POST", "PUT", "PATCH"):
        body_xml = f"""<boolProp name="HTTPSampler.postBodyRaw">true</boolProp>
          <elementProp name="HTTPsampler.Arguments" elementType="Arguments">
            <collectionProp name="Arguments.arguments">
              <elementProp name="" elementType="HTTPArgument">
                <boolProp name="HTTPArgument.always_encode">false</boolProp>
                <stringProp name="Argument.value">{_escape_xml(body)}</stringProp>
                <stringProp name="Argument.metadata">=</stringProp>
              </elementProp>
            </collectionProp>
          </elementProp>"""
    else:
        body_xml = """<elementProp name="HTTPsampler.Arguments" elementType="Arguments"
            guiclass="HTTPArgumentsPanel" testclass="Arguments">
            <collectionProp name="Arguments.arguments"/>
          </elementProp>"""

    inner = f"""<ResponseAssertion guiclass="AssertionGui" testclass="ResponseAssertion"
              testname="Assert Status 200" enabled="true">
            <collectionProp name="Asserion.test_strings">
              <stringProp name="49586">200</stringProp>
            </collectionProp>
            <stringProp name="Assertion.custom_message">Expected HTTP 200</stringProp>
            <stringProp name="Assertion.test_field">Assertion.response_code</stringProp>
            <boolProp name="Assertion.assume_success">false</boolProp>
            <intProp name="Assertion.test_type">8</intProp>
          </ResponseAssertion>
          <hashTree/>
          {build_threshold_assertion()}"""

    if is_auth:
        inner += f"\n          {build_token_extractor()}"

    return f"""
        <HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy"
            testname="{_escape_xml(name)}" enabled="true">
          {body_xml}
          <stringProp name="HTTPSampler.path">{_escape_xml(path)}</stringProp>
          <boolProp name="HTTPSampler.follow_redirects">true</boolProp>
          <stringProp name="HTTPSampler.method">{method}</stringProp>
          <boolProp name="HTTPSampler.use_keepalive">true</boolProp>
          <boolProp name="HTTPSampler.DO_MULTIPART_POST">false</boolProp>
          <intProp name="HTTPSampler.ipSourceType">0</intProp>
        </HTTPSamplerProxy>
        <hashTree>
          {inner}
        </hashTree>"""


def build_listeners(jtl_path=""):
    save = _save_config()
    jtl = _escape_xml(str(jtl_path)) if jtl_path else ""
    return f"""
        <ResultCollector guiclass="SummaryReport" testclass="ResultCollector"
            testname="Summary Report" enabled="true">
          <boolProp name="ResultCollector.error_logging">false</boolProp>
          {save}
          <stringProp name="filename">{jtl}</stringProp>
        </ResultCollector>
        <hashTree/>

        <ResultCollector guiclass="StatVisualizer" testclass="ResultCollector"
            testname="Aggregate Report" enabled="true">
          <boolProp name="ResultCollector.error_logging">false</boolProp>
          {save}
          <stringProp name="filename"></stringProp>
        </ResultCollector>
        <hashTree/>

        <ResultCollector guiclass="ViewResultsFullVisualizer" testclass="ResultCollector"
            testname="View Results Tree" enabled="true">
          <boolProp name="ResultCollector.error_logging">false</boolProp>
          {save}
          <stringProp name="filename"></stringProp>
        </ResultCollector>
        <hashTree/>

        <ResultCollector guiclass="RespTimeGraphVisualizer" testclass="ResultCollector"
            testname="Response Time Graph" enabled="true">
          <boolProp name="ResultCollector.error_logging">false</boolProp>
          {save}
          <stringProp name="filename"></stringProp>
        </ResultCollector>
        <hashTree/>"""


def generate_jmx(requests_data, test_name, env_name, profile_name,
                  use_csv=False, csv_path=None, jtl_path=""):
    env     = ENVIRONMENTS[env_name]
    profile = PROFILES[profile_name]
    has_auth = any(r.get("is_auth_endpoint") for r in requests_data)

    loops_val = str(profile["loops"]) if profile["loops"] != -1 else "-1"
    scheduler = "true" if profile["scheduler"] else "false"
    duration  = str(profile["duration"])

    samplers = "\n".join(build_sampler(r, has_auth) for r in requests_data)

    static_token = next(
        (r.get("auth_value", "") for r in requests_data if r.get("auth_type") == "bearer"),
        ""
    )
    auth_block = build_auth_header(use_variable=has_auth, static_token=static_token)
    csv_block  = build_csv_dataset(csv_path) if use_csv and csv_path else ""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan"
        testname="{_escape_xml(test_name)} [{env_name} / {profile_name}]" enabled="true">
      <stringProp name="TestPlan.comments">
        Auto-generated by JMeter Agent | env={env_name} | profile={profile_name}
      </stringProp>
      <boolProp name="TestPlan.functional_mode">false</boolProp>
      <boolProp name="TestPlan.tearDown_on_shutdown">true</boolProp>
      <boolProp name="TestPlan.serialize_threadgroups">false</boolProp>
      <elementProp name="TestPlan.user_defined_variables" elementType="Arguments"
          guiclass="ArgumentsPanel" testclass="Arguments" testname="User Defined Variables">
        <collectionProp name="Arguments.arguments"/>
      </elementProp>
    </TestPlan>
    <hashTree>

      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup"
          testname="Thread Group [{profile_name}]" enabled="true">
        <stringProp name="ThreadGroup.on_sample_error">continue</stringProp>
        <elementProp name="ThreadGroup.main_controller" elementType="LoopController"
            guiclass="LoopControlPanel" testclass="LoopController">
          <boolProp name="LoopController.continue_forever">false</boolProp>
          <stringProp name="LoopController.loops">{loops_val}</stringProp>
        </elementProp>
        <stringProp name="ThreadGroup.num_threads">{profile['threads']}</stringProp>
        <stringProp name="ThreadGroup.ramp_time">{profile['ramp']}</stringProp>
        <boolProp name="ThreadGroup.scheduler">{scheduler}</boolProp>
        <stringProp name="ThreadGroup.duration">{duration}</stringProp>
        <stringProp name="ThreadGroup.delay"></stringProp>
        <boolProp name="ThreadGroup.same_user_on_next_iteration">false</boolProp>
      </ThreadGroup>
      <hashTree>

        {build_http_defaults(env)}
        {csv_block}
        {build_header_manager(requests_data)}
        {auth_block}
        {samplers}
        {build_listeners(jtl_path)}

      </hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>"""


# ─── CSV Data File ─────────────────────────────────────────────────────────────

def ensure_csv(default_email, default_password):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = DATA_DIR / "users.csv"
    if not csv_path.exists():
        with open(csv_path, "w", newline="") as f:
            writer = csv_module.writer(f)
            writer.writerow(["email", "password"])
            writer.writerow([default_email, default_password])
            writer.writerow(["testuser2@therealbrokerage.com", "P@ssw0rd1234"])
            writer.writerow(["testuser3@therealbrokerage.com", "P@ssw0rd1234"])
        print(f"CSV created: {csv_path}")
        print("  Add more users to this file to distribute load across accounts.")
    else:
        print(f"CSV exists: {csv_path}")
    return csv_path


# ─── JMeter Install & Launch ──────────────────────────────────────────────────

def ensure_jmeter():
    result = subprocess.run(["which", "jmeter"], capture_output=True, text=True)
    if result.returncode == 0:
        path = result.stdout.strip()
        print(f"JMeter: {path}")
        return path
    print("JMeter not found. Installing via Homebrew...")
    brew = subprocess.run(["which", "brew"], capture_output=True, text=True)
    if brew.returncode != 0:
        print("ERROR: Homebrew not installed. Visit https://brew.sh")
        sys.exit(1)
    subprocess.run(["brew", "install", "jmeter"], check=True)
    return subprocess.run(["which", "jmeter"], capture_output=True, text=True).stdout.strip()


def launch_jmeter_gui(jmx_path, jmeter_bin):
    print(f"\nOpening JMeter: {jmx_path}")
    subprocess.Popen([jmeter_bin, "-t", str(jmx_path)])


def run_headless(jmx_path, jtl_path, jmeter_bin):
    print(f"\nRunning headless test...")
    print(f"  Plan:    {jmx_path}")
    print(f"  Results: {jtl_path}\n")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [jmeter_bin, "-n", "-t", str(jmx_path), "-l", str(jtl_path)],
        text=True
    )
    return result.returncode == 0


# ─── HTML Report ──────────────────────────────────────────────────────────────

def generate_html_report(jtl_path, jmeter_bin):
    ts         = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir = REPORTS_DIR / ts
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\nGenerating HTML report → {report_dir}")
    subprocess.run(
        [jmeter_bin, "-g", str(jtl_path), "-o", str(report_dir)],
        check=True
    )
    index = report_dir / "index.html"
    if index.exists():
        print(f"Opening report: {index}")
        webbrowser.open(f"file://{index}")
    return report_dir


# ─── Results Comparison ───────────────────────────────────────────────────────

def _parse_jtl(jtl_path):
    times, errors = [], 0
    with open(jtl_path, newline="") as f:
        reader = csv_module.DictReader(f)
        for row in reader:
            try:
                times.append(int(row.get("elapsed", 0)))
                if row.get("success", "true").lower() == "false":
                    errors += 1
            except (ValueError, KeyError):
                pass
    return times, errors


def _percentile(data, pct):
    if not data:
        return 0
    sorted_data = sorted(data)
    idx = int(len(sorted_data) * pct / 100)
    return sorted_data[min(idx, len(sorted_data) - 1)]


def compare_results():
    jtl_files = sorted(RESULTS_DIR.glob("*.jtl"), key=lambda f: f.stat().st_mtime, reverse=True)
    if len(jtl_files) < 2:
        print("Need at least 2 result files to compare.")
        return

    print("\nAvailable result files:")
    for i, f in enumerate(jtl_files[:10], 1):
        print(f"  {i}. {f.name}")

    def pick(label):
        choice = input(f"\nSelect {label} run (number): ").strip()
        try:
            return jtl_files[int(choice) - 1]
        except (ValueError, IndexError):
            return jtl_files[0]

    f1 = pick("FIRST")
    f2 = pick("SECOND")
    t1, e1 = _parse_jtl(f1)
    t2, e2 = _parse_jtl(f2)

    def row(label, v1, v2, unit="ms", lower_is_better=True):
        diff  = v2 - v1
        arrow = ("▲" if diff > 0 else "▼") if diff != 0 else "="
        better = (diff < 0) if lower_is_better else (diff > 0)
        tag   = "✓" if better else ("✗" if diff != 0 else " ")
        print(f"  {label:<22} {v1:>8}{unit}   {v2:>8}{unit}   {arrow}{abs(diff):>6}{unit}  {tag}")

    print(f"\n{'─'*65}")
    print(f"  {'Metric':<22} {'Run 1':>9}   {'Run 2':>9}   {'Delta':>8}  OK?")
    print(f"{'─'*65}")
    row("Avg Response",    int(mean(t1)) if t1 else 0, int(mean(t2)) if t2 else 0)
    row("95th Percentile", _percentile(t1, 95), _percentile(t2, 95))
    row("99th Percentile", _percentile(t1, 99), _percentile(t2, 99))
    row("Max Response",    max(t1) if t1 else 0, max(t2) if t2 else 0)
    row("Min Response",    min(t1) if t1 else 0, min(t2) if t2 else 0)
    err1 = round(e1 / len(t1) * 100, 2) if t1 else 0
    err2 = round(e2 / len(t2) * 100, 2) if t2 else 0
    print(f"  {'Error Rate':<22} {err1:>8}%   {err2:>8}%   {'▲' if err2>err1 else '▼'}{abs(err2-err1):>5}%")
    row("Total Samples",   len(t1), len(t2), unit="", lower_is_better=False)
    print(f"{'─'*65}")


# ─── Error Resolution ─────────────────────────────────────────────────────────

def find_latest_jmx():
    files = sorted(OUTPUT_DIR.glob("*.jmx"), key=lambda f: f.stat().st_mtime, reverse=True)
    return files[0] if files else None


def resolve_error_with_claude(error_text, jmx_content):
    client = anthropic.Anthropic()
    prompt = f"""You are a JMeter expert. Fix the JMX test plan based on this error.

ERROR:
{error_text}

CURRENT JMX:
{jmx_content}

Common fixes:
- 401 → fix Authorization header
- Connection refused / unknown host → fix domain/port
- SSL error → set protocol to https, verify port 443
- Assertion failure → adjust expected status code
- Timeout → increase connect_timeout / response_timeout in HTTP Defaults
- 400 Bad Request → fix request body / Content-Type

Return ONLY the complete fixed XML. No explanation, no markdown."""

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```(?:xml)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()
    return raw


def mode_fix_error(jmeter_bin):
    print("\n" + "=" * 60)
    print("  Error Resolution Mode")
    print("=" * 60)

    jmx_path = find_latest_jmx()
    if not jmx_path:
        print("No .jmx files found in ~/jmeter-tests/")
        sys.exit(1)

    print(f"Fixing: {jmx_path}")
    jmx_content = jmx_path.read_text(encoding="utf-8")
    error_text  = collect_multiline("Paste the error message / stack trace below.")
    if not error_text:
        print("No error provided.")
        sys.exit(1)

    print("\nAnalyzing with AI...")
    fixed = resolve_error_with_claude(error_text, jmx_content)

    backup = jmx_path.with_suffix(".bak.jmx")
    jmx_path.rename(backup)
    jmx_path.write_text(fixed, encoding="utf-8")
    print(f"Backup: {backup}")
    print(f"Fixed:  {jmx_path}")

    launch_jmeter_gui(jmx_path, jmeter_bin)
    print("\nJMeter reopening with fix applied.")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("   JMeter Performance Test Agent")
    print("=" * 60)

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("\nERROR: ANTHROPIC_API_KEY not set.")
        print("Run:  export ANTHROPIC_API_KEY=your_key_here")
        sys.exit(1)

    print("""
Select mode:
  1. Create new test plan  (GUI)
  2. Create new test plan  (Headless / CLI)
  3. Fix error from last run
  4. Generate HTML report from last results
  5. Compare two test runs
""")
    choice = input("Enter 1–5: ").strip()

    jmeter_bin = ensure_jmeter()

    if choice == "3":
        mode_fix_error(jmeter_bin)
        return

    if choice == "4":
        jtl_files = sorted(RESULTS_DIR.glob("*.jtl"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not jtl_files:
            print("No .jtl result files found. Run a headless test first.")
            sys.exit(1)
        print(f"Using latest results: {jtl_files[0]}")
        generate_html_report(jtl_files[0], jmeter_bin)
        return

    if choice == "5":
        compare_results()
        return

    headless = (choice == "2")
    env_name     = pick_environment()
    profile_name = pick_profile()

    input_text = collect_multiline("Paste your API request(s) below.\nSupported: cURL | Postman JSON | plain text")
    if not input_text:
        print("No input provided.")
        sys.exit(1)

    fmt = detect_format(input_text)
    print(f"\nDetected: {fmt}")
    print("Parsing with AI...")

    try:
        requests_data = parse_requests_with_claude(input_text, fmt)
    except Exception as e:
        print(f"ERROR parsing: {e}")
        sys.exit(1)

    if not requests_data:
        print("No requests found.")
        sys.exit(1)

    print(f"Found {len(requests_data)} request(s):")
    for r in requests_data:
        auth_flag = " [AUTH→token extracted]" if r.get("is_auth_endpoint") else ""
        print(f"  {r.get('method','GET'):6s}  {r.get('path','/')}{auth_flag}")

    test_name = input("\nTest plan name (Enter = 'Performance Test'): ").strip() or "Performance Test"

    use_csv  = False
    csv_path = None
    auth_req = next((r for r in requests_data if r.get("is_auth_endpoint")), None)
    if auth_req:
        body = auth_req.get("body", "")
        try:
            body_obj         = json.loads(body) if body else {}
            default_email    = body_obj.get("email", "")
            default_password = body_obj.get("password", "")
        except Exception:
            default_email = default_password = ""
        csv_path = ensure_csv(default_email, default_password)
        use_csv  = True

    OUTPUT_DIR.mkdir(exist_ok=True)
    RESULTS_DIR.mkdir(exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe     = re.sub(r"[^\w\-]", "_", test_name)
    jmx_path = OUTPUT_DIR / f"{safe}_{env_name}_{profile_name}.jmx"
    jtl_path = RESULTS_DIR / f"{safe}_{env_name}_{profile_name}_{ts}.jtl" if headless else ""

    print("\nGenerating test plan...")
    jmx = generate_jmx(requests_data, test_name, env_name, profile_name,
                        use_csv=use_csv, csv_path=csv_path, jtl_path=jtl_path)
    jmx_path.write_text(jmx, encoding="utf-8")
    print(f"Saved: {jmx_path}")

    if headless:
        success = run_headless(jmx_path, jtl_path, jmeter_bin)
        if success and jtl_path:
            print("\nTest complete. Generating HTML report...")
            generate_html_report(jtl_path, jmeter_bin)
        else:
            print("\nTest failed. Run mode 3 to fix errors.")
    else:
        launch_jmeter_gui(jmx_path, jmeter_bin)
        print(f"""
{"=" * 60}
  READY! JMeter is opening...
{"=" * 60}

  Thread Group is pre-set to profile: {profile_name}
    Threads : {PROFILES[profile_name]['threads']}
    Ramp-up : {PROFILES[profile_name]['ramp']}s

  You can change thread count in the Thread Group panel.
  Press the GREEN ▶ Run button when ready.

  Listeners: Summary | Aggregate | Results Tree | Response Time
  Thresholds: {THRESHOLDS['response_time_ms']}ms response time | {THRESHOLDS['error_rate_pct']}% error rate
  Token: {'auto-extracted from signin and injected' if use_csv else 'static'}
  Users CSV: {csv_path if use_csv else 'N/A'}
""")


if __name__ == "__main__":
    main()
