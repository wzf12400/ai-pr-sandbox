# Frontend

React、TypeScript 和 Vite 管理控制台，默认监听 `127.0.0.1:7100`。

## 本地开发

```bash
npm ci
npm run dev
```

开发服务器会把 `/api`、`/log-monitor`、`/jira-monitor`、`/issue` 和 `/pull`
代理到本机后端服务。

## 独立构建

```bash
npm ci
npm run build
```

将 `dist/` 作为静态资源独立发布，并由部署网关把上述接口路径转发到后端。
