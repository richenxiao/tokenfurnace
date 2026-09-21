# TokenFurnace 白嫖用户词元大熔炉

**零依赖的本地 Web 控制台，用来消耗、观测和按额度管理 LLM 的 token 用量。**

[English](README.en.md) · 简体中文

[![CI](https://github.com/richenxiao/tokenfurnace/actions/workflows/ci.yml/badge.svg)](https://github.com/richenxiao/tokenfurnace/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![Zero dependencies](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](#)

```bash
python run.py
```

浏览器自动打开 `http://127.0.0.1:8760/`。纯 Python 标准库，不用装包、不用编译。

---

## 为什么会有这个工具

起点是 SenseNova TokenPlan 的积分返赠规则：Flash-Lite 专属积分每消耗 1 分返 1 通用积分，而通用积分能跑账号里所有已开放模型。要做的事就是把专属积分尽快烧掉、换成通用积分。

写着写着发现第二个问题：换一家服务商就得改代码，因为脚本把 base_url、模型名和请求格式全写死了。于是做成了通用的。

现在的形态是：填地址、选协议、勾模型、设上限，然后开跑。

---

## 快速开始

```bash
git clone https://github.com/richenxiao/tokenfurnace.git
cd tokenfurnace
python run.py
```

也可以装成命令：

```bash
pip install -e .
tokenfurnace
```

界面上一共四步：

1. 选协议，填 Base URL 和密钥，点「测试连接」
2. 点「拉取模型」，勾选要消耗的模型
3. 调消费策略（模式、并发、输入规模）
4. 设额度上限，点「用当前配置开始消费」

不想先装再试的话，[`docs/preview.html`](docs/preview.html) 是用真实前端文件渲染的静态预览，含演示数据，双击就能看到完整界面。

### 第一次跑起来会看到什么

界面顶部会显示一块引导，逐条列出当前还缺什么：

```
还差 3 步就能开始
 1. 填 Base URL，比如 https://api.example.com
 2. 填密钥，或改用环境变量（不落盘）
 3. 点「拉取模型」勾选模型（也可以手动填模型 ID）
配好后点「测试连接」验证，再点「用当前配置开始消费」。
```

配置存到当前目录的 `config.json`，账本存到 `data/tokenfurnace.db`，两个都在 `.gitignore` 里。`config.example.json` 是一份配置模板。

命令行参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | 监听地址。改成 `0.0.0.0` 会暴露到局域网，且不带任何鉴权 |
| `--port` | `8760` | 端口被占用会自动往后找 |
| `--no-browser` | — | 不自动打开浏览器 |
| `--config` | `./config.json` | 指定配置文件 |

---

## 四种接口协议

四套协议互不兼容，选错了直接 400 / 404：

| | Anthropic Messages | OpenAI Chat | OpenAI Responses | Gemini Native |
|---|---|---|---|---|
| 路径 | `/v1/messages` | `/v1/chat/completions` | `/v1/responses` | `/v1beta/models/{m}:generateContent` |
| 默认认证 | `x-api-key` | `Authorization: Bearer` | 同左 | `x-goog-api-key` |
| 系统提示 | 顶层 `system` | messages 里 `role: system` | 顶层 `instructions` | 顶层 `systemInstruction` |
| 输入字段 | `messages` | `messages` | `input` | `contents[].parts[].text` |
| 输出上限 | `max_tokens`（必填） | `max_tokens` | `max_output_tokens` | `generationConfig.maxOutputTokens` |
| 思考开关 | `thinking.budget_tokens` | `reasoning_effort` | `reasoning.effort` | `thinkingConfig.thinkingBudget` |
| 用量字段 | `input_tokens` / `output_tokens` | `prompt_tokens` / `completion_tokens` | `input_tokens` / `output_tokens` | `usageMetadata.promptTokenCount` / `candidatesTokenCount` |

差异全在适配层里处理，选一个协议，请求体、认证头、用量字段解析都自动完成。

选哪个看端点暴露的路径：`/v1/messages` 选 Anthropic，`/v1/chat/completions` 选 Chat，`/v1/responses` 选 Responses，`:generateContent` 选 Gemini。地址里带 `anthropic`、`claude` 或 `gemini` 时会自动切过去。

想自己验证这四套确实不一样，跑 `python scripts/protocol_probe.py`。它起一个本地 mock 端点，把每种协议真实发出的 HTTP 请求抓下来打印。

### 认证字段

凭证放在哪个请求头和协议是两回事。同一个协议在不同网关下可能要求不同的头，所以分开配置：

| 认证字段 | 实际发送 | 常见于 |
|---|---|---|
| `Authorization: Bearer` | `Authorization: Bearer <key>` | OpenAI 系；Anthropic 生态里对应 `ANTHROPIC_AUTH_TOKEN` |
| `x-api-key` | `x-api-key: <key>` | Anthropic 系；对应 `ANTHROPIC_API_KEY` |
| `x-goog-api-key` | `x-goog-api-key: <key>` | Google Gemini |

切换协议时会自动带上该协议的惯用字段，之后仍可手动改。密钥留空就不发凭证头，所以不需要单独的「不认证」选项。

### Base URL 归一化

```
https://api.example.com                     → https://api.example.com/v1/chat/completions
https://api.example.com/v1                  → https://api.example.com/v1/chat/completions
https://api.example.com/v1/chat/completions → https://api.example.com/v1/chat/completions
https://api.example.com/api/paas/v4         → https://api.example.com/api/paas/v4/chat/completions
https://generativelanguage.googleapis.com   → https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent
```

填根地址就行，版本段自动补，粘贴完整端点也能救回来。

---

## 配置源

### 多配置

可以存任意多套配置（不同服务商、不同端点、不同额度规则），下拉切换，导出导入 JSON。复制配置走服务端接口，密钥和模型一起带过去。

### 密钥的三种来源

| 来源 | 说明 |
|---|---|
| 直接填写 | 明文存进本地 `config.json` |
| 环境变量 | 只存变量名，运行时读取，密钥不落盘 |
| 文件路径 | 只存路径，运行时读文件内容 |

密钥在所有 API 响应里都脱敏成 `sk-abc********wxyz`，前端拿不到明文。三种来源各自独立存值，切换不会把上一份内容带过去。

### 环境变量注入

不改配置文件也能起一个临时配置，适合容器和快速试用：

```bash
export TOKENFURNACE_BASE_URL=https://api.example.com/v1
export TOKENFURNACE_API_KEY=sk-xxxx
export TOKENFURNACE_MODEL=model-a,model-b
export TOKENFURNACE_PROTOCOL=anthropic      # 可选
python run.py
```

配置文件路径可以用 `TOKENFURNACE_CONFIG` 覆盖。

---

## 模型选择

点「拉取模型」拿到端点的全量列表，逐个勾选。每个模型可以单独设**权重**（分流概率）和**积分/1K**（换算系数，留空用全局值）。面板底部实时显示「本次将消耗 N 个模型」。

两个细节值得说：

- 批量按钮只作用于**当前筛选出来的**模型。列表可能有几百个，无差别全选会把不想消耗的也带上。先用搜索框筛，再点「勾选可见」。
- 列表里没有的模型可以**手动填 ID**。有些网关的 `/models` 只返回一部分，手动填不受列表限制。

没有「只留某个模型」这类一键预设。

---

## 多配置并发

在「⑤ 批量启动」里勾选多个配置，一次全部启动，不用来回切换。

- 每个会话用各自的密钥、模型、额度和换算系数
- 策略参数（模式、并发、输入规模、预算）取当前表单那一份，所有会话共用
- 起不来的配置会被直接禁用并写明原因（没填 Base URL / 没密钥 / 没勾模型），勾不上就不会误选
- 启动结果留在页面上，不只弹一个会消失的提示

看板顶部是所有会话的合计（速率、tokens、请求数逐点相加），下方「运行中的会话」逐个列出，可以单独停止。

滚动窗口是全局共享的。任一会话把额度跑满，所有会话一起等，因为平台额度本来就是共享的。全局 worker 上限默认 32，超出会拒绝并提示当前占用。

积分是逐请求落库的，滚动窗口本来就是全表求和，所以多个会话不会互相污染记账。

停止会话时**在途请求无法中断**（urllib 阻塞在 recv 上，Python 也没法强杀线程），状态会显示「正在停止（等在途请求结束）」。这些请求完成后仍会被记账。

配置 id 用严格查找。传一个不存在的 id 会明确报错，不会静默回退到别的配置，否则你以为在跑配置 X，实际烧的是配置 A 的额度。

---

## 消费策略

### 三种模式

| 模式 | 做法 | 适用 |
|---|---|---|
| 预填充（默认） | 超大输入 + 极短输出 | 最快，消耗效率最高 |
| 生成 | 小输入 + 长输出 | 模拟真实生成场景 |
| 混合 | 预填充为主，穿插生成 | token 构成更自然 |

默认预填充是因为实测差得多。同一模型下：

| 路径 | 实测吞吐 |
|---|---|
| 预填充（大输入 + 极短输出） | 9,000 ~ 18,600 tok/s |
| 纯生成（小输入 + 长输出） | ~111 tok/s |

差约 80 倍。预填充每次塞 38 万字符随机英文（约 18 万 tokens），模型只回两个字。

### 关键参数

| 参数 | 默认 | 说明 |
|---|---|---|
| 并发数 | 4 | 实测比较平衡；再高容易触发限流 |
| 单请求输入 | 380000 字符 | 约 18 万 tokens，接近但不越过上下文上限 |
| 最大输出 | 8 | 预填充模式下保持很小 |
| 思考强度 | `none` | 关掉最快 |
| 击穿缓存 | 开 | 见下 |

### 缓存击穿

复用同一段前缀，很多平台会走 prefix cache 并按折扣计费，你以为在消耗额度，实际只花了一点点。TokenFurnace 每次请求都重新生成随机文本，实测 `cached_tokens` 恒为 0。控制台里有这个指标，正常应该一直是 0。

### 429 不一定等于额度耗尽

有些平台触发每秒请求数限流时返回的是：

```json
{"error": {"message": "rps exhausted", "type": "quota_exceeded_error"}}
```

`type` 写着 `quota_exceeded_error`，但 `message` 是 rps，这是限流，可以重试。按 `type` 判断会在还有额度的时候误判成额度耗尽直接停机，所以 TokenFurnace 按 `message` 分类：

| 类型 | 触发 | 处理 |
|---|---|---|
| `rps` / `server` | 限流、服务端 5xx | 本会话所有线程一起退避，避免并发把限流窗口踩满 |
| `timeout` / `network` | 单条连接卡住 | 只让当前线程退避，其余线程继续跑 |
| `quota` | 真额度耗尽 | 停机，并反推换算系数 |
| `auth` | 401 / 403 | 停机并提示检查密钥 |
| `bad_request` | 400 / 404 / 422 | 记录后跳过；404 会额外提示检查协议 |

超时按实测延迟自适应（最近延迟中位数 ×3，下限 30 秒，不超过你设的上限）。固定 180 秒意味着一条挂住的连接要占住一个 worker 三分钟，而正常请求只要 25 秒。

---

## 实时速率

主数字是即时速率，统计窗口可以在 5 / 10 / 20 / 60 秒之间切换。

窗口会被**自动放宽**。预填充模式下单个请求要 20~40 秒才完成，5 秒窗口里经常一次完成都没有，主数字就会长期显示 `0.0`。实际使用的窗口会自动放宽到至少覆盖最近两次完成，界面下方会标注出来。你选的窗口是灵敏度下限，只放宽不收窄。

会话卡片上还显示**单请求实测耗时**，这是判断「谁的问题」最快的方法：

| 现象 | 结论 |
|---|---|
| 平均速率掉了，单请求耗时没变 | 本地问题：并发太低、撞了滚动窗口、或有线程在退避 |
| 单请求耗时明显变长（25 秒 → 115 秒） | 服务端拥塞，本地怎么调都救不回来 |

### 提速清单

1. **多开配置**，收益最大。勾选多个配置同时跑，总吞吐相加。
2. **提高并发**，默认 4，可以试 6~8。请求大部分时间在等服务端，并发越高吞吐越高，直到撞上限流。
3. **超时上限别设太大**。已自适应，但你的设定就是天花板。
4. **别关缓存击穿**。关掉后请求会命中 prefix cache，平台按折扣计费。
5. **输入规模不要盲目调小**。每请求 token 更多意味着更少的分摊开销，除非你怀疑平台对长请求有限时时间。

---

## 额度管理与自动刹车

### 四维预算

任一触顶即自动停止，`0` 表示不限：积分上限、token 上限、请求数上限、运行时长。

### 滚动窗口限流

本地记账推算 5 小时和 7 天两个滑动窗口。窗口内用量逼近上限时，工具算出最早一条记录还要多久滑出窗口，然后精确等待，不盲目轮询。

打开挂机模式后，撞额度会一直等下去并自动续跑。

> **注意计额口径。** 平台的通用积分池和 Flash-Lite 专属积分池各自独立计额，而本地账本只记一个合并口径。
> 一直用 Flash-Lite 刷积分时两者一致；一旦混入按通用积分计费的模型，窗口占用就和平台的真实口径对不上了，
> 需要自己留余量。

### 换算系数

如果平台按积分而不是 token 计费，填一个换算系数（积分/1K tokens），工具就能显示实时积分消耗、按积分上限刹车、按 5h/周积分额度限流。

拿到这个系数有两条路：

1. **手动**：看平台账单差额，`系数 = 消耗积分 × 1000 ÷ 消耗 tokens`
2. **自动**：点界面上的「反推」，用当前窗口已消耗的 token 数反推。或者等平台真的返回额度耗尽，此时工具会在日志里打印系数

反推的前提是该窗口额度确实被打满过。如果只是跑了很久还没报错，得到的只是上界，不是真值。

---

## 界面说明

### 控件

分段控件用于协议、认证字段、消耗模式、密钥来源、图表时间范围。数值输入带步进按钮，步长按字段设定（输入规模一次跳 2 万字符）。布尔项用开关，副标题说明后果。

### 看板

- **速率主卡**：即时速率、平均、峰值、吞吐，右侧是最近一段时间的迷你图。数字按速率分档配色
- **趋势图**：绿色面积是瞬时速率，蓝色虚线是累计 tokens，双 Y 轴，悬停显示精确值
- **滚动窗口占用**：5 小时和周两个进度条，超过 75% 变黄、92% 变红
- **运行日志**：彩色分级，自动滚到底
- **历史会话**：可点「明细」看按模型和按错误的拆分

---

## REST API

服务起来后可以直接调，方便脚本化：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state` | 全量状态（配置、实时指标、日志、历史） |
| GET | `/api/run/detail?run=<id>` | 单会话明细（按模型/错误拆分、时间桶） |
| POST | `/api/profile/save` | 新建或更新配置 |
| POST | `/api/profile/activate` | 切换当前配置 |
| POST | `/api/profile/duplicate` | 复制配置（连密钥一起） |
| POST | `/api/profile/delete` | 删除配置 |
| POST | `/api/profile/import` | 导入配置 |
| POST | `/api/config/engine` | 更新引擎默认参数 |
| POST | `/api/config/window` | 调整即时速率统计窗口（秒），立即生效 |
| POST | `/api/test` | 连通性自检（拉模型 + 打一个最小请求） |
| POST | `/api/models` | 拉取模型列表 |
| POST | `/api/guess_protocol` | 按 base_url 猜协议 |
| POST | `/api/run/start` | 启动一个会话 |
| POST | `/api/run/start_batch` | 批量启动多个配置，逐个返回成功/失败 |
| POST | `/api/run/stop` | 停止会话；带 `sid` 停单个，不带则全部停 |
| POST | `/api/estimate` | 按参数预估消耗 |
| POST | `/api/calibrate` | 手动算换算系数 |
| POST | `/api/calibrate/auto` | 按窗口反推系数 |
| GET | `/api/export/requests.csv` | 请求明细 CSV |
| GET | `/api/export/runs.csv` | 会话汇总 CSV |
| GET | `/api/export/config.json` | 配置导出 |

---

## 数据与文件

```
tokenfurnace/
├── run.py                    启动器
├── tokenfurnace/
│   ├── cli.py                命令行入口
│   ├── config.py             配置源、Profile、密钥解析
│   ├── providers.py          四协议适配、错误分类、URL 归一化
│   ├── engine.py             消费引擎（多会话 / 限流 / 预算 / 滚动窗口）
│   ├── store.py              SQLite 记账层
│   ├── server.py             REST API + 静态服务
│   └── web/                  前端（原生 JS，无框架无构建）
├── docs/preview.html         界面静态预览（自包含，可直接打开）
├── scripts/
│   ├── protocol_probe.py     实证四种协议的真实 HTTP 差异（离线）
│   ├── smoke_ci.py           进程内起服务并断言接口形状（CI 用）
│   ├── smoke_live.py         对已运行的服务做端到端联调
│   ├── build_preview.py      重新生成 docs/preview.html
│   └── check_batch_ui.js     用 jsdom 跑真实前端代码（需 npm i jsdom）
├── tests/test_core.py        90 个离线单元测试
├── config.example.json       配置模板
└── data/tokenfurnace.db      SQLite 记账库（自动生成，已 gitignore）
```

用 SQLite 而不是日志文件，是因为滚动窗口统计要走索引（几十万条记录也不慢）、历史查询一句 SQL 就够、断电不会写坏（WAL + 逐条提交）。

明细默认保留 30 天，启动时清理并 `ANALYZE`，避免库无限膨胀拖慢窗口查询。

---

## 开发与测试

```bash
python -m unittest discover -s tests -v     # 90 个测试，全部离线
python -m compileall -q tokenfurnace run.py # 语法检查
```

测试覆盖 URL 归一化、错误分类（含 rps 伪装成 quota 的回归用例）、四种协议的请求组装与响应解析、密钥三种来源、配置迁移与导入、SQLite 窗口记账、即时速率与自适应窗口、多会话聚合、预算刹车。

CI 只跑三个任务：Ubuntu × Python 3.9（声明的最低版本）、Ubuntu × 3.13、Windows × 3.13（主要用户平台）。要防的真实风险就这两个：3.9 的兼容性作者本地测不到，Windows 是路径和编码问题最容易暴露的地方。macOS 对纯标准库项目不提供额外信息，所以没开。

每个任务跑语法检查、90 个单元测试、协议差异实证和一次冒烟测试（在进程内起服务、打 API、断言、关掉）。

四个脚本可以复现关键结论：

| 脚本 | 用途 |
|---|---|
| `scripts/protocol_probe.py` | 起本地 mock 端点，抓四种协议的真实 HTTP 请求 |
| `scripts/smoke_ci.py` | 进程内起服务并断言接口形状，CI 用的就是这个 |
| `scripts/build_preview.py` | 用真实前端文件生成 `docs/preview.html` |
| `scripts/check_batch_ui.js` | 用 jsdom 跑真实前端代码，验证交互行为（需 `npm i jsdom`） |

---

## 常见问题

**报 404 / 400，提示路径不存在？**
协议选错了。看端点实际暴露的是 `/chat/completions`、`/responses`、`/messages` 还是 `:generateContent`。

**一直 429？**
并发开太高，降到 4 或更低。工具会自动退避，但降并发才是根治。

**`cached_tokens` 不是 0？**
有请求命中了 prefix cache。确认「击穿缓存」是开着的。

**实时速率显示 0.0？**
看下方有没有「已自动放宽到 N 秒」。如果正在跑，说明窗口被放宽了；如果空闲，那确实是 0。

**能同时跑多个配置吗？**
能。在「批量启动」里勾选多个，或者切配置后各点一次「用当前配置开始消费」。滚动窗口是全局的，任一会话跑满，所有会话一起等。

**能暴露到公网吗？**
默认只绑 `127.0.0.1`。`--host 0.0.0.0` 本身不带鉴权，要远程访问请自己套反向代理加认证。

---

## 免责声明

本工具是通用 API 客户端，不针对任何特定平台。请自行确认使用方式符合所用服务商的条款。作者不对因使用本工具导致的账号、额度或费用问题负责。

## License

[MIT](LICENSE)
