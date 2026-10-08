"""Verify a running NyaView.exe page via CDP — asserts the bridge works.

This is the definitive test for the 'window freezes / preview-mode' bug:
the pywebview bridge must be injected and boot() must have run.
"""
import json
import sys
import time
import urllib.request

import websocket

DEBUG_PORT = 9333


def get_page_ws() -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json", timeout=5) as resp:
        tabs = json.loads(resp.read())
    page = next((t for t in tabs if t.get("type") == "page"), None)
    if not page:
        raise RuntimeError("no page target — app not running or page not loaded")
    return page["webSocketDebuggerUrl"]


def main() -> int:
    ws = websocket.create_connection(get_page_ws(), timeout=10)
    msg_id = 0

    def send(method: str, params: dict | None = None) -> dict:
        nonlocal msg_id
        msg_id += 1
        ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            resp = json.loads(ws.recv())
            if resp.get("id") == msg_id:
                return resp

    def ev(expr: str):
        r = send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        res = r.get("result", {}).get("result", {})
        return res.get("value", res.get("description", "<none>"))

    time.sleep(1)
    results = {
        "cards": ev("document.querySelectorAll('.card').length"),
        "tabs": ev("document.querySelectorAll('.tab').length"),
        "brand": ev("(document.querySelector('.brand-name')||{}).textContent || null"),
        "urlInput": ev("!!document.getElementById('downloadUrl')"),
        "api_poll": ev("typeof (window.pywebview && window.pywebview.api && window.pywebview.api.poll)"),
        "api_start_download": ev("typeof (window.pywebview && window.pywebview.api && window.pywebview.api.start_download)"),
        "runtimeText": ev("(document.getElementById('runtimeText')||{}).textContent || null"),
        "logText": ev("(document.getElementById('logView')||{}).textContent || '<none>'"),
    }
    for k, v in results.items():
        print(f"[cdp] {k}: {v}")
    ws.close()

    ok = (
        results["cards"] == 6
        and results["tabs"] == 5
        and results["urlInput"] is True
        and results["api_poll"] == "function"
        and results["api_start_download"] == "function"
        and "浏览器预览模式" not in str(results["runtimeText"])
    )
    print("VERDICT:", "PASS — bridge injected, boot ran, UI live" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
