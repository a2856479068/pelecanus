"""Personal pelican benchmark. Chromium/Playwright are used for visual review."""
import contextlib
from concurrent.futures import ThreadPoolExecutor, as_completed
import fcntl
import hashlib
import hmac
import http.client
import ipaddress
import json
import math
import os
import queue
from pathlib import Path
import re
import secrets
import signal
import socket
import sqlite3
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error, parse, request
import xml.etree.ElementTree as ET
from visual_review import review_pelican

ROOT = Path(__file__).resolve().parent
INTERVAL = 30 * 60
LOCK_WAIT = 45
MAX_RESPONSE = 2 * 1024 * 1024
# ponytail: buffer SSE up to 16 MB; parse incrementally if concurrent streams strain memory.
MAX_STREAM_RESPONSE = 16 * 1024 * 1024
DEFAULTS = dict(base_url="https://api.example.com/v1", model="gpt-6-astra",
                effort="medium", protocol="responses", api_key="", enabled=False, next_run=None,
                interval_minutes=30, timeout_seconds=300, max_output_tokens=16000, guest_enabled=True, retry_count=2)
NODE_FIELDS = ("base_url", "api_key", "model", "effort", "protocol")
SCENES = ("海边木栈道", "秋日林道", "春日草坡", "雨后湿地", "黄昏公路", "湖畔风车", "热带海岛", "雪山谷地")
PROMPT = """创建一幅独立的 SVG 鹈鹕骑自行车 2D 循环动画。
主角是一只可爱的鹈鹕，有橙色大嘴、踩在踏板上的蹼足，以及微微张开保持平衡的翅膀。
自行车轮子持续转动，脚与踏板动作协调，动画流畅，背景明亮，配色鲜活。
本次场景：{scene}。请为这个场景独立构图。
在画面右下角用可见的 SVG text 元素显示本次校验码：{nonce}。
只返回一个完整的 SVG，可使用内联 CSS 或 SMIL 动画；不要 HTML、JavaScript、外部图片、外部字体或其他外部资源。"""
CANDY_PROMPT = """在一个黑色的袋子里放有三种口味的糖果，每种糖果有两种不同的形状（圆形和五角星形，不同的形状靠手感可以分辨）。现已知不同口味的糖和不同形状的数量统计如下表。参赛者需要在活动前决定摸出的糖果数目，那么，最少取出多少个糖果才能保证手中同时拥有不同形状的苹果味和桃子味的糖？（同时手中有圆形苹果味匹配五角星桃子味糖果，或者有圆形桃子味匹配五角星苹果味糖果都满足要求）

| 形状 | 苹果味 | 桃子味 | 西瓜味 |
| 圆形 | 7 | 9 | 8 |
| 五角星形 | 7 | 6 | 4 |"""


def candy_passes(text):
    return re.search(r"(?<![\dA-Za-z_.+\-])21(?![\dA-Za-z_]|\.\d)", text) is not None


def next_slot(now, interval=INTERVAL):
    return (math.floor(now / interval) + 1) * interval


def mask(value):
    return value[:3] + "****" + value[-3:] if len(value) > 8 else "****"


def mask_url(value):
    host = parse.urlsplit(value).hostname or ""
    return host[:2] + "****" + host[-2:] if len(host) > 5 else "****"


def redact(value, config):
    for secret, replacement in ((config.get("api_key"), "****"),
                                (config.get("base_url"), mask_url(config.get("base_url", ""))),
                                (parse.urlsplit(config.get("base_url", "")).hostname, mask_url(config.get("base_url", "")))):
        if secret:
            value = value.replace(secret, replacement)
    return value


def validate_settings(values, old):
    if not isinstance(values, dict):
        raise ValueError("设置必须为 JSON 对象")
    new = old.copy()
    for key in ("base_url", "model", "effort", "protocol", "api_key"):
        if key in values:
            if not isinstance(values[key], str) or len(values[key]) > 4096:
                raise ValueError("设置字段格式不正确")
            value = values[key].strip()
            if key in ("api_key", "base_url") and not value:
                continue
            if key in ("api_key", "base_url") and "****" in value:
                raise ValueError("请填入新的完整值，或留空保留已保存的配置")
            new[key] = value
    base = new["base_url"]
    if "://" not in base:
        base = "https://" + base.removeprefix("//")
    if any(c.isspace() or ord(c) < 32 for c in base) or "\\" in base:
        raise ValueError("API 地址不能包含空格、换行或反斜杠")
    url = parse.urlsplit(base)
    if (url.scheme != "https" and not (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1"))) or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("请填写 HTTPS API 地址，不要包含密钥、查询参数或账号密码")
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError("API 地址端口无效")
    base = base.rstrip("/")
    for suffix in ("/chat/completions", "/responses"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
    if not parse.urlsplit(base).path:
        base += "/v1"
    new["base_url"] = base
    if not new["model"] or len(new["model"]) > 120:
        raise ValueError("请输入有效的模型名称")
    if new["effort"] not in ("low", "medium", "high", "xhigh") or new["protocol"] not in ("responses", "chat"):
        raise ValueError("思考强度或接口协议不正确")
    if any(ord(c) < 32 or ord(c) > 126 for c in new["api_key"]):
        raise ValueError("API Key 格式不正确")
    for key in ("enabled", "guest_enabled"):
        if key in values:
            if not isinstance(values[key], bool):
                raise ValueError("开关必须为布尔值")
            new[key] = values[key]
    for key, low, high in (("interval_minutes", 1, 1440), ("timeout_seconds", 10, 600), ("max_output_tokens", 1024, 64000), ("retry_count", 0, 5)):
        if key in values:
            if type(values[key]) is not int or not low <= values[key] <= high:
                raise ValueError(f"{key} 必须为 {low}–{high} 的整数")
            new[key] = values[key]
    if new["api_key"] and new["base_url"] != old["base_url"] and not values.get("api_key", "").strip():
        raise ValueError("更换 API 地址时，请重新填写该地址的 API Key")
    if new["enabled"] and not new["api_key"]:
        raise ValueError("请先填写 API Key，再启用自动检测")
    reset = new["interval_minutes"] != old.get("interval_minutes", 30) or not old["enabled"]
    new["next_run"] = (next_slot(time.time(), new["interval_minutes"] * 60) if reset or not old.get("next_run") else old["next_run"]) if new["enabled"] else None
    return new


def public_addresses(url):
    parsed = parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("访客检测仅支持公网 HTTPS 地址")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    ips = [entry[4][0] for entry in addresses]
    if not ips or any(not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast for ip in ips):
        raise ValueError("访客检测不允许访问本机、内网或保留地址")
    return parsed, ips


def read_response(response, sock, deadline, limit):
    chunks, size = [], 0
    while size <= limit and not response.isclosed():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        sock.settimeout(remaining)
        chunk = response.read1(min(65536, limit + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def public_post(url, body, headers, timeout, limit=MAX_RESPONSE):
    # Pin the checked IP while preserving hostname certificate verification; no DNS rebinding or proxy bypass.
    parsed, ips = public_addresses(url)
    conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=timeout)
    deadline = time.monotonic() + timeout
    try:
        conn.sock = ssl.create_default_context().wrap_socket(
            socket.create_connection((ips[0], parsed.port or 443), timeout), server_hostname=parsed.hostname)
        sock = conn.sock
        conn.request("POST", parsed.path, body=body, headers=headers)
        response = conn.getresponse()
        if response.status != 200:
            raise ValueError(f"HTTP {response.status}：上游请求失败，请检查接口、模型、凭据或额度")
        return read_response(response, sock, deadline, limit)
    finally:
        conn.close()


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("API 返回重定向，请填写最终 API 地址；未转发密钥")


def upstream_failure(data):
    response = data.get("response")
    detail = data.get("error") or (response.get("error") if isinstance(response, dict) else None) or {}
    if not isinstance(detail, dict):
        detail = {}
    # Classify upstream details without publishing bodies that may contain credentials.
    code = str(detail.get("code", ""))
    message = str(detail.get("message", "")).lower()
    if code in ("timeout", "upstream_timeout", "gateway_timeout") or re.search(r"\b(?:524|504|timeout|timed out)\b", message):
        return "上游网关或模型响应超时，请稍后重试或联系接口服务商"
    if code == "insufficient_quota":
        return "上游额度不足，请检查接口账户余额或限额"
    if code == "rate_limit_exceeded":
        return "上游请求限流，请稍后重试"
    if code == "invalid_api_key":
        return "上游鉴权失败，请检查 API Key"
    return "上游生成失败，请稍后重试或联系接口服务商"


def parse_model_response(raw):
    if not raw.strip():
        raise ValueError("上游返回空响应，请检查接口服务状态")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    if raw.lstrip().startswith(b"<"):
        raise ValueError("上游返回 HTML/XML 页面而非 JSON，请检查 API 地址或网关状态")
    try:
        text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        completed = None
        streaming = False
        for block in text.split("\n\n"):
            lines = block.splitlines()
            payload = "\n".join(line[5:].removeprefix(" ") for line in lines if line.startswith("data:"))
            if not payload:
                continue
            streaming = True
            if payload == "[DONE]":
                continue
            event = json.loads(payload)
            if not isinstance(event, dict):
                raise ValueError("上游返回无效的流式事件")
            kind = event.get("type") or next((line[6:].strip() for line in lines if line.startswith("event:")), "")
            if event.get("error") or kind in ("error", "response.failed"):
                raise ValueError(upstream_failure(event))
            if kind == "response.incomplete":
                raise ValueError("上游响应未完成，可能达到输出上限或被中断")
            if kind == "response.completed":
                completed = event.get("response")
        if completed is not None:
            return completed
        if streaming:
            raise ValueError("上游流式响应缺少完整结果，可能已中断")
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    raise ValueError("上游返回无效 JSON，请检查接口协议或网关状态")


def call_model(config, prompt):
    deadline = time.monotonic() + config["timeout_seconds"]
    streaming = config["protocol"] == "responses"
    common = dict(model=config["model"], stream=streaming)
    limit = MAX_STREAM_RESPONSE if streaming else MAX_RESPONSE
    if config["protocol"] == "responses":
        path = "/responses"
        body = dict(common, input=[dict(role="user", content=prompt)],
                    reasoning=dict(effort=config["effort"]), max_output_tokens=config["max_output_tokens"], store=False)
    else:
        path = "/chat/completions"
        body = dict(common, messages=[dict(role="user", content=prompt)],
                    reasoning_effort=config["effort"], max_completion_tokens=config["max_output_tokens"])
    req = request.Request(config["base_url"] + path, json.dumps(body).encode(),
                          {"Authorization": "Bearer " + config["api_key"], "Content-Type": "application/json", "Accept": "text/event-stream" if streaming else "application/json", "User-Agent": "PelicanWatch/1.0"})
    try:
        if config.get("_guest"):
            raw = public_post(req.full_url, req.data, dict(req.header_items()), config["timeout_seconds"], limit)
        else:
            with request.build_opener(NoRedirect()).open(req, timeout=config["timeout_seconds"]) as response:
                raw = read_response(response, response.fp.raw._sock, deadline, limit)
    except error.HTTPError as exc:
        # Never echo upstream bodies: some gateways include request credentials.
        reasons = {401: "API Key 无效或已过期", 403: "无权访问该模型", 404: "接口或模型不存在，请检查协议", 429: "额度不足或触发限流", 524: "上游网关等待模型响应超时"}
        raise ValueError(f"HTTP {exc.code}：{reasons.get(exc.code, '上游接口请求失败')}") from None
    except TimeoutError:
        raise ValueError("上游响应超时，请稍后重试或检查接口服务状态") from None
    except socket.gaierror:
        raise ValueError("API 域名解析失败，请检查地址拼写或 DNS") from None
    except ssl.SSLCertVerificationError:
        raise ValueError("API HTTPS 证书校验失败，请检查接口证书") from None
    except (ConnectionError, ssl.SSLError):
        raise ValueError("无法建立或保持 API 连接，请检查接口服务和网络") from None
    if len(raw) > limit:
        raise ValueError(f"上游响应超过 {limit // (1024 * 1024)} MB 限制")
    data = parse_model_response(raw)
    if not isinstance(data, dict) or data.get("error"):
        raise ValueError(upstream_failure(data) if isinstance(data, dict) else "上游返回错误响应")
    if len(json.dumps(data).encode()) > MAX_RESPONSE:
        raise ValueError("上游完整结果超过 2 MB 限制")
    if config["protocol"] == "responses":
        if data.get("status") != "completed":
            raise ValueError("上游响应未完成，可能达到输出上限或被中断")
        text = "\n".join(part.get("text", "") for item in data.get("output", [])
                         if item.get("type") == "message" for part in item.get("content", [])
                         if part.get("type") == "output_text")
    else:
        choice = data.get("choices", [{}])[0]
        if choice.get("finish_reason") != "stop":
            raise ValueError("上游响应未完成，可能达到输出上限或被中断")
        text = choice.get("message", {}).get("content", "")
        if isinstance(text, list):
            text = "\n".join(p.get("text", "") for p in text if p.get("type") == "text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("上游未返回可用的文本内容")
    return text, data.get("usage", {}), str(data.get("model", "未返回"))


def inspect_svg(output, nonce):
    checks = dict(svg=False, animation=False, nonce=False)
    match = re.search(r"<svg\b[\s\S]*?</svg\s*>", output, re.I)
    if not match:
        return "", checks, "未找到完整 SVG"
    svg = match.group()
    try:
        if re.search(r"<!DOCTYPE|<!ENTITY|<\?", svg, re.I):
            raise ValueError("SVG 包含不允许的 XML 声明")
        root = ET.fromstring(svg)
        if root.tag not in ("svg", "{http://www.w3.org/2000/svg}svg"):
            raise ValueError("SVG 根元素命名空间不正确")
        for el in root.iter():
            tag = el.tag.split("}")[-1].lower()
            if tag in ("script", "foreignobject", "iframe", "object", "embed", "image", "audio", "video", "a"):
                raise ValueError("SVG 包含脚本、外部资源或不安全元素")
            for attr, value in el.attrib.items():
                name = attr.split("}")[-1].lower()
                if name.startswith("on") or (name in ("href", "src") and not value.startswith("#")):
                    raise ValueError("SVG 包含事件或外部引用")
                if name == "attributename" and (value.lower().startswith("on") or value.lower() in ("href", "xlink:href", "src")):
                    raise ValueError("SVG 动画不允许修改事件或资源引用")
            if tag == "text" and nonce in "".join(el.itertext()):
                checks["nonce"] = True
            if tag in ("animate", "animatetransform", "animatemotion"):
                checks["animation"] = True
        refs = re.findall(r"url\((.*?)\)", svg, re.I | re.S)
        if re.search(r"@import|javascript:|expression\s*\(", svg, re.I) or any(not ref.strip().strip("\"'").startswith("#") for ref in refs):
            raise ValueError("SVG 包含外部或不安全样式")
        if re.search(r"@keyframes\s", svg) and re.search(r"animation(?:-name)?\s*:", svg):
            checks["animation"] = True
        checks["svg"] = True
        if not root.get("xmlns") and root.tag == "svg":
            root.set("xmlns", "http://www.w3.org/2000/svg")
        ET.register_namespace("", "http://www.w3.org/2000/svg")
        svg = ET.tostring(root, encoding="unicode")
    except (ET.ParseError, ValueError) as exc:
        return "", checks, str(exc)
    missing = []
    if not checks["animation"]:
        missing.append("未发现动画声明")
    if not checks["nonce"]:
        missing.append("校验码缺失或不一致")
    return svg, checks, "；".join(missing)


def retryable(exc):
    if isinstance(exc, error.HTTPError):
        return exc.code in (408,429) or 500 <= exc.code < 600
    if isinstance(exc, ssl.SSLCertVerificationError):
        return False
    if isinstance(exc, error.URLError) and isinstance(exc.reason, ssl.SSLCertVerificationError):
        return False
    if isinstance(exc, (TimeoutError, ConnectionError, http.client.IncompleteRead, error.URLError)):
        return True
    message = str(exc)
    return bool(re.search(r"HTTP (?:408|429|5\d\d)\b", message) or any(term in message for term in
                ("响应超时", "无法建立或保持", "上游请求限流", "上游生成失败", "上游返回空响应", "上游返回无效 JSON", "流式响应缺少完整结果")))


def perform_test(config, prompt, nonce, model_call, kind="pelican"):
    started = time.time()
    deadline = time.monotonic() + config["timeout_seconds"]
    attempts = 0
    output, svg, checks, usage, returned_model = "", "", {}, {}, ""
    review = None
    try:
        for attempt in range(config.get("retry_count", 2) + 1):
            attempts += 1
            attempt_config = config if attempt == 0 else dict(config, timeout_seconds=max(.001, deadline-time.monotonic()))
            try:
                output, usage, returned_model = model_call(attempt_config, prompt)
                break
            except Exception as exc:
                delay = min(2 ** attempt, 8)
                if attempt >= config.get("retry_count", 2) or not retryable(exc) or deadline-time.monotonic() <= delay:
                    raise
                time.sleep(delay)
        output = redact(output, config)
        if kind == "candy":
            checks = {"answer_21": candy_passes(output)}
            message = "" if checks["answer_21"] else "回答中未出现独立数字 21"
        else:
            svg, checks, message = inspect_svg(output, nonce)
        status = "passed" if all(checks.values()) else "invalid"
        if kind == "pelican" and status == "passed" and not config.get("_guest"):
            try:
                review = review_pelican(config, svg, model_call)
                status = review["status"] if review["status"] != "uncertain" else "error"
                message = review["reason"]
            except Exception:
                review = {"status": "error", "checks": {}, "reason": "视觉审核未完成：图片输入不受支持、接口请求失败或渲染异常", "version": 1}
                status, message = "error", review["reason"]
    except Exception as exc:
        status = "error"
        message = str(exc) if isinstance(exc, ValueError) else f"连接或解析失败（{type(exc).__name__}），请检查地址、网络和接口协议"
    safe_usage = {k: v for k, v in usage.items() if k in ("input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens") and type(v) is int} if isinstance(usage, dict) else {}
    for key, value in (review or {}).get("usage", {}).items():
        safe_usage[key] = safe_usage.get(key, 0) + value
    return dict(status=status, started=started, finished=time.time(), attempts=attempts, output=output, svg=svg, checks=checks,
                error=redact(message, config)[:1000], usage=safe_usage, returned_model=redact(returned_model, config)[:200], review=review)


def guest_error(message):
    # Only publish fixed diagnoses, never arbitrary exception text or upstream bodies.
    if match := re.match(r"HTTP (\d{3})：", message):
        reasons = {401:"API Key 无效或已过期", 403:"接口拒绝访问，请检查 Key 权限或网关限制",
                   404:"接口不存在，请检查地址和协议", 429:"请求限流或额度不足", 524:"上游网关响应超时"}
        code = int(match[1])
        return f"HTTP {code}：{reasons.get(code, '上游接口请求失败')}"
    for marker, text in (("域名解析失败", "API 域名解析失败，请检查地址拼写或 DNS"),
                         ("证书校验失败", "API HTTPS 证书校验失败，请检查接口证书"),
                         ("本机、内网或保留地址", "访客检测仅支持公网地址，不支持本机、内网或保留地址"),
                         ("超时", "上游响应超时，请稍后重试或检查接口服务"),
                         ("鉴权失败", "API Key 鉴权失败，请检查密钥"),
                         ("额度不足", "上游额度不足，请检查账户余额或限额"),
                         ("限流", "上游请求限流，请稍后重试"),
                         ("HTML/XML", "接口返回网页，请检查 API 地址或网关状态"),
                         ("无效 JSON", "接口返回格式错误，请检查地址和协议"),
                         ("未完成", "模型输出未完成，可能达到输出上限或被中断"),
                         ("缺少完整结果", "上游流式响应中断，未收到完整结果"),
                         ("空响应", "上游未返回内容，请检查接口服务")):
        if marker in message:
            return text
    return "API 连接或生成失败，请检查接口服务、网络和协议"


def combine_tests(results):
    statuses = [r["status"] for r in results.values()]
    status = next((s for s in ("running", "error", "invalid") if s in statuses), "passed")
    pelican = results.get("pelican", {})
    usage = {}
    for result in results.values():
        for key, value in result.get("usage", {}).items():
            usage[key] = usage.get(key, 0) + value
    return dict(status=status, finished=None if status == "running" else max(r["finished"] for r in results.values()),
                output=pelican.get("output", ""), svg=pelican.get("svg", ""), checks=pelican.get("checks", {}),
                error="；".join(("鹈鹕：" if name == "pelican" else "糖果：") + r["error"] for name, r in results.items() if r.get("error")),
                usage=usage, returned_model=pelican.get("returned_model", results.get("candy", {}).get("returned_model", "")),
                tests={name: {k: v for k, v in r.items() if k != "svg" and not (name == "pelican" and k == "output")} for name, r in results.items()})


def perform_suite(config, prompt, nonce, model_call, test_type="both", on_result=None):
    kinds = ("pelican", "candy") if test_type == "both" else (test_type,)
    results = {name: {"status": "running"} for name in kinds}
    with ThreadPoolExecutor(max_workers=len(kinds)) as pool:
        tasks = {pool.submit(perform_test, config, CANDY_PROMPT if name == "candy" else prompt, nonce, model_call, name): name for name in kinds}
        for future in as_completed(tasks):
            results[tasks[future]] = future.result()
            result = combine_tests(results)
            if on_result:
                on_result(result)
    return result


class Monitor:
    def __init__(self, directory, model_call=call_model):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "monitor.sqlite3"
        self.model_call = model_call
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.guest_queue = queue.Queue(maxsize=20)
        # 定时检测队列：每个周期收集一次已启用节点，严格串行执行，避免同时消耗多个上游额度。
        self.scheduled_queue = []
        self.guest_workers = []
        self.sessions = {}
        self.login_failures = []
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS nodes (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE CHECK(length(name) BETWEEN 1 AND 60),
                    base_url TEXT NOT NULL, api_key TEXT NOT NULL, model TEXT NOT NULL,
                    effort TEXT NOT NULL, protocol TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0,1)),
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)));
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_node ON nodes(active) WHERE active=1;
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY, started REAL NOT NULL, finished REAL,
                    status TEXT NOT NULL, source TEXT NOT NULL, model TEXT NOT NULL,
                    base_url TEXT NOT NULL, effort TEXT NOT NULL, protocol TEXT NOT NULL,
                    scene TEXT NOT NULL, nonce TEXT NOT NULL, prompt TEXT NOT NULL,
                    output TEXT DEFAULT '', svg TEXT DEFAULT '', checks TEXT DEFAULT '{}',
                    error TEXT DEFAULT '', usage TEXT DEFAULT '{}', returned_model TEXT DEFAULT '');
                CREATE INDEX IF NOT EXISTS runs_started ON runs(started);
                CREATE TABLE IF NOT EXISTS admin_auth (id INTEGER PRIMARY KEY CHECK(id=1), salt TEXT NOT NULL, password_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS guest_results (id TEXT PRIMARY KEY, created REAL NOT NULL, result TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS guest_created ON guest_results(created);
            """)
            # Credentials are deliberately memory-only; restart never silently retries a billed request.
            for row in db.execute("SELECT id,result FROM guest_results WHERE json_extract(result,'$.status') IN ('queued','running')").fetchall():
                result = json.loads(row["result"])
                self.fail_guest(result, "服务重启，访客任务已中断，请重新提交")
                db.execute("UPDATE guest_results SET created=?,result=? WHERE id=?", (time.time(), json.dumps(result), row["id"]))
            columns = {r[1] for r in db.execute("PRAGMA table_info(runs)")}
            if "test_version" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN test_version INTEGER NOT NULL DEFAULT 1")
            if "tests" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN tests TEXT NOT NULL DEFAULT '{}'")
            if "node_id" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN node_id INTEGER REFERENCES nodes(id)")
                db.execute("ALTER TABLE runs ADD COLUMN node_name TEXT NOT NULL DEFAULT ''")
            node_columns = {r[1] for r in db.execute("PRAGMA table_info(nodes)")}
            if "enabled" not in node_columns:
                db.execute("ALTER TABLE nodes ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1))")
            db.execute("CREATE INDEX IF NOT EXISTS runs_node ON runs(node_id,id)")
            db.execute("INSERT OR IGNORE INTO settings VALUES (1, ?)", (json.dumps(DEFAULTS),))
            config = dict(DEFAULTS, **json.loads(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0]))
            if not db.execute("SELECT 1 FROM nodes").fetchone():
                db.execute("INSERT INTO nodes(name,base_url,api_key,model,effort,protocol,active,enabled) VALUES (?,?,?,?,?,?,1,1)",
                           ("默认节点", *(config[key] for key in NODE_FIELDS)))
            self.store_settings(db, config)
            for row in db.execute("SELECT id,tests FROM runs WHERE status='running'").fetchall():
                tests = json.loads(row["tests"])
                for test in tests.values():
                    if test["status"] == "running":
                        test.update(status="error", finished=time.time(), error="服务重启，本项检测中断")
                db.execute("UPDATE runs SET status='error', finished=?, error='服务重启，上一轮检测中断；未自动重试',tests=? WHERE id=?", (time.time(), json.dumps(tests), row["id"]))
        os.chmod(self.path, 0o600)

    def password_configured(self):
        with self.db() as db:
            return bool(db.execute("SELECT 1 FROM admin_auth WHERE id=1").fetchone())

    def setup_password(self, password):
        if not isinstance(password, str) or not 12 <= len(password) <= 256:
            raise ValueError("管理密码需要 12–256 个字符")
        with self.lock, self.db() as db:
            if db.execute("SELECT 1 FROM admin_auth WHERE id=1").fetchone():
                raise ValueError("管理密码已经设置，请使用密码登录")
            salt = secrets.token_hex(16)
            digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()
            db.execute("INSERT INTO admin_auth VALUES (1,?,?)", (salt,digest))

    def login(self, password):
        with self.lock:
            now = time.time()
            self.login_failures = [at for at in self.login_failures if at > now-60]
            if len(self.login_failures) >= 8:
                raise ValueError("登录失败次数过多，请 1 分钟后重试")
            with self.db() as db:
                auth = db.execute("SELECT salt,password_hash FROM admin_auth WHERE id=1").fetchone()
            valid = False
            if auth and isinstance(password,str) and len(password) <= 256:
                digest = hashlib.pbkdf2_hmac("sha256",password.encode(),bytes.fromhex(auth["salt"]),600000).hex()
                valid = hmac.compare_digest(digest, auth["password_hash"])
            if not valid:
                self.login_failures.append(now)
                raise ValueError("管理密码不正确")
            self.login_failures.clear()
            self.sessions = {key: expiry for key, expiry in self.sessions.items() if expiry > now}
            token = secrets.token_urlsafe(32)
            self.sessions[token] = now+7200
            return token

    def authenticated(self, token):
        with self.lock:
            expiry = self.sessions.get(token,0)
            if expiry <= time.time():
                self.sessions.pop(token,None)
                return False
            return True

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token,None)

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA foreign_keys=ON")
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def settings(self, public=False):
        with self.db() as db:
            config = dict(DEFAULTS, **json.loads(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0]))
            node = db.execute("SELECT * FROM nodes WHERE active=1").fetchone()
            config.update({key: node[key] for key in NODE_FIELDS})
            config.update(active_node_id=node["id"], node_name=redact(node["name"], config))
        if public:
            key = config.pop("api_key")
            config["has_key"] = bool(key)
            config["api_key_masked"] = mask(key) if key else "尚未配置"
            config["base_url"] = mask_url(config["base_url"])
        return config

    @staticmethod
    def store_settings(db, config):
        global_config = {k:v for k,v in config.items() if k not in (*NODE_FIELDS, "active_node_id", "node_name")}
        db.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(global_config),))

    def nodes(self):
        with self.db() as db:
            rows = db.execute("SELECT * FROM nodes ORDER BY id").fetchall()
            latest = {r["node_id"]:dict(r) for r in db.execute("SELECT id,node_id,status,started,finished,error FROM runs WHERE id IN (SELECT MAX(id) FROM runs WHERE node_id IS NOT NULL GROUP BY node_id)")}
        return [dict(id=row["id"], name=redact(row["name"], dict(row)), active=bool(row["active"]),
                     base_url=mask_url(row["base_url"]), has_key=bool(row["api_key"]),
                     api_key_masked=mask(row["api_key"]) if row["api_key"] else "尚未配置", enabled=bool(row["enabled"]),
                     model=redact(row["model"], dict(row)), effort=row["effort"], protocol=row["protocol"],
                     last_run=latest.get(row["id"])) for row in rows]

    def save_node(self, values, node_id=None):
        if not isinstance(values, dict) or set(values) - {"name", *NODE_FIELDS, "enabled"}:
            raise ValueError("节点字段不正确")
        name = values.get("name", "")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60 or any(ord(c)<32 for c in name):
            raise ValueError("请输入 1–60 字符的节点名称")
        with self.lock, self.db() as db:
            old = db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone() if node_id is not None else None
            if node_id is not None and not old:
                raise ValueError("节点不存在")
            enabled = values.get("enabled", bool(old["enabled"]) if old else True)
            if not isinstance(enabled, bool):
                raise ValueError("节点启用状态必须为布尔值")
            if old is None and any(not isinstance(values.get(k), str) or not values[k].strip() for k in ("base_url", "api_key")):
                raise ValueError("新增节点需填写 API 地址和 API Key")
            config = validate_settings(values, dict(DEFAULTS, **({k:old[k] for k in NODE_FIELDS} if old else {})))
            fields = (name.strip(), *(config[k] for k in NODE_FIELDS))
            try:
                if old:
                    db.execute("UPDATE nodes SET name=?,base_url=?,api_key=?,model=?,effort=?,protocol=?,enabled=? WHERE id=?", (*fields,enabled,node_id))
                else:
                    node_id = db.execute("INSERT INTO nodes(name,base_url,api_key,model,effort,protocol,enabled) VALUES (?,?,?,?,?,?,?)", (*fields,enabled)).lastrowid
            except sqlite3.IntegrityError:
                raise ValueError("节点名称已存在，请使用其他名称") from None
        return next(n for n in self.nodes() if n["id"] == node_id)

    def copy_node(self, node_id):
        with self.lock, self.db() as db:
            node = db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
            if not node:
                raise ValueError("节点不存在")
            base = f"{node['name']}（副本）"
            name, suffix = base, 2
            while db.execute("SELECT 1 FROM nodes WHERE name=?", (name,)).fetchone():
                name, suffix = f"{base} {suffix}", suffix + 1
            new_id = db.execute("INSERT INTO nodes(name,base_url,api_key,model,effort,protocol,enabled) VALUES (?,?,?,?,?,?,?)",
                                (name, *(node[key] for key in NODE_FIELDS), node["enabled"])).lastrowid
        return next(n for n in self.nodes() if n["id"] == new_id)

    def set_node_enabled(self, node_id, enabled):
        with self.lock, self.db() as db:
            node = db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
            if not node:
                raise ValueError("节点不存在")
            if not enabled and node["active"]:
                raise ValueError("当前节点不能停用，请先切换到其他已启用节点")
            if enabled and not node["api_key"]:
                raise ValueError("请先为该节点配置 API Key")
            db.execute("UPDATE nodes SET enabled=? WHERE id=?", (int(enabled), node_id))
        return self.nodes()

    def activate_node(self, node_id):
        with self.lock, self.db() as db:
            node = db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
            if not node:
                raise ValueError("节点不存在")
            if not node["enabled"]:
                raise ValueError("该节点已停用，请先启用后再设为当前")
            if not node["api_key"]:
                raise ValueError("请先为该节点配置 API Key")
            db.execute("UPDATE nodes SET active=0 WHERE active=1")
            db.execute("UPDATE nodes SET active=1 WHERE id=?", (node_id,))
        return self.settings(public=True)

    def save(self, values):
        with self.lock:
            config = validate_settings(values, self.settings())
            with self.db() as db:
                db.execute("UPDATE nodes SET base_url=?,api_key=?,model=?,effort=?,protocol=? WHERE active=1", tuple(config[k] for k in NODE_FIELDS))
                self.store_settings(db, config)
        return self.settings(public=True)

    def start_run(self, source="manual", now=None, node_id=None):
        now = time.time() if now is None else now
        with self.lock, self.db() as db:
            config = self.settings()
            if node_id is not None:
                node = db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
                if not node:
                    raise ValueError("节点不存在")
                if not node["enabled"]:
                    raise ValueError("该节点已停用")
                config.update({key:node[key] for key in NODE_FIELDS})
                config.update(active_node_id=node["id"], node_name=redact(node["name"], config))
            if not config["api_key"]:
                raise ValueError("请先在检测设置中填写 API Key")
            # 调度器会为每个节点排队；只有不指定节点的旧式调用才读取全局 next_run。
            if source == "scheduled" and (not config["enabled"] or (node_id is None and (config["next_run"] or float("inf")) > now)):
                return None
            if db.execute("SELECT 1 FROM runs WHERE status='running'").fetchone():
                if source == "scheduled":
                    return None
                raise ValueError("已有检测正在运行，请等待完成")
            scene, nonce = secrets.choice(SCENES), secrets.token_hex(4).upper()
            prompt = PROMPT.format(scene=scene, nonce=nonce)
            row = db.execute("""INSERT INTO runs (started,status,source,model,base_url,effort,protocol,scene,nonce,prompt,test_version,tests,node_id,node_name)
                VALUES (?,'running',?,?,?,?,?,?,?,?,2,?,?,?)""", (now, source, config["model"], config["base_url"], config["effort"], config["protocol"], scene, nonce, prompt, json.dumps({"pelican":{"status":"running"},"candy":{"status":"running"}}),config["active_node_id"],config["node_name"]))
            run_id = row.lastrowid
            if source == "scheduled" and node_id is None:
                config["next_run"] = next_slot(now, config["interval_minutes"] * 60)
                self.store_settings(db, config)
        threading.Thread(target=self.execute, args=(run_id, config, prompt, nonce), daemon=True).start()
        return run_id

    def execute(self, run_id, config, prompt, nonce):
        def persist(result):
            with self.db() as db:
                db.execute("UPDATE runs SET status=?,finished=?,output=?,svg=?,checks=?,error=?,usage=?,returned_model=?,tests=? WHERE id=?",
                           (result["status"], result["finished"], result["output"], result["svg"], json.dumps(result["checks"]), result["error"], json.dumps(result["usage"]), result["returned_model"], json.dumps(result["tests"]), run_id))
        perform_suite(config, prompt, nonce, self.model_call, on_result=persist)

    def prepare_guest(self, values):
        if not self.settings()["guest_enabled"]:
            raise ValueError("访客检测暂未开放")
        allowed = {"base_url", "api_key", "model", "effort", "protocol", "test_type", "retry_count"}
        if set(values) - allowed:
            raise ValueError("访客请求包含不支持的字段")
        if any(not isinstance(values.get(k),str) or not values[k].strip() for k in ("base_url","api_key")):
            raise ValueError("请填写你自己的 API 地址和 API Key")
        config = validate_settings(values, DEFAULTS)
        test_type = values.get("test_type", "both")
        if test_type not in ("both", "pelican", "candy"):
            raise ValueError("请选择有效的检测项目")
        if parse.urlsplit(config["base_url"]).scheme != "https":
            raise ValueError("访客检测仅支持公网 HTTPS 地址")
        config["_guest"] = True
        scene, nonce = secrets.choice(SCENES), secrets.token_hex(4).upper()
        kinds = ("pelican", "candy") if test_type == "both" else (test_type,)
        host = parse.urlsplit(config["base_url"]).hostname or ""
        published = dict(id=secrets.token_hex(12), status="queued", submitted=time.time(), started=None, finished=None,
                         scene=scene, model=redact(config["model"], config), effort=config["effort"], test_type=test_type,
                         api_masked=mask(host), checks={}, svg="", tests={name:{"status":"queued"} for name in kinds})
        return config, nonce, published

    def submit_guest(self, values):
        config, nonce, published = self.prepare_guest(values)
        with self.lock:
            if self.guest_queue.full():
                raise queue.Full()
            if not self.guest_workers:
                for _ in range(4):
                    worker = threading.Thread(target=self.guest_worker, daemon=True)
                    worker.start()
                    self.guest_workers.append(worker)
            with self.db() as db:
                db.execute("INSERT INTO guest_results VALUES (?,?,?)", (published["id"], published["submitted"], json.dumps(published)))
            accepted = self.guest_view(published)
            self.guest_queue.put_nowait((config, nonce, published))
        return accepted

    def guest_worker(self):
        while not self.stopped.is_set():
            try:
                config, nonce, published = self.guest_queue.get(timeout=.2)
            except queue.Empty:
                continue
            try:
                self.execute_guest(config, nonce, published)
            except Exception:
                self.fail_guest(published, "访客任务执行异常，请重新提交")
                try:
                    self.store_guest(published)
                except Exception:
                    print("Guest task persistence failed", flush=True)
            finally:
                config.clear()
                self.guest_queue.task_done()

    @staticmethod
    def fail_guest(published, reason):
        published.update(status="error", finished=time.time())
        for test in published["tests"].values():
            if test["status"] in ("queued", "running"):
                test.update(status="error", finished=published["finished"], error=reason)

    def store_guest(self, published):
        terminal = published["status"] not in ("queued", "running")
        with self.db() as db:
            db.execute("UPDATE guest_results SET created=?,result=? WHERE id=?",
                       (published["finished"] if terminal else published["submitted"], json.dumps(published), published["id"]))

    def execute_guest(self, config, nonce, published):
        published.update(started=time.time(), status="running")
        published["tests"] = {name:{"status":"running"} for name in published["tests"]}
        self.store_guest(published)
        def persist(result):
            published.update({key: result[key] for key in ("status", "finished", "checks", "svg")})
            # Explicit public whitelist: no full address, key, raw upstream error or visual review.
            for name, test in result["tests"].items():
                public_test = {key: test[key] for key in ("status", "checks", "started", "finished", "attempts") if key in test}
                if name == "candy":
                    public_test["output"] = test.get("output", "")
                if test.get("error"):
                    public_test["error"] = guest_error(test["error"]) if test["status"] == "error" else ("回答中未出现独立数字 21" if name == "candy" else "SVG 基础校验未通过")
                published["tests"][name] = public_test
            self.store_guest(published)
        perform_suite(config, PROMPT.format(scene=published["scene"], nonce=nonce), nonce, self.model_call,
                      published["test_type"], on_result=persist)
        self.prune_guests()
        return self.guest_view(published)

    def guest_test(self, values):
        """Synchronous core for local checks; HTTP submissions always use the bounded queue."""
        config, nonce, published = self.prepare_guest(values)
        with self.db() as db:
            db.execute("INSERT INTO guest_results VALUES (?,?,?)",(published["id"],time.time(),json.dumps(published)))
        return self.execute_guest(config, nonce, published)

    def prune_guests(self):
        with self.db() as db:
            terminal = "json_extract(result,'$.status') NOT IN ('queued','running')"
            db.execute("DELETE FROM guest_results WHERE " + terminal + " AND created<?",(time.time()-3600,))
            db.execute("DELETE FROM guest_results WHERE " + terminal + " AND id NOT IN (SELECT id FROM guest_results WHERE " + terminal + " ORDER BY created DESC LIMIT 100)")

    @staticmethod
    def guest_view(result):
        public = dict(result)
        public["has_svg"] = bool(public.pop("svg"))
        public["tests"] = {name: {key:value for key,value in test.items() if key != "review"} for name,test in public.get("tests", {}).items()}
        public["error"] = "；".join(("鹈鹕：" if name == "pelican" else "糖果：") + test["error"] for name,test in public.get("tests",{}).items() if test.get("error"))
        return public

    def guest_results(self):
        with self.db() as db:
            rows = db.execute("SELECT result FROM guest_results WHERE created>=? OR json_extract(result,'$.status') IN ('queued','running') ORDER BY created DESC",(time.time()-3600,)).fetchall()
        results = [self.guest_view(json.loads(row[0])) for row in rows]
        queued = sorted((r for r in results if r['status'] == 'queued'), key=lambda r:(r['submitted'],r['id']))
        for position, result in enumerate(queued, 1):
            result['queue_position'] = position
        return results

    def scheduler(self):
        last_cleanup = 0
        while not self.stopped.wait(2):
            try:
                now = time.time()
                if now-last_cleanup > 60:
                    self.prune_guests()
                    last_cleanup = now

                with self.lock, self.db() as db:
                    settings = self.settings()
                    if not settings["enabled"]:
                        self.scheduled_queue.clear()
                        continue
                    if not self.scheduled_queue and (settings["next_run"] or float("inf")) <= now:
                        self.scheduled_queue = [row["id"] for row in db.execute(
                            "SELECT id FROM nodes WHERE enabled=1 AND length(trim(api_key))>0 ORDER BY id"
                        ).fetchall()]
                        # 保持 next_run 处于到期状态，直到本轮节点全部完成；重启后会重新收集到期节点。
                        if not self.scheduled_queue:
                            settings["next_run"] = next_slot(now, settings["interval_minutes"] * 60)
                            self.store_settings(db, settings)
                    next_node = self.scheduled_queue[0] if self.scheduled_queue else None
                    running = db.execute("SELECT 1 FROM runs WHERE status='running' LIMIT 1").fetchone()

                if next_node is not None and not running:
                    try:
                        run_id = self.start_run("scheduled", now=now, node_id=next_node)
                    except ValueError as exc:
                        print(f"Scheduler node skipped: {type(exc).__name__}", flush=True)
                        run_id = False
                    if run_id is not None:
                        with self.lock:
                            if self.scheduled_queue and self.scheduled_queue[0] == next_node:
                                self.scheduled_queue.pop(0)
                                if not self.scheduled_queue:
                                    with self.db() as db:
                                        settings = self.settings()
                                        settings["next_run"] = next_slot(now, settings["interval_minutes"] * 60)
                                        self.store_settings(db, settings)
            except Exception as exc:
                print(f"Scheduler error: {type(exc).__name__}", flush=True)

    def runs(self, before=None):
        with self.db() as db:
            rows = db.execute("SELECT * FROM runs WHERE id < ? ORDER BY id DESC LIMIT 48", (before or 2**63-1,)).fetchall()
        return [self.serialize(row) for row in rows]

    def stats(self):
        since = time.time() - 86400
        with self.db() as db:
            total, passed, invalid, failed, running = db.execute("""
                SELECT COUNT(*), COALESCE(SUM(status='passed'),0), COALESCE(SUM(status='invalid'),0),
                       COALESCE(SUM(status='error'),0), COALESCE(SUM(status='running'),0)
                FROM runs WHERE started>=?
            """, (since,)).fetchone()
            enabled_nodes = db.execute("SELECT COUNT(*) FROM nodes WHERE enabled=1 AND length(trim(api_key))>0").fetchone()[0]
        return dict(window_hours=24, total=total, passed=passed, invalid=invalid,
                    failed=failed, running=running, enabled_nodes=enabled_nodes)

    def retry_run(self, run_id):
        with self.db() as db:
            row = db.execute("SELECT status,node_id FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise ValueError("检测记录不存在")
        if row["status"] not in ("error", "invalid"):
            raise ValueError("只有失败或校验未通过的记录可以重试")
        return self.start_run(source="retry", node_id=row["node_id"])

    def delete_runs(self, run_ids):
        if not isinstance(run_ids, list) or not run_ids or len(run_ids) > 1000:
            raise ValueError("请提供 1–1000 个检测记录编号")
        if any(isinstance(run_id, bool) or type(run_id) is not int or run_id < 1 for run_id in run_ids):
            raise ValueError("检测记录编号无效")
        run_ids = list(dict.fromkeys(run_ids))
        placeholders = ",".join("?" for _ in run_ids)
        with self.lock, self.db() as db:
            rows = db.execute(f"SELECT id,status FROM runs WHERE id IN ({placeholders})", run_ids).fetchall()
            if len(rows) != len(run_ids):
                raise ValueError("检测记录不存在")
            if any(row["status"] == "running" for row in rows):
                raise ValueError("检测正在运行，完成后才可以删除")
            db.execute(f"DELETE FROM runs WHERE id IN ({placeholders})", run_ids)
        return {"deleted": len(run_ids)}

    def gallery(self, page=1, status="all", protocol="all", source="all", effort="all",
                has_svg="all", group_by="none"):
        allowed = {
            "status": ("all", "passed", "invalid", "error", "running", "legacy"),
            "protocol": ("all", "responses", "chat"),
            "source": ("all", "manual", "scheduled"),
            "effort": ("all", "low", "medium", "high", "xhigh"),
            "has_svg": ("all", "yes", "no"),
            "group_by": ("none", "node", "model", "date"),
        }
        values = locals()
        if type(page) is not int or not 1 <= page <= 1000000 or any(
            values[key] not in options for key, options in allowed.items()
        ):
            raise ValueError("画廊分页或筛选参数无效")
        clauses, args = [], []
        if status == "legacy":
            clauses.append("test_version = 1")
        elif status != "all":
            clauses.extend(("test_version >= 2", "status = ?"))
            args.append(status)
        for column, value in (("protocol", protocol), ("source", source), ("effort", effort)):
            if value != "all":
                clauses.append(f"{column} = ?")
                args.append(value)
        if has_svg != "all":
            clauses.append("length(svg) > 0" if has_svg == "yes" else "(svg IS NULL OR length(svg) = 0)")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        group_expression = {
            "node": "COALESCE(NULLIF(node_name, ''), '历史节点')",
            "model": "model",
            "date": "strftime('%Y-%m-%d', started, 'unixepoch', '+8 hours')",
        }.get(group_by)
        with self.db() as db:
            total = db.execute("SELECT COUNT(*) FROM runs"+where,args).fetchone()[0]
            pages = max(1, math.ceil(total/12))
            page = min(page,pages)
            order = f"{group_expression} ASC, id DESC" if group_expression else "id DESC"
            rows = db.execute("SELECT * FROM runs"+where+f" ORDER BY {order} LIMIT 12 OFFSET ?",[*args,(page-1)*12]).fetchall()
        items = [self.serialize(row) for row in rows]
        for item in items:
            item["group_key"] = ((item.get("node_name") or "历史节点") if group_by == "node" else
                                  item.get("model") if group_by == "model" else
                                  time.strftime("%Y-%m-%d", time.gmtime(item["started"] + 8 * 3600)) if group_by == "date" else None)
        return dict(items=items,total=total,page=page,pages=pages,group_by=group_by)

    @staticmethod
    def serialize(row, detail=False):
        result = dict(row)
        result["has_svg"] = bool(result.pop("svg"))
        config = {"base_url": result["base_url"]}
        for key in ("output", "error", "returned_model", "model"):
            result[key] = redact(result[key], config)
        result["base_url"] = mask_url(result["base_url"])
        for key in ("checks", "usage", "tests"):
            result[key] = json.loads(result[key])
        result["candy_status"] = result["tests"].get("candy",{}).get("status","not_run") if result["test_version"] >= 2 else "not_run"
        if result["test_version"] == 1:
            result["tests"] = {"pelican": {"status": result["status"]}, "candy": {"status": "not_run"}}
            result["status"] = "legacy"
        for test in result["tests"].values():
            for key in ("output", "error", "returned_model"):
                if key in test:
                    test[key] = redact(test[key], config)
        if detail and result["test_version"] >= 2:
            result["candy_prompt"] = CANDY_PROMPT
        if not detail:
            for key in ("output", "prompt"):
                result.pop(key)
            for test in result["tests"].values():
                test.pop("output", None)
        return result

    def state(self):
        now = time.time()
        with self.db() as db:
            rows = db.execute("SELECT * FROM runs WHERE started>=? ORDER BY id DESC", (now-86400,)).fetchall()
            running = db.execute("SELECT id FROM runs WHERE status='running'").fetchone()
        results = [self.serialize(r) for r in rows]
        current = [r for r in results if r["candy_status"] != "not_run"]
        completed = [r for r in current if r["candy_status"] != "running"]
        settings = self.settings(public=True)
        public = {key: settings[key] for key in ("base_url", "model", "effort", "enabled", "next_run", "interval_minutes", "guest_enabled", "node_name", "active_node_id")}
        with self.lock:
            queued_ids = list(self.scheduled_queue)
        with self.db() as db:
            queued_nodes = [f"节点 {index}" for index, row in enumerate(db.execute(
                f"SELECT name FROM nodes WHERE id IN ({','.join('?' for _ in queued_ids)}) ORDER BY id", queued_ids
            ).fetchall(), 1)] if queued_ids else []
        return dict(settings=public, server_time=now,
                    running=running[0] if running else None, queued_nodes=queued_nodes,
                    candy_running=any(r["candy_status"]=="running" for r in current), timeline=results,
                    stats=dict(total=len(current), legacy=len(results)-len(current), passed=sum(r["candy_status"] == "passed" for r in completed),
                               completed=len(completed), errors=sum(r["candy_status"] == "error" for r in completed),
                               invalid=sum(r["candy_status"] == "invalid" for r in completed)))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, data, content_type="application/json; charset=utf-8", status=200, svg=False):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        policy = "sandbox; default-src 'none'; style-src 'unsafe-inline'" if svg else "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        self.send_header("Content-Security-Policy", policy)
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        return self.server.monitor.authenticated(self.headers.get("Authorization", "").removeprefix("Bearer "))

    def local_setup(self):
        host = parse.urlsplit("http://" + self.headers.get("Host", "")).hostname
        return (self.server.server_address[0] in ("127.0.0.1", "::1") and host in ("127.0.0.1", "localhost", "::1")
                and self.client_address[0] in ("127.0.0.1", "::1") and not self.headers.get("Forwarded") and not self.headers.get("X-Forwarded-For"))

    def do_GET(self):
        url = parse.urlsplit(self.path)
        if url.path.startswith("/api/"):
            monitor = self.server.monitor
            if url.path == "/api/auth/status":
                return self.send({"configured": monitor.password_configured(), "setup_allowed": self.local_setup()})
            if url.path == "/api/guest/results":
                return self.send(monitor.guest_results())
            guest_match = re.fullmatch(r"/api/guest/results/([0-9a-f]{24})(/svg)?",url.path)
            if guest_match:
                with monitor.db() as db:
                    row = db.execute("SELECT result FROM guest_results WHERE id=? AND (created>=? OR json_extract(result,'$.status') IN ('queued','running'))",(guest_match[1],time.time()-3600)).fetchone()
                if row:
                    result = json.loads(row[0])
                    if not guest_match[2]:
                        return self.send(monitor.guest_view(result))
                    if svg := result.get("svg"):
                        return self.send(svg.encode(),"image/svg+xml; charset=utf-8",svg=True)
                return self.send({"error":"结果已过期或不存在"},status=404)
            if url.path.startswith("/api/admin/"):
                if not self.authorized():
                    return self.send({"error": "请输入管理口令"}, status=401)
                if url.path == "/api/admin/settings":
                    return self.send(monitor.settings(public=True))
                if url.path == "/api/admin/nodes":
                    return self.send(monitor.nodes())
                if url.path == "/api/admin/runs":
                    return self.send(monitor.runs())
                if url.path == "/api/admin/stats":
                    return self.send(monitor.stats())
            if url.path == "/api/state":
                return self.send(monitor.state())
            if url.path == "/api/runs":
                params = parse.parse_qs(url.query)
                if "page" in params:
                    try:
                        return self.send(monitor.gallery(
                            page=int(params["page"][0]), status=params.get("status", ["all"])[0],
                            protocol=params.get("protocol", ["all"])[0], source=params.get("source", ["all"])[0],
                            effort=params.get("effort", ["all"])[0], has_svg=params.get("has_svg", ["all"])[0],
                            group_by=params.get("group_by", ["none"])[0]))
                    except ValueError:
                        return self.send({"error":"画廊分页或筛选参数无效"},status=400)
                before = parse.parse_qs(url.query).get("before", [None])[0]
                if before and (not before.isdigit() or len(before) > 18):
                    return self.send({"error": "无效分页参数"}, status=400)
                return self.send(monitor.runs(int(before) if before else None))
            match = re.fullmatch(r"/api/runs/(\d{1,18})(/svg)?", url.path)
            if match:
                with monitor.db() as db:
                    row = db.execute("SELECT * FROM runs WHERE id=?", (int(match[1]),)).fetchone()
                if row:
                    if match[2] and row["svg"]:
                        return self.send(redact(row["svg"], {"base_url":row["base_url"]}).encode(), "image/svg+xml; charset=utf-8", svg=True)
                    if not match[2]:
                        return self.send(monitor.serialize(row, detail=True))
            return self.send({"error": "未找到记录"}, status=404)
        files = {"/": "index.html", "/admin": "admin.html", "/admin/": "admin.html", "/admin.js": "admin.js", "/privacy.js": "privacy.js", "/app.js": "app.js", "/style.css": "style.css", "/favicon.svg": "favicon.svg"}
        name = files.get(url.path)
        if name:
            types = {"html": "text/html", "js": "text/javascript", "css": "text/css", "svg": "image/svg+xml"}
            data = (ROOT / "web" / name).read_bytes()
            if name.endswith(".html"):
                commit = os.environ.get("ZEABUR_GIT_COMMIT_SHA") or os.environ.get("GIT_COMMIT") or "dev"
                commit = re.sub(r"[^0-9A-Za-z._-]", "", commit)[:12] or "dev"
                data = data.replace(b"__GIT_COMMIT__", commit.encode())
            return self.send(data, types[name.split(".")[-1]] + "; charset=utf-8")
        self.send({"error": "页面不存在"}, status=404)

    def do_POST(self):
        if self.path not in ("/api/guest/run", "/api/auth/login", "/api/auth/setup") and not self.authorized():
            return self.send({"error": "请输入管理口令"}, status=401)
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.send({"error": "仅接受 JSON 请求"}, status=415)
        origin = self.headers.get("Origin")
        if origin and parse.urlsplit(origin).netloc != self.headers.get("Host"):
            return self.send({"error": "不接受跨站请求"}, status=403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384:
                raise ValueError("请求大小无效")
            values = json.loads(self.rfile.read(length))
            if not isinstance(values, dict):
                raise ValueError("请求必须为 JSON 对象")
            if self.path == "/api/auth/setup":
                if not self.local_setup():
                    return self.send({"error":"首次设置密码仅允许在服务所在机器完成"},status=403)
                self.server.monitor.setup_password(values.get("password"))
                return self.send({"configured":True})
            if self.path == "/api/auth/login":
                try:
                    return self.send({"token":self.server.monitor.login(values.get("password"))})
                except ValueError as exc:
                    return self.send({"error":str(exc)},status=401)
            if self.path == "/api/auth/logout":
                self.server.monitor.logout(self.headers.get("Authorization", "").removeprefix("Bearer "))
                return self.send({"ok":True})
            if self.path == "/api/admin/settings":
                return self.send(self.server.monitor.save(values))
            if self.path == "/api/admin/nodes":
                return self.send(self.server.monitor.save_node(values), status=201)
            node_action = re.fullmatch(r"/api/admin/nodes/([1-9]\d{0,17})(?:/(activate|run))?", self.path)
            if node_action:
                node_id = int(node_action[1])
                if node_action[2] == "activate":
                    return self.send(self.server.monitor.activate_node(node_id))
                if node_action[2] == "run":
                    return self.send({"id":self.server.monitor.start_run(node_id=node_id)}, status=202)
                return self.send(self.server.monitor.save_node(values,node_id))
            node_copy = re.fullmatch(r"/api/admin/nodes/([1-9]\d{0,17})/copy", self.path)
            if node_copy:
                return self.send(self.server.monitor.copy_node(int(node_copy[1])), status=201)
            node_toggle = re.fullmatch(r"/api/admin/nodes/([1-9]\d{0,17})/(enable|disable)", self.path)
            if node_toggle:
                return self.send(self.server.monitor.set_node_enabled(int(node_toggle[1]), node_toggle[2] == "enable"))
            retry = re.fullmatch(r"/api/admin/runs/([1-9]\d{0,17})/retry", self.path)
            if retry:
                return self.send({"id": self.server.monitor.retry_run(int(retry[1]))}, status=202)
            if self.path == "/api/admin/runs/delete":
                return self.send(self.server.monitor.delete_runs(values.get("ids")))
            if self.path == "/api/admin/run":
                return self.send({"id": self.server.monitor.start_run()}, status=202)
            if self.path == "/api/guest/run":
                try:
                    result = self.server.monitor.submit_guest(values)
                except queue.Full:
                    return self.send({"error": "访客队列已满，请稍后再试"}, status=429)
                return self.send(result, status=202)
            return self.send({"error": "接口不存在"}, status=404)
        except (ValueError, TypeError) as exc:
            self.send({"error": str(exc)}, status=400)


def _request_stop(*_):
    """把 SIGTERM 转成 KeyboardInterrupt，走和 Ctrl-C 相同的收尾路径。"""
    raise KeyboardInterrupt


def acquire_instance_lock(directory):
    """取得数据目录的独占锁，调用方需持有返回的文件对象，否则锁会随之释放。

    平台重建容器时旧实例可能还在停止过程中，宽限期内它仍然活着并持有锁，因此放弃
    之前留出一段重试窗口，免得把能自愈的竞争变成部署失败。窗口要盖过平台的停止宽限
    期（Kubernetes 默认 30 秒），否则旧实例还没退干净，新实例就先放弃了。
    """
    instance_lock = (directory / "server.lock").open("a")
    deadline = time.monotonic() + LOCK_WAIT
    while True:
        try:
            fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return instance_lock
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise SystemExit(f"此数据目录已有检测服务运行（已等待 {LOCK_WAIT} 秒），"
                                 "不能启动第二个调度进程") from None
            time.sleep(1)


def main():
    os.umask(0o077)
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8765"))
    token = os.environ.get("ADMIN_TOKEN", "")
    directory = Path(os.environ.get("DATA_DIR", str(ROOT / "data")))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # ponytail: one process owns scheduling; use a distributed lease before running multiple replicas.
    instance_lock = acquire_instance_lock(directory)
    monitor = Monitor(directory)
    if token and not monitor.password_configured():
        # 只在初始化时校验；已设过密码的实例继续忽略 ADMIN_TOKEN，改坏了也不至于起不来。
        if not 12 <= len(token) <= 256:
            raise SystemExit(f"ADMIN_TOKEN 需要 12–256 个字符，当前为 {len(token)} 个，"
                             "请修改环境变量后重新部署")
        monitor.setup_password(token)
    if host not in ("127.0.0.1", "localhost") and not monitor.password_configured():
        raise SystemExit("未设置管理密码：请通过 ADMIN_TOKEN 环境变量提供 12–256 个字符的初始密码后重新部署；"
                         "仅监听本机地址时也可以在页面上设置")
    # 配置齐备之后才开端口，免得平台的健康探针先看到一个随即退出的服务。
    server = ThreadingHTTPServer((host, port), Handler)
    server.monitor = monitor
    threading.Thread(target=monitor.scheduler, daemon=True).start()
    print(f"Pelican Watch: http://{host}:{port}", flush=True)
    # PID 1 不执行信号的默认处置，不接管 SIGTERM 就只能等平台宽限期结束后被 SIGKILL。
    # 转成 KeyboardInterrupt 交给下面现成的退出路径；注意不能在这里调 server.shutdown()，
    # 它必须由 serve_forever 之外的线程调用，从主线程调会死锁。
    signal.signal(signal.SIGTERM, _request_stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stopped.set()
        server.server_close()
        instance_lock.close()


if __name__ == "__main__":
    main()
