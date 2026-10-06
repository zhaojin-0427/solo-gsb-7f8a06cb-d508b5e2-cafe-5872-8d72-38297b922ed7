# Static Site Publisher

面向运维人员的静态站点发布 HTTP API。上传 ZIP 生成**不可变版本**，
通过**修订号 CAS** 激活/回滚版本，按版本清单提供资源访问，支持版本回收。
Python + FastAPI + SQLite（WAL）+ 本地文件目录，无外部服务依赖。

## 一键启动

```bash
docker compose up --build
```

唯一需要的一条命令。服务监听 **8000** 端口，数据持久化在名为
`site-data` 的卷中（容器内路径 `/data`）。交互式 API 文档：
<http://localhost:8000/docs>

## 调用示例

```bash
# 1. 打包站点（根目录必须包含 index.html），上传生成不可变版本
zip -r site.zip dist/
curl -F "file=@site.zip" http://localhost:8000/api/versions
# => {"version":"b7e1…(64位十六进制摘要)","size_bytes":…,"file_count":…,"created":true}
#    相同内容重复上传返回同一版本（200, "created":false）

# 2. 查询当前版本与修订号
curl http://localhost:8000/api/site
# => {"version":null,"revision":0}

# 3. 携带当前修订号激活版本（CAS；冲突返回 409 并带上当前修订号）
curl -X PUT http://localhost:8000/api/site/active \
     -H "Content-Type: application/json" \
     -d '{"version":"b7e1…","revision":0}'
# => {"version":"b7e1…","revision":1}

# 4. 访问站点：302 跳转到带版本号的 index.html，相对资源始终从该版读取
curl -L http://localhost:8000/
curl http://localhost:8000/site/b7e1…/css/style.css

# 5. 版本列表 / 详情（含文件清单）
curl http://localhost:8000/api/versions
curl http://localhost:8000/api/versions/b7e1…

# 6. 回收非当前版本（当前版本返回 409）
curl -X DELETE http://localhost:8000/api/versions/b7e1…
# => 202；回收开始后激活该版 409、新读取 410，已打开的下载完整结束
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/versions` | 上传 ZIP（表单字段 `file`）。201 新建 / 200 已存在；校验失败 422 |
| `GET` | `/api/versions` | 版本列表 + 当前版本 |
| `GET` | `/api/versions/{id}` | 版本详情与清单；回收后 410 |
| `DELETE` | `/api/versions/{id}` | 回收版本：202；当前版本 409；不存在 404 |
| `GET` | `/api/site` | 当前版本与修订号 |
| `PUT` | `/api/site/active` | 激活：`{"version", "revision"}`；修订号冲突 409，回收中 409，不存在 404 |
| `GET` | `/` | 未激活 404；已激活 302 → `/site/{version}/index.html` |
| `GET` | `/site/{id}/{path}` | 按版本清单读资源；未知路径 404；已回收 410 |

## 规则与语义

**上传校验（违反即 422，且不产生任何可见版本）**

- 条目只能是普通文件或目录（拒绝符号链接、设备节点等）；
- 路径为 `/` 分隔的相对路径：拒绝空段、`.`、`..`、反斜杠、绝对路径；
- 拒绝重复条目名、文件/目录路径冲突、加密条目；
- 解压后总量超过 16 MiB 拒绝；
- 根目录必须包含 `index.html`。

**版本摘要**：按字典序排序的文件路径 + 长度 + 字节计算 SHA-256，
即版本号。内容寻址保证相同内容重复上传得到同一版本。

**激活**：携带当前修订号的 compare-and-swap，每次成功激活修订号 +1；
允许重新激活任何仍存在的旧版本（回滚）。`GET /api/site` 返回当前版本
与修订号。

**资源访问**：激活后 `/` 302 跳转到 `/site/{version}/index.html`，页面
相对资源因此始终解析到同一版本目录，版本切换期间不会混用；资源仅按
数据库中的版本清单读取，清单外路径一律 404。

**回收**：仅可回收非当前版本。`DELETE` 先把版本标记为回收中（此后激活
409、新读取 410），再异步删除文件；已打开的文件描述符不受 unlink 影响，
进行中的流式下载保证完整结束。回收完成后保留墓碑记录，410 语义持久成立。

## 可靠性与多进程

- **崩溃恢复**：上传先写库（`uploading`）→ 文件目录原子 `rename` 到位 →
  置 `ready`，任何时刻崩溃都不会留下指向缺失文件的可见版本；重启时清理
  中断的上传残留，并**继续完成未完成的回收**。
- **多进程共用数据目录**：SQLite WAL + `BEGIN IMMEDIATE` 事务串行化所有
  状态迁移；同内容并发上传幂等（内容寻址）；激活修订号 CAS 跨进程成立。
  例如：`docker compose up --scale web=2`（需两个容器挂载同一卷，或
  `uvicorn --workers 2`）。

## 本地开发

```bash
pip install -r requirements.txt
DATA_DIR=./data uvicorn app.main:app --port 8000

# 端到端冒烟测试（65 项断言：上传校验/激活/切换/回收/双进程/崩溃恢复）
python3 tests/smoke_test.py
```

环境变量：`DATA_DIR`（数据目录，默认 `./data`，容器内 `/data`）、
`CLEANUP_GRACE_SECONDS`（启动时清理临时/孤儿目录的宽限期，默认 60）。
