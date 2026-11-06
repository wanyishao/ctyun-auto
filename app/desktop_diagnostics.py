"""Pure helpers for conservative cloud desktop disconnect classification."""

from __future__ import annotations

import base64
import binascii
import os
import re
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlsplit, urlunsplit


PREEMPTED = "PREEMPTED"
NETWORK_RETRYABLE = "NETWORK_RETRYABLE"
UNKNOWN = "UNKNOWN"
CONNECTED = "CONNECTED"

NETWORK_RECONNECT_DELAYS_SECONDS = (10, 30, 60)
INITIAL_RECONNECT_MARKERS = (
    "正在为您重连",
    "正在重连",
    "连接已断开",
    "连接中断",
)

# These identify an explicit account/session conflict. Generic "disconnected"
# or WebSocket-close wording is deliberately not treated as preemption.
PREEMPTION_MARKERS = (
    "已在其他设备登录",
    "已在其它设备登录",
    "已在其他终端登录",
    "已在其它终端登录",
    "已在另一台设备登录",
    "该账号已在其他设备登录",
    "您的账号已在其他设备登录",
    "当前账号在其他设备登录",
    "当前账号已在其他设备登录",
    "账号已在其他设备登录",
    "账号已在其它设备登录",
    "账号已在其他终端登录",
    "账号在别处登录",
    "账号被其他设备占用",
    "已被其他客户端登录",
    "被其他客户端挤占",
    "其他客户端正在使用",
    "云电脑已被其他用户连接",
    "云电脑已被其他设备占用",
    "账号已被挤下线",
    "客户端已占用",
    "logged in on another device",
    "session replaced by another client",
    "session is occupied by another client",
)

# The host and port must come from a CDP WebSocket event on the current
# desktop-entry attempt. A similarly worded DLL log is not sufficient.
DESKTOP_WEBSOCKET_HOST = "desk.ctyun.cn"
DESKTOP_WEBSOCKET_PORT = 8810

RETRYABLE_NETWORK_MARKERS = (
    "err_name_not_resolved",
    "err_name_resolution_failed",
    "err_dns_timed_out",
    "err_connection_failed",
    "err_connection_refused",
    "err_connection_timed_out",
    "err_timed_out",
    "err_address_unreachable",
    "err_network_changed",
    "err_socket_not_connected",
    "err_socket_is_not_connected",
    "err_socket_timed_out",
    "socket is not connected",
    "socket error",
    "socket failure",
    "resource temporarily unavailable",
    "dns lookup failed",
    "name or service not known",
    "connection timed out",
    "connection timeout",
)


def sanitize_url(url: str) -> str:
    """Keep useful URL context while removing credentials and secret values."""
    def redact_fragment(fragment: str) -> str:
        # Fragments may contain hash-router paths followed by query parameters
        # (for example #/desktop?access_token=...). Keep delimiters and route
        # text intact while redacting sensitive key/value pairs.
        sensitive_markers = ("token", "secret", "auth", "sign", "password", "cookie", "key")
        pattern = re.compile(r"(^|[?&;])([^?&;=]+)=([^?&;]*)")

        def redact(match):
            key = unquote_plus(match.group(2)).lower()
            value = match.group(3)
            if any(marker in key for marker in sensitive_markers):
                value = "[redacted]"
            return f"{match.group(1)}{match.group(2)}={value}"

        return pattern.sub(redact, fragment)

    try:
        parts = urlsplit(str(url or ""))
        hostname = parts.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = hostname
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        safe_query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if any(
                marker in key.lower()
                for marker in ("token", "secret", "auth", "sign", "password", "cookie", "key")
            ):
                value = "[redacted]"
            safe_query.append((key, value))
        return urlunsplit(
            (
                parts.scheme,
                netloc,
                parts.path,
                urlencode(safe_query),
                redact_fragment(parts.fragment),
            )
        )
    except (TypeError, ValueError):
        return "<unavailable>"


def is_desktop_websocket_url(url: str) -> bool:
    """Match only the known desktop tunnel endpoint, never points/API traffic."""
    try:
        parts = urlsplit(str(url or ""))
        return (
            parts.scheme.lower() in {"ws", "wss"}
            and (parts.hostname or "").lower() == DESKTOP_WEBSOCKET_HOST
            and parts.port == DESKTOP_WEBSOCKET_PORT
        )
    except (TypeError, ValueError):
        return False


def is_retryable_network_error(error: str) -> bool:
    """Recognize concrete DNS/TCP/socket/timeout failures, not generic closes."""
    normalized = str(error or "").lower()
    return any(marker in normalized for marker in RETRYABLE_NETWORK_MARKERS)


def page_reports_reconnecting(page_text: str) -> bool:
    """Recognize the website's in-progress reconnect state during initial entry."""
    text = str(page_text or "")
    return any(marker in text for marker in INITIAL_RECONNECT_MARKERS)


def classify_desktop_disconnect(
    *, page_text: str, canvas_present: bool, network_events: list[dict]
) -> str:
    """Classify a lost desktop conservatively; conflict text always wins."""
    text = str(page_text or "")
    event_text = " ".join(
        str(event.get(key) or "")
        for event in network_events
        if event.get("desktop_connection")
        for key in ("error", "close_reason", "status_text")
    )
    preemption_evidence = f"{text} {event_text}".lower()
    if any(marker.lower() in preemption_evidence for marker in PREEMPTION_MARKERS):
        return PREEMPTED

    if canvas_present:
        return CONNECTED

    for event in network_events:
        if (
            event.get("desktop_connection")
            and event.get("event") in {"Network.loadingFailed", "Network.webSocketFrameError"}
            and is_retryable_network_error(event.get("error", ""))
        ):
            return NETWORK_RETRYABLE

    # This includes a missing canvas with only a generic WebSocket close.
    return UNKNOWN


def next_network_reconnect_delay(reconnects_already_used: int, classification: str) -> int | None:
    """Return the next bounded delay only for confirmed desktop network faults."""
    if classification != NETWORK_RETRYABLE:
        return None
    if reconnects_already_used < 0 or reconnects_already_used >= len(
        NETWORK_RECONNECT_DELAYS_SECONDS
    ):
        return None
    return NETWORK_RECONNECT_DELAYS_SECONDS[reconnects_already_used]


def confirm_canvas_loss(
    classification: str, consecutive_misses: int, confirmations: int = 3
) -> tuple[bool, int]:
    """Require repeated ordinary canvas loss; honor explicit evidence at once."""
    if classification == CONNECTED:
        return False, 0
    if classification in {PREEMPTED, NETWORK_RETRYABLE}:
        return True, consecutive_misses
    misses = consecutive_misses + 1
    return misses >= max(1, confirmations), misses


def parse_websocket_close_frame(opcode: int, payload_data: str) -> dict | None:
    """Decode close code/reason when CDP exposes an opcode-8 frame payload."""
    if opcode != 8 or not payload_data:
        return None
    try:
        payload = base64.b64decode(payload_data, validate=True)
    except (ValueError, binascii.Error):
        return None
    if len(payload) < 2:
        return None
    close_code = int.from_bytes(payload[:2], byteorder="big")
    try:
        reason = payload[2:].decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        reason = payload[2:].decode("utf-8", errors="replace")
    return {"close_code": close_code, "close_reason": reason[:300]}


class DesktopCdpMonitor:
    """Bounded, read-only recorder for control and abnormal WebSocket events."""

    EVENT_METHODS = (
        "Network.webSocketCreated",
        "Network.webSocketWillSendHandshakeRequest",
        "Network.webSocketHandshakeResponseReceived",
        "Network.webSocketClosed",
        "Network.webSocketFrameError",
        "Network.webSocketFrameReceived",
        "Network.loadingFailed",
    )

    def __init__(self, max_events: int | None = None, max_sockets: int = 64):
        if max_events is None:
            try:
                max_events = int(os.getenv("PC_CDP_EVENT_MAX", "512"))
            except ValueError:
                max_events = 512
        self._max_events = min(max(1, max_events), 4096)
        self._max_sockets = min(max(1, max_sockets), 512)
        self._lock = threading.Lock()
        self._active = False
        self._attempt_started = 0.0
        self._canvas_connected_at: float | None = None
        self._socket_urls: OrderedDict[str, str] = OrderedDict()
        self._events: deque[dict] = deque(maxlen=self._max_events)

    def begin_desktop_attempt(self) -> None:
        with self._lock:
            self._active = True
            self._attempt_started = time.monotonic()
            self._canvas_connected_at = None
            self._socket_urls.clear()
            self._events.clear()

    def mark_canvas_connected(self) -> None:
        with self._lock:
            self._canvas_connected_at = time.monotonic()

    def _remember_socket_url(self, request_id: str, url: str) -> None:
        if not request_id or not url:
            return
        self._socket_urls[request_id] = url
        self._socket_urls.move_to_end(request_id)
        while len(self._socket_urls) > self._max_sockets:
            self._socket_urls.popitem(last=False)

    def record(self, method: str, **params) -> None:
        """Store event metadata only; skip ordinary data frames and payloads."""
        now_mono = time.monotonic()
        request_id = str(params.get("requestId") or "")
        with self._lock:
            if not self._active or now_mono < self._attempt_started:
                return

            if method == "Network.webSocketCreated":
                created_url = str(params.get("url") or "")
                if not is_desktop_websocket_url(created_url):
                    return
                self._remember_socket_url(request_id, created_url)

            url = self._socket_urls.get(request_id, "")
            if not url or not is_desktop_websocket_url(url):
                return
            event = {
                "time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "_monotonic": now_mono,
                "event": method,
                "request_id": request_id or None,
                "url": sanitize_url(url) if url else "",
                "desktop_connection": True,
            }

            if method == "Network.loadingFailed":
                # API, unrelated sockets, and iframe HTTP failures are omitted.
                if str(params.get("type") or "").lower() != "websocket":
                    return
                event["error"] = str(params.get("errorText") or "")[:500]
                event["canceled"] = bool(params.get("canceled"))
            elif method == "Network.webSocketFrameError":
                event["error"] = str(params.get("errorMessage") or "")[:500]
            elif method == "Network.webSocketFrameReceived":
                frame = params.get("response") or {}
                opcode = frame.get("opcode")
                # Routine text/binary frames can be frequent and carry task data.
                if opcode != 8:
                    return
                event["opcode"] = opcode
                close_data = parse_websocket_close_frame(
                    int(opcode), str(frame.get("payloadData") or "")
                )
                if close_data:
                    event.update(close_data)
            elif method == "Network.webSocketHandshakeResponseReceived":
                response = params.get("response") or {}
                event["status"] = response.get("status")
                event["status_text"] = str(response.get("statusText") or "")[:300]
            elif method == "Network.webSocketClosed":
                event["note"] = "CDP webSocketClosed contains no code or reason"
            elif method in {
                "Network.webSocketWillSendHandshakeRequest",
                "Network.webSocketCreated",
            }:
                pass
            else:
                return

            self._events.append(event)

    def events_for_classification(self, since_canvas_connected: bool = False) -> list[dict]:
        with self._lock:
            connected_at = self._canvas_connected_at if since_canvas_connected else None
            return [
                {key: value for key, value in event.items() if not key.startswith("_")}
                for event in self._events
                if connected_at is None or event["_monotonic"] >= connected_at
            ]

    def snapshot(self) -> list[dict]:
        return self.events_for_classification()
