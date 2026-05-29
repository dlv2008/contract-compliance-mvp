# 服务器状态记录（2026-05-28）

这份记录说明的是：在临时清理后又执行了回滚恢复，云服务器重新回到“历史演示服务可用”的状态，同时解释为什么 `MVP` 主链路改回本地 `WSL`。

## 主机信息

- 主机：`www.trendbot.cn`
- 主机名：`pulse`
- 系统：`Ubuntu 24.04.3 LTS`
- 登录用户：`dlv`
- SSH 端口：`7956`

## 当前对外开放入口

- `80/tcp`
- `443/tcp`
- `7443/tcp`
- `9443/tcp`
- `7956/tcp`

## 已恢复的历史服务

- 原 `ameeting` 前端、API、语音识别、说话人分离服务
- 原 `Supabase intrascribe` 容器组
- `site_total.service`
- `pm2-dlv.service`
- `nanning-agents-mvp` 进程
- `9443` 上的历史会议系统
- `7443` 上的 `LiveKit` 相关入口

## 当前保留的本地辅助监听

- `127.0.0.1:11434`：`ollama`
- `127.0.0.1:6379`：`redis`

## 已不再作为 MVP 主目标的部分

- 云服务器不再是 `contract-compliance-mvp` 的首选运行环境
- `compliance.trendbot.cn` 和 `rag.trendbot.cn` 保留为后续阶段目标
- 当前立即可行的 `MVP` 路线是：`Windows + WSL + 本地 RAGFlow`

## 之前准备过的服务器目录

- `/opt/stacks/compliance-app`
- `/opt/stacks/ragflow`
- `/opt/stacks/shared/env`
- `/opt/stacks/shared/nginx`
- `/opt/stacks/shared/backups`

## 仅在未来恢复云端阶段时仍可能需要的动作

- 为下面两个域名补齐 DNS：
  - `compliance.trendbot.cn`
  - `rag.trendbot.cn`
- 保持云防火墙开放：
  - `7956/tcp`
  - `80/tcp`
  - `443/tcp`

## 主要限制

当时主机可用内存约为 `7.3 GiB`，低于 `RAGFlow` 官方自托管建议值，这也是 `MVP` 主链路切回本地 `WSL` 的关键原因。
