# Phase 1 集成完成总结

## ✅ 已完成

### 1. 核心模块
- `data/a_stock_data.py` - a-stock-data核心接口封装
  - 融资融券明细
  - 限售解禁日历
  - mootdx K线备份
  - 东财统一限流防封

### 2. 选股器增强
- `strategy/stock_picker.py` 新增2个维度
  - `_get_financing_candidates()` - 融资融券候选（6→8维度）
  - `_get_unlock_alert_candidates()` - 解禁预警（负面信号）
  - `pick_stocks()` 参数增加 `use_financing` / `use_unlock_alert`

### 3. 多数据源降级
- `data/kline_fallback.py` - K线多源冗余
  - 优先：长桥API
  - 降级：mootdx TCP
  - 失败：抛异常

---

## 🎯 功能验证

### 测试命令
```bash
# 测试融资融券
python -c "from data.a_stock_data import get_margin_detail; print(get_margin_detail('600519'))"

# 测试限售解禁
python -c "from data.a_stock_data import get_unlock_schedule; print(get_unlock_schedule('600519'))"

# 测试mootdx（需要先配置服务器）
python -m mootdx bestip  # 自动选择最快服务器

# 测试选股器
python -c "from strategy.stock_picker import pick_stocks; print(len(pick_stocks(top_n=5)))"
```

---

## ⚠️ 已知问题

### 1. mootdx首次运行需要配置
```bash
# 自动选择最快服务器
python -m mootdx bestip

# 或手动指定
python -m mootdx config --set server=hq.sinajs.cn:7727
```

### 2. 融资融券/解禁扫描较慢
- 每只股票需要1.5秒（东财限流）
- 扫描50只 = 75秒
- 默认只扫描活跃股Top50

### 3. 解禁预警是负面信号
- 返回的候选分数是负数（-20分）
- merge时会降低该股票的总分
- 用于过滤有解禁风险的标的

---

## 📊 选股维度对比

| 维度 | 改造前 | Phase 1后 | 数据源 |
|------|--------|----------|--------|
| 涨停板 | ✅ | ✅ | 东财 |
| 龙虎榜 | ✅ | ✅ | 东财 |
| 北向资金 | ✅ | ✅ | 东财 |
| 业绩预增 | ✅ | ✅ | 东财 |
| 机构调研 | ✅ | ✅ | 东财 |
| 异动放量 | ✅ | ✅ | 东财 |
| **融资融券** | ❌ | ✅ | 东财（新增） |
| **解禁预警** | ❌ | ✅ | 东财（新增） |

**K线备份**：长桥 → mootdx降级 ✅

---

## 🚀 下一步（Phase 2）

1. 腾讯实时价备份（长桥WebSocket补充）
2. 大宗交易维度（新增）
3. 数据源质量监控

---

## 使用示例

### 选股（8维度）
```python
from strategy.stock_picker import pick_stocks

candidates = pick_stocks(
    top_n=10,
    use_financing=True,      # 启用融资融券
    use_unlock_alert=True,   # 启用解禁预警
)

for c in candidates:
    print(f"{c.code} {c.name} {c.score:.0f}分 {c.source}")
```

### K线降级
```python
from data.kline_fallback import get_kline_with_fallback

# 自动降级：长桥失败→mootdx
kline = get_kline_with_fallback("600519", "2026-05-01", "2026-06-11")
```

### 融资融券分析
```python
from data.a_stock_data import get_margin_detail

margin = get_margin_detail("600519")
print(f"融资余额: {margin['financing_balance']/1e8:.2f}亿")
print(f"趋势: {margin['trend']}")
```

### 解禁风险预警
```python
from data.a_stock_data import get_unlock_schedule

unlock = get_unlock_schedule("600519", days_ahead=90)
if unlock["has_major_unlock"]:
    print("⚠️ 未来90天有重大解禁")
    for u in unlock["upcoming_unlocks"]:
        print(f"  {u['date']} 解禁 {u['unlock_ratio']:.2%}")
```
