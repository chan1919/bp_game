# 期货基差表

以**最近一个已交割合约的最后交易日结算价**为基准（下称"交割价"），一览全部商品期货后续活跃合约的**基差**与**基差率**，并自动找出每个品种中 |基差率| 最大的合约，方便筛选基差矛盾最大的品种和合约。

在线地址：<https://chan1919.github.io/bp_game/>

- 静态站点，纯 HTML + 原生 JS，无外部依赖
- 数据由 GitHub Actions 每个交易日收盘后自动采集
- 只做商品期货：贵金属（黄金/白银/铂/钯）、中金所（股指/国债）已剔除；集运指数（欧线）保留

## 基差定义

```
基差   = 交割价 - 合约最新结算价
基差率 = 基差 / 交割价 × 100%
```

即以"最近交割价"当作现货的代理价。基差为正 = 期货贴水（合约比交割价便宜），基差为负 = 期货升水。

## 页面

- **基差矛盾排行**：每个品种取 |基差率| 最大的合约，按基差率画柱状图；柱子外侧用竖排小字标品种代码（红柱上方、绿柱下方），红=正基差（期货贴水），绿=负基差（期货升水）；手机端可左右滑动查看，点击柱子直接定位到品种明细
- **一览表**：按最大矛盾从大到小排列，直接看到品种、矛盾合约、基差率、最新结算价、交割价，以及该品种全部后续合约的基差率
- **原地展开**：点品种行展开该品种的合约明细（最新结算、涨跌、基差、基差率、成交量、持仓量），不跳页
- **筛选**：品种名称/代码搜索、交易所切换、按 |基差率| 阈值过滤、全部展开/收起
- 价格为结算价，显示时**不使用千分位逗号**

## 数据来源与口径

| 项目 | 说明 |
| --- | --- |
| 行情源 | 新浪财经期货（`getDailyKLine` 日线、`getHQFuturesData` 挂牌合约） |
| 价格口径 | 日**结算价**（缺失时回退收盘价） |
| 交割价 | 最近已交割合约**最后一个仍有持仓的交易日**的结算价 |
| 僵尸合约 | 日成交量 < 50 **且** 持仓量 < 200 的合约剔除；两者任一达标即保留，避免误伤远月套保合约 |
| 僵尸品种 | 强麦、普麦、粳稻、早籼稻、晚籼稻、菜籽、动力煤、线材、纤维板、胶合板等已无成交品种不参与 |
| 窗口 | 历史明细窗口为最近 1 年（365 天）；一览页使用最新截面数据 |
| 更新频率 | 交易日 17:45（北京时间）左右 |

> 新浪对已交割合约会追加几根持仓为 0 的占位日线，价格失真，因此交割价取"最后一个持仓 > 0 的交易日"，而不是最后一根 K 线。

## 本地运行

```bash
pip install requests
python pipeline.py            # 生成 data/index.json 与 data/<品种>.json
python -m http.server 8000    # 打开 http://localhost:8000
```

`pipeline.py` 常用参数：

```bash
python pipeline.py --only JD,RB,M   # 只跑指定品种
python pipeline.py --limit 5        # 只跑前 5 个品种（调试）
python pipeline.py --workers 8      # 并发数
```

## 目录

```
index.html            页面
pipeline.py           数据采集与计算
data/index.json       品种索引 + 每个品种的合约阶梯（一览页直接读取）
data/<CODE>.json      单品种历史明细（prices/dates，备用）
```

## 排除名单

在 `pipeline.py` 顶部维护：

```python
EXCLUDE_EXCHANGE = {"cffex"}          # 中金所：股指 / 国债
EXCLUDE_PREFIX   = {"AU", "AG", "PT", "PD", ...}
```

`sina` 的品种表里那些已无成交的僵尸品种（动力煤、强麦、粳稻、线材、纤维板、胶合板等）也一并剔除。
集运指数（欧线，EC）属于商品指数期货，已保留，并归入上期能源。

维护细节见 [MAINTENANCE.md](MAINTENANCE.md)。

## 部署（一次性设置）

仓库里的 `index.html` + `data/` 就是整个站点，Actions 每天把新数据 commit 回来即可。

1. **Pages**：Settings → Pages → Build and deployment → Source 选 **GitHub Actions**
   （站点地址 `https://<用户名>.github.io/bp_game/`）
2. **Actions 权限**：Settings → Actions → General → Workflow permissions 选 **Read and write permissions**
   （否则机器人无法把数据 commit 回仓库）
3. **Wiki**（可选）：Wiki 标签页 → Create the first page → 标题 `Home`，
   把 [WIKI.md](WIKI.md) 的内容贴进去

> 工作流里的 `configure-pages` 带了 `enablement: true`，但 GITHUB_TOKEN 通常没有开启 Pages 的权限，
> 第一次还是手动点一下最稳；开启后 `pages.yml` 每次 push 都会自动发布。

两个工作流：

| 文件 | 作用 |
| --- | --- |
| `.github/workflows/daily_update.yml` | 交易日 17:45 跑 `pipeline.py`，19:30 兜底补跑，提交 `data/` 并直接部署 Pages |
| `.github/workflows/pages.yml` | 人工 push 到 main 后发布 Pages |

> GitHub 用 `GITHUB_TOKEN` 推送的 commit 不会触发其他 workflow，所以 `daily_update.yml` 在提交数据后会自己部署一次 Pages；`pages.yml` 主要负责人工 push 的部署。
> 如果当天一直没更新，可在 GitHub 仓库的 **Actions → Daily Basis Update → Run workflow** 手动触发一次。

---

数据来自新浪财经，仅供学习参考，不构成投资建议。
