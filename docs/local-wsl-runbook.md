# 本地 WSL 运行手册

## 目标

把 `contract-compliance-mvp` 的开发、调试、演示主链路固定到本地 `WSL`，不再依赖当前云服务器。

## 当前约束

- 云服务器内存不足，不再承担 `MVP` 主部署
- 本地已经存在 `RAGFlow` 源码目录
- `RAGFlow` 主服务继续按命令方式启动，不改成 Docker 主服务
- 允许 `RAGFlow` 的底层依赖继续保持你现有的本地运行方式

## 已确认的本地事实

- `WSL` 发行版：`Ubuntu`
- 已发现本地 `RAGFlow` 源码目录：`~/ragflow`
- 已发现旧版本目录：`~/ragflow_0.19`

## 建议的本地链路

`Windows 浏览器 -> localhost -> WSL 中的审查服务 -> 本地源码版 RAGFlow -> MiniMax/Qwen 接口`

## 启动顺序

### 1. 启动或确认 RAGFlow 依赖

保留你当前的本地做法，不强制重构。只要求下面几类依赖可用：

- 数据库
- 检索引擎
- 对象存储
- Redis

如果你是按 `RAGFlow` 源码文档维护这些基础依赖，就继续沿用。

### 2. 启动 RAGFlow 后端

在 `WSL` 中进入：

```bash
cd ~/ragflow
source .venv/bin/activate
export PYTHONPATH=$(pwd)
JEMALLOC_PATH=$(pkg-config --variable=libdir jemalloc)/libjemalloc.so
LD_PRELOAD=$JEMALLOC_PATH python rag/svr/task_executor.py 1
```

再开第二个终端：

```bash
cd ~/ragflow
source .venv/bin/activate
export PYTHONPATH=$(pwd)
python api/ragflow_server.py
```

### 3. 启动 RAGFlow 前端

如果你需要直接使用 `RAGFlow` 的管理界面：

```bash
cd ~/ragflow/web
npm install
npm run dev -- --host 0.0.0.0
```

### 4. 验证 RAGFlow 是否已起来

在 `WSL` 中检查：

```bash
ps -ef | grep -E 'ragflow_server.py|task_executor.py' | grep -v grep
ss -ltnp | grep 9380
```

在 `Windows` 中打开：

- `RAGFlow` 前端开发端口
- 或后续由你自己的审查工作台直接调用 `RAGFlow` API

## 审查系统本地开发方式

### 1. 审查服务绑定

- 开发时优先监听 `127.0.0.1`
- 如果要手机真机访问，再切到 `0.0.0.0`

### 2. 推荐端口

- 审查服务：`18080`
- 如果有单独前端开发服务：`15173`
- `RAGFlow` 保持它当前本地端口，不强行统一

### 3. 关键环境变量

建议统一在本项目 `.env` 中维护：

```env
RAGFLOW_BASE_URL=http://127.0.0.1:9380
RAGFLOW_API_KEY=replace_me
LLM_PROVIDER=minimax_or_qwen
LLM_BASE_URL=replace_me
LLM_API_KEY=replace_me
DATABASE_URL=replace_me
UPLOAD_DIR=./data/uploads
REPORT_DIR=./data/reports
```

## 手机演示方式

如果只需要看手机版布局：

- 直接用浏览器 DevTools 手机视图

如果需要真机访问：

1. 让审查服务监听 `0.0.0.0`
2. 在 `Windows` 查询 `WSL` IP：

```powershell
wsl -d Ubuntu -- hostname -I
```

3. 用 `netsh interface portproxy` 做端口转发：

```powershell
netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=18080 connectaddress=<WSL_IP> connectport=18080
```

4. 在 Windows 防火墙仅对“专用网络”放行该端口
5. 手机与电脑连接同一局域网

## 当前建议

1. 先把桌面端链路跑通
2. 再做手机尺寸适配
3. 最后再决定是否开放给手机真机访问

## 与云端方案的关系

- 现在的 `GitHub CI` 只负责代码检查
- `GHCR + SSH` 自动发布先暂停
- 等云服务器硬件恢复后，再把这套本地链路迁回云端
