# File Hash Browser

一个面向 NAS 和可信内网的轻量 Web 文件哈希工具。服务只展示管理员授权的目录，支持在后台并行计算 MD5、SHA-1、SHA-256、SHA-512，并把结果保存在本地 SQLite 数据库中。

项目采用纯 Python 标准库实现，Web 前端、HTTP 服务、后台任务和 SQLite 全部封装在**单个容器**内，不依赖外部数据库、消息队列或前端运行时。

## 功能

- 左侧为可懒加载、可展开的授权目录树，右侧为当前目录文件表格。
- 表格显示文件大小、最后修改时间、最后哈希时间及各算法摘要。
- “显示列”菜单可独立显示或隐藏大小、时间和每一种哈希列，选择保存在当前浏览器中。
- 可选择多个文件、一个或多个目录；目录由服务端递归展开并去重。
- “仅计算缺失值”和“重新计算并覆盖”两种策略。
- 固定大小的全局后台工作池。网页关闭后任务继续运行；并行数可按 CPU 核心数自动设置，也可由配置强制指定。
- 顶栏“后台任务”弹框显示等待、枚举和计算中的任务，支持中断、删除及一键清空。
- 已完成、失败或取消的任务不保留历史记录，只保留成功计算出的哈希结果。
- 哈希算法使用可信插件注册表，前端从服务端动态获得算法列表，新增算法不需要修改界面。
- 每次访问目录时，服务端用该目录的完整扫描结果清除已经消失的直接子文件记录；分页不会导致误删。
- 不提供任何读取、预览或下载文件内容的 HTTP 路由。

## 单容器部署

容器是推荐运行方式，宿主机不需要安装 Python。

1. 复制容器配置：

   ```powershell
   Copy-Item config.docker.example.json config.docker.json
   ```

   Linux/macOS：

   ```sh
   cp config.docker.example.json config.docker.json
   ```

2. 编辑 `config.docker.json`：

   - 把 `server.allowed_hosts` 改为实际访问使用的域名或 IP。
   - 按需设置 `privacy.show_full_filename` 和 `hashing.parallel_tasks`。

3. 编辑 `compose.yaml`，把 `/path/to/authorized/files` 改成宿主机上的授权目录。该目录必须保持 `:ro` 只读挂载。

4. 构建并启动唯一的服务容器：

   ```sh
   docker compose up -d --build
   ```

5. 默认在宿主机打开 <http://127.0.0.1:8080>。如需可信局域网访问，把 `compose.yaml` 的端口映射改成 `8080:8080`，并同步更新 `allowed_hosts`。

容器内使用 `/opt/venv` 隔离 Python 环境，以 UID/GID `10001` 的非 root 用户运行，根文件系统只读，所有 Linux capabilities 均被移除。授权文件位于只读 `/files`，SQLite 位于命名卷 `/data`。Web、后台任务和数据访问仍是同一个容器进程，不会启动其他服务。

## 本地开发

本地开发也必须使用项目虚拟环境，不直接在系统 Python 中安装或运行依赖。本项目没有第三方运行时依赖。

Windows：

```powershell
py -m venv .venv
Copy-Item config.example.json config.json
# 创建并在 config.json 中配置授权目录，然后：
.\.venv\Scripts\python.exe -m file_hash_browser --check-config --config config.json
.\.venv\Scripts\python.exe -m file_hash_browser --config config.json
```

Linux/macOS：

```sh
python3 -m venv .venv
cp config.example.json config.json
./.venv/bin/python -m file_hash_browser --check-config --config config.json
./.venv/bin/python -m file_hash_browser --config config.json
```

## 配置

完整示例见 `config.example.json` 和 `config.docker.example.json`。

| 配置 | 说明 |
| --- | --- |
| `server.host` / `server.port` | 监听地址和端口；默认应使用 loopback。 |
| `server.allowed_hosts` | 允许的 HTTP Host 列表。设置 `"*"` 会关闭 Host 限制，不推荐。 |
| `privacy.show_full_filename` | `false` 时在服务端半隐藏所有文件、目录和根显示名。 |
| `browse.show_hidden_files` | 是否显示点文件和操作系统隐藏项。 |
| `browse.default_page_size` | 目录树和文件表格每页条目数。 |
| `hashing.parallel_tasks` | `0` 表示使用 CPU 核心数；正整数表示固定工作线程数。 |
| `hashing.chunk_size_bytes` | 每个工作线程的流式读取块大小。 |
| `hashing.max_files_per_job` | 一个任务递归展开后允许的最大文件数。 |
| `hashing.max_active_jobs` | 同时存在的等待/运行任务上限。 |
| `plugins.directory` | 可选的管理员可信 Python 插件目录。Web API 不能上传插件。 |
| `plugins.enabled_algorithms` | 允许计算的算法 ID 白名单；省略时启用全部已注册插件。 |
| `data.database` | SQLite 数据库路径，不能位于任何授权根目录中。 |
| `roots` | 授权根目录列表。每项包含稳定 `id`、显示 `label` 和真实 `path`。 |

配置文件、数据库和插件目录都不能位于授权目录内；授权根也不能彼此重叠。相对路径以配置文件所在目录为基准。

## 名称隐藏与访问边界

当 `privacy.show_full_filename` 为 `false` 时，脱敏发生在服务端公开 DTO 层：

- 长度大于 1 的文件名、目录名和根显示名只返回前 `ceil(长度 / 2)` 个 Unicode 字符，再追加 `…`。
- 单字符名称按需求保留原样，避免同一目录内无法区分。
- 目录树、面包屑、右侧列表、任务接口、错误消息和 URL 都不会收到真实路径或完整名称。
- 浏览器仅使用进程内 HMAC 生成的不透明节点 ID；ID 不是 Base64 路径，无法解码还原名称，服务重启后自动失效。
- 后台任务 API 只返回算法、状态和计数，不返回任何文件或目录名称。

这是一项信息最小化措施，不是用户身份认证。项目按需求不提供登录，因此任何能访问服务的人都可以浏览脱敏元数据和提交计算。默认只监听本机；在 NAS/局域网部署时应配合防火墙、反向代理访问控制和只读文件挂载。

服务从未把授权目录交给静态文件服务器，也不存在文件内容、Range、预览或下载 API。Linux/macOS 上使用固定授权根目录句柄和逐级 `openat + O_NOFOLLOW`；Windows 上拒绝 symlink、junction 和 reparse point，并核对打开前后的文件标识。

## 哈希结果与文件变化

SQLite 只保存授权根 ID、内部相对路径、算法、摘要、读取字节数和最后计算时间。真实绝对路径不会保存。

- 文件仍然存在但内容或修改时间改变时，旧哈希不会自动失效；右侧会继续显示最后一次计算结果。
- 用户选择“重新计算并覆盖”后才会更新摘要和最后哈希时间。
- 进入一个目录时会先完整扫描该目录，再删除其中已经消失的直接子文件哈希记录。
- 同时提交同一文件的多个重算任务时，以较新提交的任务为准，较早任务晚完成也不会覆盖新结果。
- 文件在计算时增长不会无限占用线程：每次只读取打开文件句柄最初观察到的长度。

## 后台任务

任务由应用级队列管理，不依赖创建任务的 HTTP 连接，因此关闭标签页或断开客户端不会取消任务。并行单位是“文件”：同一文件选择多个算法时只读取一次，再同时更新所有摘要。

任务弹框只显示活动队列：

- `等待计算`：可以直接删除。
- `正在读取目录` / `正在计算`：可以中断并删除；工作线程最迟在当前读取块结束后停止。
- “清空全部任务”会中断并删除所有活动任务。
- 任务完成、失败或取消后立即从 SQLite 删除任务和明细；哈希结果独立保留。

停止或重启服务进程会中断活动任务，不会自动恢复。重新打开网页不会中断任务。

## 扩展哈希算法

四个内建算法分别位于 `file_hash_browser/hash_plugins/`。每个插件提供 `register(registry)`；注册表要求稳定算法 ID、显示信息、摘要长度和一个每次返回新哈希对象的 factory。

`plugins/example_blake2b.py.example` 展示了外部管理员插件格式。外部插件是**受信任的服务端代码**，只能由管理员放入配置的插件目录，不能通过网页上传或指定。启用后，算法会自动出现在计算选择器和可显示的表格列中。

## HTTP API

主要接口：

- `GET /api/v1/bootstrap`：公开配置、算法和授权根显示节点。
- `GET /api/v1/nodes/{opaqueId}/children`：懒加载目录并触发消失记录清理。
- `POST /api/v1/jobs`：持久化并提交后台任务。
- `GET /api/v1/jobs`：只列出当前活动任务。
- `POST /api/v1/jobs/{id}/cancel`：中断并删除任务。
- `DELETE /api/v1/jobs/{id}`：删除等待任务或中断运行任务。
- `DELETE /api/v1/jobs`：清空活动队列。
- `GET /api/v1/health`：容器健康检查。

API 不启用 CORS；写请求会检查同源 `Origin`，JSON 请求体限制为 64 KiB，并限制选择数、文件数、算法数和活动任务数。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Linux/macOS：

```sh
./.venv/bin/python -m unittest discover -s tests -v
```

## License

[GNU Affero General Public License v3.0](LICENSE)
