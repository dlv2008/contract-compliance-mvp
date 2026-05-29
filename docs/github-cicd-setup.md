# GitHub CI 配置说明

仓库目标：

- 所有者：`dlv2008`
- 仓库名：`contract-compliance-mvp`

## 1. 创建仓库

1. 在 GitHub 上创建 `contract-compliance-mvp`
2. 把当前工作目录推送到 `main` 分支

## 2. 当前阶段目标

由于云服务器暂时不再承担 `MVP` 主链路，当前更推荐的做法是：

- `pull_request`：运行 `lint` 和测试
- `push` 到 `main`：运行 `lint`、测试和构建检查
- `GHCR + SSH` 自动发布先暂停，等硬件恢复后再启用

## 3. GitHub Actions 权限检查

请确认：

1. 打开 GitHub 仓库 `Settings`
2. 进入 `Actions > General`
3. 确认工作流允许读取和写入 `packages`

## 4. 当前阶段所需 Secrets

本地 `WSL` 优先阶段，实际上可以先不配置任何部署类 `Secrets`。

如果未来恢复云端自动发布，再在 `Settings > Secrets and variables > Actions` 中增加：

- `DEPLOY_HOST`：`www.trendbot.cn`
- `DEPLOY_PORT`：`7956`
- `DEPLOY_USER`：`dlv`
- `DEPLOY_SSH_KEY`：专用于 GitHub Actions 的部署私钥
- `GHCR_PULL_TOKEN`：服务器拉取私有镜像所需的令牌

可选的下一阶段配置：

- `APP_ENV_FILE`：如果未来希望由 Actions 上传环境变量文件，可用这个多行变量

## 5. 未来云端阶段的 SSH Key 建议

不要直接复用你个人工作机的私钥，更稳妥的方式是单独生成一把部署密钥：

1. 本地生成一对新的 `ed25519` 密钥
2. 把公钥追加到服务器 `/home/dlv/.ssh/authorized_keys`
3. 把私钥保存到 GitHub Secret `DEPLOY_SSH_KEY`

## 6. 未来云端工作流默认依赖的服务器文件

- `/opt/stacks/compliance-app/docker-compose.prod.yml`
- `/opt/stacks/shared/env/compliance-app.env`

说明：

- 工作流可以上传 `compose` 文件
- 真实环境变量文件仍需要你在服务器上准备

## 7. 当前推荐的工作流分工

1. 先让 `CI` 在每次提交时保持绿色
2. 开发、调试、演示全部在本地 `WSL` 中完成
3. 等硬件恢复后，再开启 `GHCR + SSH` 自动发布

## 8. 如果以后恢复云端部署，首次检查顺序

1. 为 `compliance.trendbot.cn` 增加 DNS
2. 确认云防火墙仍放行 `80/443/7956`
3. 创建 `/opt/stacks/shared/env/compliance-app.env`
4. 推送到 `main`
5. 在 GitHub `Actions > deploy` 中观察发布过程

## 9. 云端恢复后的冒烟验证

- `curl http://127.0.0.1:19080/api/health`
- 打开 `https://compliance.trendbot.cn`
- 检查桌面和手机布局是否正常
