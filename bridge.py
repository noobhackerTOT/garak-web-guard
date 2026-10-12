"""Native garak function.Single generator, talking to the browser actor."""
import json
import os
import urllib.request
import urllib.error


def generate(prompt: str, **kwargs):
    request = urllib.request.Request(
        os.environ["WG_BRIDGE_URL"],
        data=json.dumps({"prompt": prompt}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Bridge-Token": os.environ["WG_BRIDGE_TOKEN"]},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=int(os.environ.get("WG_BRIDGE_TIMEOUT", "660"))) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read()).get("error", str(exc))
        except Exception:
            message = str(exc)
        raise RuntimeError("网页连接失败：" + message) from None
    if not isinstance(result.get("response"), str) or not result["response"].strip():
        raise RuntimeError("网页没有返回有效回答")
    return [result["response"]]
