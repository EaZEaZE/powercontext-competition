"""接口冒烟验证：依次打一遍后端接口，任一步失败就 exit(1)。

用法：
    python verify.py                                    # 默认 http://127.0.0.1:8000
    BASE_URL=http://127.0.0.1:9000 python verify.py      # 指定地址
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000")

# 每句都超过后端的最小切块长度（20 字），保证能被索引
IMPORT_TEXT = (
    "机器学习是人工智能的一个重要分支，它让计算机能够从数据中自动总结规律并做出预测。"
    "在自然语言处理领域，机器学习被广泛应用于机器翻译、文本分类、情感分析和信息抽取。"
    "计算机视觉同样依赖机器学习，典型场景包括医学影像诊断、人脸识别和自动驾驶中的目标检测。"
    "推荐系统利用机器学习分析用户的历史行为，从而预测用户可能感兴趣的商品、视频或文章。"
    "此外，机器学习还应用于金融风控、智能语音助手、搜索引擎排序以及科学研究中的数据分析。"
)


def call(method, path, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{BASE_URL}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def step(method, path, payload=None):
    print(f"\n--> {method} {BASE_URL}{path}")
    status, body = call(method, path, payload)
    print(f"<-- 状态码 {status}")
    try:
        print(json.dumps(json.loads(body), ensure_ascii=False, indent=2))
    except ValueError:
        print(body)
    if not 200 <= status < 300:
        print(f"FAILED: {method} {path} 返回 {status}")
        sys.exit(1)


def main():
    print(f"目标服务: {BASE_URL}")
    step("GET", "/api/stats")
    step("POST", "/api/import", {"text": IMPORT_TEXT, "title": "机器学习应用概览"})
    step("POST", "/api/query", {"question": "机器学习有哪些应用", "user_id": "default"})
    step("GET", "/api/history/default")
    step("GET", "/api/stats")
    print("\n全部接口调用成功")


if __name__ == "__main__":
    main()
