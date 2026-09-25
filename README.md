# 个人投资组合仪表盘

三账户持仓 + 退休目标跟踪看板。股价每日自动抓取，持仓数量由你维护。

## 日常怎么用

**方式一：双击 `update.bat`** —— 抓行情、算账、刷新 `dashboard.html`，然后打开看板。
本地默认生成**明文页面**，双击就能看，不需要密码。

**方式二：命令行**

```bash
python update.py              # 抓行情并刷新看板（本地明文）
python update.py --offline    # 不联网，用已有数据重画页面
python update.py --encrypt    # 加密生成，会询问密码并可选存入 secret.txt
python update.py --no-encrypt # 强制明文
python update.py --pack       # 把 holdings.json 加密成 holdings.json.enc
python update.py --unpack     # 把 holdings.json.enc 解回 holdings.json
```

## 文件说明

| 文件 | 作用 | 是否需要你改 | 进仓库 |
|---|---|---|---|
| `holdings.json` | 持仓、成本、账户现金与融资、退休目标、历史净值 | **要改**（买卖后） | ❌ 明文，绝不提交 |
| `holdings.json.enc` | 上面那份的密文版本 | 用 `--pack` 生成 | ✅ 只提交这个 |
| `update.py` | 抓行情 → 计算 → 生成页面 | 不用改 | ✅ |
| `crypto_lite.py` | 零依赖 AES-256-GCM + PBKDF2 | 不用改 | ✅ |
| `template.html` | 页面版式与渲染逻辑（含密码解锁层） | 想换样式才改 | ✅ |
| `dashboard.html` | 生成的看板，单文件自包含 | 自动生成 | ❌ 本地明文版 |
| `index.html` | 线上页面入口，由 Actions 从加密版复制而来 | 自动生成 | ✅ |
| `update.bat` | 双击运行入口 | 不用改 | ✅ |
| `secret.txt` | 本地保存的密码 | 首次 `--encrypt` 生成 | ❌ |
| `trade_calendar.json` | A 股交易日历（2026 年 242 个交易日），节假日据此跳过更新 | **每年更新一次** | ✅ |
| `.github/workflows/update.yml` | 每交易日 15:30 自动更新，休市跳过 | 不用改 | ✅ |

## 买卖之后怎么改

打开 `holdings.json`，只动这两处：

1. **`holdings` 数组** —— 每只股票的 `shares`（股数）和 `cost`（成本价）。
   代码规则：沪市（6 开头、688）`"market": 1`，深市（0、3 开头）`"market": 0`。
   成本价留 `null` 表示不统计盈亏（如送转股）。

2. **`accounts` 数组** —— 每个账户的 `cash`（可用现金）和 `margin_debt`（融资负债）。

改完运行一次 `update.bat` 即可，历史净值曲线会自动延续。
**如果已经发布到 GitHub，还要再跑一次 `python update.py --pack` 并 push**，
否则线上拿到的还是旧持仓。

## 报单格式：怎么说我就不用追问

一句话就能改完，照这个顺序给：

> **9月24日，主账户，买入望变电气(603191) 2000股，成交价 13.27，现金扣款**

### 必须给的 6 项

| # | 信息 | 为什么不能少 |
|---|---|---|
| 1 | **成交日期** | 决定历史净值曲线记在哪一天；当天买卖要写实际成交日 |
| 2 | **哪个账户** | 三个账户都可能有同一只票，成本价各不相同，缺了只能猜 |
| 3 | **买 / 卖** | 方向反了净资产会算错 |
| 4 | **股票代码**（6 位最保险） | 名称可能重名或改过名；代码同时决定沪市/深市 |
| 5 | **成交价** | 用于摊薄成本；市价单请给实际成交均价 |
| 6 | **数量（股）** | A 股按「股」报，不要报「手」或金额 |

### 必须给的第 7 项（买入时）

**这笔钱从哪来** —— 三种写法，影响的是杠杆而不是净资产：

- `现金扣款` → 该账户 `cash` 减少成交额，融资负债不变
- `融资买入` → `cash` 不变，该账户 `margin_debt` 增加成交额
- `卖出所得` → 卖出时默认记入账户现金；**如果要拿去还融资，请明说** `还融资`

### 可以不给的（我自己抓）

实时股价、当日收盘价、沪市/深市归属、历史净值自动回补、净资产与集中度重算。

### 几种容易漏的特殊情况

- **手续费**：不给就默认「忽略」（成本按成交价记）。要给就给总额，我会摊进成本价。
- **分红 / 送转股 / 配股**：股数和成本都会变，**必须单独告诉我**，不要只说"账户里多了点钱"。
- **银证转账**（入金、出金）：只动 `cash`，不影响持仓，说一声就行。
- **偿还融资**：只动 `margin_debt`，不影响持仓。
- **新股中签**：按上面的格式报，代码给新股代码。

### 一句话不够时的完整版

```
日期：2026-09-24
账户：主账户
操作：买入
股票：望变电气 603191
价格：13.27
数量：2000 股
资金：现金扣款
手续费：忽略（或填 7 元）
```

## 数据说明

- **行情来源**：东方财富 → 新浪 → 腾讯，三级自动降级，任一可用即可。
- **行情归属**：以 K 线最新交易日为准。盘中未结算时不会误记为当天。
- **历史补缺**：漏更新的交易日会自动用当日收盘价反推补回，曲线不会断。
- **净资产** = 持仓市值合计 + 可用现金合计 − 融资负债合计。
- **所需年化** 按「不再追加储蓄」口径计算，仅供参考。

## 发布到 GitHub（加密方案）

免费账号的 GitHub Pages 要求仓库 **public**，所以能进仓库的只有密文：
`holdings.json.enc`（加密持仓）+ `index.html`（加密页面）。
链接任何人都能打开，但**没有密码只看到一串密文**。

### 一次性设置

```bash
# 1. 设密码：询问一次并存入 secret.txt（该文件已排除在 git 之外）
python update.py --encrypt

# 2. 用这个密码把持仓打包成密文
python update.py --pack

# 3. 生成加密看板并作为线上入口
python update.py
cp dashboard.html index.html        # Windows 用 copy dashboard.html index.html

# 4. 确认没有明文文件混进来（这一步别跳过）
git status --short
#   只应看到：holdings.json.enc、index.html、update.py、crypto_lite.py、
#   template.html、update.bat、README.md、.gitignore、.github/
#   出现 holdings.json、dashboard.html、secret.txt 任意一个 = 立刻停下检查
```

然后：

5. 建一个 **Public** 仓库并推送（私有仓库开 Pages 需要付费版）。
6. 仓库 **Settings → Secrets and variables → Actions → New repository secret**，
   名字填 `DASHBOARD_PASSWORD`，值填你的密码。
7. 仓库 **Settings → Pages → Build and deployment → Source** 选
   `Deploy from a branch`，分支 `main`、目录 `/ (root)`，保存。
8. 拿到地址：`https://<你的用户名>.github.io/<仓库名>/`

之后 **Actions 会在每交易日 15:30（北京时间）自动跑**：解开加密持仓 → 抓行情 →
生成加密页面 → 把更新后的持仓重新加密回仓。也可以在 Actions 页手动 `Run workflow`。

### 休市不更新：法定节假日怎么处理的

定时任务本身只排了周一至周五，法定节假日（春节、清明、五一、端午、中秋、国庆等）
由 `trade_calendar.json` 拦掉——那个文件是 **2026 年 A 股全部 242 个交易日**的清单。

流程是：先跑 `python update.py --check-trading-day`，不是交易日就把后面所有步骤标成
`skipped`，**不解密持仓、不抓行情、不产生提交**，线上页面保持最近交易日的数据不变。

```
TODAY=2026-09-25
TRADING_DAY=false
PREV=2026-09-24
NEXT=2026-09-28
```

页面顶部也不会误报「数据陈旧」：只要数据截止日已经是最近一个交易日，就正常显示
「数据截止 XXXX-XX-XX（最近交易日），下一交易日 XXXX-XX-XX 收盘后自动更新」。

**本地跑**想复现同样行为：`python update.py --skip-if-closed`。

> ⚠️ **每年要更新一次日历**：`trade_calendar.json` 目前只覆盖 2026 年。
> 到了 2027 年若没更新，脚本会退化为「周一至周五」判断——元旦这类工作日假仍会跑一次
> （宁可多跑一次，也不能因为日历过期而永久停更），但春节/国庆长假会照常更新。
> 续期方法：拿到新一年的 A 股交易日历后，替换 `trading_days` 数组即可，格式不变。

### 安全须知

- 密码只存在于三个地方：你的脑子、本地 `secret.txt`（不提交）、GitHub Secrets。
  **没有找回机制**——密码忘了，仓库里的密文就永远解不开，只能重新录入持仓。
- 想换密码：改 `secret.txt` 和 GitHub Secret，再跑一次 `python update.py --pack` 并 push。
- 页面上的「记住密码」只写进浏览器 localStorage，勾了就别在公用设备上用。
- 加密参数：PBKDF2-HMAC-SHA256 20 万次迭代 → AES-256-GCM。纯标准库实现
  （`crypto_lite.py`），Actions 里不需要装任何包。
- 行情接口从境外（GitHub 的 runner 在美国）访问国内接口偶尔会失败，
  脚本遇失败会放弃本次更新、保留上次数据，不会写出错误数字。

## 其它托管方式

`dashboard.html` 是单文件自包含页面，任何静态托管都能直接放。
**明文版本只适合放内网或本机**，放到公网请先按上面的方式加密。
