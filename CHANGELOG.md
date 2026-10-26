# Changelog

所有值得记录的变更都在这里。版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [1.6] - 2026-10-26

### Fixed
- `log_message` 在处理 400 时崩溃（`TypeError: argument of type 'HTTPStatus' is not iterable`）：
  现在先格式化再按字符串过滤，并对 400 不记日志。
- 畸形请求（手机误用 `https://` 访问、扫描器探测）不再刷 traceback，改为回一段纯文本提示。
- `_lan_ips()` 缺少 `import socket`，导致启动横幅里不显示局域网地址。
- 页面初始化链：`loadStatus()` 失败时 `syncState()` 不再被跳过。

### Changed
- 版本号显示在页脚：`前端 v1.6 · 服务端 v1.6（机器名）`。
- 首页响应补 `Pragma` / `Expires`，减少移动端缓存旧页面。

## [1.5] - 2026-10-25

### Added
- **跨设备同步**：`GET /api/state` 返回上次解析的列表与任务摘要，任意设备打开都恢复同一份进度；
  空闲页面每 5 秒比对 `job.id`，别的设备开始下载时自动接管。
- **运行中调限速**：`POST /api/limit`，令牌桶热更新，1 秒内生效；速度显示改为 0.5s 滑动窗口。
- 进度面板内新增「取消下载」，任务运行期间两个取消入口始终可用。
- 同步失败时显示醒目横幅并给出原因（网络 / 401 / 版本过旧）。

### Removed
- 页面上的「退出（关闭服务）」按钮（接口 `/api/shutdown` 保留）。

### Fixed
- 取消任务时清理 `.parts` 半成品分片。
- 运行中重复点「开始下载」返回 409，避免两个任务写同一目录。

## [1.4] - 2026-10-25

### Added
- 去水印五种模式：`delogo` / `blur` / `mosaic` / `crop` / `mask`，支持多区域（`;` 分隔）与时段 `0-30`。
- 4K 或 HEVC 源自动切 `libx265` 重编码，避免体积失控。
- `docs/WATERMARK.md`：playurl / DOM / 像素三层实测与方案对比。

## [1.3] - 2026-10-24

### Added
- 限速：`--limit` / 页面下拉，令牌桶按任务合计带宽限制。
- 去水印：`delogo` 矩形插值，支持四角预设与自定义坐标。
- Docker 部署（alpine + python3 + ffmpeg），单文件包 `app_all_in_one.py`。

## [1.2] - 2026-10-24

### Added
- 选集 / 全集：`--all`、`--eps 1-3,7`、`--ep <id>`、`--dry-run`。
- 批量结果汇总与失败原因。

## [1.1] - 2026-10-24

### Added
- 浏览器扫码登录（CDP 抓 Cookie），或手动粘贴 `SESSDATA`。
- 设置持久化到 `cookies.txt`，自动复用。

## [1.0] - 2026-10-24

### Added
- 首个版本：PGC 解析、DASH 多线程下载、ffmpeg 合并、字幕、单页 Web UI。
