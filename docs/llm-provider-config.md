# 大模型配置说明

## 安全原则

- 真实密钥只放在本地 `.env` 或进程环境变量中。
- 不要把 `LLM_API_KEY`、`RAGFLOW_API_KEY` 写进代码、测试、提交记录或文档。
- API 响应只返回 `api_key_present`，不会返回真实密钥。

## OpenAI 兼容配置

官方 Chat Completions 的标准路径是 `/v1/chat/completions`。本项目默认按这个约定拼接：

```env
LLM_BASE_URL=https://gen.trendbot.cn/v1
LLM_MODEL=I2AI/minimax-m2.5
LLM_API_KEY=replace_me
```

如果供应商实际要求完整自定义路径，可以覆盖：

```env
LLM_CHAT_COMPLETIONS_URL=https://gen.trendbot.cn/v1/chat/completion
```

默认不会主动请求模型服务，避免无意消耗额度。需要启动连通性探测时再设置：

```env
LLM_PROBE_ENABLED=true
```

## 检查接口

```bash
curl http://127.0.0.1:18080/api/llm/health
curl http://127.0.0.1:18080/api/system/status
```
