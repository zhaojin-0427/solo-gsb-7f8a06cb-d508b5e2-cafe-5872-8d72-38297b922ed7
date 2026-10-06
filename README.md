# 静态站点发布服务

面向运维人员的静态站点发布 HTTP API。上传 ZIP 生成**不可变、内容寻址**的版本，
通过带修订号的乐观锁激活版本；站点入口跳转到带版本号的 `index.html`，
页面相对资源始终从该版本读取；非当前版本可回收。数据持久化在本地 SQLite + 文件目录中，
**两个服务进程共用同一数据目录也安全**（上传/回收中途进程退出，重启自动恢复）。

## 一键启动

```bash
docker compose up -d --build
```

- 端口：**8080**
- 数据持久化：Docker named volume `static-data`（容器内 `/data`，包含 `db.sqlite3` 与 `blobs/`）
- Compose 默认起 **2 个 uvicorn worker**，即“两个服务进程共用数据目录”的形态
- 健康检查：`GET /healthz`

停止与清理：

```bash
docker compose down            # 停止（保留数据）
docker compose down -v         # 停止并删除数据卷
```

本地无 Docker 时也可直接运行：

```bash
pip install -r requirements.txt
DATA_DIR=./data uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 2
```

## 调用示例

设 `BASE=http://localhost:8080`。

### 1. 打包并上传版本

ZIP 要求：条目仅为普通文件或目录；路径以 `/` 分隔且为相对路径；
不得包含空段、`.`、`..`、反斜杠、符号链接、重复文件名；
根目录必须含 `index.html`；解压总量 ≤ **16 MiB**。任一不满足返回 `422`，不会产生版本。

```bash
mkdir -p site/css
printf '<link rel="stylesheet" href="css/app.css"><h1>v1</h1>' > site/index.html
printf 'h1{color:red}' > site/css/app.css
(cd site && zip -r ../site.zip .)

curl -sS -X POST "$BASE/versions" \
  -F "file=@site.zip;type=application/zip"
# 201 {"version":"ab12…(64位十六进制)","created":true,"files":["css/app.css","index.html"]}
```

> 版本号 = 排序后文件路径 + 文件字节的 SHA-256。相同内容重复上传（即使 ZIP 元数据不同）
> 返回**同一个版本号**（此时 HTTP 200、`created:false`）。

### 2. 查询当前修订号并激活

激活必须携带**当前修订号**（乐观锁）；修订号不一致返回 `409`（响应体带回当前修订号）。
允许重新激活旧版；每次成功激活修订号 +1。

```bash
curl -sS "$BASE/current"
# 未激活：{"version":null,"revision":0}

V=ab12…   # 用上一步的版本号替换
curl -sS -X POST "$BASE/versions/$V/activate" \
  -H 'Content-Type: application/json' -d '{"revision":0}'
# {"version":"ab12…","revision":1}
```

### 3. 访问站点

```bash
curl -sS -i "$BASE/"
# 未激活：404；激活后：307 -> /v/<version>/index.html
```

浏览器访问 `http://localhost:8080/` 后，页面与相对资源都固定在
`/v/<version>/` 前缀下（如 `/v/<version>/css/app.css`），切换版本期间不会混用旧版资源。
资源严格按该版本的清单读取，任何未知版本/路径返回 `404`。

### 4. 上传 v2 并切换

```bash
curl -sS -X POST "$BASE/versions" -F "file=@site-v2.zip;type=application/zip"
curl -sS -X POST "$BASE/versions/<v2>/activate" \
  -H 'Content-Type: application/json' -d '{"revision":1}'   # 必须带当前修订号
# 409 冲突示例：{"error":{"code":409,"detail":"修订号冲突：当前为 1，提交的是 0","revision":1}}
```

### 5. 回收旧版本

```bash
curl -sS -X POST "$BASE/versions/<v1>/recycle"     # 204
# 回收当前激活版本 -> 409
```

回收语义：

| 场景 | 结果 |
| --- | --- |
| 回收当前版本 | `409` |
| 回收开始后再激活该版 | `409` |
| 回收开始后的新读取 | `410 Gone` |
| 回收开始前已打开的流式下载 | 完整结束后才释放 |
| 回收完成 | 版本与文件彻底删除，读取/查询变为 `404` |
| 回收后再上传相同内容 | 同一版本号重新可用（`201`） |

### 其他接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/versions` | 列出全部版本及状态（`available` / `recycling`） |
| `GET` | `/versions/{digest}` | 版本详情与文件清单 |
| `GET` | `/current` | 当前版本与修订号 |
| `GET`/`HEAD` | `/v/{digest}/{path}` | 读取指定版本清单内的资源 |

## 数据与一致性设计

```
/data
├── db.sqlite3          # 版本表、文件清单、当前指针 + 修订号（WAL 模式）
├── blobs/<digest>/     # 不可变版本内容
├── staging/<uuid>/     # 上传暂存，提交时原子 rename 到 blobs/
└── recovery.lock       # 启动恢复互斥锁
```

- **上传原子性**：先解压校验到 `staging/`，再把目录原子改名到 `blobs/<digest>`，
  最后提交数据库事务。任何校验失败或中途崩溃都不会出现可见版本；重启清理残留暂存与孤儿 blob。
- **多进程**：所有写事务使用 `BEGIN IMMEDIATE`（配合 `busy_timeout`），
  相同内容并发上传只产生一个版本；激活/回收跨进程互斥。
- **回收崩溃恢复**：回收第一步先把状态持久化为 `recycling`，再删文件。
  若进程退出，任一存活/新启动进程在启动时继续完成删除并解激活，不会留下指向缺失文件的版本。
- **在途下载**：服务端先打开文件句柄再开始流式响应；POSIX 下目录项被 unlink 不影响
  已打开句柄读完，因此回收期间已建立的下载可以完整结束。

## 测试

```bash
pip install -r requirements.txt httpx pytest
pytest -q
```

`tests/test_multiprocess.py` 会真实启动两个 uvicorn 进程，验证跨进程可见性、
修订号冲突、回收窗口（409/410）、在途流式下载完整性，以及强杀进程后重启完成回收。
资源受限环境可用 `SKIP_MULTIPROC=1 pytest -q` 跳过该文件。
