# 安全策略

## 这个项目会接触到的敏感数据

| 数据 | 位置 | 说明 |
|---|---|---|
| `SESSDATA` 等 Cookie | `cookies.txt`、浏览器 profile | 等同于账号登录态 |
| 下载的视频文件 | `downloads/` | 个人内容 |

以上都在 `.gitignore` 里，**不会被提交**。如果你 fork 后自行改动，请确保没有把它们加进版本库。

## 报告问题

发现安全问题时请不要开公开 issue，改用 GitHub 的 **Security → Report a vulnerability**
（Private vulnerability reporting）或直接邮件联系维护者。

请包含：受影响版本、复现步骤、影响范围。

## 部署建议

- 默认只监听 `127.0.0.1`；需要局域网访问用 `--host 0.0.0.0`，并**设置 `BILI_TOKEN`**：
  ```
  BILI_TOKEN=<足够长的随机串>
  ```
  之后访问需带 `?token=…`，首次访问会种 HttpOnly cookie；
- 不要直接暴露到公网；必须暴露时请走 HTTPS 反代（Nginx / Caddy），并保留 `BILI_TOKEN`；
- 本服务没有多用户隔离，`/api/*` 能做的事包括修改 Cookie、开始/取消下载 —— 只给自己用。
