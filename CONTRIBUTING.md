# 参与贡献

感谢愿意改进这个项目。以下是几条约定，能省下双方的时间。

## 环境

- Python 3.9+（只用标准库）
- ffmpeg 在 `PATH` 里
- 无需任何 pip 依赖，提交前请不要引入第三方包

## 本地跑起来

```bash
python server.py --port 8123
# 浏览器打开 http://127.0.0.1:8123/
```

## 提交前自检

```bash
python -m py_compile server.py core.py
BILI_NO_BROWSER=1 python server.py --port 8123 &
curl -fsS http://127.0.0.1:8123/healthz    # 应返回 ok
```

改了前端（`index.html` 内联 JS）请顺手在浏览器里点一遍：解析 → 选集 → 开始 → 取消。

## 代码约定

- 只依赖标准库；新增依赖的 PR 需要说明为什么不能用标准库实现；
- 界面文案用中文，代码标识符用英文；
- 不要提交 `cookies.txt`、`downloads/`、`bin/`、`__pycache__/`（`.gitignore` 已覆盖）；
- 大改动请先开 issue 说明思路，避免白写。

## Pull Request

1. 一个 PR 只做一件事；
2. 描述里写清楚：改了什么、为什么、怎么验证；
3. 涉及 UI 的请附截图（手机宽度也拍一张更好）；
4. 提交信息用英文祈使句，例如 `fix: log_message crashes on HTTP 400`。
