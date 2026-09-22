# 语音输入配置指南

## 功能说明

支持客人通过企微客服发送语音消息，自动识别为文字后走 AI 应答流程。

## 配置步骤

### 1. 获取腾讯云凭证

1. 登录 [腾讯云控制台](https://console.cloud.tencent.com/)
2. 进入「访问管理」→「API密钥管理」
3. 获取 SecretId 和 SecretKey

### 2. 配置环境变量

在 QwenPaw 环境变量中添加：

```bash
# 腾讯云 ASR 语音识别
TENCENT_SECRET_ID=your_secret_id
TENCENT_SECRET_KEY=your_secret_key
```

### 3. 重启服务

配置完成后重启 QwenPaw 服务使配置生效。

## 功能特性

- ✅ 支持微信/企微原生语音输入
- ✅ 自动识别中文普通话
- ✅ 识别结果走 AI 应答流程
- ✅ 识别失败提示重新发送

## 注意事项

1. 语音格式：企微默认使用 AMR 格式
2. 语音时长：建议不超过 60 秒
3. 识别准确率：普通话 > 95%，方言可能较低
4. 费用：腾讯云 ASR 有免费额度，超出后按量计费

## 测试方法

1. 在企微客服发送语音消息
2. 查看日志：`tail -f /var/log/app.err.log | grep "voice\|ASR"`
3. 验证识别结果是否正确
