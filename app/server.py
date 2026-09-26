"""Single-task pelican SVG request monitor."""
import contextlib
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
from http.cookies import CookieError, SimpleCookie
from html.parser import HTMLParser
from urllib import error, parse, request
import xml.etree.ElementTree as ET

from codex_runner import call_codex, login_status, available_models
from local_runtime import lock_file

ROOT = Path(__file__).resolve().parent
LOCK_WAIT = 45
MAX_RESPONSE = 2 * 1024 * 1024
SESSION_TTL = 30 * 24 * 60 * 60
SESSION_COOKIE = "pelican_session"
# ponytail: buffer SSE up to 16 MB; parse incrementally if concurrent streams strain memory.
MAX_STREAM_RESPONSE = 16 * 1024 * 1024
PROMPT = "创建一个HTML，内容是用SVG绘制一个鹈鹕骑自行车的2D动画，你不能进行任何测试，不能调用skills，不能网络检索，不能调用子智能体，直接生成"
DEFAULTS = dict(base_url="codex://local", model="gpt-6-luna",
                effort="low", protocol="codex", api_key="", enabled=False, next_run=None,
                interval_seconds=1800, task_prompt=PROMPT, timeout_seconds=600, max_output_tokens=16000, guest_enabled=False, retry_count=0, schedule_mode="single")
NODE_FIELDS = ("base_url", "api_key", "model", "effort", "protocol")
MODEL_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
RUN_SELECT = "SELECT r.*, i.id AS library_image_id, i.content_type AS library_content_type, i.svg AS library_svg, i.created AS library_created FROM runs r LEFT JOIN image_library i ON i.run_id=r.id"

def normalized_settings(saved):
    saved = dict(saved)
    if "interval_seconds" not in saved and "interval_minutes" in saved:
        saved["interval_seconds"] = int(saved["interval_minutes"] * 60)
    return dict(DEFAULTS, **{key: value for key, value in saved.items() if key in DEFAULTS})


def model_combinations(models):
    """Return the exact model/effort pairs used by the admin combo builder."""
    combinations = []
    seen = set()
    for model in models or ():
        model_id = model.get("model") if isinstance(model, dict) else ""
        if not isinstance(model_id, str) or not model_id:
            continue
        efforts = model.get("efforts") if isinstance(model, dict) else None
        efforts = efforts if isinstance(efforts, list) and efforts else MODEL_EFFORTS
        for effort in efforts:
            key = (model_id, effort)
            if effort in MODEL_EFFORTS and key not in seen:
                seen.add(key)
                combinations.append({"model": model_id, "effort": effort})
    return combinations


def request_status(status):
    """Expose only request outcomes while normalizing records from older runs."""
    if status in ("passed", "invalid", "success"):
        return "success"
    if status in ("running", "queued", "error", "cancelled"):
        return status
    return "error"


def mask(value):
    return value[:3] + "****" + value[-3:] if len(value) > 8 else "****"


def filename_slug(value, fallback="item", limit=48):
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip(".-_")[:limit]
    return slug or fallback


def record_value(record, key, default=None):
    try:
        return record.get(key, default)
    except AttributeError:
        try:
            return record[key]
        except (IndexError, KeyError, TypeError):
            return default


def run_filename(record):
    started = record_value(record, "started") or record_value(record, "submitted") or record_value(record, "created") or 0
    # Match the gallery's UTC+8 display, even when the server runs in UTC.
    stamp = time.strftime("%m%d-%H%M%S", time.gmtime(float(started) + 8 * 3600))
    model = re.sub(r"^gpt-(?=\d)", "gpt", filename_slug(record_value(record, "model"), "model"), flags=re.I)
    return f"{model}-{stamp}.svg"


def guest_filename(result):
    return run_filename(result)


def mask_url(value):
    if value == "codex://local":
        return "本机 Codex · ChatGPT 登录"
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
    values = dict(values)
    if "interval_minutes" in values and "interval_seconds" not in values:
        minutes = values["interval_minutes"]
        if type(minutes) is not int or not 1 <= minutes <= 1440:
            raise ValueError("interval_minutes 必须为 1–1440 的整数")
        values["interval_seconds"] = minutes * 60
    new = dict(old, **normalized_settings(old))
    if "task_prompt" in values:
        prompt = values["task_prompt"]
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000 or "\x00" in prompt:
            raise ValueError("提示词必须为 1–4000 个字符，且不能全为空白")
        new["task_prompt"] = prompt
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
    if new["protocol"] == "codex":
        new["base_url"], new["api_key"] = "codex://local", ""
    base = new["base_url"]
    if "://" not in base:
        base = "https://" + base.removeprefix("//")
    if any(c.isspace() or ord(c) < 32 for c in base) or "\\" in base:
        raise ValueError("API 地址不能包含空格、换行或反斜杠")
    url = parse.urlsplit(base)
    if new["protocol"] != "codex" and ((url.scheme != "https" and not (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1"))) or not url.hostname or url.username or url.password or url.query or url.fragment):
        raise ValueError("请填写 HTTPS API 地址，不要包含密钥、查询参数或账号密码")
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError("API 地址端口无效")
    base = base.rstrip("/")
    for suffix in ("/chat/completions", "/responses"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
    if new["protocol"] != "codex" and not parse.urlsplit(base).path:
        base += "/v1"
    new["base_url"] = base
    if not new["model"] or len(new["model"]) > 120 or new["model"].startswith("-") or any(ord(c) < 32 for c in new["model"]):
        raise ValueError("请输入有效的模型名称")
    if new["effort"] not in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra") or new["protocol"] not in ("responses", "chat", "codex"):
        raise ValueError("思考强度或接口协议不正确")
    if any(ord(c) < 32 or ord(c) > 126 for c in new["api_key"]):
        raise ValueError("API Key 格式不正确")
    for key in ("enabled", "guest_enabled"):
        if key in values:
            if not isinstance(values[key], bool):
                raise ValueError("配置值必须为布尔值")
            new[key] = values[key]
    for key, low, high in (("interval_seconds", 1, 86400), ("timeout_seconds", 10, 1800), ("max_output_tokens", 1024, 64000), ("retry_count", 0, 5)):
        if key in values:
            if type(values[key]) is not int or not low <= values[key] <= high:
                raise ValueError(f"{key} 必须为 {low}–{high} 的整数")
            new[key] = values[key]
    if new["api_key"] and new["base_url"] != old["base_url"] and not values.get("api_key", "").strip():
        raise ValueError("更换 API 地址时，请重新填写该地址的 API Key")
    if "schedule_mode" in values:
        if values["schedule_mode"] not in ("single", "groups"):
            raise ValueError("请选择单个模型或循环组")
        new["schedule_mode"] = values["schedule_mode"]
    if new["enabled"] and new.get("schedule_mode", "single") == "single" and not node_ready(new):
        raise ValueError("请先填写 API Key，再启用自动检测")
    new["next_run"] = old.get("next_run") if new["enabled"] else None
    return new


def account_filter(account):
    if account == "all":
        return "", []
    if account == "unknown":
        return "r.protocol='codex' AND r.account_fingerprint=''", []
    if account == "api":
        return "r.protocol!='codex'", []
    if isinstance(account, str) and re.fullmatch(r"acct-[0-9a-f]{12}", account):
        return "r.protocol='codex' AND r.account_fingerprint=?", [account]
    raise ValueError("账号筛选参数无效")


def account_options(db):
    return [dict(value=row[0], label=row[1] or f"账号 {row[0][5:]}", count=row[2]) for row in db.execute(
        "SELECT account_fingerprint,MAX(account_label),count(*) FROM runs "
        "WHERE protocol='codex' AND account_fingerprint!='' GROUP BY account_fingerprint ORDER BY MAX(id) DESC")]


def node_ready(config):
    return config["protocol"] == "codex" or bool(config["api_key"])


def public_addresses(url):
    parsed = parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("访客检测仅支持公网 HTTPS 地址")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    ips = [entry[4][0] for entry in addresses]
    if not ips or any(not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast for ip in ips):
        raise ValueError("访客检测不允许访问本机、内网或保留地址")
    return parsed, ips


def read_response(response, sock, deadline, limit, cancel_event=None):
    chunks, size = [], 0
    while size <= limit and not response.isclosed():
        if cancel_event and cancel_event.is_set():
            raise ValueError("已手动停止生成")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        sock.settimeout(min(remaining, .25) if cancel_event else remaining)
        try:
            chunk = response.read1(min(65536, limit + 1 - size))
        except socket.timeout:
            continue
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def public_post(url, body, headers, timeout, limit=MAX_RESPONSE, cancel_event=None):
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
        return read_response(response, sock, deadline, limit, cancel_event)
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
    if config["protocol"] == "codex":
        return call_codex(config, prompt)
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
            raw = public_post(req.full_url, req.data, dict(req.header_items()), config["timeout_seconds"], limit, config.get("_cancel_event"))
        else:
            with request.build_opener(NoRedirect()).open(req, timeout=config["timeout_seconds"]) as response:
                raw = read_response(response, response.fp.raw._sock, deadline, limit, config.get("_cancel_event"))
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


def inspect_svg(output):
    """Build a safe gallery preview without changing the request outcome."""
    checks = dict(svg=False)
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
        refs = re.findall(r"url\((.*?)\)", svg, re.I | re.S)
        if re.search(r"@import|javascript:|expression\s*\(", svg, re.I) or any(not ref.strip().strip("\"'").startswith("#") for ref in refs):
            raise ValueError("SVG 包含外部或不安全样式")
        checks["svg"] = True
        if not root.get("xmlns") and root.tag == "svg":
            root.set("xmlns", "http://www.w3.org/2000/svg")
        ET.register_namespace("", "http://www.w3.org/2000/svg")
        svg = ET.tostring(root, encoding="unicode")
    except (ET.ParseError, ValueError) as exc:
        return "", checks, str(exc)
    return svg, checks, ""


def extract_html(output):
    """Keep the generated document intact; the preview response isolates it."""
    match = re.search(r"(?:<!doctype\s+html\s*>\s*)?<html\b[\s\S]*?</html\s*>", output or "", re.I)
    return match.group() if match else ""


class SVGDocumentAssets(HTMLParser):
    """Collect inline document assets without duplicating those inside the SVG."""
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.svg_depth = 0
        self.current = None
        self.styles, self.scripts = [], []

    def handle_starttag(self, tag, attrs):
        if tag == "svg":
            self.svg_depth += 1
        if self.svg_depth == 0 and tag in ("style", "script"):
            self.current = (tag, dict(attrs), [])

    def handle_endtag(self, tag):
        if self.current and tag == self.current[0]:
            kind, attrs, parts = self.current
            text = "".join(parts)
            if kind == "style":
                self.styles.append(text)
            elif "src" not in attrs and text.strip():
                self.scripts.append((attrs, text))
            self.current = None
        if tag == "svg":
            self.svg_depth = max(0, self.svg_depth - 1)

    def handle_data(self, data):
        if self.current:
            self.current[2].append(data)


class SVGDocumentContext(HTMLParser):
    """Keep non-graphical controls referenced by the original animation script."""
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    OMIT = {"svg", "script", "style", "iframe", "object", "embed", "img", "audio", "video", "source", "link", "meta", "base"}
    ATTRS = {"id", "class", "type", "value", "name", "min", "max", "step", "checked", "selected", "disabled", "multiple", "for", "role", "open"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = None
        self.stack = []
        self.ancestors = []

    def handle_starttag(self, tag, attrs):
        safe_attrs = {name: value or "" for name, value in attrs
                      if name in self.ATTRS or name.startswith(("data-", "aria-"))}
        if tag == "body" and self.root is None:
            self.root = ET.Element("{http://www.w3.org/1999/xhtml}body", safe_attrs)
            self.stack.append((tag, self.root))
            return
        if not self.stack:
            return
        parent = self.stack[-1][1]
        if tag == "svg" and parent is not None and not self.ancestors:
            self.ancestors = [node for _, node in self.stack if node is not None]
        node = None if parent is None or tag in self.OMIT else ET.SubElement(
            parent, "{http://www.w3.org/1999/xhtml}" + tag, safe_attrs)
        if tag not in self.VOID:
            self.stack.append((tag, node))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_data(self, data):
        node = self.stack[-1][1] if self.stack else None
        if node is not None:
            if len(node):
                node[-1].tail = (node[-1].tail or "") + data
            else:
                node.text = (node.text or "") + data


def downloadable_svg(svg, output):
    """Preserve inline animation drivers in downloads; previews remain script-free."""
    assets = SVGDocumentAssets()
    assets.feed(extract_html(output))
    if not assets.styles and not assets.scripts:
        return svg
    try:
        root = ET.fromstring(svg)
        ET.register_namespace("", "http://www.w3.org/2000/svg")
        if assets.styles:
            style = ET.Element("{http://www.w3.org/2000/svg}style")
            style.text = "\n".join(assets.styles)
            root.insert(0, style)
            standalone, _, _ = inspect_svg(ET.tostring(root, encoding="unicode"))
            if not standalone and not assets.scripts:
                return svg
            root = ET.fromstring(standalone or svg)
        if assets.scripts:
            context = SVGDocumentContext()
            context.feed(extract_html(output))
            if context.root is not None and len(context.root):
                # Controls stay available to getElementById/querySelector but do
                # not contribute to the artwork's bounds or visible content.
                hidden = ET.SubElement(root, "{http://www.w3.org/2000/svg}foreignObject",
                                       {"width": "0", "height": "0", "style": "display:none !important",
                                        "aria-hidden": "true", "data-pelican-context": ""})
                hidden.append(context.root)
                for node in context.ancestors:
                    node.set("data-pelican-ancestor", "")
                bridge = ET.SubElement(root, "{http://www.w3.org/2000/svg}script")
                bridge.text = """(() => {
  const art = document.documentElement;
  const context = art.querySelector('[data-pelican-context]');
  const ancestors = [...context.querySelectorAll('[data-pelican-ancestor]')];
  const ownClasses = [...art.classList];
  const sync = () => art.setAttribute('class', [...new Set([
    ...ownClasses, ...ancestors.flatMap(element => [...element.classList])
  ])].join(' '));
  new MutationObserver(sync).observe(context, {attributes:true, attributeFilter:['class'], subtree:true});
  sync();
})();"""
        # SVG is an XML document. ElementTree escapes JS operators and strings
        # correctly; copying a raw HTML <script> would produce malformed XML.
        # Only the explicit attachment contains scripts, just like HTML downloads.
        for attrs, source in assets.scripts:
            script = ET.SubElement(root, "{http://www.w3.org/2000/svg}script")
            for name in ("type", "id"):
                if attrs.get(name):
                    script.set(name, attrs[name])
            script.text = source
        if root.get("viewBox"):
            # HTML's svg { height:auto } can make a standalone document taller
            # than its viewport and clip the wheels. Fit the entire viewBox.
            root.set("style", root.get("style", "").rstrip(";") +
                     ";width:100% !important;height:100% !important;max-width:none !important;max-height:none !important")
        return ET.tostring(root, encoding="unicode")
    except ET.ParseError:
        return svg


def fitted_preview(document):
    # Only the embedded preview gets this bridge. Downloads and saved output
    # retain the original document, including its styles and animation scripts.
    bridge = (ROOT / "web" / "preview-bridge.js").read_text(encoding="utf-8")
    return document + "\n<script>" + bridge + "</script>"


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


def perform_test(config, prompt, model_call):
    started = time.time()
    deadline = time.monotonic() + config["timeout_seconds"]
    attempts = 0
    output, svg, usage, returned_model = "", "", {}, ""
    cancel_event = config.get("_cancel_event")
    try:
        for attempt in range(config.get("retry_count", 0) + 1):
            if cancel_event and cancel_event.is_set():
                raise ValueError("已手动停止生成")
            attempts += 1
            attempt_config = config if attempt == 0 else dict(config, timeout_seconds=max(.001, deadline-time.monotonic()))
            try:
                output, usage, returned_model = model_call(attempt_config, prompt)
                break
            except Exception as exc:
                if cancel_event and cancel_event.is_set():
                    raise ValueError("已手动停止生成") from None
                delay = min(2 ** attempt, 8)
                if attempt >= config.get("retry_count", 0) or not retryable(exc) or deadline-time.monotonic() <= delay:
                    raise
                if cancel_event and cancel_event.wait(delay):
                    raise ValueError("已手动停止生成") from None
                time.sleep(0)  # yield after a cancellable retry delay
        output = redact(output, config)
        status, message = "success", ""
    except Exception as exc:
        status = "error"
        message = str(exc) if isinstance(exc, ValueError) else f"连接或解析失败（{type(exc).__name__}），请检查地址、网络和接口协议"
    # Preview parsing is deliberately outside request status handling.
    if status == "success":
        try:
            svg, _, _ = inspect_svg(output)
        except Exception:
            svg = ""
    safe_usage = {k: v for k, v in usage.items() if k in ("input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens") and type(v) is int} if isinstance(usage, dict) else {}
    return dict(status=status, started=started, finished=time.time(), attempts=attempts,
                output=output, svg=svg, error=redact(message, config)[:1000], usage=safe_usage,
                returned_model=redact(returned_model, config)[:200])


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
        self.login_failures = []
        self.run_cancellations = {}
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS node_groups (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE CHECK(length(name) BETWEEN 1 AND 60),
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                    created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS nodes (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE CHECK(length(name) BETWEEN 1 AND 60),
                    base_url TEXT NOT NULL, api_key TEXT NOT NULL, model TEXT NOT NULL,
                    effort TEXT NOT NULL, protocol TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0,1)),
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                    group_id INTEGER REFERENCES node_groups(id) ON DELETE SET NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_node ON nodes(active) WHERE active=1;
                -- Keep the former scene/check columns so existing SQLite files can be opened;
                -- new single-task runs leave those compatibility fields empty.
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY, started REAL NOT NULL, finished REAL,
                    status TEXT NOT NULL, source TEXT NOT NULL, model TEXT NOT NULL,
                    base_url TEXT NOT NULL, effort TEXT NOT NULL, protocol TEXT NOT NULL,
                    scene TEXT NOT NULL, nonce TEXT NOT NULL, prompt TEXT NOT NULL,
                    output TEXT DEFAULT '', svg TEXT DEFAULT '', checks TEXT DEFAULT '{}',
                    error TEXT DEFAULT '', usage TEXT DEFAULT '{}', returned_model TEXT DEFAULT '',
                    group_id INTEGER REFERENCES node_groups(id) ON DELETE SET NULL,
                    group_name TEXT NOT NULL DEFAULT '');
                CREATE INDEX IF NOT EXISTS runs_started ON runs(started);
                CREATE TABLE IF NOT EXISTS admin_auth (id INTEGER PRIMARY KEY CHECK(id=1), salt TEXT NOT NULL, password_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS admin_sessions (
                    token_hash TEXT PRIMARY KEY, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS guest_results (id TEXT PRIMARY KEY, created REAL NOT NULL, result TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS guest_created ON guest_results(created);
                -- The gallery is backed by a relational image library. Keep
                -- runs.svg during migration for old readers, but use this
                -- table as the associated image record for new reads.
                CREATE TABLE IF NOT EXISTS image_library (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
                    content_type TEXT NOT NULL DEFAULT 'image/svg+xml',
                    svg TEXT NOT NULL CHECK(length(svg) > 0),
                    created REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS image_library_created ON image_library(created);
            """)
            # Credentials are deliberately memory-only; restart never silently retries a billed request.
            for row in db.execute("SELECT id,result FROM guest_results WHERE json_extract(result,'$.status') IN ('queued','running')").fetchall():
                result = json.loads(row["result"])
                self.fail_guest(result, "服务重启，访客任务已中断，请重新提交")
                db.execute("UPDATE guest_results SET created=?,result=? WHERE id=?", (time.time(), json.dumps(result), row["id"]))
            columns = {r[1] for r in db.execute("PRAGMA table_info(runs)")}
            if "favorite" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0 CHECK(favorite IN (0,1))")
            if "test_version" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN test_version INTEGER NOT NULL DEFAULT 1")
            if "tests" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN tests TEXT NOT NULL DEFAULT '{}'")
            if "node_id" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN node_id INTEGER REFERENCES nodes(id)")
                db.execute("ALTER TABLE runs ADD COLUMN node_name TEXT NOT NULL DEFAULT ''")
            if "group_id" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN group_id INTEGER REFERENCES node_groups(id) ON DELETE SET NULL")
            if "group_name" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN group_name TEXT NOT NULL DEFAULT ''")
            # Account provenance starts here; older runs cannot be attributed
            # from whichever account happens to be logged in during migration.
            if "account_fingerprint" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN account_fingerprint TEXT NOT NULL DEFAULT ''")
            if "account_label" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN account_label TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE INDEX IF NOT EXISTS runs_account ON runs(account_fingerprint,id)")
            # Old fingerprints came from a shared workspace, not an individual.
            # Relabel the ambiguity without guessing ownership or changing IDs.
            db.execute("UPDATE runs SET account_label='旧工作区 ' || substr(account_fingerprint,6) || '（未区分个人账号）' "
                       "WHERE protocol='codex' AND account_fingerprint!='' AND account_label='账号 ' || substr(account_fingerprint,6)")
            # An outcome is tiny and survives deletion of the artwork and run.
            # Exact timestamps keep the rolling 24-hour denominator accurate.
            db.executescript("""
                CREATE TABLE IF NOT EXISTS run_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL, started REAL NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('running','queued','success','error','cancelled'))
                );
                CREATE UNIQUE INDEX IF NOT EXISTS run_outcomes_request ON run_outcomes(run_id,started);
                CREATE INDEX IF NOT EXISTS run_outcomes_started ON run_outcomes(started);
            """)
            db.execute("UPDATE runs SET status='cancelled' WHERE status='error' AND error='已手动停止生成'")
            node_columns = {r[1] for r in db.execute("PRAGMA table_info(nodes)")}
            if "enabled" not in node_columns:
                db.execute("ALTER TABLE nodes ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1))")
            if "group_id" not in node_columns:
                db.execute("ALTER TABLE nodes ADD COLUMN group_id INTEGER REFERENCES node_groups(id) ON DELETE SET NULL")
            if "group_position" not in node_columns:
                db.execute("ALTER TABLE nodes ADD COLUMN group_position INTEGER NOT NULL DEFAULT 0")
            db.execute("CREATE INDEX IF NOT EXISTS nodes_group ON nodes(group_id,id)")
            db.execute("CREATE INDEX IF NOT EXISTS runs_node ON runs(node_id,id)")
            db.execute("""INSERT OR IGNORE INTO image_library (run_id, content_type, svg, created)
                         SELECT id, 'image/svg+xml', svg, COALESCE(finished, started)
                         FROM runs WHERE length(COALESCE(svg, '')) > 0""")
            db.execute("INSERT OR IGNORE INTO settings VALUES (1, ?)", (json.dumps(DEFAULTS),))
            saved_config = json.loads(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])
            config = normalized_settings(saved_config)
            if not db.execute("SELECT 1 FROM nodes").fetchone():
                db.execute("INSERT INTO nodes(name,base_url,api_key,model,effort,protocol,active,enabled) VALUES (?,?,?,?,?,?,1,1)",
                           ("默认节点", *(config[key] for key in NODE_FIELDS)))
            legacy_nodes = db.execute("SELECT id FROM nodes WHERE group_id IS NULL AND active=0 AND enabled=1 ORDER BY id").fetchall()
            if legacy_nodes:
                group = db.execute("SELECT id FROM node_groups WHERE name=?", ("默认循环组",)).fetchone()
                group_id = group[0] if group else db.execute(
                    "INSERT INTO node_groups(name,enabled,created) VALUES (?,1,?)", ("默认循环组", time.time())
                ).lastrowid
                db.executemany("UPDATE nodes SET group_id=? WHERE id=?", [(group_id, row[0]) for row in legacy_nodes])
                if "schedule_mode" not in saved_config:
                    config["schedule_mode"] = "groups"
            self.store_settings(db, config)
            for row in db.execute("SELECT id,tests FROM runs WHERE status='running'").fetchall():
                tests = json.loads(row["tests"])
                for test in tests.values():
                    if test["status"] == "running":
                        test.update(status="error", finished=time.time(), error="服务重启，本项检测中断")
                db.execute("UPDATE runs SET status='error', finished=?, error='服务重启，上一轮检测中断；未自动重试',tests=? WHERE id=?", (time.time(), json.dumps(tests), row["id"]))
            db.execute("""INSERT OR IGNORE INTO run_outcomes(run_id,started,status)
                SELECT id,started,CASE WHEN status IN ('passed','invalid','success') THEN 'success'
                    WHEN status IN ('running','queued','cancelled') THEN status ELSE 'error' END FROM runs""")
            db.execute("""UPDATE run_outcomes SET status=(SELECT CASE
                WHEN r.status IN ('passed','invalid','success') THEN 'success'
                WHEN r.status IN ('running','queued','cancelled') THEN r.status ELSE 'error' END
                FROM runs r WHERE r.id=run_outcomes.run_id AND r.started=run_outcomes.started)
                WHERE EXISTS (SELECT 1 FROM runs r WHERE r.id=run_outcomes.run_id AND r.started=run_outcomes.started AND
                    run_outcomes.status != CASE WHEN r.status IN ('passed','invalid','success') THEN 'success'
                    WHEN r.status IN ('running','queued','cancelled') THEN r.status ELSE 'error' END)""")
            db.executescript("""
                CREATE TRIGGER IF NOT EXISTS runs_outcome_insert AFTER INSERT ON runs BEGIN
                    INSERT OR IGNORE INTO run_outcomes(run_id,started,status) VALUES
                    (NEW.id,NEW.started,CASE WHEN NEW.status IN ('passed','invalid','success') THEN 'success'
                        WHEN NEW.status IN ('running','queued','cancelled') THEN NEW.status ELSE 'error' END);
                END;
                CREATE TRIGGER IF NOT EXISTS runs_outcome_status AFTER UPDATE OF status ON runs BEGIN
                    UPDATE run_outcomes SET status=CASE WHEN NEW.status IN ('passed','invalid','success') THEN 'success'
                        WHEN NEW.status IN ('running','queued','cancelled') THEN NEW.status ELSE 'error' END
                    WHERE run_id=NEW.id AND started=NEW.started;
                END;
            """)
            if config["enabled"]:
                last_finished = db.execute("SELECT MAX(finished) FROM runs").fetchone()[0]
                if last_finished is not None:
                    config["next_run"] = max(config["next_run"] or 0, last_finished + config["interval_seconds"])
                elif config["next_run"] is None:
                    config["next_run"] = time.time() + config["interval_seconds"]
                self.store_settings(db, config)
        os.chmod(self.path, 0o600)

    def password_configured(self):
        with self.db() as db:
            return bool(db.execute("SELECT 1 FROM admin_auth WHERE id=1").fetchone())

    def setup_password(self, password):
        if not isinstance(password, str) or not 6 <= len(password) <= 256:
            raise ValueError("管理密码需要 6–256 个字符")
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
            token = secrets.token_urlsafe(32)
            with self.db() as db:
                db.execute("DELETE FROM admin_sessions WHERE expires<=?", (now,))
                db.execute("INSERT INTO admin_sessions VALUES (?,?)",
                           (hashlib.sha256(token.encode()).hexdigest(), now + SESSION_TTL))
            return token

    def authenticated(self, token):
        if not isinstance(token, str) or not token:
            return False
        with self.db() as db:
            return bool(db.execute("SELECT 1 FROM admin_sessions WHERE token_hash=? AND expires>?",
                                  (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone())

    def logout(self, token):
        with self.lock, self.db() as db:
            db.execute("DELETE FROM admin_sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))

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
            saved_config = json.loads(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])
            config = normalized_settings(saved_config)
            node = db.execute("SELECT * FROM nodes WHERE active=1").fetchone()
            config.update({key: node[key] for key in NODE_FIELDS})
            group_name = db.execute("SELECT name FROM node_groups WHERE id=?", (node["group_id"],)).fetchone()[0] if node["group_id"] else ""
            config.update(active_node_id=node["id"], node_name=redact(node["name"], config), group_id=node["group_id"], group_name=group_name)
        if public:
            key = config.pop("api_key")
            config["has_key"] = config["protocol"] == "codex" or bool(key)
            config["api_key_masked"] = "使用本机登录" if config["protocol"] == "codex" else mask(key) if key else "尚未配置"
            config["base_url"] = mask_url(config["base_url"])
        return config

    @staticmethod
    def store_settings(db, config):
        global_config = {k:v for k,v in config.items() if k in DEFAULTS and k not in NODE_FIELDS}
        db.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(global_config),))

    def groups(self):
        with self.db() as db:
            groups = db.execute("SELECT id,name,enabled,created FROM node_groups ORDER BY id").fetchall()
            members = db.execute("SELECT id,group_id,name,model,effort,protocol,enabled FROM nodes WHERE group_id IS NOT NULL AND enabled=1 ORDER BY group_id,group_position,id").fetchall()
        grouped = {row["id"]: [] for row in groups}
        for row in members:
            grouped[row["group_id"]].append(dict(id=row["id"], name=row["name"], model=row["model"],
                                                  effort=row["effort"], protocol=row["protocol"], enabled=bool(row["enabled"])))
        return [dict(id=row["id"], name=row["name"], enabled=bool(row["enabled"]), created=row["created"],
                     members=grouped[row["id"]]) for row in groups]

    @staticmethod
    def _group_pairs(values):
        pairs = values.get("pairs", []) if isinstance(values, dict) else []
        if not isinstance(pairs, list) or not 1 <= len(pairs) <= 100:
            raise ValueError("循环组至少需要 1 个组合，最多 100 个")
        result, seen = [], set()
        for pair in pairs:
            if not isinstance(pair, dict) or set(pair) - {"model", "effort"}:
                raise ValueError("循环组组合格式不正确")
            model, effort = pair.get("model"), pair.get("effort")
            if not isinstance(model, str) or not 1 <= len(model.strip()) <= 120 or not isinstance(effort, str) or effort not in MODEL_EFFORTS:
                raise ValueError("循环组包含无效的模型或思考强度")
            key = (model.strip(), effort)
            if key[0].startswith("-") or any(ord(c) < 32 for c in key[0]):
                raise ValueError("循环组包含无效的模型名称")
            if key not in seen:
                seen.add(key)
                result.append(key)
        return result

    def save_group(self, values, group_id=None):
        if not isinstance(values, dict) or set(values) - {"name", "enabled", "pairs"}:
            raise ValueError("循环组字段不正确")
        name = values.get("name", "")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60 or any(ord(c) < 32 for c in name):
            raise ValueError("请输入 1–60 字符的循环组名称")
        pairs = self._group_pairs(values)
        enabled = values.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("循环组启用状态必须为布尔值")
        name = name.strip()
        with self.lock, self.db() as db:
            old = db.execute("SELECT id FROM node_groups WHERE id=?", (group_id,)).fetchone() if group_id is not None else None
            if group_id is not None and not old:
                raise ValueError("循环组不存在")
            try:
                if old:
                    db.execute("UPDATE node_groups SET name=?,enabled=? WHERE id=?", (name, int(enabled), group_id))
                else:
                    group_id = db.execute("INSERT INTO node_groups(name,enabled,created) VALUES (?,?,?)", (name, int(enabled), time.time())).lastrowid
            except sqlite3.IntegrityError:
                raise ValueError("循环组名称已存在，请使用其他名称") from None
            existing = {tuple((row["model"], row["effort"])): row for row in db.execute(
                "SELECT * FROM nodes WHERE group_id=?", (group_id,)).fetchall()}
            keep = set(pairs)
            for position, (model, effort) in enumerate(pairs):
                if (model, effort) in existing:
                    db.execute("UPDATE nodes SET enabled=1,group_position=? WHERE id=?", (position, existing[(model, effort)]["id"]))
                    continue
                base = f"{name} · {model} · {effort}"
                candidate, suffix = base[:60], 2
                while db.execute("SELECT 1 FROM nodes WHERE name=?", (candidate,)).fetchone():
                    tail = f" {suffix}"
                    candidate, suffix = f"{base[:60-len(tail)]}{tail}", suffix + 1
                db.execute("INSERT INTO nodes(name,base_url,api_key,model,effort,protocol,active,enabled,group_id,group_position) VALUES (?,?,?,?,?,?,0,1,?,?)",
                           (candidate, "codex://local", "", model, effort, "codex", group_id, position))
            for key, row in existing.items():
                if key not in keep and not row["active"]:
                    db.execute("UPDATE nodes SET enabled=0 WHERE id=?", (row["id"],))
            self._remove_group_from_queue(db, group_id)
        return next(group for group in self.groups() if group["id"] == group_id)

    def _remove_group_from_queue(self, db, group_id):
        ids = {row[0] for row in db.execute("SELECT id FROM nodes WHERE group_id=?", (group_id,))}
        self.scheduled_queue = [node_id for node_id in self.scheduled_queue if node_id not in ids]

    def set_group_enabled(self, group_id, enabled):
        if not isinstance(enabled, bool):
            raise ValueError("循环组启用状态必须为布尔值")
        with self.lock, self.db() as db:
            if not db.execute("SELECT 1 FROM node_groups WHERE id=?", (group_id,)).fetchone():
                raise ValueError("循环组不存在")
            db.execute("UPDATE node_groups SET enabled=? WHERE id=?", (int(enabled), group_id))
            if not enabled:
                self._remove_group_from_queue(db, group_id)
        return self.groups()

    def delete_group(self, group_id):
        with self.lock, self.db() as db:
            row = db.execute("SELECT 1 FROM node_groups WHERE id=?", (group_id,)).fetchone()
            if not row:
                raise ValueError("循环组不存在")
            if db.execute("SELECT 1 FROM nodes WHERE group_id=? AND active=1", (group_id,)).fetchone():
                raise ValueError("当前节点不能从循环组移除，请先切换单个节点")
            self._remove_group_from_queue(db, group_id)
            db.execute("UPDATE nodes SET group_id=NULL,enabled=0 WHERE group_id=?", (group_id,))
            db.execute("DELETE FROM node_groups WHERE id=?", (group_id,))
        return {"deleted": 1}

    def nodes(self):
        with self.db() as db:
            rows = db.execute("SELECT n.*,g.name AS group_name FROM nodes n LEFT JOIN node_groups g ON g.id=n.group_id ORDER BY n.id").fetchall()
            latest = {r["node_id"]:dict(r) for r in db.execute("SELECT id,node_id,status,started,finished,error FROM runs WHERE id IN (SELECT MAX(id) FROM runs WHERE node_id IS NOT NULL GROUP BY node_id)")}
            for item in latest.values():
                item["status"] = request_status(item["status"])
                if item["status"] == "success": item["error"] = ""
        return [dict(id=row["id"], name=redact(row["name"], dict(row)), active=bool(row["active"]),
                     base_url=mask_url(row["base_url"]), has_key=node_ready(row),
                     api_key_masked="使用本机登录" if row["protocol"] == "codex" else mask(row["api_key"]) if row["api_key"] else "尚未配置", enabled=bool(row["enabled"]),
                     model=redact(row["model"], dict(row)), effort=row["effort"], protocol=row["protocol"],
                     group_id=row["group_id"], group_name=row["group_name"] or "",
                     last_run=latest.get(row["id"])) for row in rows]

    def save_node(self, values, node_id=None):
        if not isinstance(values, dict) or set(values) - {"name", *NODE_FIELDS, "enabled", "group_id"}:
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
            group_id = values.get("group_id", old["group_id"] if old else None)
            if group_id is not None and (type(group_id) is not int or not db.execute("SELECT 1 FROM node_groups WHERE id=?", (group_id,)).fetchone()):
                raise ValueError("循环组不存在")
            if old and old["active"] and group_id is not None:
                raise ValueError("当前单个节点不能加入循环组，请先切换其他单个节点")
            if old is None and values.get("protocol", "codex") != "codex" and any(not isinstance(values.get(k), str) or not values[k].strip() for k in ("base_url", "api_key")):
                raise ValueError("新增节点需填写 API 地址和 API Key")
            config = validate_settings(values, dict(DEFAULTS, **({k:old[k] for k in NODE_FIELDS} if old else {})))
            fields = (name.strip(), *(config[k] for k in NODE_FIELDS))
            try:
                if old:
                    db.execute("UPDATE nodes SET name=?,base_url=?,api_key=?,model=?,effort=?,protocol=?,enabled=?,group_id=? WHERE id=?", (*fields,enabled,group_id,node_id))
                else:
                    node_id = db.execute("INSERT INTO nodes(name,base_url,api_key,model,effort,protocol,enabled,group_id) VALUES (?,?,?,?,?,?,?,?)", (*fields,enabled,group_id)).lastrowid
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
            new_id = db.execute("INSERT INTO nodes(name,base_url,api_key,model,effort,protocol,enabled,group_id) VALUES (?,?,?,?,?,?,?,?)",
                                (name, *(node[key] for key in NODE_FIELDS), node["enabled"], node["group_id"])).lastrowid
        return next(n for n in self.nodes() if n["id"] == new_id)

    def set_node_enabled(self, node_id, enabled):
        with self.lock, self.db() as db:
            node = db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
            if not node:
                raise ValueError("节点不存在")
            if node["group_id"] is not None:
                raise ValueError("请在循环组中管理该组合")
            if not enabled and node["active"]:
                raise ValueError("当前节点不能停用，请先切换到其他已启用节点")
            if enabled and not node_ready(node):
                raise ValueError("请先为该节点配置 API Key")
            db.execute("UPDATE nodes SET enabled=? WHERE id=?", (int(enabled), node_id))
        return self.nodes()

    def activate_node(self, node_id):
        with self.lock, self.db() as db:
            node = db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
            if not node:
                raise ValueError("节点不存在")
            if node["group_id"] is not None:
                raise ValueError("循环组成员不能设为单个当前节点")
            if not node["enabled"]:
                raise ValueError("该节点已停用，请先启用后再设为当前")
            if not node_ready(node):
                raise ValueError("请先为该节点配置 API Key")
            db.execute("UPDATE nodes SET active=0 WHERE active=1")
            db.execute("UPDATE nodes SET active=1 WHERE id=?", (node_id,))
        return self.settings(public=True)

    def save(self, values):
        with self.lock:
            previous = self.settings()
            config = validate_settings(values, previous)
            with self.db() as db:
                if config["enabled"]:
                    if self.run_cancellations or db.execute("SELECT 1 FROM runs WHERE status='running'").fetchone():
                        config["next_run"] = None
                    elif not previous["enabled"] or config["schedule_mode"] != previous["schedule_mode"]:
                        config["next_run"] = time.time() + config["interval_seconds"]
                    elif config["interval_seconds"] != previous["interval_seconds"] or config["next_run"] is None:
                        finished = db.execute("SELECT MAX(finished) FROM runs").fetchone()[0]
                        config["next_run"] = (finished if finished is not None else time.time()) + config["interval_seconds"]
                db.execute("UPDATE nodes SET base_url=?,api_key=?,model=?,effort=?,protocol=? WHERE active=1", tuple(config[k] for k in NODE_FIELDS))
                self.store_settings(db, config)
            if config["schedule_mode"] != previous["schedule_mode"] or not config["enabled"]:
                self.scheduled_queue.clear()
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
                if node["group_id"] and not db.execute("SELECT enabled FROM node_groups WHERE id=?", (node["group_id"],)).fetchone()[0]:
                    raise ValueError("该节点所属循环组已停用")
                config.update({key:node[key] for key in NODE_FIELDS})
                group_name = db.execute("SELECT name FROM node_groups WHERE id=?", (node["group_id"],)).fetchone()[0] if node["group_id"] else ""
                config.update(active_node_id=node["id"], node_name=redact(node["name"], config), group_id=node["group_id"], group_name=group_name)
            if not node_ready(config):
                raise ValueError("请先在检测设置中填写 API Key")
            if source == "scheduled" and (not config["enabled"] or config["next_run"] is None or config["next_run"] > now
                                           or (node_id is not None and node_id not in self.scheduled_queue)):
                return None
            account_fingerprint = ""
            account_label = ""
            if config["protocol"] == "codex":
                status = login_status()
                if not status["logged_in"]:
                    raise ValueError(status["message"])
                config["_codex_account_fingerprint"] = status.get("account_fingerprint", "")
                candidate = status.get("account_fingerprint", "")
                if re.fullmatch(r"acct-[0-9a-f]{12}", candidate):
                    account_fingerprint = candidate
                    account_label = status.get('account_label') or f'个人账号 {candidate[5:]}'
            if self.run_cancellations or db.execute("SELECT 1 FROM runs WHERE status='running'").fetchone():
                if source == "scheduled":
                    return None
                raise ValueError("已有生成正在运行，请等待完成")
            prompt = config["task_prompt"]
            row = db.execute("""INSERT INTO runs (started,status,source,model,base_url,effort,protocol,scene,nonce,prompt,test_version,tests,node_id,node_name,group_id,group_name,account_fingerprint,account_label)
                VALUES (?,'running',?,?,?,?,?,?,?, ?,3,'{}',?,?,?,?,?,?)""",
                (now, source, config["model"], config["base_url"], config["effort"], config["protocol"],
                 "", "", prompt, config["active_node_id"], config["node_name"], config.get("group_id"), config.get("group_name", ""),
                 account_fingerprint, account_label))
            run_id = row.lastrowid
            if source == "manual":
                self.scheduled_queue.clear()
            elif source == "scheduled" and node_id is not None:
                # Remove before starting the worker: a fast completion must not
                # race with a scheduler that still sees this node as pending.
                self.scheduled_queue.remove(node_id)
            config["next_run"] = None
            self.store_settings(db, config)
            cancel_event = threading.Event()
            self.run_cancellations[run_id] = cancel_event
        threading.Thread(target=self.execute, args=(run_id, config, prompt, cancel_event), daemon=True).start()
        return run_id

    def run_once(self):
        """Submit one saved combination without enabling the scheduler."""
        with self.lock, self.db() as db:
            config = self.settings()
            if config["enabled"]:
                raise ValueError("请先暂停循环生成，再手动生成一次")
            node_id = None
            if config["schedule_mode"] == "groups":
                node = db.execute("SELECT n.id FROM nodes n JOIN node_groups g ON g.id=n.group_id "
                                  "WHERE n.enabled=1 AND g.enabled=1 AND (n.protocol='codex' OR length(trim(n.api_key))>0) "
                                  "ORDER BY g.id,n.group_position,n.id LIMIT 1").fetchone()
                if not node:
                    raise ValueError("请先保存并启用一个模型组合")
                node_id = node[0]
            return self.start_run(node_id=node_id)

    def start_testing(self):
        """Start the saved single-model or group schedule immediately."""
        with self.lock, self.db() as db:
            config = self.settings()
            if any(event.is_set() for event in self.run_cancellations.values()):
                raise ValueError("上一轮正在停止，请稍后开始")
            ready = db.execute(
                "SELECT 1 FROM nodes n LEFT JOIN node_groups g ON g.id=n.group_id "
                "WHERE n.enabled=1 AND ((?='single' AND n.active=1 AND n.group_id IS NULL) "
                "OR (?='groups' AND g.enabled=1)) "
                "AND (n.protocol='codex' OR length(trim(n.api_key))>0) LIMIT 1",
                (config["schedule_mode"], config["schedule_mode"])).fetchone()
            if not ready:
                raise ValueError("请先保存可用的模型或启用至少一个循环组")
            if not config["enabled"]:
                self.scheduled_queue.clear()
                running = self.run_cancellations or db.execute("SELECT 1 FROM runs WHERE status='running'").fetchone()
                config.update(enabled=True, next_run=None if running else time.time())
                self.store_settings(db, config)
        return self.settings(public=True)

    def stop_testing(self):
        """Pause future tests, drop queued work, and cancel the active request."""
        with self.lock, self.db() as db:
            config = self.settings()
            config.update(enabled=False, next_run=None)
            self.store_settings(db, config)
            self.scheduled_queue.clear()
            for event in self.run_cancellations.values():
                event.set()
            db.execute("UPDATE runs SET status='cancelled',finished=?,error='已手动停止生成' WHERE status='running'",
                       (time.time(),))
        return self.settings(public=True)

    def stop_run(self, run_id):
        """Stop the active request and make its terminal outcome explicit."""
        now = time.time()
        with self.lock, self.db() as db:
            row = db.execute("SELECT id,status FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise ValueError("生成记录不存在")
            if row["status"] != "running":
                raise ValueError("这轮生成已经结束")
            event = self.run_cancellations.get(run_id)
            if event:
                event.set()
            db.execute("DELETE FROM image_library WHERE run_id=?", (run_id,))
            db.execute("UPDATE runs SET status='cancelled',finished=?,output='',svg='',checks='{}',error=?,usage='{}',returned_model='',tests='{}' WHERE id=? AND status='running'",
                       (now, "已手动停止生成", run_id))
            if not event:
                self._schedule_after_finish(db, now)
        return {"id": run_id, "status": "cancelled", "stopped": True, "finished": now, "error": "已手动停止生成"}

    def _schedule_after_finish(self, db, finished):
        # Read the current settings so edits or a stop during generation win.
        config = normalized_settings(json.loads(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0]))
        config["next_run"] = finished + config["interval_seconds"] if config["enabled"] else None
        self.store_settings(db, config)

    def execute(self, run_id, config, prompt, cancel_event=None):
        config = dict(config, _cancel_event=cancel_event) if cancel_event else config
        result = perform_test(config, prompt, self.model_call)
        with self.lock, self.db() as db:
            # A stop request wins any race with a late upstream response. The
            # worker may still be unwinding, but it cannot resurrect the run.
            if cancel_event and cancel_event.is_set():
                self.run_cancellations.pop(run_id, None)
                self._schedule_after_finish(db, result["finished"])
                return
            # Persist the generated asset and its request atomically. The
            # legacy runs.svg column remains populated for older databases and
            # readers; image_library is the relational gallery source.
            db.execute("DELETE FROM image_library WHERE run_id=?", (run_id,))
            if result["svg"]:
                db.execute("""INSERT INTO image_library (run_id, content_type, svg, created)
                             VALUES (?, 'image/svg+xml', ?, ?)""",
                           (run_id, result["svg"], result["finished"]))
            db.execute("UPDATE runs SET status=?,finished=?,output=?,svg=?,checks='{}',error=?,usage=?,returned_model=?,tests='{}' WHERE id=? AND status='running'",
                       (result["status"], result["finished"], result["output"], result["svg"], result["error"],
                        json.dumps(result["usage"]), result["returned_model"], run_id))
            self.run_cancellations.pop(run_id, None)
            self._schedule_after_finish(db, result["finished"])

    def prepare_guest(self, values):
        if values.get("protocol") not in ("responses", "chat"):
            raise ValueError("访客仅支持 API 调用，不能使用本机 Codex 登录")
        if not self.settings()["guest_enabled"]:
            raise ValueError("访客检测暂未开放")
        allowed = {"base_url", "api_key", "model", "effort", "protocol", "retry_count"}
        if set(values) - allowed:
            raise ValueError("访客请求包含不支持的字段")
        if any(not isinstance(values.get(k),str) or not values[k].strip() for k in ("base_url","api_key")):
            raise ValueError("请填写你自己的 API 地址和 API Key")
        config = validate_settings(values, DEFAULTS)
        if parse.urlsplit(config["base_url"]).scheme != "https":
            raise ValueError("访客检测仅支持公网 HTTPS 地址")
        config["_guest"] = True
        config["task_prompt"] = self.settings()["task_prompt"]
        host = parse.urlsplit(config["base_url"]).hostname or ""
        published = dict(id=secrets.token_hex(12), status="queued", submitted=time.time(), started=None, finished=None,
                         model=redact(config["model"], config), effort=config["effort"], protocol=config["protocol"],
                         api_masked=mask(host), prompt=config["task_prompt"], output="", svg="", error="", attempts=0, usage={})
        return config, published

    def submit_guest(self, values):
        config, published = self.prepare_guest(values)
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
            self.guest_queue.put_nowait((config, published))
        return accepted

    def guest_worker(self):
        while not self.stopped.is_set():
            try:
                config, published = self.guest_queue.get(timeout=.2)
            except queue.Empty:
                continue
            try:
                self.execute_guest(config, published)
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
        published.update(status="error", finished=time.time(), error=reason)

    def store_guest(self, published):
        terminal = published["status"] not in ("queued","running")
        with self.db() as db:
            db.execute("UPDATE guest_results SET created=?,result=? WHERE id=?",
                       (published["finished"] if terminal else published["submitted"], json.dumps(published), published["id"]))

    def execute_guest(self, config, published):
        published.update(started=time.time(), status="running")
        self.store_guest(published)
        result = perform_test(config, config["task_prompt"], self.model_call)
        published.update(status=result["status"], finished=result["finished"], output=result["output"], svg=result["svg"],
                         error=guest_error(result["error"]) if result["status"] == "error" else "",
                         attempts=result["attempts"], usage=result["usage"], returned_model=result["returned_model"])
        self.store_guest(published)
        self.prune_guests()
        return self.guest_view(published)

    def guest_test(self, values):
        """Synchronous core for local checks; HTTP submissions use the bounded queue."""
        config, published = self.prepare_guest(values)
        with self.db() as db:
            db.execute("INSERT INTO guest_results VALUES (?,?,?)",(published["id"],time.time(),json.dumps(published)))
        return self.execute_guest(config, published)

    @staticmethod
    def guest_view(result):
        public = dict(result)
        tests = public.pop("tests", {})
        if not public.get("output") and isinstance(tests, dict):
            public["output"] = next((test.get("output", "") for test in tests.values() if test.get("output")), "")
        public["status"] = request_status(public.get("status", "error"))
        svg = public.pop("svg", "")
        public["has_svg"] = bool(svg)
        public["has_html"] = bool(extract_html(public.get("output")))
        if public["has_svg"] or public["has_html"]:
            public["filename"] = guest_filename(public)
        if public["status"] == "success":
            public["error"] = ""
        elif public["status"] == "error" and not public.get("error"):
            old_error = next((test.get("error", "") for test in tests.values() if test.get("error")), "") if isinstance(tests, dict) else ""
            public["error"] = guest_error(old_error)
        for key in ("scene", "nonce", "test_type", "checks"):
            public.pop(key, None)
        return public

    def guest_results(self):
        with self.db() as db:
            rows = db.execute("SELECT result FROM guest_results WHERE created>=? OR json_extract(result,'$.status') IN ('queued','running') ORDER BY created DESC",(time.time()-3600,)).fetchall()
        results = [self.guest_view(json.loads(row[0])) for row in rows]
        queued = sorted((r for r in results if r['status'] == 'queued'), key=lambda r:(r['submitted'],r['id']))
        for position, result in enumerate(queued, 1):
            result['queue_position'] = position
        return results

    def prune_guests(self):
        with self.db() as db:
            terminal = "json_extract(result,'$.status') NOT IN ('queued','running')"
            db.execute("DELETE FROM guest_results WHERE " + terminal + " AND created<?",(time.time()-3600,))
            db.execute("DELETE FROM guest_results WHERE " + terminal + " AND id NOT IN (SELECT id FROM guest_results WHERE " + terminal + " ORDER BY created DESC LIMIT 100)")

    def schedule_once(self, now=None):
        """Start at most one due request; every group member observes the same delay."""
        now = time.time() if now is None else now
        with self.lock:
            with self.db() as db:
                settings = self.settings()
                if not settings["enabled"]:
                    self.scheduled_queue.clear()
                    return None
                if self.run_cancellations or db.execute("SELECT 1 FROM runs WHERE status='running'").fetchone():
                    return None
                if settings["next_run"] is None:
                    finished = db.execute("SELECT MAX(finished) FROM runs").fetchone()[0]
                    settings["next_run"] = (finished if finished is not None else now) + settings["interval_seconds"]
                    self.store_settings(db, settings)
                if settings["next_run"] > now:
                    return None
                if not self.scheduled_queue:
                    self.scheduled_queue = [row[0] for row in db.execute(
                        "SELECT n.id FROM nodes n LEFT JOIN node_groups g ON g.id=n.group_id "
                        "WHERE n.enabled=1 AND ((?='single' AND n.active=1 AND n.group_id IS NULL) OR (?='groups' AND g.enabled=1)) "
                        "AND (n.protocol='codex' OR length(trim(n.api_key))>0) ORDER BY COALESCE(n.group_id,0),n.group_position,n.id",
                        (settings["schedule_mode"], settings["schedule_mode"]))]
                if not self.scheduled_queue:
                    self._schedule_after_finish(db, now)
                    return None
                next_node = self.scheduled_queue[0]
            try:
                return self.start_run("scheduled", now=now, node_id=next_node)
            except ValueError as exc:
                self.scheduled_queue.remove(next_node)
                with self.db() as db:
                    self._schedule_after_finish(db, now)
                print(f"Scheduler node skipped: {type(exc).__name__}", flush=True)
                return None

    def scheduler(self):
        last_cleanup = 0
        while not self.stopped.wait(.2):
            try:
                now = time.time()
                if now-last_cleanup > 60:
                    self.prune_guests()
                    last_cleanup = now
                self.schedule_once(now)
            except Exception as exc:
                print(f"Scheduler error: {type(exc).__name__}", flush=True)

    def runs(self, before=None, include_account=False):
        with self.db() as db:
            rows = db.execute(RUN_SELECT + " WHERE r.id < ? ORDER BY r.id DESC LIMIT 48", (before or 2**63-1,)).fetchall()
        return [self.serialize(row, include_account=include_account) for row in rows]

    def history(self, page=1, status="all", account="all", model="all", group="all",
                search="", period="all", selection_only=False, favorite="all"):
        """Admin-only pagination and selection across the entire saved history."""
        if (type(page) is not int or not 1 <= page <= 1000000
                or status not in ("all", "success", "error", "running", "cancelled")
                or period not in ("all", "24h", "7d")
                or favorite not in ("all", "yes", "no")
                or any(not isinstance(value, str) or len(value) > 160 for value in (account, model, group, search))):
            raise ValueError("生成记录筛选参数无效")
        clauses, args = [], []
        if favorite != "all":
            clauses.append("r.favorite=?")
            args.append(int(favorite == "yes"))
        if status == "success":
            clauses.append("r.status IN ('success','passed','invalid')")
        elif status != "all":
            clauses.append("r.status=?")
            args.append(status)
        account_clause, account_args = account_filter(account)
        if account_clause:
            clauses.append(account_clause)
            args.extend(account_args)
        if model != "all":
            clauses.append("r.model=?")
            args.append(model)
        if group != "all":
            clauses.append("r.group_name=?")
            args.append("" if group == "single" else group)
        if period != "all":
            clauses.append("r.started>=?")
            args.append(time.time() - (86400 if period == "24h" else 7 * 86400))
        if search.strip():
            clauses.append("(CAST(r.id AS TEXT)=? OR instr(lower(r.model || ' ' || r.node_name || ' ' || r.group_name || ' ' || r.account_label || ' ' || r.account_fingerprint), lower(?))>0)")
            args.extend((search.strip().lstrip("#"), search.strip()))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.db() as db:
            if selection_only:
                where += (" AND " if where else " WHERE ") + "r.status NOT IN ('running','queued') AND r.favorite=0"
                ids = [row[0] for row in db.execute("SELECT r.id FROM runs r" + where + " ORDER BY r.id DESC", args)]
                return dict(ids=ids, total=len(ids))
            total = db.execute("SELECT count(*) FROM runs r" + where, args).fetchone()[0]
            pages = max(1, math.ceil(total / 20))
            page = min(page, pages)
            rows = db.execute(RUN_SELECT + where + " ORDER BY r.id DESC LIMIT 20 OFFSET ?", [*args, (page - 1) * 20]).fetchall()
            accounts = account_options(db)
            models = [row[0] for row in db.execute("SELECT DISTINCT model FROM runs ORDER BY model")]
            groups = [row[0] for row in db.execute("SELECT DISTINCT group_name FROM runs WHERE group_name!='' ORDER BY group_name")]
        return dict(items=[self.serialize(row, include_account=True) for row in rows], total=total, page=page, pages=pages,
                    accounts=accounts, models=models, groups=groups)

    def stats(self):
        since = time.time() - 86400
        with self.db() as db:
            counts = {row["status"]: row["n"] for row in db.execute(
                "SELECT status,count(*) AS n FROM run_outcomes WHERE started>=? GROUP BY status", (since,))}
            enabled_nodes = db.execute("SELECT COUNT(*) FROM nodes n LEFT JOIN node_groups g ON g.id=n.group_id "
                                       "WHERE n.enabled=1 AND (n.group_id IS NULL OR g.enabled=1) "
                                       "AND (n.protocol='codex' OR length(trim(n.api_key))>0)").fetchone()[0]
        success, failed = counts.get("success", 0), counts.get("error", 0)
        completed = success + failed
        # The displayed total and success-rate denominator use the same outcomes.
        # Cancelled and unfinished requests remain separate diagnostic counts.
        return dict(window_hours=24, total=completed, success=success,
                    completed=completed, failed=failed, cancelled=counts.get("cancelled", 0),
                    running=counts.get("running", 0) + counts.get("queued", 0),
                    rate=round(success / completed * 100, 1) if completed else None,
                    enabled_nodes=enabled_nodes)

    def retry_run(self, run_id):
        with self.db() as db:
            row = db.execute("SELECT status,node_id FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise ValueError("检测记录不存在")
        if request_status(row["status"]) != "error":
            raise ValueError("只有请求失败的记录可以重试")
        return self.start_run(source="retry", node_id=row["node_id"])

    def set_favorite(self, run_id, favorite):
        if type(favorite) is not bool:
            raise ValueError("收藏状态必须为 true 或 false")
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise ValueError("检测记录不存在")
            if row["status"] in ("running", "queued"):
                raise ValueError("生成完成后才可以收藏")
            db.execute("UPDATE runs SET favorite=? WHERE id=?", (int(favorite), run_id))
        return dict(id=run_id, favorite=favorite)

    def delete_runs(self, run_ids, account="all"):
        if not isinstance(run_ids, list) or not run_ids or len(run_ids) > 1000:
            raise ValueError("请提供 1–1000 个检测记录编号")
        if any(isinstance(run_id, bool) or type(run_id) is not int or run_id < 1 for run_id in run_ids):
            raise ValueError("检测记录编号无效")
        run_ids = list(dict.fromkeys(run_ids))
        placeholders = ",".join("?" for _ in run_ids)
        account_clause, account_args = account_filter(account)
        account_where = " AND " + account_clause if account_clause else ""
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(f"SELECT r.id,r.status,r.favorite FROM runs r WHERE r.id IN ({placeholders})" + account_where,
                              [*run_ids, *account_args]).fetchall()
            if len(rows) != len(run_ids):
                raise ValueError("所选记录不存在或不属于当前账号筛选" if account != "all" else "检测记录不存在")
            if any(row["status"] in ("running", "queued") for row in rows):
                raise ValueError("检测正在运行，完成后才可以删除")
            if any(row["favorite"] for row in rows):
                raise ValueError("所选记录包含已收藏作品，请先取消收藏；本批次未删除任何记录")
            db.execute(f"DELETE FROM runs WHERE id IN ({placeholders})", run_ids)
        return {"deleted": len(run_ids)}

    def reset_generation_data(self):
        """Clear this instance's generated content and request ledger, keeping access and configuration."""
        with self.lock:
            with self.db() as db:
                db.execute("PRAGMA secure_delete=ON")
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM runs WHERE favorite=1 LIMIT 1").fetchone():
                    raise ValueError("存在已收藏作品，请先取消全部收藏再初始化数据库")
                if self.run_cancellations or db.execute("SELECT 1 FROM runs WHERE status IN ('running','queued') LIMIT 1").fetchone():
                    raise ValueError("正在生成，请先停止测试并等待当前请求结束")
                if self.guest_queue.unfinished_tasks or db.execute(
                        "SELECT 1 FROM guest_results WHERE json_extract(result,'$.status') IN ('queued','running') LIMIT 1").fetchone():
                    raise ValueError("访客请求正在运行，请等待完成后再初始化数据库")
                counts = {name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                          for name in ("runs", "image_library", "guest_results", "run_outcomes")}
                saved = json.loads(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])
                saved.update(enabled=False, next_run=None)
                db.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(saved),))
                for name in ("image_library", "runs", "guest_results", "run_outcomes"):
                    db.execute(f"DELETE FROM {name}")
                db.execute("DELETE FROM sqlite_sequence WHERE name IN ('image_library','run_outcomes')")
            self.scheduled_queue.clear()
            try:
                with self.db() as db:
                    db.execute("VACUUM")
                compacted = True
            except sqlite3.OperationalError:
                compacted = False
        return dict(deleted_runs=counts["runs"], deleted_images=counts["image_library"],
                    deleted_guest_results=counts["guest_results"], deleted_outcomes=counts["run_outcomes"],
                    compacted=compacted)

    def gallery(self, page=1, status="all", protocol="all", source="all", effort="all",
                has_svg="all", group_by="none", selection_only=False, account="all", include_account=False, favorite="all"):
        allowed = {
            "status": ("all", "success", "error", "running", "cancelled"),
            "protocol": ("all", "responses", "chat", "codex"),
            "source": ("all", "manual", "scheduled", "retry"),
            "effort": ("all", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
            "has_svg": ("all", "yes", "no"),
            "favorite": ("all", "yes", "no"),
            "group_by": ("none", "node", "loop_group", "model", "date"),
        }
        values = locals()
        if type(page) is not int or not 1 <= page <= 1000000 or any(
            values[key] not in options for key, options in allowed.items()
        ):
            raise ValueError("画廊分页或筛选参数无效")
        clauses, args = [], []
        if favorite != "all":
            clauses.append("r.favorite=?")
            args.append(int(favorite == "yes"))
        if account != "all" and not include_account:
            raise ValueError("账号筛选需要管理登录")
        account_clause, account_args = account_filter(account)
        if account_clause:
            clauses.append(account_clause)
            args.extend(account_args)
        if status == "success":
            clauses.append("r.status IN ('success','passed','invalid')")
        elif status != "all":
            clauses.append("r.status = ?")
            args.append(status)
        for column, value in (("protocol", protocol), ("source", source), ("effort", effort)):
            if value != "all":
                clauses.append(f"r.{column} = ?")
                args.append(value)
        if has_svg != "all":
            clauses.append("length(COALESCE(i.svg, r.svg)) > 0" if has_svg == "yes" else "(i.svg IS NULL AND (r.svg IS NULL OR length(r.svg) = 0))")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        group_expression = {
            "node": "COALESCE(NULLIF(r.node_name, ''), '历史节点')",
            "loop_group": "COALESCE(NULLIF(r.group_name, ''), '单个模型 / 历史记录')",
            "model": "r.model",
            "date": "strftime('%Y-%m-%d', r.started, 'unixepoch', '+8 hours')",
        }.get(group_by)
        with self.db() as db:
            if selection_only:
                selection_where = where + (" AND " if where else " WHERE ") + "r.status NOT IN ('running','queued') AND r.favorite=0"
                ids = [row[0] for row in db.execute(
                    "SELECT r.id FROM runs r LEFT JOIN image_library i ON i.run_id=r.id" + selection_where + " ORDER BY r.id DESC", args)]
                return dict(ids=ids, total=len(ids))
            total = db.execute("SELECT COUNT(*) FROM runs r LEFT JOIN image_library i ON i.run_id=r.id"+where,args).fetchone()[0]
            pages = max(1, math.ceil(total/12))
            page = min(page,pages)
            order = f"{group_expression} ASC, r.id DESC" if group_expression else "r.id DESC"
            rows = db.execute(RUN_SELECT+where+f" ORDER BY {order} LIMIT 12 OFFSET ?",[*args,(page-1)*12]).fetchall()
            accounts = account_options(db) if include_account else None
        items = [self.serialize(row, include_account=include_account) for row in rows]
        for item in items:
            item["group_key"] = ((item.get("node_name") or "历史节点") if group_by == "node" else
                                  (item.get("group_name") or "单个模型 / 历史记录") if group_by == "loop_group" else
                                  item.get("model") if group_by == "model" else
                                  time.strftime("%Y-%m-%d", time.gmtime(item["started"] + 8 * 3600)) if group_by == "date" else None)
        return dict(items=items,total=total,page=page,pages=pages,group_by=group_by,
                    **({"accounts": accounts} if include_account else {}))

    @staticmethod
    def serialize(row, detail=False, include_account=False):
        result = dict(row)
        result["favorite"] = bool(result.get("favorite", False))
        if not include_account:
            result.pop("account_fingerprint", None)
            result.pop("account_label", None)
        library_svg = result.pop("library_svg", "") or ""
        legacy_svg = result.pop("svg", "") or ""
        result["has_svg"] = bool(library_svg or legacy_svg)
        result["image_id"] = result.pop("library_image_id", None)
        result.pop("library_content_type", None)
        result.pop("library_created", None)
        result["filename"] = run_filename(result)
        config = {"base_url": result["base_url"]}
        tests = json.loads(result.pop("tests", "{}"))
        if not result.get("output") and isinstance(tests, dict):
            result["output"] = next((test.get("output", "") for test in tests.values() if test.get("output")), "")
        result["status"] = request_status(result.get("status"))
        if result["status"] == "success":
            result["error"] = ""
        for key in ("output", "error", "returned_model", "model"):
            result[key] = redact(result.get(key) or "", config)
        result["has_html"] = bool(extract_html(result.get("output")))
        result["base_url"] = mask_url(result["base_url"])
        result["usage"] = json.loads(result.get("usage") or "{}")
        for key in ("checks", "test_version", "scene", "nonce", "test_type"):
            result.pop(key, None)
        if not detail:
            result.pop("output", None)
            result.pop("prompt", None)
        return result

    def state(self):
        # All scheduling mutations use this same lock. Return one coherent
        # database snapshot, never a client-side guess of the current state.
        with self.lock:
            return self._state_snapshot()

    def _state_snapshot(self):
        now = time.time()
        with self.db() as db:
            rows = db.execute(RUN_SELECT + " WHERE r.started>=? ORDER BY r.id DESC", (now-86400,)).fetchall()
            running = db.execute("SELECT id FROM runs WHERE status='running'").fetchone()
        results = [self.serialize(r) for r in rows]
        metrics = self.stats()
        settings = self.settings(public=True)
        public = {key: settings[key] for key in ("base_url", "model", "effort", "protocol", "enabled", "next_run", "interval_seconds", "guest_enabled", "node_name", "active_node_id", "schedule_mode")}
        with self.lock:
            queued_ids = list(self.scheduled_queue)
            stopping = any(event.is_set() for event in self.run_cancellations.values())
        with self.db() as db:
            queued_nodes = [f"节点 {index}" for index, row in enumerate(db.execute(
                f"SELECT name FROM nodes WHERE id IN ({','.join('?' for _ in queued_ids)}) ORDER BY id", queued_ids
            ).fetchall(), 1)] if queued_ids else []
        return dict(settings=public, server_time=now,
                    running=running[0] if running else None, stopping=stopping, queued_nodes=queued_nodes, timeline=results, task_prompt=settings["task_prompt"],
                    stats=dict(total=metrics["total"], success=metrics["success"],
                               completed=metrics["completed"], errors=metrics["failed"],
                               cancelled=metrics["cancelled"], running=metrics["running"], rate=metrics["rate"]))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, data, content_type="application/json; charset=utf-8", status=200, svg=False, download_name=None, preview=False, auth_cookie=None, attachment=False):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if auth_cookie is not None:
            self.send_header("Set-Cookie", auth_cookie)
        if download_name:
            disposition = "attachment" if attachment else "inline"
            self.send_header("Content-Disposition", f'{disposition}; filename="{filename_slug(download_name, "result.svg", 180)}"')
        policy = "sandbox; default-src 'none'; style-src 'unsafe-inline'" if svg else "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        if preview:
            policy = "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
        self.send_header("Content-Security-Policy", policy)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # A browser may cancel a stale poll while refreshing or navigating.
            # The request is already over; do not print a misleading traceback.
            return

    def bearer_token(self):
        scheme, _, token = self.headers.get("Authorization", "").partition(" ")
        return token if scheme.lower() == "bearer" else ""

    def cookie_token(self):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
            token = cookies[SESSION_COOKIE].value if SESSION_COOKIE in cookies else ""
        except (CookieError, ValueError):
            return ""
        return token if re.fullmatch(r"[A-Za-z0-9_-]{1,256}", token) else ""

    def session_cookie(self, token=""):
        cookies = SimpleCookie()
        cookies[SESSION_COOKIE] = token
        cookie = cookies[SESSION_COOKIE]
        cookie["path"] = "/"
        cookie["httponly"] = True
        cookie["samesite"] = "Strict"
        cookie["max-age"] = SESSION_TTL if token else 0
        if not token:
            cookie["expires"] = "Thu, 01 Jan 1970 00:00:00 GMT"
        # Only the actual connection determines TLS; forwarded headers are untrusted.
        if isinstance(self.connection, ssl.SSLSocket):
            cookie["secure"] = True
        return cookie.OutputString()

    def authorized(self):
        return any(self.server.monitor.authenticated(token)
                   for token in (self.bearer_token(), self.cookie_token()) if token)

    def local_setup(self):
        host = parse.urlsplit("http://" + self.headers.get("Host", "")).hostname
        return (self.server.server_address[0] in ("127.0.0.1", "::1") and host in ("127.0.0.1", "localhost", "::1")
                and self.client_address[0] in ("127.0.0.1", "::1") and not self.headers.get("Forwarded") and not self.headers.get("X-Forwarded-For"))

    def do_GET(self):
        url = parse.urlsplit(self.path)
        if url.path.startswith("/api/"):
            monitor = self.server.monitor
            if url.path == "/api/auth/status":
                return self.send({"configured": monitor.password_configured(), "setup_allowed": self.local_setup(),
                                  "authenticated": self.authorized()})
            if url.path == "/api/guest/results":
                return self.send(monitor.guest_results())
            guest_match = re.fullmatch(r"/api/guest/results/([0-9a-f]{24})(/svg|/html)?",url.path)
            if guest_match:
                with monitor.db() as db:
                    row = db.execute("SELECT result FROM guest_results WHERE id=? AND (created>=? OR json_extract(result,'$.status') IN ('queued','running'))",(guest_match[1],time.time()-3600)).fetchone()
                if row:
                    result = json.loads(row[0])
                    if not guest_match[2]:
                        return self.send(monitor.guest_view(result))
                    if guest_match[2] == "/html":
                        if html := extract_html(result.get("output")):
                            if parse.parse_qs(url.query).get("preview") == ["1"]:
                                html = fitted_preview(html)
                            return self.send(html.encode(), "text/html; charset=utf-8", preview=True,
                                             download_name=guest_filename(result).removesuffix(".svg") + ".html")
                        return self.send({"error":"未找到 HTML 文档"}, status=404)
                    if svg := result.get("svg"):
                        download = parse.parse_qs(url.query).get("download") == ["1"]
                        if download:
                            svg = downloadable_svg(svg, result.get("output", ""))
                        return self.send(svg.encode(),"image/svg+xml; charset=utf-8",svg=True,download_name=guest_filename(result),
                                         attachment=download, preview=download)
                return self.send({"error":"结果已过期或不存在"},status=404)
            if url.path.startswith("/api/admin/"):
                if not self.authorized():
                    return self.send({"error": "请输入管理口令"}, status=401)
                if url.path == "/api/admin/settings":
                    return self.send(monitor.settings(public=True))
                if url.path == "/api/admin/nodes":
                    return self.send(monitor.nodes())
                if url.path == "/api/admin/groups":
                    return self.send(monitor.groups())
                if url.path == "/api/admin/runs":
                    params = parse.parse_qs(url.query)
                    if "page" in params:
                        try:
                            return self.send(monitor.history(page=int(params["page"][0]),
                                **{key: params.get(key, ["all"])[0] for key in ("status", "account", "model", "group", "period", "favorite")},
                                search=params.get("search", [""])[0], selection_only=params.get("selection", ["0"])[0] == "1"))
                        except ValueError as exc:
                            return self.send({"error":str(exc)}, status=400)
                    before = parse.parse_qs(url.query).get("before", [None])[0]
                    if before and (not before.isdigit() or len(before) > 18):
                        return self.send({"error": "无效分页参数"}, status=400)
                    return self.send(monitor.runs(int(before) if before else None, include_account=True))
                if url.path == "/api/admin/stats":
                    return self.send(monitor.stats())
                if url.path == "/api/admin/codex/account":
                    return self.send(login_status())
                if url.path == "/api/admin/codex":
                    status = login_status()
                    try:
                        refresh = parse.parse_qs(url.query).get("refresh", ["0"])[0] == "1"
                        status["models"] = available_models(status.get("account_fingerprint", ""), force_refresh=refresh) if status["logged_in"] else []
                        status["combinations"] = model_combinations(status["models"])
                    except ValueError as exc:
                        status.update(models=[], combinations=[], model_error=str(exc))
                    return self.send(status)
            if url.path == "/api/state":
                return self.send(dict(monitor.state(), authenticated=self.authorized()))
            if url.path == "/api/runs":
                params = parse.parse_qs(url.query)
                include_account = self.authorized()
                account = params.get("account", ["all"])[0]
                if account != "all" and not include_account:
                    return self.send({"error": "账号筛选需要管理登录"}, status=401)
                if "page" in params or "account" in params or "favorite" in params:
                    try:
                        return self.send(monitor.gallery(
                            page=int(params.get("page", ["1"])[0]), status=params.get("status", ["all"])[0],
                            protocol=params.get("protocol", ["all"])[0], source=params.get("source", ["all"])[0],
                            effort=params.get("effort", ["all"])[0], has_svg=params.get("has_svg", ["all"])[0],
                            group_by=params.get("group_by", ["none"])[0], selection_only=params.get("selection", ["0"])[0] == "1",
                            account=account, include_account=include_account, favorite=params.get("favorite", ["all"])[0]))
                    except ValueError:
                        return self.send({"error":"画廊分页或筛选参数无效"},status=400)
                before = parse.parse_qs(url.query).get("before", [None])[0]
                if before and (not before.isdigit() or len(before) > 18):
                    return self.send({"error": "无效分页参数"}, status=400)
                return self.send(monitor.runs(int(before) if before else None, include_account=include_account))
            match = re.fullmatch(r"/api/runs/(\d{1,18})(/svg|/html)?", url.path)
            if match:
                with monitor.db() as db:
                    row = db.execute(RUN_SELECT + " WHERE r.id=?", (int(match[1]),)).fetchone()
                if row:
                    if match[2] == "/html":
                        result = monitor.serialize(row, detail=True)
                        if html := extract_html(result.get("output")):
                            if parse.parse_qs(url.query).get("preview") == ["1"]:
                                html = fitted_preview(html)
                            return self.send(html.encode(), "text/html; charset=utf-8", preview=True,
                                             download_name=run_filename(row).removesuffix(".svg") + ".html")
                        return self.send({"error":"未找到 HTML 文档"}, status=404)
                    if match[2] == "/svg" and (row["library_svg"] or row["svg"]):
                        svg = row["library_svg"] or row["svg"]
                        download = parse.parse_qs(url.query).get("download") == ["1"]
                        if download:
                            svg = downloadable_svg(svg, monitor.serialize(row, detail=True).get("output", ""))
                        return self.send(redact(svg, {"base_url":row["base_url"]}).encode(), "image/svg+xml; charset=utf-8", svg=True, download_name=run_filename(row),
                                         attachment=download, preview=download)
                    if not match[2]:
                        return self.send(monitor.serialize(row, detail=True, include_account=self.authorized()))
            return self.send({"error": "未找到记录"}, status=404)
        files = {"/": "index.html", "/admin": "admin.html", "/admin/": "admin.html", "/admin/records": "records.html", "/admin/records/": "records.html", "/admin.js": "admin.js", "/records.js": "records.js", "/privacy.js": "privacy.js", "/live.js": "live.js", "/previews.js": "previews.js", "/app.js": "app.js", "/style.css": "style.css", "/favicon.svg": "favicon.svg"}
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
        if self.path not in ("/api/guest/run", "/api/auth/login", "/api/auth/setup", "/api/auth/logout") and not self.authorized():
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
                    token = self.server.monitor.login(values.get("password"))
                    return self.send({"token": token}, auth_cookie=self.session_cookie(token))
                except ValueError as exc:
                    return self.send({"error":str(exc)},status=401)
            if self.path == "/api/auth/logout":
                for token in (self.bearer_token(), self.cookie_token()):
                    self.server.monitor.logout(token)
                return self.send({"ok":True}, auth_cookie=self.session_cookie())
            if self.path == "/api/admin/settings":
                return self.send(self.server.monitor.save(values))
            if self.path == "/api/admin/testing/start":
                return self.send(self.server.monitor.start_testing())
            if self.path == "/api/admin/testing/stop":
                return self.send(self.server.monitor.stop_testing())
            if self.path == "/api/admin/groups":
                return self.send(self.server.monitor.save_group(values), status=201)
            group_action = re.fullmatch(r"/api/admin/groups/([1-9]\d{0,17})(?:/(enable|disable|delete))?", self.path)
            if group_action:
                group_id = int(group_action[1])
                if group_action[2] == "enable":
                    return self.send(self.server.monitor.set_group_enabled(group_id, True))
                if group_action[2] == "disable":
                    return self.send(self.server.monitor.set_group_enabled(group_id, False))
                if group_action[2] == "delete":
                    return self.send(self.server.monitor.delete_group(group_id))
                return self.send(self.server.monitor.save_group(values, group_id))
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
            stop = re.fullmatch(r"/api/admin/runs/([1-9]\d{0,17})/stop", self.path)
            if stop:
                return self.send(self.server.monitor.stop_run(int(stop[1])))
            if self.path == "/api/admin/runs/delete":
                return self.send(self.server.monitor.delete_runs(values.get("ids"), account=values.get("account", "all")))
            favorite = re.fullmatch(r"/api/admin/runs/([1-9]\d{0,17})/favorite", self.path)
            if favorite:
                return self.send(self.server.monitor.set_favorite(int(favorite[1]), values.get("favorite")))
            if self.path == "/api/admin/database/reset":
                if values != {"confirm": "RESET_GENERATION_DATA"}:
                    raise ValueError("请先确认初始化生成数据")
                return self.send(self.server.monitor.reset_generation_data())
            if self.path == "/api/admin/run":
                return self.send({"id": self.server.monitor.run_once()}, status=202)
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
    instance_lock = (directory / "server.lock").open("a+b")
    deadline = time.monotonic() + LOCK_WAIT
    while True:
        try:
            lock_file(instance_lock)
            return instance_lock
        except OSError:
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
        if not 6 <= len(token) <= 256:
            raise SystemExit(f"ADMIN_TOKEN 需要 6–256 个字符，当前为 {len(token)} 个，"
                             "请修改环境变量后重新部署")
        monitor.setup_password(token)
    if host not in ("127.0.0.1", "localhost") and not monitor.password_configured():
        raise SystemExit("未设置管理密码：请通过 ADMIN_TOKEN 环境变量提供 6–256 个字符的初始密码后重新部署；"
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
