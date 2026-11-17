# Glados自动签到

## 食用方式：

### 注册一个GLaDOS的账号([注册地址](https://glados.space/landing/0A58E-NV28S-6U3QV-33VMG))

#### 我的邀请码：([0A58E-NV28S-6U3QV-33VMG](https://0a58e-nv28s-6u3qv-33vmg.glados.space)) 

#### 我的优惠码（9折）：([DEVILSTORE](https://0a58e-nv28s-6u3qv-33vmg.glados.space)) 

### **Fork**本仓库

![图片加载失败](imgs/1.png)

### 添加**secret**

1. 跳转至自己的仓库的`Settings`->`Secrets and variables`->`Action`

2. 添加1个`repository secret`，命名为`GLADOS_COOKIES`，其值对应GLaDOS账号的cookie值中的有效部分（获取方式如下）

- 在GLaDOS的签到页面按`F12`

- 切换到`Network`页面下，刷新

![图片加载失败](imgs/2.png)

- 点击第一个选项卡后在`Request Headers`下找到`Cookie`，右键复制cookie的值即可

  > 参考格式：koa:sess=eyJ1c2xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxAwMH0=; koa:sess.sig=xJkOxxxxxxxxxxxxxxxtnM;

![图片加载失败](imgs/3.png)

- 多账号请在 `COOKIES` 中 添加多个 `cookies` 中间使用 `&`连接即可。（例如： `c1&c3&c3...`）

3. 配置积分兑换策略（非必须）

- 添加1个`repository secret`，命名为`GLADOS_EXCHANGE_PLAN`，配置自动兑换积分策略：

| 值 | 积分要求 | 兑换天数 |
|---|---------|---------|
| `plan100` | 100 积分 | 10 天 |
| `plan200` | 200 积分 | 30 天 |
| `plan500` | 500 积分 | 100 天 |

> 不配置时默认不进行自动兑换；如需兑换，设置 `GLADOS_EXCHANGE_PLAN` 为 `plan100` / `plan200` / `plan500`。

4. 手机推送（非必须）

- 添加1个`repository secret`，命名为`PUSHDEER_SENDKEY`，其值对应 PushDeer key: ([获取地址](https://www.pushdeer.com/product.html))。

### **star**自己的仓库

![图片加载失败](imgs/4.png)

## 文件结构

```shell
│  checkin.py	# 签到脚本
│
├─.github
│  └─workflows
│          gladosCheck.yml	# Actions 配置文件
```

## 更新日志

- **2026-01**: 重构代码，添加log输出方便定位，支持新版网址，支持配置积分兑换策略。
- **2026-04**: 优化代码逻辑，优化日志输出，支持[新版域名](https://railgun.info) ，在 GLADOS_COOKIES 中添加新版域名下的 cookies 即可使用。
- **2026-11**: 接入签到挑战协议（`GET /api/user/checkin/challenge` 取 nonce → 2.5s 观测停顿 → `SHA-256(nonce + "." + "glados-observe-v1")` 计算 proof → 提交），可获取完整积分（Base/Plan/Streak/Cable）；补充会员等级、连签天数、总积分展示；签到失败时以非零退出码结束以便 Actions 告警。
- **2026-11**: HTTP 客户端由 `requests` 换成 `curl_cffi`，复刻浏览器 TLS/HTTP2 指纹以通过 Cloudflare 校验；请求头改为按 XHR 形态完整构造（`referer` / `accept` / `sec-fetch-*` 等），UA 与 `sec-ch-ua` 由指纹配置统一生成、保持自洽；新增 `code 5`（Turnstile 校验未通过）识别。
- **2026-11**: 签到请求体对齐前端协议，补充 `tsState` 字段；识别 challenge 响应中的 `turnstile`（服务端按风控决定是否下发）并按 `reason: "turnstile"` 判定人机校验失败。Turnstile token 只能由真实浏览器执行 JS 取得，脚本拿不到时直接失败并给出明确原因，不再做无效的 nonce 重试。


## 问题排查与定位
- 大家可以通过查询 actions 中的 running checkin 日志快速定位问题，有其他问题提交issue。

  <img width="1684" height="844" alt="image" src="https://github.com/user-attachments/assets/45348a5f-43e4-45f5-8fdf-ce84d343b30d" />

- 日志出现 `Turnstile 人机校验未通过`：说明服务端已对本次请求下发 Turnstile 人机校验（签到响应 `reason: "turnstile"`）。token 必须由真实浏览器执行 JS 取得，纯 HTTP 脚本无法生成；浏览器里的 `cf_clearance` 又绑定 IP + UA 且有有效期，无法在 GitHub Actions 上复用。需要在此状态下签到成功，只能改用带真实浏览器的方案。

## 声明

本项目不保证稳定运行与更新, 因GitHub相关规定可能会删库, 请注意备份







