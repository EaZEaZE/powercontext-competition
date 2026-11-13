"""五步流程编排：检索资料 → 构造上下文 → MiniMax 改码 → 测试 → 产出证据。"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from typing import List, Tuple

from ingest import COLLECTION  # noqa: F401
from minimax_client import chat
from retrieve import build_context

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT = os.path.join(BASE, "business_project")
EVIDENCE = os.path.join(BASE, "evidence")
WORKSPACE = os.environ.get("WORKSPACE", "/root/run/workspace")

SERVICE_REL = "src/inventory/service.py"
TEST_REL = "tests/test_ttl.py"

CHANGE_REQUEST = (
    "按 specs/spec-v2.md 的 CR-2026-1111-TTL，为库存预留增加可选的 ttl_seconds 过期能力。"
    "要求保持 spec-v1 的全部既有行为不变。"
)

SYSTEM_PROMPT = (
    "你是资深 Python 工程师，负责在既有仓库中做小范围、可回归的改造。"
    "必须严格遵守给定的规格文档与工程规范，不得修改既有测试的断言，不得引入第三方依赖或后台线程。"
    "只输出代码，不要解释。"
)


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def extract_code(text: str) -> str:
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    if not blocks:
        return text.strip()
    return max(blocks, key=len).strip() + "\n"


def run(cmd: List[str], cwd: str) -> Tuple[int, str]:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def pytest_run(cwd: str, target: str | None) -> str:
    cmd = [sys.executable, "-m", "pytest", "-v"] + ([target] if target else [])
    rc, out = run(cmd, cwd)
    return "exit=%d\n%s" % (rc, out)


def make_patch(left: str, right: str, cwd: str) -> str:
    """等价 `diff -ruN left right`；无 diff 可执行文件时用 difflib 兜底（Windows 兼容）。"""
    if shutil.which("diff"):
        return run(["diff", "-ruN", "-x", "__pycache__", "-x", ".pytest_cache",
                    "-x", "*.pyc", left, right], cwd)[1]
    lines: List[str] = []
    for base, _dirs, files in os.walk(left):
        for name in sorted(files):
            lp = os.path.join(base, name)
            rel = os.path.relpath(lp, left)
            if "__pycache__" in rel or ".pytest_cache" in rel:
                continue
            rp = os.path.join(right, rel)
            lrel = os.path.join(os.path.basename(left), rel).replace("\\", "/")
            rrel = os.path.join(os.path.basename(right), rel).replace("\\", "/")
            ltxt = open(lp, "r", encoding="utf-8", errors="replace").read().splitlines(True)
            if os.path.exists(rp):
                rtxt = open(rp, "r", encoding="utf-8", errors="replace").read().splitlines(True)
            else:
                rtxt = []
                lines.append("Only in %s: %s\n" % (os.path.basename(left), rel))
            if ltxt != rtxt:
                lines.extend(difflib.unified_diff(ltxt, rtxt, fromfile=lrel, tofile=rrel))
    return "".join(l if l.endswith("\n") else l + "\n" for l in lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ingest", action="store_true", help="先重建 seekdb 索引")
    ap.add_argument("--skip-minimax", action="store_true")
    ap.add_argument("--k", type=int, default=6)
    args = ap.parse_args()

    ensure_dir(EVIDENCE)
    log_jsonl = os.path.join(EVIDENCE, "minimax-calls.jsonl")

    # ---------- 步骤 1：导入资料 ----------
    if args.ingest:
        rc, out = run([sys.executable, os.path.join(BASE, "tools", "ingest.py"),
                       "--root", BASE, "--reset"], BASE)
        print(out)
        if rc != 0:
            return rc

    # ---------- 步骤 2：检索并构造上下文 ----------
    context = build_context(CHANGE_REQUEST, args.k)
    with open(os.path.join(EVIDENCE, "01-context.md"), "w", encoding="utf-8") as f:
        f.write(context + "\n")
    print("[step2] 上下文已生成，长度 %d" % len(context))

    with open(os.path.join(PROJECT, SERVICE_REL), "r", encoding="utf-8") as f:
        service_src = f.read()

    # ---------- 步骤 3：MiniMax 改码 ----------
    prompt1 = "\n\n".join([
        "## 检索到的工程资料（唯一可信依据）",
        context,
        "## 当前实现（%s）" % SERVICE_REL,
        "```python\n" + service_src + "\n```",
        "## 本次要求",
        CHANGE_REQUEST,
        "## 输出要求",
        "输出 `%s` 的完整新版本，放在一个 ```python 代码块中。" % SERVICE_REL,
        "不要输出其他文件，不要输出解释。",
    ])
    with open(os.path.join(EVIDENCE, "02-prompt-service.md"), "w", encoding="utf-8") as f:
        f.write(prompt1 + "\n")

    if args.skip_minimax:
        print("[step3] 跳过 MiniMax（--skip-minimax）")
        return 0

    reply1 = chat(prompt1, system=SYSTEM_PROMPT, log_path=log_jsonl)
    with open(os.path.join(EVIDENCE, "03-response-service.md"), "w", encoding="utf-8") as f:
        f.write(reply1 + "\n")
    new_service = extract_code(reply1)
    print("[step3] service.py 已生成，%d 字符" % len(new_service))

    # ---------- 步骤 3b：MiniMax 补功能测试 ----------
    prompt2 = "\n\n".join([
        "## 检索到的工程资料（唯一可信依据）",
        context,
        "## 变更后的实现（%s）" % SERVICE_REL,
        "```python\n" + new_service + "\n```",
        "## 本次要求",
        "为 CR-2026-1111-TTL 编写功能测试，覆盖：TTL 内可 commit、过期后 get_available 恢复、"
        "过期后 release/commit 返回 False、ttl_seconds<=0 抛 InventoryError、ttl_seconds=None 不会过期。",
        "禁止使用 time.sleep；通过注入或构造时间实现确定性测试。",
        "## 输出要求",
        "输出 `%s` 的完整内容，放在一个 ```python 代码块中。" % TEST_REL,
    ])
    with open(os.path.join(EVIDENCE, "02-prompt-tests.md"), "w", encoding="utf-8") as f:
        f.write(prompt2 + "\n")
    reply2 = chat(prompt2, system=SYSTEM_PROMPT, log_path=log_jsonl)
    with open(os.path.join(EVIDENCE, "03-response-tests.md"), "w", encoding="utf-8") as f:
        f.write(reply2 + "\n")
    new_tests = extract_code(reply2)
    print("[step3] test_ttl.py 已生成，%d 字符" % len(new_tests))

    # ---------- 步骤 4：跑测试 ----------
    if os.path.isdir(WORKSPACE):
        shutil.rmtree(WORKSPACE)
    shutil.copytree(PROJECT, WORKSPACE,
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))

    baseline = pytest_run(WORKSPACE, None)
    with open(os.path.join(EVIDENCE, "05-pytest-baseline.txt"), "w", encoding="utf-8") as f:
        f.write(baseline + "\n")
    print("[step4] 基线测试完成")

    with open(os.path.join(WORKSPACE, SERVICE_REL), "w", encoding="utf-8") as f:
        f.write(new_service)
    with open(os.path.join(WORKSPACE, TEST_REL), "w", encoding="utf-8") as f:
        f.write(new_tests)

    patch = make_patch(PROJECT, WORKSPACE, BASE)
    with open(os.path.join(EVIDENCE, "04-code-diff.patch"), "w", encoding="utf-8") as f:
        f.write(patch + "\n")

    after = pytest_run(WORKSPACE, None)
    repair_rounds = 0
    while "exit=0" not in after.splitlines()[0] and repair_rounds < 3:
        repair_rounds += 1
        print("[step4] 测试未通过，第 %d 轮修复" % repair_rounds)
        prompt_fix = "\n\n".join([
            "## 检索到的工程资料（唯一可信依据）",
            context,
            "## 当前测试文件（%s）" % TEST_REL,
            "```python\n" + new_tests + "\n```",
            "## pytest 实际输出",
            "```\n" + after[-3000:] + "\n```",
            "## 修复要求",
            "只修复该测试文件，使其符合 specs/spec-v2.md 与 knowledge/testing-policy.md；"
            "测试通过 `from inventory import ...` 导入被测代码，不要改动被测实现，不要修改既有用例断言。",
            "## 输出要求",
            "输出 `%s` 的完整新版本，放在一个 ```python 代码块中。" % TEST_REL,
        ])
        reply_fix = chat(prompt_fix, system=SYSTEM_PROMPT, log_path=log_jsonl)
        with open(os.path.join(EVIDENCE, "09-repair-%d.md" % repair_rounds), "w", encoding="utf-8") as f:
            f.write(reply_fix + "\n")
        new_tests = extract_code(reply_fix)
        with open(os.path.join(WORKSPACE, TEST_REL), "w", encoding="utf-8") as f:
            f.write(new_tests)
        after = pytest_run(WORKSPACE, None)
        patch = make_patch(PROJECT, WORKSPACE, BASE)
        with open(os.path.join(EVIDENCE, "04-code-diff.patch"), "w", encoding="utf-8") as f:
            f.write(patch + "\n")

    with open(os.path.join(EVIDENCE, "06-pytest-after.txt"), "w", encoding="utf-8") as f:
        f.write(after + "\n")
    regression = pytest_run(WORKSPACE, "tests/test_service.py")
    feature = pytest_run(WORKSPACE, "tests/test_ttl.py")
    with open(os.path.join(EVIDENCE, "07-pytest-split.txt"), "w", encoding="utf-8") as f:
        f.write("===== 回归测试 tests/test_service.py =====\n" + regression)
        f.write("\n===== 功能测试 tests/test_ttl.py =====\n" + feature)
    print("[step4] 变更后测试完成")

    summary = {
        "generated_files": [SERVICE_REL, TEST_REL],
        "after_exit": after.splitlines()[0],
        "repair_rounds": repair_rounds,
        "regression_ok": "failed" not in regression.lower() and "exit=0" in regression.splitlines()[0],
        "feature_ok": "failed" not in feature.lower() and "exit=0" in feature.splitlines()[0],
    }
    with open(os.path.join(EVIDENCE, "08-summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
