# 期货基差表

以**最近一个已交割合约的最后交易日结算价**为基准（下称"交割价"），展示各品种后续全部活跃合约近一年的结算价走势，以及每个合约相对交割价的**基差**与**基差率**。

在线地址：<https://chan1919.github.io/bp_game/>

- 静态站点，纯 HTML + 原生 JS，无外部依赖
- 数据由 GitHub Actions 每个交易日收盘后自动采集
- 只做商品期货：贵金属（黄金/白银/铂/钯）、中金所（股指/国债）、集运指数等已剔除

## 基差定义

```
基差   = 交割价 - 合约最新结算价
基差率 = 基差 / 交割价 × 100%
```

即以"最近交割价"当作现货的代理价。基差为正 = 期货贴水（合约比交割价便宜），基差为负 = 期货升水。

## 页面

- **品种下拉**：按交易所分组，支持直接输入搜索
- **价格走势**：全部活跃合约近一年日结算价，灰色虚线为交割价
- **基差走势**：各合约历史基差（交割价 − 结算价），0 轴以上为期货贴水
- **期限结构**：横轴合约月份，纵轴基差率，一眼看清整条曲线
- **表格**：最新结算价、涨跌、基差、基差率、成交量、持仓量、近一年迷你走势，可点表头排序
- 图例可点击隐藏/显示单个合约，图表支持鼠标悬停 / 手指滑动查看当日各合约数值

## 数据来源与口径

| 项目 | 说明 |
| --- | --- |
| 行情源 | 新浪财经期货（`getDailyKLine` 日线、`getHQFuturesData` 挂牌合约） |
| 价格口径 | 日**结算价**（缺失时回退收盘价） |
| 交割价 | 最近已交割合约**最后一个仍有持仓的交易日**的结算价 |
| 窗口 | 最近 1 年（365 天） |
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
data/index.json       品种索引
data/<CODE>.json      单品种明细（按需加载）
```

## 排除名单

在 `pipeline.py` 顶部维护：

```python
EXCLUDE_EXCHANGE = {"cffex"}          # 中金所：股指 / 国债
EXCLUDE_PREFIX   = {"AU", "AG", "PT", "PD", ...}
```

`sina` 的品种表里那些已无成交的僵尸品种（动力煤、强麦、粳稻、线材、纤维板、胶合板等）也一并剔除。

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
| `.github/workflows/daily_update.yml` | 交易日 17:45 跑 `pipeline.py`，把 `data/` commit & push |
| `.github/workflows/pages.yml` | 每次 push 到 main 后发布 Pages |

---

数据来自新浪财经，仅供学习参考，不构成投资建议。
