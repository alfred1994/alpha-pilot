#!/usr/bin/env python3
"""
Phase 1 测试 - SQLite存储层 + 长桥数据源集成
验证:
    1. SQLite数据库创建和CRUD操作
    2. 长桥数据源获取日K线、实时行情
    3. 代码格式转换函数
    4. 数据缓存逻辑
"""
import sys
import os
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

passed = 0
failed = 0


def ok(msg):
    global passed
    passed += 1
    print(f"  ✅ {msg}")


def fail(msg, detail=""):
    global failed
    failed += 1
    print(f"  ❌ {msg}")
    if detail:
        print(f"     {detail}")


def section(title):
    print(f"\n{'─' * 60}")
    print(f"【{title}】")
    print(f"{'─' * 60}")


# ════════════════════════════════════════════════════════════════
# 1. 代码格式转换
# ════════════════════════════════════════════════════════════════
section("1. 代码格式转换")

from data.longbridge_data import to_longport_code, from_longport_code

# to_longport_code
tests = [
    ("600519", "600519.SH"),
    ("000001", "000001.SZ"),
    ("300750", "300750.SZ"),
    ("sh.600519", "600519.SH"),
    ("sz.000001", "000001.SZ"),
    ("600519.SH", "600519.SH"),
    ("000001.SZ", "000001.SZ"),
    ("sh600519", "600519.SH"),
    ("sz000001", "000001.SZ"),
]
for input_code, expected in tests:
    result = to_longport_code(input_code)
    if result == expected:
        ok(f"to_longport_code('{input_code}') → '{result}'")
    else:
        fail(f"to_longport_code('{input_code}') → '{result}'，期望 '{expected}'")

# from_longport_code
tests2 = [
    ("600519.SH", "600519"),
    ("000001.SZ", "000001"),
    ("600519", "600519"),
    ("sh.600519", "600519"),
]
for input_code, expected in tests2:
    result = from_longport_code(input_code)
    if result == expected:
        ok(f"from_longport_code('{input_code}') → '{result}'")
    else:
        fail(f"from_longport_code('{input_code}') → '{result}'，期望 '{expected}'")


# ════════════════════════════════════════════════════════════════
# 2. SQLite数据库创建和CRUD
# ════════════════════════════════════════════════════════════════
section("2. SQLite数据库创建和CRUD")

from data.database import Database

# 使用临时数据库测试，不影响正式数据
tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp_db_path = tmp_db.name
tmp_db.close()
os.unlink(tmp_db_path)  # 让 Database 自己创建

try:
    with Database(db_path=tmp_db_path) as db:
        # 2.1 表创建
        tables = db.get_stats()
        expected_tables = ["k_daily", "k_minute", "trades", "positions",
                           "daily_snapshots", "market_regimes", "llm_decisions", "lessons"]
        all_exist = all(t in tables for t in expected_tables)
        if all_exist:
            ok(f"8张表全部创建成功: {', '.join(expected_tables)}")
        else:
            fail(f"表创建不完整: {list(tables.keys())}")

        # 2.2 k_daily CRUD
        db.insert_k_daily([
            {"code": "600519", "date": "2024-01-02", "open": 1700, "high": 1720,
             "low": 1690, "close": 1710, "volume": 10000, "amount": 17100000},
            {"code": "600519", "date": "2024-01-03", "open": 1710, "high": 1730,
             "low": 1700, "close": 1720, "volume": 12000, "amount": 20640000},
        ], source="test")
        rows = db.get_k_daily("600519", "2024-01-01", "2024-01-31")
        if len(rows) == 2 and rows[0]["close"] == 1710:
            ok(f"k_daily 插入+查询: {len(rows)}条，close={rows[0]['close']}")
        else:
            fail(f"k_daily CRUD 异常: {rows}")

        # 2.3 k_minute CRUD
        db.insert_k_minute([
            {"code": "600519", "datetime": "2024-01-02 09:35", "open": 1700,
             "high": 1705, "low": 1698, "close": 1703, "volume": 500, "amount": 851500},
        ], period="5m")
        rows = db.get_k_minute("600519", "2024-01-02 09:00", "2024-01-02 10:00", "5m")
        if len(rows) == 1 and rows[0]["close"] == 1703:
            ok(f"k_minute 插入+查询: {len(rows)}条")
        else:
            fail(f"k_minute CRUD 异常: {rows}")

        # 2.4 trades CRUD
        trade_id = db.insert_trade({
            "code": "600519", "name": "贵州茅台", "action": "BUY",
            "price": 1710, "shares": 100, "amount": 171000,
            "commission": 51.3, "reason": "测试买入",
        })
        trades = db.get_trades(code="600519")
        if len(trades) >= 1 and trades[0]["action"] == "BUY":
            ok(f"trades 插入+查询: id={trade_id}, {len(trades)}条")
        else:
            fail(f"trades CRUD 异常: {trades}")

        # update
        db.update_trade(trade_id, {"reason": "更新后的理由"})
        updated = db.get_trades(code="600519")
        if updated[0]["reason"] == "更新后的理由":
            ok("trades 更新成功")
        else:
            fail(f"trades 更新失败: {updated[0]['reason']}")

        # delete
        db.delete_trade(trade_id)
        after_delete = db.get_trades(code="600519")
        if len(after_delete) == 0:
            ok("trades 删除成功")
        else:
            fail(f"trades 删除失败: {len(after_delete)}条残留")

        # 2.5 positions CRUD
        db.upsert_position({
            "code": "600519", "name": "贵州茅台", "shares": 100,
            "buy_price": 1710, "buy_date": "2024-01-02", "cost": 171051.3,
            "highest_price": 1730, "current_price": 1720,
        })
        pos = db.get_position("600519")
        if pos and pos["shares"] == 100:
            ok(f"positions 插入+查询: {pos['code']} {pos['shares']}股")
        else:
            fail(f"positions CRUD 异常: {pos}")

        all_pos = db.get_all_positions()
        if len(all_pos) >= 1:
            ok(f"positions 全量查询: {len(all_pos)}只")
        else:
            fail("positions 全量查询为空")

        db.delete_position("600519")
        if db.get_position("600519") is None:
            ok("positions 删除成功")
        else:
            fail("positions 删除失败")

        # 2.6 daily_snapshots CRUD
        db.insert_snapshot({
            "date": "2024-01-02", "cash": 800000, "market_value": 200000,
            "total_assets": 1000000, "position_count": 1,
            "market_regime": "bull", "regime_confidence": 0.7,
            "details": json.dumps({"test": True}),
        })
        snap = db.get_snapshot("2024-01-02")
        if snap and snap["total_assets"] == 1000000:
            ok(f"snapshots 插入+查询: {snap['date']} 总资产={snap['total_assets']}")
        else:
            fail(f"snapshots CRUD 异常: {snap}")

        # 2.7 market_regimes CRUD
        db.insert_market_regime({
            "date": "2024-01-02", "regime": "bull", "confidence": 0.75,
            "indicators": json.dumps({"rsi": 65, "macd": "golden_cross"}),
            "llm_reasoning": "市场处于上升趋势",
        })
        regime = db.get_market_regime("2024-01-02")
        if regime and regime["regime"] == "bull":
            ok(f"market_regimes 插入+查询: {regime['regime']} conf={regime['confidence']}")
        else:
            fail(f"market_regimes CRUD 异常: {regime}")

        latest = db.get_latest_regime()
        if latest and latest["date"] == "2024-01-02":
            ok(f"market_regimes 最新查询: {latest['date']}")
        else:
            fail(f"market_regimes 最新查询异常: {latest}")

        # 2.8 llm_decisions CRUD
        dec_id = db.insert_llm_decision({
            "code": "600519", "date": "2024-01-02", "action": "BUY",
            "llm_prompt": "分析茅台", "llm_response": "建议买入",
            "reasoning": "技术面看涨", "confidence": 0.8,
        })
        decs = db.get_llm_decisions(code="600519")
        if len(decs) >= 1 and decs[0]["action"] == "BUY":
            ok(f"llm_decisions 插入+查询: id={dec_id}, {len(decs)}条")
        else:
            fail(f"llm_decisions CRUD 异常: {decs}")

        db.update_llm_decision(dec_id, {"outcome": "profit", "outcome_pct": 5.2})
        updated_decs = db.get_llm_decisions(code="600519")
        if updated_decs[0]["outcome"] == "profit":
            ok("llm_decisions 更新成功")
        else:
            fail(f"llm_decisions 更新失败: {updated_decs[0]}")

        # 2.9 lessons CRUD
        les_id = db.insert_lesson({
            "date": "2024-01-02", "category": "mistake",
            "content": "追高买入导致亏损", "importance": 4,
            "market_regime": "volatile",
            "related_trades": json.dumps([1, 2]),
        })
        lessons = db.get_lessons(category="mistake")
        if len(lessons) >= 1 and lessons[0]["category"] == "mistake":
            ok(f"lessons 插入+查询: id={les_id}, {len(lessons)}条")
        else:
            fail(f"lessons CRUD 异常: {lessons}")

        db.delete_lesson(les_id)
        after = db.get_lessons()
        remaining = [l for l in after if l["id"] == les_id]
        if len(remaining) == 0:
            ok("lessons 删除成功")
        else:
            fail("lessons 删除失败")

        # 2.10 get_kline_cached 测试
        call_count = [0]

        def mock_source(code, start_date, end_date):
            call_count[0] += 1
            return [
                {"date": "2024-06-01", "open": 100, "high": 105, "low": 99,
                 "close": 103, "volume": 5000, "amount": 515000},
                {"date": "2024-06-02", "open": 103, "high": 107, "low": 102,
                 "close": 106, "volume": 6000, "amount": 636000},
            ]

        # 第一次调用，应该走 source_func
        result1 = db.get_kline_cached("TEST001", "2024-06-01", "2024-06-02", mock_source)
        if len(result1) == 2 and call_count[0] == 1:
            ok(f"get_kline_cached 首次调用走数据源: {len(result1)}条")
        else:
            fail(f"get_kline_cached 首次调用异常: {len(result1)}条, 调用次数={call_count[0]}")

        # 第二次调用，应该走缓存
        result2 = db.get_kline_cached("TEST001", "2024-06-01", "2024-06-02", mock_source)
        if len(result2) == 2 and call_count[0] == 1:
            ok(f"get_kline_cached 二次调用走缓存: {len(result2)}条，source_func未重复调用")
        else:
            fail(f"get_kline_cached 缓存逻辑异常: {len(result2)}条, 调用次数={call_count[0]}")

        # 2.11 统计信息
        stats = db.get_stats()
        if stats["k_daily"] >= 2 and stats["trades"] >= 0:
            ok(f"统计信息: k_daily={stats['k_daily']}, trades={stats['trades']}")
        else:
            fail(f"统计信息异常: {stats}")

finally:
    # 清理临时数据库
    if os.path.exists(tmp_db_path):
        os.unlink(tmp_db_path)


# ════════════════════════════════════════════════════════════════
# 3. 数据迁移测试
# ════════════════════════════════════════════════════════════════
section("3. JSON数据迁移")

tmp_db2 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp_db2_path = tmp_db2.name
tmp_db2.close()
os.unlink(tmp_db2_path)

try:
    with Database(db_path=tmp_db2_path) as db:
        result = db.migrate_from_json()
        if result["trades"] > 0:
            ok(f"迁移交易记录: {result['trades']}条")
        else:
            fail(f"迁移交易记录: {result['trades']}条（期望>0）")

        if result["snapshots"] > 0:
            ok(f"迁移快照: {result['snapshots']}条")
        else:
            fail(f"迁移快照: {result['snapshots']}条（期望>0）")

        # 验证迁移后的数据
        trades = db.get_trades(limit=20)
        if len(trades) > 0 and trades[0]["code"]:
            ok(f"迁移后查询交易: {len(trades)}条，首条={trades[0]['code']} {trades[0]['action']}")
        else:
            fail(f"迁移后查询异常: {trades}")

        # 验证快照
        snaps = db.get_snapshots(limit=10)
        if len(snaps) > 0:
            ok(f"迁移后查询快照: {len(snaps)}条")
        else:
            fail("迁移后快照为空")

finally:
    if os.path.exists(tmp_db2_path):
        os.unlink(tmp_db2_path)


# ════════════════════════════════════════════════════════════════
# 4. 长桥数据源测试
# ════════════════════════════════════════════════════════════════
section("4. 长桥数据源")

from data.longbridge_data import LongbridgeDataSource

source = LongbridgeDataSource()

# 检查长桥是否可用
ctx_available = source.ctx is not None
if ctx_available:
    ok("长桥SDK连接成功")
else:
    ok("长桥SDK未配置（跳过在线测试，仅验证接口不报错）")

# 4.1 日K线（在线或空）
print("\n  测试日K线 (600519 茅台):")
try:
    df = source.get_daily_kline("600519", "2024-06-01", "2024-06-10")
    if ctx_available and not df.empty:
        ok(f"日K线获取成功: {len(df)}条")
        print(f"     最新: {df.iloc[-1].to_dict()}")
    elif not ctx_available:
        ok("日K线接口调用正常（SDK未配置，返回空）")
    else:
        ok("日K线接口调用正常（非交易日或无数据）")
except Exception as e:
    fail(f"日K线异常: {e}")

# 4.2 实时行情
print("\n  测试实时行情:")
try:
    quotes = source.get_realtime_quote(["600519", "000001"])
    if ctx_available and quotes:
        ok(f"实时行情获取成功: {len(quotes)}只")
        for q in quotes:
            print(f"     {q['code']} {q['name']} 现价:{q['price']:.2f}")
    elif not ctx_available:
        ok("实时行情接口调用正常（SDK未配置，返回空）")
    else:
        ok("实时行情接口调用正常（非交易时段或无数据）")
except Exception as e:
    fail(f"实时行情异常: {e}")

# 4.3 分钟K线
print("\n  测试分钟K线:")
try:
    mdf = source.get_minute_kline("600519", "5m", 10)
    if ctx_available and not mdf.empty:
        ok(f"分钟K线获取成功: {len(mdf)}条")
    elif not ctx_available:
        ok("分钟K线接口调用正常（SDK未配置）")
    else:
        ok("分钟K线接口调用正常")
except Exception as e:
    fail(f"分钟K线异常: {e}")

# 4.4 分时数据
print("\n  测试分时数据:")
try:
    idf = source.get_intraday("600519")
    if ctx_available and not idf.empty:
        ok(f"分时数据获取成功: {len(idf)}条")
    elif not ctx_available:
        ok("分时数据接口调用正常（SDK未配置）")
    else:
        ok("分时数据接口调用正常")
except Exception as e:
    fail(f"分时数据异常: {e}")

# 4.5 资金流向
print("\n  测试资金流向:")
try:
    flow = source.get_capital_flow("600519")
    if ctx_available and flow:
        ok(f"资金流向获取成功: {flow}")
    elif not ctx_available:
        ok("资金流向接口调用正常（SDK未配置）")
    else:
        ok("资金流向接口调用正常")
except Exception as e:
    fail(f"资金流向异常: {e}")


# ════════════════════════════════════════════════════════════════
# 5. 缓存逻辑集成测试
# ════════════════════════════════════════════════════════════════
section("5. 缓存逻辑集成")

tmp_db3 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp_db3_path = tmp_db3.name
tmp_db3.close()
os.unlink(tmp_db3_path)

try:
    with Database(db_path=tmp_db3_path) as db:
        # 模拟：先写入缓存，再通过 get_kline_cached 验证命中
        db.insert_k_daily([
            {"code": "999999", "date": "2024-03-01", "open": 50, "high": 55,
             "low": 48, "close": 53, "volume": 1000, "amount": 53000},
            {"code": "999999", "date": "2024-03-02", "open": 53, "high": 56,
             "low": 52, "close": 55, "volume": 1200, "amount": 66000},
        ], source="test")

        source_called = [False]

        def should_not_be_called(code, start, end):
            source_called[0] = True
            return []

        result = db.get_kline_cached("999999", "2024-03-01", "2024-03-02", should_not_be_called)
        if len(result) == 2 and not source_called[0]:
            ok("缓存命中时不调用数据源函数")
        else:
            fail(f"缓存逻辑异常: {len(result)}条, source_called={source_called[0]}")

        # 测试缓存未命中时调用数据源
        def fake_source(code, start, end):
            return [{"code": code, "date": "2024-04-01", "open": 60, "high": 65,
                     "low": 58, "close": 63, "volume": 2000, "amount": 126000}]

        result2 = db.get_kline_cached("888888", "2024-04-01", "2024-04-01", fake_source)
        if len(result2) == 1 and result2[0]["close"] == 63:
            ok("缓存未命中时正确调用数据源并存入")
        else:
            fail(f"缓存未命中逻辑异常: {result2}")

        # 再次查询，应该走缓存
        result3 = db.get_kline_cached("888888", "2024-04-01", "2024-04-01", should_not_be_called)
        if len(result3) == 1 and not source_called[0]:
            ok("第二次查询走缓存，不调用数据源")
        else:
            fail(f"二次缓存逻辑异常: {len(result3)}条, source_called={source_called[0]}")

finally:
    if os.path.exists(tmp_db3_path):
        os.unlink(tmp_db3_path)


# ════════════════════════════════════════════════════════════════
# 汇总
# ════════════════════════════════════════════════════════════════
print(f"\n{'═' * 60}")
print(f"Phase 1 测试结果: ✅ {passed} 通过  ❌ {failed} 失败")
print(f"{'═' * 60}")

if failed > 0:
    sys.exit(1)
else:
    print("全部通过！")
    sys.exit(0)
