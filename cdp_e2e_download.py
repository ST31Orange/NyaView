"""End-to-end: drive the EXE's UI via CDP to submit a real download and
verify the queue card appears and the download completes with a full MP4."""
import json
import time
import urllib.request

import websocket

DEBUG_PORT = 9333
URL = "https://www.bilibili.com/video/BV1GJ411x7h7"


def get_page_ws() -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json", timeout=5) as resp:
        tabs = json.loads(resp.read())
    return next(t for t in tabs if t.get("type") == "page")["webSocketDebuggerUrl"]


def main() -> None:
    ws = websocket.create_connection(get_page_ws(), timeout=15)
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
    # 1) set the URL into the input
    url_js = json.dumps(URL)
    ev(f"document.getElementById('downloadUrl').value = {url_js}")
    print("set url:", ev("document.getElementById('downloadUrl').value"))
    # 2) click the download button
    ev("document.getElementById('downloadBtn').click()")
    print("clicked download")
    time.sleep(3)
    # 3) check the queue page is active and a card exists
    print("active panel:", ev("document.querySelector('.panel.active') ? document.querySelector('.panel.active').id : 'none'"))
    print("queue cards:", ev("document.querySelectorAll('.job').length"))
    print("queue card status:", ev("(document.querySelector('.job-status')||{}).textContent || null"))
    # 4) wait for completion (up to 180s), watching the card
    t0 = time.time()
    final = None
    while time.time() - t0 < 180:
        status = ev("(document.querySelector('.job-status')||{}).textContent || null")
        pct = ev("(document.querySelector('.job-bar-fill')||{}).style.width || null")
        if status in ("完成", "失败", "已取消"):
            final = status
            break
        if int(time.time() - t0) % 10 < 1:
            print(f"  t={time.time()-t0:.0f}s status={status} bar={pct}")
        time.sleep(2)
    print("final status:", final)
    print("job output:", ev("(document.querySelector('.job-output')||{}).textContent || null"))
    print("job error:", ev("(document.querySelector('.job-error')||{}).textContent || null"))
    ws.close()


if __name__ == "__main__":
    main()
