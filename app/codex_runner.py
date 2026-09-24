"""Use the official local CLI; credentials stay in Codex's own storage."""
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time

from local_runtime import process_options, stop_process

MAX_OUTPUT = 2 * 1024 * 1024
_catalog_lock = threading.Lock()
_catalog = None
_catalog_at = 0
_catalog_account = ""
_catalog_command = None


def desktop_codex_command():
    """Find the desktop-managed CLI even outside the app's inherited PATH."""
    if os.name != "nt":
        return None
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    root = local / "OpenAI" / "Codex" / "bin"
    try:
        # Desktop updates use versioned folders. Prefer the latest installed
        # executable and ignore folders that contain no CLI.
        candidates = [(path.stat().st_mtime_ns, str(path), path)
                      for path in root.glob("*/codex.exe") if path.is_file()]
        return str(max(candidates)[2]) if candidates else None
    except OSError:
        return None


def codex_command():
    explicit = os.environ.get("PELICAN_CODEX_BIN")
    executable = explicit or desktop_codex_command() or shutil.which("codex.exe") or shutil.which("codex")
    if not executable:
        raise ValueError("未找到 Codex CLI，请安装 @openai/codex 并执行 codex login")
    path = Path(executable).resolve()
    if path.suffix.lower() in (".cmd", ".bat", ".ps1"):
        # npm's wrapper needs a shell; invoke its JS entry with argv instead.
        script = path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        node = shutil.which("node")
        if node and script.is_file():
            return [node, str(script)]
        raise ValueError("无法定位 Codex 程序，请将 PELICAN_CODEX_BIN 设为 codex.exe 的完整路径")
    if not path.is_file():
        raise ValueError("PELICAN_CODEX_BIN 指定的程序不存在")
    return [str(path)]


def cli_environment():
    env = dict(os.environ)
    # Force the requested account login rather than inherited API billing.
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
        env.pop(key, None)
    return env


def _auth_path():
    home = os.environ.get("CODEX_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".codex") / "auth.json"


def _account_id():
    """Read only the stable account id; never expose token contents."""
    try:
        data = json.loads(_auth_path().read_text(encoding="utf-8"))
        account_id = data.get("tokens", {}).get("account_id")
        return account_id.strip() if isinstance(account_id, str) and account_id.strip() else ""
    except (OSError, ValueError, TypeError, UnicodeDecodeError):
        return ""


def _account_fingerprint(account_id=""):
    if not account_id:
        return ""
    return "acct-" + hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:12]


def login_status():
    try:
        result = subprocess.run(codex_command() + ["login", "status"], capture_output=True,
                                timeout=15, env=cli_environment(), **process_options())
        message = (result.stdout + result.stderr).decode("utf-8", errors="replace").lower()
        if result.returncode == 0 and "chatgpt" in message:
            fingerprint = _account_fingerprint(_account_id())
            return {"installed": True, "logged_in": True, "mode": "chatgpt", "message": "已通过 ChatGPT 账号登录",
                    "account_fingerprint": fingerprint, "account_label": f"账号指纹 {fingerprint[5:]}" if fingerprint else "账号身份不可读取"}
        return {"installed": True, "logged_in": False, "mode": "other", "message": "请在终端执行 codex login，选择 ChatGPT 账号登录"}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"installed": False, "logged_in": False, "mode": "unknown", "message": "无法读取 Codex 登录状态，请检查 CLI 安装"}


def verify_account(config):
    status = login_status()
    if not status["logged_in"]:
        raise ValueError("请先在本机执行 codex login 并使用 ChatGPT 账号登录")
    expected = config.get("_codex_account_fingerprint", "")
    current = status.get("account_fingerprint", "")
    if expected and current and expected != current:
        raise ValueError("Codex 登录账号已切换，本轮已停止；请重新开始以保持上下文一致")
    return status


def available_models(account_fingerprint=None, force_refresh=False):
    """Read model metadata through the public app-server protocol, no generation."""
    global _catalog, _catalog_at, _catalog_account, _catalog_command
    with _catalog_lock:
        command = codex_command()
        account_fingerprint = account_fingerprint if account_fingerprint is not None else _account_fingerprint(_account_id())
        if (not force_refresh and _catalog is not None and _catalog_account == account_fingerprint
                and _catalog_command == command
                and time.monotonic() - _catalog_at < 300):
            return _catalog
        incoming = queue.Queue()
        with tempfile.TemporaryDirectory(prefix="pelican-models-") as folder:
            try:
                process = subprocess.Popen(command + ["app-server", "-c", 'model_provider="openai"'],
                                           cwd=folder, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, env=cli_environment(), **process_options())
            except OSError as exc:
                raise ValueError("无法启动 Codex 模型目录，请检查 CLI 安装") from exc
            def read_messages():
                try:
                    for line in process.stdout:
                        try:
                            incoming.put(json.loads(line))
                        except (ValueError, UnicodeDecodeError):
                            continue
                finally:
                    incoming.put(None)
            threading.Thread(target=read_messages, daemon=True).start()
            deadline = time.monotonic() + 25
            def send(message):
                process.stdin.write((json.dumps(message) + "\n").encode())
                process.stdin.flush()
            def receive(identifier):
                while True:
                    item = incoming.get(timeout=max(.01, deadline-time.monotonic()))
                    if item is None:
                        raise ValueError("Codex 模型目录连接已关闭")
                    if item.get("id") == identifier:
                        if "error" in item:
                            raise ValueError("Codex 未能返回模型目录，请更新 CLI 或手动填写模型名称")
                        return item.get("result", {})
            try:
                send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "pelican_local", "version": "1.0.0"}}})
                receive(1)
                send({"method": "initialized", "params": {}})
                models, cursor, identifier = [], None, 2
                while True:
                    send({"id": identifier, "method": "model/list", "params": {"limit": 100, "includeHidden": False, "cursor": cursor}})
                    result = receive(identifier)
                    for model in result.get("data", []):
                        models.append({"model": model["model"], "name": model.get("displayName", model["model"]),
                                       "efforts": [e["reasoningEffort"] for e in model.get("supportedReasoningEfforts", [])],
                                       "default_effort": model.get("defaultReasoningEffort", "medium")})
                    cursor = result.get("nextCursor")
                    if not cursor:
                        break
                    identifier += 1
                _catalog, _catalog_at, _catalog_account = models, time.monotonic(), account_fingerprint
                _catalog_command = command
                return models
            except (queue.Empty, OSError):
                raise ValueError("读取模型列表超时，可手动填写当前账号可用的模型") from None
            finally:
                stop_process(process)
                with contextlib.suppress(OSError):
                    process.stdin.close()
                with contextlib.suppress(OSError):
                    process.stdout.close()


def failure_message(text):
    lower = text.lower()
    if any(word in lower for word in ("rate limit", "usage limit", "quota", "limit reached")):
        return "Codex 账号额度不足或限流，请等待额度恢复后再试"
    if any(word in lower for word in ("not supported", "not found", "unsupported model")):
        return "Codex 不支持当前模型或思考强度，请从账号模型列表重新选择"
    if any(word in lower for word in ("unauthorized", "not logged", "login", "auth")):
        return "Codex 登录失效，请在终端重新执行 codex login"
    return "Codex 生成失败，请检查 CLI 登录、网络、所选模型及思考强度"


def call_codex(config, prompt):
    if config.get("_guest"):
        raise ValueError("访客不能调用本机 Codex 账号")
    cancel_event = config.get("_cancel_event")
    if cancel_event and cancel_event.is_set():
        raise ValueError("已手动停止生成")
    status = verify_account(config)
    try:
        catalog = available_models(status.get("account_fingerprint", ""))
    except ValueError:
        # Model metadata is advisory; if the CLI cannot list it, let exec report
        # the authoritative result instead of blocking manual model names.
        catalog = []
    if catalog:
        selected = next((item for item in catalog if item["model"] == config["model"]), None)
        if selected is None or config["effort"] not in selected["efforts"]:
            # A cached directory can lag behind account/model availability.
            # Refresh once before blocking the requested combination.
            try:
                catalog = available_models(status.get("account_fingerprint", ""), force_refresh=True)
            except ValueError:
                catalog = []
            selected = next((item for item in catalog if item["model"] == config["model"]), None)
            if catalog and selected is None:
                raise ValueError("当前模型不在此 ChatGPT 账号的 Codex 可用列表中，请刷新账号模型并重新选择")
            # An empty effort list means the catalog did not publish
            # capability metadata. In that case the UI exposes the full
            # supported range and the request should use the same rule.
            if selected and selected.get("efforts") and config["effort"] not in selected["efforts"]:
                raise ValueError("当前思考等级不受所选模型支持，请从模型列表重新选择思考等级")
    with tempfile.TemporaryDirectory(prefix="pelican-codex-") as folder:
        root = Path(folder)
        final = root / "result.txt"
        image_args, texts = [], []
        if isinstance(prompt, list):
            for part in prompt:
                if "text" in part:
                    texts.append(part["text"])
                elif part.get("type") in ("image_url", "input_image"):
                    url = part["image_url"]
                    url = url["url"] if isinstance(url, dict) else url
                    if not url.startswith("data:image/png;base64,"):
                        raise ValueError("Codex 图像输入仅接收本地生成的 PNG 帧")
                    image = root / f"frame-{len(image_args)//2}.png"
                    image.write_bytes(base64.b64decode(url.split(",", 1)[1], validate=True))
                    image_args.extend(["--image", str(image)])
        else:
            texts.append(prompt)
        instructions = "\n".join(texts)
        command = codex_command() + ["exec", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "--ephemeral",
                    "--sandbox", "read-only", "--model", config["model"], "--json", "--color", "never",
                    "--output-last-message", str(final), "-c", 'forced_login_method="chatgpt"',
                    "-c", 'model_provider="openai"', "-c", 'approval_policy="never"',
                    "-c", "model_reasoning_effort=" + json.dumps(config["effort"]),
                    "-c", "project_doc_max_bytes=0", "-c", "features.shell_tool=false",
                    "-c", "features.multi_agent=false", "-c", 'web_search="disabled"'] + image_args + ["-"]
        with (root / "events.jsonl").open("w+b") as events, (root / "stderr.txt").open("w+b") as errors:
            process = subprocess.Popen(command, cwd=root, stdin=subprocess.PIPE, stdout=events, stderr=errors,
                                       env=cli_environment(), **process_options())
            try:
                process.stdin.write(instructions.encode("utf-8"))
                process.stdin.close()
                deadline = time.monotonic() + config["timeout_seconds"]
                while process.poll() is None:
                    if cancel_event and cancel_event.is_set():
                        stop_process(process)
                        raise ValueError("已手动停止生成")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, config["timeout_seconds"])
                    try:
                        process.wait(timeout=min(.25, remaining))
                    except subprocess.TimeoutExpired:
                        continue
            except subprocess.TimeoutExpired:
                stop_process(process)
                raise ValueError("Codex 生成超时，已停止该次任务；可增加后台请求超时") from None
            except BaseException:
                stop_process(process)
                raise
            events.seek(0)
            usage, failure, completed = {}, "", False
            for line in events:
                if len(line) > MAX_OUTPUT * 2:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if event.get("type") == "turn.completed":
                    completed = True
                    usage = event.get("usage", {})
                if event.get("type") in ("error", "turn.failed"):
                    failure = json.dumps(event)
            if process.returncode or not completed or not final.is_file():
                errors.seek(0)
                raise ValueError(failure_message(failure + errors.read(32768).decode("utf-8", errors="replace")))
        if final.stat().st_size > MAX_OUTPUT:
            raise ValueError("Codex 返回结果超过 2 MB 上限")
        text = final.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError("Codex 未返回可展示的内容")
        # Check again after the process exits so an account switch during generation
        # cannot be mistaken for a result from the account captured at start.
        verify_account(config)
        # CLI does not expose a verified backend model identity in its final event.
        return text, usage, "CLI 未返回后端模型标识"
