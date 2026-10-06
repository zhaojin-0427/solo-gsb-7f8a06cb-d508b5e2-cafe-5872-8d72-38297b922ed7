#!/usr/bin/env python3
"""端到端冒烟测试：覆盖题目全部行为要求。

运行方式（需先安装依赖）：
    PYTHONPATH=/tmp/pylibs python3 tests/smoke_test.py
"""

from __future__ import annotations

import io
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = tempfile.mkdtemp(prefix="sitepub-test-")
PORT_A, PORT_B = 18081, 18082
BASE_A = f"http://127.0.0.1:{PORT_A}"
BASE_B = f"http://127.0.0.1:{PORT_B}"

passed = failed = 0


def check(name: str, cond: bool, extra: str = ""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


def make_zip(entries: list[tuple[str, bytes | None]]) -> bytes:
    """entries: (name, content)；content 为 None 表示显式目录条目。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries:
            if content is None:
                zf.writestr(zipfile.ZipInfo(name), b"")
            else:
                zf.writestr(name, content)
    return buf.getvalue()


def make_symlink_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("index.html", b"<h1>x</h1>")
        info = zipfile.ZipInfo("link.txt")
        info.external_attr = 0o120777 << 16  # S_IFLNK
        zf.writestr(info, b"/etc/passwd")
    return buf.getvalue()


def upload(base: str, data: bytes) -> httpx.Response:
    return httpx.post(
        f"{base}/api/versions",
        files={"file": ("site.zip", data, "application/zip")},
        timeout=30,
    )


def wait_ready(base: str, proc_name: str, timeout: float = 20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/healthz", timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"{proc_name} did not become ready")


def start_server(port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env["DATA_DIR"] = DATA_DIR
    env["CLEANUP_GRACE_SECONDS"] = "2"
    env["PYTHONPATH"] = env.get("PYTHONPATH", "") + os.pathsep + ROOT
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    wait_ready(f"http://127.0.0.1:{port}", f"port {port}")
    return proc


SITE1 = [
    ("index.html", b"<html><head><link rel=stylesheet href=css/style.css></head>"
                   b"<body><h1>v1</h1><script src=js/app.js></script></body></html>"),
    ("css/style.css", b"body { color: red; }"),
    ("js/app.js", b"console.log('v1');"),
    ("assets/", None),  # 显式目录条目
    ("assets/logo.txt", b"LOGO-V1"),
]
SITE2 = [
    ("index.html", b"<html><body><h1>v2</h1></body></html>"),
    ("css/style.css", b"body { color: blue; }"),
]


def main():
    # ---------------------------------------------------------- 1. 上传校验
    print("== 上传校验 ==")
    srv_a = start_server(PORT_A)
    srv_b = None
    try:
        r = httpx.get(f"{BASE_A}/api/site")
        check("初始修订号为 0、无当前版本",
              r.status_code == 200 and r.json() == {"version": None, "revision": 0})
        r = httpx.get(f"{BASE_A}/", follow_redirects=False)
        check("未激活时站点入口 404", r.status_code == 404)

        r = upload(BASE_A, make_zip(SITE1))
        check("合法 ZIP 上传 201", r.status_code == 201, r.text)
        v1 = r.json()["version"]
        check("版本号为 64 位十六进制摘要", len(v1) == 64 and all(c in "0123456789abcdef" for c in v1))

        # 相同内容（条目顺序不同）重复上传 -> 同一版本
        r = upload(BASE_A, make_zip(list(reversed(SITE1))))
        check("相同内容重复上传返回同一版本",
              r.status_code == 200 and r.json()["version"] == v1 and r.json()["created"] is False,
              r.text)

        bad_cases = {
            "空段 a//b.txt": [("index.html", b"x"), ("a//b.txt", b"x")],
            "点段 ./x.txt": [("index.html", b"x"), ("./x.txt", b"x")],
            "父目录 ../x.txt": [("index.html", b"x"), ("../x.txt", b"x")],
            "反斜杠 a\\b.txt": [("index.html", b"x"), ("a\\b.txt", b"x")],
            "绝对路径 /x.txt": [("index.html", b"x"), ("/x.txt", b"x")],
            "重复文件名": [("index.html", b"x"), ("dup.txt", b"1"), ("dup.txt", b"2")],
            "文件当目录用": [("index.html", b"x"), ("a", b"f"), ("a/b.txt", b"x")],
            "缺少 index.html": [("only.txt", b"x")],
            "空包": [],
        }
        for name, entries in bad_cases.items():
            r = upload(BASE_A, make_zip(entries))
            check(f"422: {name}", r.status_code == 422, f"got {r.status_code}")

        r = upload(BASE_A, make_symlink_zip())
        check("422: 符号链接", r.status_code == 422, f"got {r.status_code}")
        r = upload(BASE_A, make_zip([("index.html", b"x"), ("big.bin", b"\0" * (16 * 1024 * 1024 + 1))]))
        check("422: 解压总量超 16 MiB", r.status_code == 422, f"got {r.status_code}")
        r = upload(BASE_A, b"this is not a zip")
        check("422: 非 ZIP 文件", r.status_code == 422, f"got {r.status_code}")

        r = httpx.get(f"{BASE_A}/api/versions")
        check("失败上传不产生可见版本（仅 1 个版本）",
              r.status_code == 200 and len(r.json()["versions"]) == 1, r.text)

        # ------------------------------------------------------ 2. 激活流程
        print("== 激活与修订号 ==")
        r = httpx.put(f"{BASE_A}/api/site/active", json={"version": v1, "revision": 5})
        check("错误修订号激活 409", r.status_code == 409, r.text)
        check("409 响应携带当前修订号", r.json().get("current_revision") == 0, r.text)
        r = httpx.put(f"{BASE_A}/api/site/active", json={"version": "0" * 64, "revision": 0})
        check("激活不存在版本 404", r.status_code == 404, r.text)
        r = httpx.put(f"{BASE_A}/api/site/active", json={"version": v1, "revision": 0})
        check("正确修订号激活成功", r.status_code == 200 and r.json()["revision"] == 1, r.text)
        r = httpx.get(f"{BASE_A}/api/site")
        check("查询返回当前版本与修订号",
              r.json() == {"version": v1, "revision": 1}, r.text)

        # ------------------------------------------------------ 3. 站点访问
        print("== 站点访问 ==")
        r = httpx.get(f"{BASE_A}/", follow_redirects=False)
        check("入口 302 跳转到带版本号的 index.html",
              r.status_code == 302 and r.headers["location"] == f"/site/{v1}/index.html",
              f"{r.status_code} {r.headers.get('location')}")
        r = httpx.get(f"{BASE_A}/site/{v1}/index.html")
        check("index.html 内容与类型正确",
              r.status_code == 200 and b"<h1>v1</h1>" in r.content
              and r.headers["content-type"].startswith("text/html"))
        r = httpx.get(f"{BASE_A}/site/{v1}", follow_redirects=False)
        check("/site/{vid} 重定向到 index.html",
              r.status_code == 302 and r.headers["location"].endswith("/index.html"))
        r = httpx.get(f"{BASE_A}/site/{v1}/", follow_redirects=False)
        check("/site/{vid}/ 重定向到 index.html",
              r.status_code == 302 and r.headers["location"].endswith("/index.html"))
        r = httpx.get(f"{BASE_A}/site/{v1}/css/style.css")
        check("相对资源从该版本读取", r.status_code == 200 and b"color: red" in r.content)
        r = httpx.get(f"{BASE_A}/site/{v1}/assets/logo.txt")
        check("子目录资源可读", r.status_code == 200 and r.content == b"LOGO-V1")
        r = httpx.get(f"{BASE_A}/site/{v1}/nope.txt")
        check("清单外路径 404", r.status_code == 404)
        r = httpx.get(f"{BASE_A}/site/{v1}/..%2F..%2Fapp.db")
        check("路径穿越 404", r.status_code == 404)
        r = httpx.get(f"{BASE_A}/site/{'0' * 64}/index.html")
        check("不存在版本 404", r.status_code == 404)

        # 上传 v2 并切换
        r = upload(BASE_A, make_zip(SITE2))
        v2 = r.json()["version"]
        check("v2 上传成功", r.status_code == 201)
        r = httpx.put(f"{BASE_A}/api/site/active", json={"version": v2, "revision": 0})
        check("过期修订号切换 409", r.status_code == 409)
        r = httpx.put(f"{BASE_A}/api/site/active", json={"version": v2, "revision": 1})
        check("切换到 v2 成功", r.status_code == 200 and r.json()["revision"] == 2)
        r = httpx.get(f"{BASE_A}/", follow_redirects=False)
        check("入口跳转到 v2", r.headers.get("location") == f"/site/{v2}/index.html")
        r = httpx.get(f"{BASE_A}/site/{v1}/css/style.css")
        check("切换后旧版资源仍按旧版读取（不混用）",
              r.status_code == 200 and b"color: red" in r.content)
        r = httpx.get(f"{BASE_A}/site/{v2}/css/style.css")
        check("新版资源按新版读取", r.status_code == 200 and b"color: blue" in r.content)

        # 重新激活旧版
        r = httpx.put(f"{BASE_A}/api/site/active", json={"version": v1, "revision": 2})
        check("允许重新激活旧版", r.status_code == 200 and r.json()["revision"] == 3)
        r = httpx.get(f"{BASE_A}/", follow_redirects=False)
        check("入口重新指向 v1", r.headers.get("location") == f"/site/{v1}/index.html")

        # ------------------------------------------------------ 4. 回收
        print("== 回收 ==")
        r = httpx.request("DELETE", f"{BASE_A}/api/versions/{v1}")
        check("回收当前版本 409", r.status_code == 409, r.text)
        r = httpx.request("DELETE", f"{BASE_A}/api/versions/{'0' * 64}")
        check("回收不存在版本 404", r.status_code == 404)

        # 已打开的流式下载须完整结束
        big = os.urandom(8 * 1024 * 1024)
        r = upload(BASE_A, make_zip([("index.html", b"x"), ("big.bin", big)]))
        v3 = r.json()["version"]
        with httpx.stream("GET", f"{BASE_A}/site/{v3}/big.bin", timeout=30) as s:
            it = s.iter_bytes(65536)
            first = next(it)
            r = httpx.request("DELETE", f"{BASE_A}/api/versions/{v3}")
            check("回收非当前版本 202", r.status_code == 202, r.text)
            rest = b"".join(it)
        check("回收期间已打开的流式下载完整结束", first + rest == big,
              f"{len(first + rest)} != {len(big)}")

        r = httpx.get(f"{BASE_A}/site/{v3}/big.bin")
        check("回收开始后新读取 410", r.status_code == 410, f"got {r.status_code}")
        r = httpx.put(f"{BASE_A}/api/site/active", json={"version": v3, "revision": 3})
        check("回收开始后激活该版 409", r.status_code == 409, f"got {r.status_code}")
        r = httpx.get(f"{BASE_A}/api/versions/{v3}")
        check("回收后版本详情 410", r.status_code == 410, f"got {r.status_code}")
        deadline = time.time() + 10
        while time.time() < deadline:
            if not os.path.exists(os.path.join(DATA_DIR, "versions", v3)):
                break
            time.sleep(0.2)
        check("回收最终完成（文件已删除，410 语义保持）",
              not os.path.exists(os.path.join(DATA_DIR, "versions", v3))
              and httpx.get(f"{BASE_A}/api/versions/{v3}").status_code == 410)

        # 回收后同内容重新上传 -> 版本重建
        r = upload(BASE_A, make_zip([("index.html", b"x"), ("big.bin", big)]))
        check("回收后同内容可重新上传", r.status_code == 201 and r.json()["version"] == v3, r.text)
        r = httpx.get(f"{BASE_A}/site/{v3}/big.bin")
        check("重建后资源可再次读取", r.status_code == 200 and r.content == big)
        r = httpx.request("DELETE", f"{BASE_A}/api/versions/{v3}")
        check("再次回收 202", r.status_code == 202)

        # -------------------------------------------- 5. 双进程共用数据目录
        print("== 双进程共用数据目录 ==")
        srv_b = start_server(PORT_B)
        r = httpx.get(f"{BASE_B}/api/site")
        check("进程 B 看到相同当前版本与修订号",
              r.json() == {"version": v1, "revision": 3}, r.text)
        r = httpx.get(f"{BASE_B}/site/{v1}/js/app.js")
        check("进程 B 可读取 A 上传的资源", r.status_code == 200 and b"v1" in r.content)
        r = upload(BASE_B, make_zip(SITE2))
        check("进程 B 重复上传返回同一版本", r.status_code == 200 and r.json()["version"] == v2)
        r = httpx.put(f"{BASE_B}/api/site/active", json={"version": v2, "revision": 3})
        check("进程 B 激活成功（修订号 4）", r.status_code == 200 and r.json()["revision"] == 4)
        r = httpx.get(f"{BASE_A}/api/site")
        check("进程 A 看到 B 的激活结果", r.json() == {"version": v2, "revision": 4})
        r1 = httpx.put(f"{BASE_A}/api/site/active", json={"version": v1, "revision": 4})
        r2 = httpx.put(f"{BASE_B}/api/site/active", json={"version": v2, "revision": 4})
        codes = sorted([r1.status_code, r2.status_code])
        check("两进程并发同修订号激活：一个 200 一个 409", codes == [200, 409], f"{codes}")
        srv_b.terminate()
        srv_b.wait()
        srv_b = None

        # ------------------------------------ 6. 崩溃恢复（植入残留状态）
        print("== 崩溃恢复 ==")
        srv_a.terminate()
        srv_a.wait()
        db = sqlite3.connect(os.path.join(DATA_DIR, "app.db"))
        db.execute("INSERT INTO versions VALUES ('deadupload', 'uploading', 1, 1, ?)",
                   (time.time(),))
        db.execute("INSERT INTO versions VALUES ('deadgc', 'reclaiming', 1, 1, ?)",
                   (time.time(),))
        db.execute("INSERT INTO manifest VALUES ('deadgc', 'index.html', 1, 'x')")
        db.commit()
        db.close()
        os.makedirs(os.path.join(DATA_DIR, "versions", "deadupload"))
        os.makedirs(os.path.join(DATA_DIR, "versions", "deadgc"))
        open(os.path.join(DATA_DIR, "versions", "deadgc", "index.html"), "w").write("x")
        os.makedirs(os.path.join(DATA_DIR, "tmp", "junk-dir"))

        srv_a = start_server(PORT_A)
        r = httpx.get(f"{BASE_A}/api/versions/deadupload")
        check("中断的上传不留可见版本", r.status_code == 404)
        r = httpx.get(f"{BASE_A}/api/versions/deadgc")
        check("未完成的回收已继续完成（410）", r.status_code == 410, f"got {r.status_code}")
        check("回收残留目录已清除",
              not os.path.exists(os.path.join(DATA_DIR, "versions", "deadgc")))
        check("中断上传残留目录已清除",
              not os.path.exists(os.path.join(DATA_DIR, "versions", "deadupload")))
        r = httpx.get(f"{BASE_A}/api/site")
        check("重启后当前版本与修订号保持", r.json()["version"] in (v1, v2))
        cur = r.json()["version"]
        r = httpx.get(f"{BASE_A}/site/{cur}/index.html")
        check("重启后存活版本可正常读取", r.status_code == 200)
        r = httpx.get(f"{BASE_A}/", follow_redirects=False)
        check("重启后入口正常跳转", r.status_code == 302)

        # 上传与回收进行中进程退出（真实 kill -9）
        r = upload(BASE_A, make_zip([("index.html", b"killed"), ("f.txt", b"data")]))
        vk = r.json()["version"]
        db = sqlite3.connect(os.path.join(DATA_DIR, "app.db"))
        db.execute("UPDATE versions SET state = 'reclaiming' WHERE id = ?", (vk,))
        db.commit()
        db.close()
        srv_a.kill()  # kill -9，模拟回收中进程退出
        srv_a.wait()
        srv_a = start_server(PORT_A)
        deadline = time.time() + 10
        while time.time() < deadline:
            if not os.path.exists(os.path.join(DATA_DIR, "versions", vk)):
                break
            time.sleep(0.2)
        check("kill -9 后重启：未完成的回收继续完成",
              httpx.get(f"{BASE_A}/api/versions/{vk}").status_code == 410
              and not os.path.exists(os.path.join(DATA_DIR, "versions", vk)))

    finally:
        for p in (srv_a, srv_b):
            if p and p.poll() is None:
                p.terminate()
                p.wait()

    print(f"\n{passed} passed, {failed} failed")
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
