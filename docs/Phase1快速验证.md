# Phase 1 集成 - 快速验证

## 快速测试（无需真实API）

```bash
# 1. 测试模块导入
python -c "from data.a_stock_data import get_margin_detail, get_unlock_schedule, normalize_code; print('✓ 模块导入成功')"

# 2. 测试选股器增强（干跑）
python -c "from strategy.stock_picker import _get_financing_candidates; print('✓ 选股器增强成功')"

# 3. 测试K线降级
python -c "from data.kline_fallback import get_kline_with_fallback; print('✓ K线降级模块成功')"
```

## 真实数据测试（需要网络）

```bash
# 配置mootdx服务器（首次必须）
python -m mootdx bestip

# 测试融资融券（需要1.5秒，东财限流）
python -c "from data.a_stock_data import get_margin_detail; m=get_margin_detail('600519'); print(f'融资余额: {m[\"financing_balance\"]/1e8:.2f}亿' if m else '无数据')"

# 测试限售解禁
python -c "from data.a_stock_data import get_unlock_schedule; u=get_unlock_schedule('600519'); print(f'未来90天解禁: {len(u[\"upcoming_unlocks\"])}次' if u else '无数据')"

# 测试完整选股（慢，约2-5分钟）
python -c "from strategy.stock_picker import pick_stocks; cs=pick_stocks(top_n=3, use_financing=True); print([f'{c.code} {c.score:.0f}' for c in cs])"
```

## 集成到现有流程

### 方式1：盘前预热集成
```python
# scheduler/pipeline.py 的 prefetch() 中添加
def prefetch(candidate_codes: List[str] = None):
    # ... 原有逻辑
    
    # 新增：预取融资融券和解禁数据
    from data.a_stock_data import get_margin_detail, get_unlock_schedule
    for code in candidate_codes:
        try:
            margin = get_margin_detail(code)
            unlock = get_unlock_schedule(code, days_ahead=30)
            # 存入快照
        except:
            pass
```

### 方式2：选股时直接调用（推荐）
```python
# 盘前预热时启用新维度
python main.py --prefetch

# 会自动调用 pick_stocks(use_financing=True, use_unlock_alert=True)
```

### 方式3：K线降级自动化
```python
# data/history.py 添加降级逻辑
def get_daily(code, start_date, end_date):
    try:
        # 原有长桥逻辑
        return longbridge_get_daily(code, start_date, end_date)
    except:
        # 降级mootdx
        from data.kline_fallback import get_kline_with_fallback
        return get_kline_with_fallback(code, start_date, end_date)
```

## 注意事项

1. **mootdx首次运行慢**
   - 需要测试多个服务器选最快的
   - 运行 `python -m mootdx bestip` 一次即可
   - 配置文件保存在 `~/.mootdx/config.json`

2. **东财限流必须遵守**
   - 融资融券/解禁扫描默认限制50只
   - 每只间隔1.5秒 = 75秒总耗时
   - 不要在快链路（90秒预算）中使用

3. **解禁预警是负面信号**
   - 不要单独看 `_get_unlock_alert_candidates()` 返回值
   - 必须在 `_merge_candidates()` 中才生效
   - 负分会降低该股票的总分

## 效果预期

| 指标 | 改造前 | Phase 1后 |
|------|--------|----------|
| 选股维度 | 6个 | 8个 |
| K线数据源 | 1个 | 2个（主+备） |
| 故障恢复 | 无 | 自动降级 |
| 融资融券 | ❌ | ✅ |
| 解禁预警 | ❌ | ✅ |

## 故障排查

### 问题1：mootdx连接超时
```bash
# 手动指定服务器
python -m mootdx config --set server=218.108.50.178:7727
```

### 问题2：融资融券返回空
```
原因：部分股票没有融资融券业务
解决：正常现象，候选池会自动跳过
```

### 问题3：东财API 403
```
原因：请求过快被封IP
解决：调大 EM_MIN_INTERVAL（默认1.5秒，可改到2秒）
```
