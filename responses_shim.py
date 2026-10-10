"""Responses-API → chat-completions 本地转换层（纯标准库，随路线产物提交）。

为什么存在：本路线固定的 codex 0.154.0 只有 responses wire_api（chat 已删除），
而 ARC-Bench 决赛网关的 `/v1/responses` 实际是 chat-completions 协议别名
（要求 `messages`、嵌套 `function` 工具、拒绝 `developer` 角色）——两边协议对不上，
直连必 422 `tools[0]: missing field 'function'`。

做法：在 127.0.0.1 起一次性 HTTP 服务；codex 仍配 `wire_api=responses`、base_url
指向本服务。本服务把 Responses 请求翻译成 chat 请求转发给真网关，再把 chat SSE
回流翻译成 codex 认识的 Responses SSE（事件集合按其 0.154.0 的解析器源码实现：

- 认：response.created / output_item.added|done / output_text.delta /
  reasoning_text.delta / response.failed / response.completed（id 必填，usage 可选）
- 忽略但可安全发送：response.in_progress（用作流保活，防止 codex 触发流空闲超时）

已知取舍：
- 请求里 `type=namespace` / `web_search` 工具直接丢弃（chat 协议无法表达托管工具）。
- 工具输出中的图片部件转成 `[image omitted]` 文本占位（保持纯文本链路）。
- `reasoning_content` 以 response.reasoning_text.delta 透传（喂空闲计时器 + 可观测）。
- 网关要等模型首个 token 才回响应头（2026-10-06 实测：30k 字符≈4s、120k≈10s，
  思考档大上下文可达分钟级），所以 30s 的响应头等待上限会误杀“长思考”轮次。
  处理：响应头在 20s 内到达 → 原样转发状态码（codex 走自己的 HTTP 错误分类）；
  超过 20s → 先向 codex 回 200+response.created，以 response.in_progress 每 15s
  保活，再等上游（上限 900s）；此模式下上游后到的非 200 只能以 response.failed
  事件呈现（200 已发出，无法再原样转发状态码）。
- 网关会把上游瞬时故障包装成 HTTP 400 + `code=proxy_error`（如 "read: connection
  reset by peer"），而 codex 视 400 为不可重试、直接判 turn 失败（2026-10-08 运行
  日志实例：REQ-5 因此中断整个 run）。处理：对「400 + 代理瞬时标记」在桥内退避
  重试（10/20/30/30/30 秒，共约 2 分钟）；重试等待期间自然落入上面的慢模式保活，
  codex 侧无感、turn 现场不丢。重试耗尽或非此类 400 仍原样转发（快速失败语义不变）。
  已开流后的失败不能重试（内容可能已发出），由 main.py 的模块级兜底重试覆盖。
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib import error as urlerror
from urllib import request as urlrequest

_UPSTREAM_READ_TIMEOUT = 30.0  # 流内单次读超时；超时即发保活事件
_UPSTREAM_FAST_HEADER_WINDOW = 20.0  # 响应头快等待窗口；超窗转“先开流再等头”模式
_UPSTREAM_HEADER_TIMEOUT = 900.0  # 上游连接+响应头总上限（慢模式）
_KEEPALIVE_INTERVAL = 15.0  # 慢模式保活事件间隔
_UPSTREAM_SILENCE_CAP = 900.0
_ERROR_BODY_CAP = 65536

# 网关把上游瞬时故障包成 HTTP 400 + code=proxy_error；仅此类 400 触发桥内重试
# （标记口径与 self-agent/engine/thin.py 的 _PROXY_TRANSIENT_MARKERS 同源）。
_PROXY_TRANSIENT_MARKERS = (
    "proxy_error",
    "read tcp",
    "write tcp",
    "connection reset",
    "i/o timeout",
    "upstream",
)
_PROXY_TRANSIENT_RETRY_DELAYS = (10.0, 20.0, 30.0, 30.0, 30.0)  # 退避序列，总等待≈2 分钟


def _log_default(message: str) -> None:
    print(f"[codex][shim] {message}", flush=True)


def _is_proxy_transient(status: int, body: bytes) -> bool:
    """仅网关“上游瞬时故障”包装出的 400 可重试；其余 400 维持快速失败。"""
    if status != 400:
        return False
    text = body[:500].decode("utf-8", errors="replace").lower()
    return any(marker in text for marker in _PROXY_TRANSIENT_MARKERS)


def _build_opener() -> urlrequest.OpenerDirector:
    """只用环境变量里的代理设置（忽略 Windows 注册表代理，容器内行为不变）。"""
    proxies: dict[str, str] = {}
    for scheme in ("http", "https"):
        for name in (f"{scheme.upper()}_PROXY", f"{scheme}_proxy"):
            value = os.environ.get(name)
            if value:
                proxies[scheme] = value
                break
    return urlrequest.build_opener(urlrequest.ProxyHandler(proxies))


def _as_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


def _flatten_content(parts: Any) -> str:
    """responses 的 content 部件列表 → chat 纯文本（图片记占位符）。"""
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        return ""
    texts: list[str] = []
    for part in parts:
        if isinstance(part, str):
            texts.append(part)
            continue
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str):
            texts.append(text)
            continue
        if part.get("type") in ("input_image", "image_url", "input_audio"):
            texts.append("[image omitted]")
    return "\n".join(texts)


def _output_text(output: Any) -> str:
    """function_call_output 的 output 字段：字符串或部件列表（models.rs 里是 untagged）。"""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return _flatten_content(output)
    if output is None:
        return ""
    return _as_str(output)


def _custom_input(arguments_text: str) -> str:
    """custom（freeform）工具：把 chat 的 JSON arguments 还原成原始输入字符串。"""
    text = arguments_text.strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except Exception:  # noqa: BLE001 - 原样透传是安全回退
        return arguments_text
    if isinstance(parsed, str):
        return parsed
    if isinstance(parsed, dict) and isinstance(parsed.get("input"), str):
        return parsed["input"]
    return arguments_text


def _translate_tools(raw_tools: Any) -> tuple[list[dict[str, Any]], set[str]]:
    chat_tools: list[dict[str, Any]] = []
    custom_names: set[str] = set()
    for tool in raw_tools or []:
        if not isinstance(tool, dict):
            continue
        tool_type = tool.get("type")
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        if tool_type == "function":
            parameters = tool.get("parameters")
            if not isinstance(parameters, dict):
                parameters = {"type": "object", "properties": {}}
            chat_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": tool.get("description") or "",
                        "parameters": parameters,
                    },
                }
            )
        elif tool_type == "custom":
            custom_names.add(name)
            chat_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": (tool.get("description") or "")
                        + "\n(freeform text tool: put the raw content in the `input` field)",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "input": {
                                    "type": "string",
                                    "description": "The raw freeform text.",
                                }
                            },
                            "required": ["input"],
                        },
                    },
                }
            )
        # namespace / web_search / 其它托管工具：chat 协议无法表达，丢弃。
    return chat_tools, custom_names


def _translate_input(raw_input: Any) -> list[dict[str, Any]]:
    if isinstance(raw_input, str):
        return [{"role": "user", "content": raw_input}]
    if not isinstance(raw_input, list):
        return []
    messages: list[dict[str, Any]] = []
    pending_calls: list[dict[str, Any]] = []

    def flush_calls() -> None:
        if not pending_calls:
            return
        calls = list(pending_calls)
        pending_calls.clear()
        last = messages[-1] if messages else None
        if last is not None and last.get("role") == "assistant" and "tool_calls" not in last:
            last["tool_calls"] = calls
            if last.get("content") == "":
                last["content"] = None
        else:
            messages.append({"role": "assistant", "content": None, "tool_calls": calls})

    for item in raw_input:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            flush_calls()
            role = item.get("role")
            if role == "developer":
                role = "system"
            if role not in ("system", "user", "assistant"):
                role = "user"
            messages.append({"role": role, "content": _flatten_content(item.get("content"))})
        elif item_type == "function_call":
            pending_calls.append(
                {
                    "id": item.get("call_id") or ("call_" + uuid.uuid4().hex[:20]),
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": _as_str(item.get("arguments")),
                    },
                }
            )
        elif item_type == "custom_tool_call":
            pending_calls.append(
                {
                    "id": item.get("call_id") or ("call_" + uuid.uuid4().hex[:20]),
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": json.dumps(
                            {"input": _as_str(item.get("input"))}, ensure_ascii=False
                        ),
                    },
                }
            )
        elif item_type in ("function_call_output", "custom_tool_call_output"):
            flush_calls()
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or "",
                    "content": _output_text(item.get("output")),
                }
            )
        # reasoning / 其它未知项：chat 无对应，跳过。
    flush_calls()
    return messages


def translate_request(body: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
    """Responses 请求体 → chat 请求体（返回 chat body 与 custom 工具名单）。"""
    chat: dict[str, Any] = {
        "model": body.get("model") or "",
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    messages: list[dict[str, Any]] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    messages.extend(_translate_input(body.get("input")))
    chat["messages"] = messages

    chat_tools, custom_names = _translate_tools(body.get("tools"))
    if chat_tools:
        chat["tools"] = chat_tools
        # thinking 档模型拒绝 required/dict 形式的 tool_choice，统一降级为 auto。
        choice = body.get("tool_choice")
        chat["tool_choice"] = choice if choice in ("auto", "none") else "auto"
        if isinstance(body.get("parallel_tool_calls"), bool):
            chat["parallel_tool_calls"] = body["parallel_tool_calls"]
    return chat, custom_names


def _usage_payload(usage: Any) -> dict[str, Any] | None:
    if not isinstance(usage, dict):
        return None

    def as_int(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return int(value)

    input_tokens = as_int(usage.get("prompt_tokens"))
    output_tokens = as_int(usage.get("completion_tokens"))
    total_tokens = as_int(usage.get("total_tokens"))
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None
    input_tokens = input_tokens or 0
    output_tokens = output_tokens or 0
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens
    payload: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    prompt_details = usage.get("prompt_tokens_details")
    if isinstance(prompt_details, dict):
        cached = as_int(prompt_details.get("cached_tokens"))
        if cached is not None:
            payload["input_tokens_details"] = {"cached_tokens": cached}
    completion_details = usage.get("completion_tokens_details")
    if isinstance(completion_details, dict):
        reasoning = as_int(completion_details.get("reasoning_tokens"))
        if reasoning is not None:
            payload["output_tokens_details"] = {"reasoning_tokens": reasoning}
    return payload


class _ClientGone(Exception):
    """codex 侧断开了本地 SSE 连接。"""


class _ResponsesTranslator:
    """chat SSE chunk 流 → Responses SSE 事件流。"""

    def __init__(
        self,
        response_id: str,
        custom_names: set[str],
        emit: Callable[[dict[str, Any]], None],
    ) -> None:
        self.response_id = response_id
        self._custom_names = custom_names
        self._emit = emit
        self._seq = 0
        self._message: dict[str, Any] | None = None
        self._calls: dict[int, dict[str, Any]] = {}
        self._usage: dict[str, Any] | None = None
        self._done = False
        self.finish_reason = ""

    def emit_created(self, model: str = "") -> None:
        response: dict[str, Any] = {
            "id": self.response_id,
            "object": "response",
            "status": "in_progress",
        }
        if model:
            response["model"] = model
        self._emit({"type": "response.created", "response": response})

    def keepalive(self) -> None:
        if not self._done:
            self._emit({"type": "response.in_progress", "response": {"id": self.response_id}})

    def on_chunk(self, chunk: dict[str, Any]) -> None:
        if self._done:
            return
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self._usage = usage
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            index = choice.get("index")
            if isinstance(index, int) and index > 0:
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict):
                self._on_delta(delta)
            reason = choice.get("finish_reason")
            if isinstance(reason, str) and reason:
                self.finish_reason = reason

    def _on_delta(self, delta: dict[str, Any]) -> None:
        content = delta.get("content")
        if isinstance(content, str) and content:
            self._on_text(content)
        reasoning = delta.get("reasoning_content")
        if not isinstance(reasoning, str) or not reasoning:
            reasoning = delta.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            self._emit(
                {
                    "type": "response.reasoning_text.delta",
                    "delta": reasoning,
                    "content_index": 0,
                }
            )
        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            for call in tool_calls:
                if isinstance(call, dict):
                    self._on_tool_delta(call)

    def _on_text(self, piece: str) -> None:
        if self._message is None:
            self._seq += 1
            self._message = {
                "id": "msg_" + uuid.uuid4().hex[:16],
                "text": "",
                "seq": self._seq,
            }
            self._emit(
                {
                    "type": "response.output_item.added",
                    "output_index": self._seq - 1,
                    "item": {
                        "type": "message",
                        "id": self._message["id"],
                        "role": "assistant",
                        "content": [],
                    },
                }
            )
        self._message["text"] += piece
        self._emit(
            {
                "type": "response.output_text.delta",
                "item_id": self._message["id"],
                "output_index": self._message["seq"] - 1,
                "content_index": 0,
                "delta": piece,
            }
        )

    def _on_tool_delta(self, call: dict[str, Any]) -> None:
        index = call.get("index")
        if not isinstance(index, int):
            index = max(self._calls, default=-1) + 1
        slot = self._calls.get(index)
        if slot is None:
            self._seq += 1
            slot = {
                "seq": self._seq,
                "item_id": "fc_" + uuid.uuid4().hex[:16],
                "id": "",
                "call_id": "",
                "name": "",
                "args": [],
                "added": False,
                "custom": False,
            }
            self._calls[index] = slot
        function = call.get("function")
        if not isinstance(function, dict):
            function = {}
        name = function.get("name")
        if isinstance(name, str) and name and not slot["name"]:
            slot["name"] = name
            slot["custom"] = name in self._custom_names
        call_id = call.get("id")
        if isinstance(call_id, str) and call_id:
            slot["id"] = call_id
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            slot["args"].append(arguments)
        elif arguments is not None and not isinstance(arguments, (int, float, bool)):
            slot["args"].append(json.dumps(arguments, ensure_ascii=False))
        self._maybe_emit_added(slot)

    def _maybe_emit_added(self, slot: dict[str, Any]) -> None:
        if slot["added"] or not slot["name"]:
            return
        slot["added"] = True
        slot["call_id"] = slot["id"] or ("call_" + uuid.uuid4().hex[:20])
        item: dict[str, Any] = {
            "type": "custom_tool_call" if slot["custom"] else "function_call",
            "id": slot["item_id"],
            "call_id": slot["call_id"],
            "name": slot["name"],
        }
        item["input" if slot["custom"] else "arguments"] = ""
        self._emit(
            {"type": "response.output_item.added", "output_index": slot["seq"] - 1, "item": item}
        )

    def _done_items(self) -> list[tuple[int, dict[str, Any]]]:
        items: list[tuple[int, dict[str, Any]]] = []
        if self._message is not None:
            items.append(
                (
                    self._message["seq"],
                    {
                        "type": "message",
                        "id": self._message["id"],
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": self._message["text"]}
                        ],
                    },
                )
            )
        for slot in self._calls.values():
            if not slot["name"]:
                continue
            call_id = slot["call_id"] or slot["id"] or ("call_" + uuid.uuid4().hex[:20])
            slot["call_id"] = call_id
            arguments = "".join(slot["args"])
            if slot["custom"]:
                item = {
                    "type": "custom_tool_call",
                    "id": slot["item_id"],
                    "call_id": call_id,
                    "name": slot["name"],
                    "input": _custom_input(arguments),
                }
            else:
                item = {
                    "type": "function_call",
                    "id": slot["item_id"],
                    "call_id": call_id,
                    "name": slot["name"],
                    "arguments": arguments,
                }
            items.append((slot["seq"], item))
        items.sort(key=lambda pair: pair[0])
        return items

    def finish(self) -> None:
        if self._done:
            return
        self._done = True
        for seq, item in self._done_items():
            self._emit(
                {"type": "response.output_item.done", "output_index": seq - 1, "item": item}
            )
        response: dict[str, Any] = {"id": self.response_id, "status": "completed"}
        usage = _usage_payload(self._usage)
        if usage:
            response["usage"] = usage
        self._emit({"type": "response.completed", "response": response})

    def fail(self, message: str) -> None:
        if self._done:
            return
        self._done = True
        self._emit(
            {
                "type": "response.failed",
                "response": {
                    "id": self.response_id,
                    "status": "failed",
                    "error": {
                        "message": message,
                        "type": "stream_error",
                        "code": "shim_upstream_error",
                    },
                },
            }
        )

    def summary(self) -> str:
        text_len = len(self._message["text"]) if self._message else 0
        return (
            f"turn done: finish={self.finish_reason or '-'} text_chars={text_len} "
            f"tool_calls={len(self._calls)} usage={'yes' if self._usage else 'no'}"
        )


class _ShimHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "arc-responses-shim/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/health"):
            self._send_body(200, b'{"ok":true}', "application/json")
        else:
            self._send_json(404, {"error": {"message": f"unknown path {self.path}"}})

    def do_POST(self) -> None:  # noqa: N802
        shim: ResponsesShimServer = self.server.shim  # type: ignore[attr-defined]
        if not self.path.rstrip("/").endswith("/responses"):
            self._send_json(404, {"error": {"message": f"unknown path {self.path}"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception as exc:  # noqa: BLE001 - 对端是 codex，错误直接回 JSON
            self._send_json(400, {"error": {"message": f"invalid JSON body: {exc}"}})
            return
        if not isinstance(body, dict):
            self._send_json(400, {"error": {"message": "body must be a JSON object"}})
            return
        chat_body, custom_names = translate_request(body)
        shim.log(
            f"POST /v1/responses -> chat: msgs={len(chat_body['messages'])} "
            f"tools={len(chat_body.get('tools') or [])}"
        )
        payload = json.dumps(chat_body, ensure_ascii=False).encode("utf-8")
        auth = self.headers.get("Authorization") or f"Bearer {shim.api_key}"
        request = urlrequest.Request(
            shim.upstream_chat_url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Authorization": auth,
            },
        )
        abandon = threading.Event()
        upstream_q = self._queue_upstream(request, abandon, shim)
        try:
            entry = upstream_q.get(timeout=_UPSTREAM_FAST_HEADER_WINDOW)
        except queue.Empty:
            entry = None
        if entry is None:
            # 响应头迟到（含瞬时故障重试中）：先给 codex 开流，保活等上游。
            self._relay_slow(
                upstream_q, abandon, custom_names, shim, model=chat_body.get("model") or ""
            )
            return
        upstream, exc, body = entry
        if exc is not None:
            self._relay_upstream_error(exc, shim, body)
            return
        try:
            if upstream.status != 200:
                detail = upstream.read(_ERROR_BODY_CAP)
                shim.log(f"upstream status {upstream.status}: {detail[:300]!r}")
                self._send_body(
                    upstream.status,
                    detail,
                    upstream.headers.get("Content-Type") or "application/json",
                )
                return
            self._relay(upstream, custom_names, shim, model=chat_body.get("model") or "")
        finally:
            upstream.close()

    def _queue_upstream(
        self,
        request: urlrequest.Request,
        abandon: threading.Event,
        shim: "ResponsesShimServer",
    ) -> "queue.Queue":
        """后台线程发起上游请求；响应头可能要等首 token，不能阻塞 handler 线程。

        400 代理瞬时故障（网关把上游 TCP 故障包成 proxy_error）在这里退避重试：
        重试等待期间 handler 的 20s 快窗口超时，自然转入慢模式保活，codex 无感。
        队列只投递最终结果：(响应或 None, 异常或 None, 错误体)。
        """
        upstream_q: "queue.Queue" = queue.Queue(maxsize=1)

        def open_once() -> tuple[Any, Exception | None, bytes]:
            try:
                resp = _build_opener().open(request, timeout=_UPSTREAM_HEADER_TIMEOUT)
                return resp, None, b""
            except urlerror.HTTPError as exc:
                body = b""
                try:
                    body = exc.read(_ERROR_BODY_CAP)
                except Exception:  # noqa: BLE001 - 读不到错误体也要能分类并转发状态码
                    pass
                return None, exc, body
            except Exception as exc:  # noqa: BLE001 - 分类留给调用方
                return None, exc, b""

        def open_upstream() -> None:
            attempts = len(_PROXY_TRANSIENT_RETRY_DELAYS) + 1
            for attempt in range(1, attempts + 1):
                resp, exc, body = open_once()
                if isinstance(exc, urlerror.HTTPError) and _is_proxy_transient(exc.code, body):
                    if attempt < attempts:
                        delay = _PROXY_TRANSIENT_RETRY_DELAYS[attempt - 1]
                        shim.log(
                            f"upstream HTTP {exc.code} proxy transient "
                            f"(attempt {attempt}/{attempts}); retrying in {int(delay)}s"
                        )
                        if abandon.wait(delay):
                            return
                        continue
                    shim.log(
                        f"upstream HTTP {exc.code} proxy transient: "
                        f"retries exhausted ({attempts} attempts)"
                    )
                if abandon.is_set():
                    if resp is not None:
                        try:
                            resp.close()
                        except Exception:  # noqa: BLE001
                            pass
                    return
                if attempt > 1 and resp is not None:
                    shim.log(f"upstream recovered on attempt {attempt}/{attempts}")
                upstream_q.put((resp, exc, body))
                return

        threading.Thread(target=open_upstream, name="arc-shim-upstream", daemon=True).start()
        return upstream_q

    def _relay_upstream_error(
        self, exc: Exception, shim: "ResponsesShimServer", body: bytes
    ) -> None:
        """快路径下上游报错：原样转发（错误体已由重试分类读取），让 codex 走自己的 HTTP 错误/重试分类。"""
        if isinstance(exc, urlerror.HTTPError):
            shim.log(f"upstream HTTP {exc.code}: {body[:300]!r}")
            self._send_body(
                exc.code, body, exc.headers.get("Content-Type") or "application/json"
            )
            return
        shim.log(f"upstream connect failed: {type(exc).__name__}: {exc}")
        self._send_json(
            502,
            {"error": {"message": f"shim upstream connect failed: {exc}", "type": "api_error"}},
        )

    def _begin_sse(
        self, custom_names: set[str], model: str
    ) -> "_ResponsesTranslator":
        response_id = "resp_" + uuid.uuid4().hex
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def emit(event: dict[str, Any]) -> None:
            data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            chunk = f"event: {event.get('type', 'message')}\ndata: {data}\n\n"
            try:
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
            except OSError as exc:  # 含 BrokenPipe/ConnectionReset
                raise _ClientGone() from exc

        translator = _ResponsesTranslator(response_id, custom_names, emit)
        translator.emit_created(model=model)
        return translator

    def _relay(
        self,
        upstream: Any,
        custom_names: set[str],
        shim: "ResponsesShimServer",
        model: str,
    ) -> None:
        try:
            translator = self._begin_sse(custom_names, model)
        except _ClientGone:
            return
        self._pump(upstream, translator, shim)

    def _relay_slow(
        self,
        upstream_q: "queue.Queue",
        abandon: threading.Event,
        custom_names: set[str],
        shim: "ResponsesShimServer",
        model: str,
    ) -> None:
        """响应头超过快等待窗口：先向 codex 开流，以保活事件等上游。"""
        try:
            translator = self._begin_sse(custom_names, model)
        except _ClientGone:
            abandon.set()
            return
        shim.log(
            f"slow wait: upstream headers >{int(_UPSTREAM_FAST_HEADER_WINDOW)}s, "
            "stream opened to codex with keepalives"
        )
        while True:
            try:
                upstream, exc, body = upstream_q.get(timeout=_KEEPALIVE_INTERVAL)
                break
            except queue.Empty:
                try:
                    translator.keepalive()
                except _ClientGone:
                    abandon.set()
                    return
        if exc is not None:
            if isinstance(exc, urlerror.HTTPError):
                shim.log(f"upstream HTTP {exc.code} after long wait: {body[:300]!r}")
                message = f"upstream HTTP {exc.code}: {body[:300]!r}"
            else:
                shim.log(f"upstream connect failed after long wait: {type(exc).__name__}: {exc}")
                message = f"upstream connect failed: {type(exc).__name__}: {exc}"
            try:
                translator.fail(message)
            except _ClientGone:
                pass
            return
        try:
            if upstream.status != 200:
                detail = upstream.read(_ERROR_BODY_CAP)
                shim.log(f"upstream status {upstream.status} after long wait: {detail[:300]!r}")
                try:
                    translator.fail(f"upstream status {upstream.status}: {detail[:300]!r}")
                except _ClientGone:
                    pass
                return
            self._pump(upstream, translator, shim)
        finally:
            upstream.close()

    def _pump(
        self, upstream: Any, translator: _ResponsesTranslator, shim: "ResponsesShimServer"
    ) -> None:
        pending: list[str] = []
        last_activity = time.monotonic()
        try:
            while True:
                try:
                    line = upstream.readline()
                except TimeoutError:
                    now = time.monotonic()
                    if now - last_activity > _UPSTREAM_SILENCE_CAP:
                        translator.fail(
                            f"upstream produced no data for {int(_UPSTREAM_SILENCE_CAP)}s"
                        )
                        break
                    translator.keepalive()
                    continue
                if not line:
                    if pending:
                        self._consume(translator, "\n".join(pending))
                        pending.clear()
                    break
                last_activity = time.monotonic()
                text = line.decode("utf-8", "replace").rstrip("\r\n")
                if not text:
                    if pending:
                        payload = "\n".join(pending)
                        pending.clear()
                        if payload.strip() == "[DONE]":
                            break
                        self._consume(translator, payload)
                    continue
                if text.startswith(":"):
                    continue
                if text.startswith("data:"):
                    pending.append(text[5:].lstrip(" "))
                    continue
                # event: / id: / retry: 行忽略
            translator.finish()
        except _ClientGone:
            return
        except Exception as exc:  # noqa: BLE001 - 上游半路断开
            try:
                translator.fail(f"upstream stream failed: {type(exc).__name__}: {exc}")
            except _ClientGone:
                return
            try:
                translator.finish()
            except _ClientGone:
                return
        shim.log(translator.summary())

    def _consume(self, translator: _ResponsesTranslator, payload: str) -> None:
        text = payload.strip()
        if not text or translator._done:  # noqa: SLF001 - 同一模块内的状态查询
            return
        try:
            chunk = json.loads(text)
        except Exception as exc:  # noqa: BLE001 - 坏分片丢弃，流继续
            return
        if not isinstance(chunk, dict):
            return
        error = chunk.get("error")
        if isinstance(error, dict):
            translator.fail("upstream error: " + json.dumps(error, ensure_ascii=False)[:400])
            return
        translator.on_chunk(chunk)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        self._send_body(
            status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json"
        )

    def _send_body(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(body)
        except OSError:
            pass


class ResponsesShimServer:
    """127.0.0.1 上的 Responses→chat 转换服务。"""

    def __init__(
        self,
        upstream_base_url: str,
        api_key: str,
        *,
        port: int = 0,
        bind_host: str = "127.0.0.1",
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.upstream_base = upstream_base_url.rstrip("/")
        self.api_key = api_key
        self.bind_host = bind_host
        self.log = log or _log_default
        self._httpd = ThreadingHTTPServer((bind_host, port), _ShimHandler)
        self._httpd.daemon_threads = True
        self._httpd.shim = self  # type: ignore[attr-defined]
        self.port = int(self._httpd.server_address[1])
        self._thread: Any = None

    @property
    def base_url(self) -> str:
        host = "127.0.0.1" if self.bind_host in ("127.0.0.1", "localhost") else self.bind_host
        return f"http://{host}:{self.port}/v1"

    @property
    def upstream_chat_url(self) -> str:
        return f"{self.upstream_base}/chat/completions"

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="arc-responses-shim", daemon=True
        )
        self._thread.start()
        self.log(f"listening on {self.base_url} -> {self.upstream_chat_url}")


def start_responses_shim_from_env(
    *,
    port: int = 0,
    bind_host: str | None = None,
    log: Callable[[str], None] | None = None,
) -> ResponsesShimServer | None:
    """按 OPENAI_BASE_URL/OPENAI_API_KEY 起转换层；缺配置时返回 None（保持旧行为）。

    bind_host 仅用于本机调试（容器外探测时需 0.0.0.0）；比赛容器内一律 127.0.0.1。
    """
    logger = log or _log_default
    upstream = os.environ.get("OPENAI_BASE_URL", "").strip()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not upstream or not api_key:
        logger("disabled: OPENAI_BASE_URL / OPENAI_API_KEY not set")
        return None
    if not upstream.rstrip("/").endswith("/v1") and "/v1/" not in upstream:
        logger(f"warning: OPENAI_BASE_URL does not look versioned: {upstream}")
    if bind_host is None:
        bind_host = os.environ.get("ARC_SHIM_BIND", "").strip() or "127.0.0.1"
    server = ResponsesShimServer(
        upstream, api_key, port=port, bind_host=bind_host, log=logger
    )
    server.start()
    return server
