"""跨进程集成测试：两个 uvicorn 进程共用同一数据目录。

覆盖：
- 进程 A 上传/激活，进程 B 立即可见并可服务；
- 回收开始后另一进程的在途流式下载必须完整结束，新请求得到 410；
- 回收中途杀死进程，重启第二个进程完成未结束的回收。

默认运行；如需在资源受限环境跳过，设置 SKIP_MULTIPROC=1。
"""

from __future__ import annotations

import io
import os
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("SKIP_MULTIPROC") == "1", reason="SKIP_MULTIPROC=1"
)

_EXTRA = "/workspace/.pylib/lib/python3.11/site-packages"
ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, data_dir: str, port: int, env_extra: dict | None = None):
        self.port = port
        env = os.environ.copy()
        env["DATA_DIR"] = data_dir
        env["PYTHONPATH"] = str(ROOT) + (
            os.pathsep + _EXTRA if os.path.isdir(_EXTRA) else ""
        )
        if env_extra:
            env.update(env_extra)
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def wait_ready(self, timeout: float = 15.0) -> None:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            if self.proc.poll() is not None:
                out = self.proc.stdout.read().decode() if self.proc.stdout else ""
                raise AssertionError(f"server exited early:\n{out}")
            try:
                if httpx.get(f"{self.base}/healthz", timeout=1).status_code == 200:
                    return
            except Exception as exc:  # noqa: BLE001
                last = exc
            time.sleep(0.2)
        raise AssertionError(f"server not ready: {last}")

    def stop(self) -> str:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
                return ""
            except subprocess.TimeoutExpired:
                self.proc.kill()
        out, _ = self.proc.communicate()
        return out.decode() if out else ""


@pytest.fixture()
def servers(tmp_path):
    data_dir = str(tmp_path / "shared-data")
    os.makedirs(data_dir)
    a = Server(data_dir, _free_port())
    b = Server(data_dir, _free_port())
    try:
        a.wait_ready()
        b.wait_ready()
        yield a, b
    finally:
        a.stop()
        b.stop()


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _upload(base: str, files: dict[str, bytes]) -> str:
    r = httpx.post(
        f"{base}/versions",
        files={"file": ("s.zip", _zip_bytes(files), "application/zip")},
        timeout=30,
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["version"]


def test_cross_process_visibility_and_serving(servers):
    a, b = servers
    v = _upload(a.base, {"index.html": b"A-uploaded", "a.js": b"js-a"})
    # B 进程看到同一份元数据与文件
    assert httpx.get(f"{b.base}/versions/{v}", timeout=5).status_code == 200
    r = httpx.post(f"{b.base}/versions/{v}/activate", json={"revision": 0})
    assert r.status_code == 200, r.text
    # A 进程立即看到 B 的激活结果
    cur = httpx.get(f"{a.base}/current", timeout=5).json()
    assert cur == {"version": v, "revision": 1}
    # 两边都能按版本清单服务
    assert httpx.get(f"{a.base}/v/{v}/a.js", timeout=5).content == b"js-a"
    assert httpx.get(f"{b.base}/v/{v}/index.html", timeout=5).content == b"A-uploaded"
    # 修订号乐观锁在跨进程下成立
    conflict = httpx.post(
        f"{a.base}/versions/{v}/activate", json={"revision": 0}, timeout=5
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["revision"] == 1


def test_inflight_stream_completes_after_recycle_across_processes(tmp_path):
    """进程 B 打开慢速流式下载期间，进程 A 发起回收：
    下载必须完整结束；回收窗口内的新请求返回 410。"""
    data_dir = str(tmp_path / "shared-data-2")
    os.makedirs(data_dir)
    # 发起回收的 A 使用删除延迟制造 recycling 窗口
    a = Server(data_dir, _free_port(), {"RECYCLE_DELETE_DELAY": "8"})
    b = Server(data_dir, _free_port())
    try:
        a.wait_ready()
        b.wait_ready()
        payload = b"0123456789" * (512 * 1024)  # 5 MiB
        v_old = _upload(a.base, {"index.html": b"old", "big.bin": payload})
        v_new = _upload(a.base, {"index.html": b"new"})
        assert httpx.post(
            f"{a.base}/versions/{v_old}/activate", json={"revision": 0}, timeout=5
        ).status_code == 200
        assert httpx.post(
            f"{a.base}/versions/{v_new}/activate", json={"revision": 1}, timeout=5
        ).status_code == 200

        # 用原始 socket 建立在途下载，只读一小段，保证响应仍在进行
        sock = socket.create_connection(("127.0.0.1", b.port), timeout=15)
        sock.sendall(
            f"GET /v/{v_old}/big.bin HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode()
        )
        got = bytearray()

        def read_some(more: int) -> None:
            while len(got) < more:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                got.extend(chunk)

        read_some(65536)  # 读到响应头 + 首块即停
        assert b"200 OK" in got.split(b"\r\n", 1)[0]

        # A 后台发起回收（请求阻塞到删除完成）
        import threading

        recycle_result = {}

        def fire_recycle():
            recycle_result["status"] = httpx.post(
                f"{a.base}/versions/{v_old}/recycle", timeout=30
            ).status_code

        rt = threading.Thread(target=fire_recycle, daemon=True)
        rt.start()

        # 回收已开始：新读取 410、激活 409（等状态可见）
        probe = None
        deadline = time.time() + 12
        while time.time() < deadline:
            probe = httpx.get(f"{b.base}/v/{v_old}/index.html", timeout=5)
            if probe.status_code == 410:
                break
            time.sleep(0.1)
        assert probe is not None and probe.status_code == 410
        act = httpx.post(
            f"{b.base}/versions/{v_old}/activate",
            json={"revision": 2},
            timeout=5,
        )
        assert act.status_code == 409

        # 已打开的流必须读到完整响应（剥掉响应头）
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            got.extend(chunk)
        sock.close()
        header_end = got.index(b"\r\n\r\n") + 4
        body = bytes(got[header_end:])
        assert body == payload

        # 回收请求本身应成功返回
        rt.join(timeout=30)
        assert recycle_result.get("status") == 204

        # 回收窗口结束后：版本彻底消失 -> 404
        deadline = time.time() + 10
        final = None
        while time.time() < deadline:
            final = httpx.get(f"{b.base}/v/{v_old}/index.html", timeout=5)
            if final.status_code == 404:
                break
            time.sleep(0.2)
        assert final is not None and final.status_code == 404
        # 当前版本指针不受影响
        cur = httpx.get(f"{a.base}/current", timeout=5).json()
        assert cur["version"] == v_new
    finally:
        try:
            sock.close()
        except Exception:
            pass
        a.stop()
        b.stop()


def test_recycle_killed_midway_finished_on_restart(tmp_path):
    """进程 A 在回收窗口内被强杀，启动的新进程必须完成未结束回收。"""
    data_dir = str(tmp_path / "shared-data-3")
    os.makedirs(data_dir)
    a = Server(data_dir, _free_port(), {"RECYCLE_DELETE_DELAY": "30"})
    try:
        a.wait_ready()
        v_old = _upload(a.base, {"index.html": b"old", "x.txt": b"x"})
        v_new = _upload(a.base, {"index.html": b"new"})
        httpx.post(f"{a.base}/versions/{v_old}/activate", json={"revision": 0})
        httpx.post(f"{a.base}/versions/{v_new}/activate", json={"revision": 1})

        # 发起回收并等待 recycling 状态落库，然后强杀 A
        import threading

        def fire():
            try:
                httpx.post(f"{a.base}/versions/{v_old}/recycle", timeout=40)
            except Exception:
                pass

        t = threading.Thread(target=fire, daemon=True)
        t.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            import sqlite3

            conn = sqlite3.connect(str(Path(data_dir) / "db.sqlite3"))
            state = conn.execute(
                "SELECT state FROM versions WHERE digest = ?", (v_old,)
            ).fetchone()
            conn.close()
            if state and state[0] == "recycling":
                break
            time.sleep(0.1)
        assert state[0] == "recycling"
        # blob 文件此时仍在；强杀进程模拟退出
        assert (Path(data_dir) / "blobs" / v_old / "index.html").exists()
        a.proc.kill()
        a.proc.wait(timeout=10)

        # 启动新进程（恢复会完成回收；宽限期设 0）
        c = Server(data_dir, _free_port(), {"RECOVERY_GRACE_SECONDS": "0"})
        try:
            c.wait_ready()
            r = httpx.get(f"{c.base}/v/{v_old}/index.html", timeout=10)
            assert r.status_code == 404
            assert not (Path(data_dir) / "blobs" / v_old).exists()
            detail = httpx.get(f"{c.base}/versions/{v_old}", timeout=5)
            assert detail.status_code == 404
            # 当前版本仍为 v_new
            assert httpx.get(f"{c.base}/current", timeout=5).json()[
                "version"
            ] == v_new
        finally:
            c.stop()
    finally:
        a.stop()
