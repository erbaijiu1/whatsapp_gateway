# whatsapp_gateway

WhatsApp Cloud API 接入服务。接收 Meta 回调并转交 `logistics_ai_agent`，再把报价系统的异步回复发送给对应客户。无需新建前端页面。

```text
Meta -> 网关验签/识别接收账号 -> logistics_ai_agent 持久化消息、去重、返回 buffered
                              -> 原有后台模型/报价/审核流程
Meta <- 网关按 account_code 选 phone_number_id/token <- 报价服务提交回复
```

## 统一部署（local_dev_env）

网关作为服务插件接入 `../../local_dev_env/docker-compose.yml` 的 include 列表，加入共享 `web` 网络。
Nginx 入口在 `../../local_dev_env/nginx/locations/whatsapp_gateway.conf`，不修改公共主配置。

1. 环境变量统一放在 **`../envs/whatsapp_gateway.env`**，Compose 的 `env_file` 直接引用该文件。
   新机器可以从 `.env.example` 复制到此位置；不要在项目目录创建 `.env`。
   `toCom/.gitignore` 已忽略 `/envs/`。账号凭据放在 `config/accounts.json`，该文件也已忽略。
2. 参考 `config/accounts.example.json` 创建 `config/accounts.json`，填写 Meta phone_number_id、access_token、verify_token、app_secret。
3. 生成两个不同的服务令牌（至少32字符），按以下关系配置：

| 网关环境文件 | 报价服务配置 |
| --- | --- |
| `AGENT_INGRESS_TOKEN` | 与 `WHATSAPP_GATEWAY_INGRESS_TOKEN` 相同 |
| `GATEWAY_SEND_TOKEN` | 与 `WHATSAPP_GATEWAY_TOKEN` 相同 |
| `AGENT_BASE_URL=http://logistics-ai-agent:8000/logistics_ai_agent/api` | `WHATSAPP_GATEWAY_URL=http://whatsapp-gateway:8000/whatsapp_gateway/api` |

服务间使用容器服务名通信。宿主机调试端口固定为 `127.0.0.1:8011`，不再从项目 `.env` 插值。
生产回调仍需公网 HTTPS；当前统一环境的 Nginx 只提供本地 HTTP。

4. 在统一入口目录运行：

```bash
cd /Users/hc/Desktop/code/local_dev_env
# 不打印渲染后的环境变量，避免终端泄露凭据
docker compose config --quiet
docker compose up -d --no-deps --build whatsapp-gateway
docker compose exec nginx nginx -t
docker compose exec nginx nginx -s reload
docker compose ps whatsapp-gateway
```

报价服务也必须在该环境启动并填好对应服务令牌；`--no-deps` 只更新网关，不重建其他服务。
网关缺少令牌或账号文件时会启动失败。不能使用空配置假装完成部署。
报价服务保留 `REVIEW_REQUIRED` 审核模式，批准后才发送；账号级设置可能覆盖全局设置。
原先在网关目录执行 `docker compose -f docker-compose.yml -f deploy/compose.network.yml ...` 的方式不再作为本地标准入口。

## Meta 回调

- 验证与事件地址：`https://你的域名/whatsapp_gateway/api/webhook`
- 也可为账号使用：`https://你的域名/whatsapp_gateway/api/webhook/default`
- Verify Token 对应 `accounts.json` 中的 `verify_token`；订阅 WhatsApp `messages`。
- 健康检查：`http://127.0.0.1:8011/whatsapp_gateway/api/health`
- API 文档：`http://127.0.0.1:8011/whatsapp_gateway/api/docs`

反向代理示例见 `deploy/nginx.conf.example`，需要部署到具有有效 HTTPS 证书的公网域名。回调 POST 根据**原始请求体**校验 `X-Hub-Signature-256`。同一回调中每个接收账号都必须匹配其自己的 App Secret。状态回执只验签接收，不进入报价；支持文字、按钮和列表选择文本，图片/语音/文档暂只记录类型与 ID，不提取内容。

## 内部接口

- 网关：`POST /whatsapp_gateway/api/internal/messages`，`Authorization: Bearer <GATEWAY_SEND_TOKEN>`。
- 报价服务：`POST /logistics_ai_agent/api/internal/whatsapp/messages`，`Authorization: Bearer <AGENT_INGRESS_TOKEN>`。

网关发送请求示例（由报价服务调用，message_id 来自已落库的本地消息）：

```json
{"message_id":"local-unique-id","account_code":"default","recipient":"85291234567","content":"这票共351元，其中运输费266元、派送费85元。"}
```

成功返回 `{"status":"sent","message_id":"wamid.…"}`。这里表示 Meta 接收了发送请求，并不保证客户已读。普通文本回复仍受 Meta 客服窗口限制；本服务不提供主动营销模板发送。

## 失败与去重

- 入站：不等待模型。报价服务确认消息已保存后才返回200；服务不可用时返回503，让上游重试。重复回调携带相同原始消息ID，报价服务去重。批次部分转交成功后失败也可安全重试。
- 出站：`data/deliveries.sqlite3` 持久化发送ID与请求摘要，同一ID同一内容成功后重试直接返回原 Meta ID；同一ID改内容返回409。并发请求只允许一次调用Meta。
- Meta超时、异常返回或进程在发送中断开，无法判断是否已经送达时保留 `UNCERTAIN` / `SENDING`，同一ID不会自动重发。需要运营核查发送记录后再决定是否发送新消息；**不承诺跨系统 exactly-once**。调用方若重新生成新ID会被视为新消息。
- 使用一个网关实例与本地持久卷；多个容器共用网络文件系统上的 SQLite 不在支持范围内。发送日志只记录消息ID、账号和状态，不打印令牌或全文。
- 不能同时把 Meta 指向旧直连接口与网关；启用 `WHATSAPP_GATEWAY_URL` 后报价服务旧直连回调返回410。

## 测试

`PYTHONPATH=backend python -m pytest -q`

测试使用临时SQLite和HTTP模拟，不发送真实WhatsApp消息。

### 本地模拟 Meta 入站

服务启动后，可使用脚本生成 Meta 格式的回调并按本地账号的 App Secret 签名：

```bash
cd /Users/hc/Desktop/code/toCom/whatsapp_gateway
python3 scripts/simulate_webhook.py \
  --phone 85290002024 \
  --name 本地模拟客户 \
  --content '我有100公斤芒果，从深圳发到香港观塘，有电梯，请问多少钱？'
```

脚本默认经 Nginx 请求 `/whatsapp_gateway/api/webhook`，不会联系 Meta。每次自动生成新的
message ID；传入相同的 `--message-id` 可验证去重。建议本地将 agent 的
`AI_REPLY_DELIVERY_MODE` 设为 `REVIEW_REQUIRED`，然后在
<http://localhost:8010/logistics_ai_agent/reply-review> 检查待审核回复；也可以打开
<http://localhost/logistics_ai_agent/test-console> 查看测试会话。测试账号不能验证真实 Meta
发送；批准草稿前必须换成有效账号凭据，或接入本地 Meta Mock。

接口参考：[Meta 官方消息示例](https://www.postman.com/meta/whatsapp-business-platform/documentation/wlk6lh4/whatsapp-cloud-api)、[Meta Webhook 文档](https://developers.facebook.com/docs/graph-api/webhooks/getting-started/)。
