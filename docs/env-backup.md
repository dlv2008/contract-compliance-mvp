# `.env` 安全备份

推荐在 `WSL` 中使用 [backup_env.sh](\\wsl.localhost\Ubuntu\home\sdt00157\contract-compliance-mvp\scripts\backup_env.sh) 对 `.env` 做**加密备份**，默认输出目录在仓库外：

- `~/.local/share/contract-compliance-mvp/backups`

这样做有 3 个好处：

- 不会把明文 `.env` 误留在 Git 工作区
- 备份文件和业务代码分离，降低误上传风险
- 备份后自带 `sha256` 校验文件，便于确认备份是否损坏

执行方式：

```bash
cd ~/contract-compliance-mvp
bash scripts/backup_env.sh
```

脚本规则：

- 优先使用 `gpg` 做对称加密
- 如果本机没有 `gpg`，自动回退到 `openssl`
- 备份目录权限收紧到当前用户
- 备份文件权限收紧到当前用户

恢复命令会在脚本执行完成后直接打印出来。
