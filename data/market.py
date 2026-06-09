"""
市场数据模块 - 东方财富 (CloakBrowser)
比AKShare更稳定，无频率限制
功能: 涨停板、龙虎榜、融资融券、板块数据
"""
import pandas as pd
from datetime import datetime, timedelta
from data import logger

# 数据源选择: "eastmoney" 或 "akshare"
DATA_SOURCE = "eastmoney"


def get_limit_up(date: str = None) -> pd.DataFrame:
    """
    获取涨停板数据
    
    Args:
        date: 日期 "YYYYMMDD", 默认今天
    
    Returns:
        DataFrame: 代码, 名称, 涨跌幅, 最新价, 成交额, 连板数, 所属行业
    """
    if not date:
        date = datetime.now().strftime("%Y%m%d")
    
    if DATA_SOURCE == "eastmoney":
        return _get_limit_up_eastmoney(date)
    else:
        return _get_limit_up_akshare(date)


def _get_limit_up_eastmoney(date: str) -> pd.DataFrame:
    """东方财富涨停板"""
    from data.eastmoney import get_limit_up as em_get_limit_up
    
    try:
        stocks = em_get_limit_up(date)
        if not stocks:
            return pd.DataFrame()
        
        # 转换为DataFrame格式
        df = pd.DataFrame(stocks)
        df = df.rename(columns={
            "code": "代码",
            "name": "名称",
            "change_pct": "涨跌幅",
            "price": "最新价",
            "amount": "成交额",
            "consecutive": "连板数",
            "first_seal_time": "首次封板时间",
            "industry": "所属行业",
            "market_cap": "流通市值",
            "turnover": "换手率",
        })
        
        logger.info(f"涨停板 {date}: {len(df)}只 (东方财富)")
        return df
    except Exception as e:
        logger.error(f"东方财富涨停板失败: {e}")
        return pd.DataFrame()


def _get_limit_up_akshare(date: str) -> pd.DataFrame:
    """AKShare涨停板 (备用)"""
    import akshare as ak
    from data import rate_limit
    
    rate_limit("akshare", 2.5)
    try:
        df = ak.stock_zt_pool_em(date=date)
        logger.info(f"涨停板 {date}: {len(df)}只 (AKShare)")
        return df
    except Exception as e:
        logger.error(f"AKShare涨停板失败: {e}")
        return pd.DataFrame()


def get_limit_down(date: str = None) -> pd.DataFrame:
    """获取跌停板数据"""
    if not date:
        date = datetime.now().strftime("%Y%m%d")
    
    if DATA_SOURCE == "eastmoney":
        return _get_limit_down_eastmoney(date)
    else:
        return _get_limit_down_akshare(date)


def _get_limit_down_eastmoney(date: str) -> pd.DataFrame:
    """东方财富跌停板"""
    from data.eastmoney import get_limit_down as em_get_limit_down

    try:
        stocks = em_get_limit_down(date)
        if not stocks:
            return pd.DataFrame()

        df = pd.DataFrame(stocks)
        df = df.rename(columns={
            "code": "代码",
            "name": "名称",
            "change_pct": "涨跌幅",
            "price": "最新价",
            "amount": "成交额",
            "industry": "所属行业",
            "market_cap": "流通市值",
            "turnover": "换手率",
        })

        logger.info(f"跌停板 {date}: {len(df)}只 (东方财富)")
        return df
    except Exception as e:
        logger.error(f"东方财富跌停板失败: {e}")
        return pd.DataFrame()


def _get_limit_down_akshare(date: str) -> pd.DataFrame:
    """AKShare跌停板"""
    import akshare as ak
    from data import rate_limit
    
    rate_limit("akshare", 2.5)
    try:
        df = ak.stock_zt_pool_dtgc_em(date=date)
        logger.info(f"跌停板 {date}: {len(df)}只")
        return df
    except Exception as e:
        logger.error(f"跌停板获取失败: {e}")
        return pd.DataFrame()


def get_dragon_tiger(date: str = None) -> pd.DataFrame:
    """
    获取龙虎榜数据（自动回退到最近有数据的交易日）
    
    Returns:
        DataFrame: 代码, 名称, 涨跌幅, 龙虎榜净买额, 上榜原因
    """
    if DATA_SOURCE == "eastmoney":
        return _get_dragon_tiger_eastmoney(date)
    else:
        return _get_dragon_tiger_akshare(date)


def _get_dragon_tiger_eastmoney(date: str = None) -> pd.DataFrame:
    """东方财富龙虎榜"""
    from data.eastmoney import get_dragon_tiger as em_get_dragon_tiger
    
    try:
        # 转换日期格式
        em_date = None
        if date:
            em_date = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
        
        stocks = em_get_dragon_tiger(em_date)
        if not stocks:
            return pd.DataFrame()
        
        # 转换为DataFrame
        df = pd.DataFrame(stocks)
        df = df.rename(columns={
            "code": "代码",
            "name": "名称",
            "date": "上榜日",
            "change_pct": "涨跌幅",
            "net_buy": "龙虎榜净买额",
            "reason": "上榜原因",
        })
        
        logger.info(f"龙虎榜: {len(df)}只 (东方财富)")
        return df
    except Exception as e:
        logger.error(f"东方财富龙虎榜失败: {e}")
        return pd.DataFrame()


def _get_dragon_tiger_akshare(date: str = None) -> pd.DataFrame:
    """AKShare龙虎榜"""
    import akshare as ak
    from data import rate_limit
    
    if not date:
        date = datetime.now().strftime("%Y%m%d")
    
    rate_limit("akshare", 2.5)
    try:
        df = ak.stock_lhb_detail_em(start_date=date, end_date=date)
        if df is None or df.empty:
            return pd.DataFrame()
        logger.info(f"龙虎榜 {date}: {len(df)}只 (AKShare)")
        return df
    except Exception as e:
        logger.warning(f"AKShare龙虎榜失败: {e}")
        return pd.DataFrame()


def get_margin(code: str = None) -> pd.DataFrame:
    """
    获取融资融券数据
    
    Args:
        code: 股票代码 (可选，不传则获取沪市汇总)
    
    Returns:
        DataFrame 或 dict
    """
    if DATA_SOURCE == "eastmoney" and code:
        return _get_margin_eastmoney(code)
    else:
        return _get_margin_akshare()


def _get_margin_eastmoney(code: str) -> dict:
    """东方财富融资融券"""
    from data.eastmoney import get_margin_data
    
    try:
        data = get_margin_data(code)
        if data:
            logger.info(f"融资融券 {code}: 余额{data.get('margin_balance', 0):,.0f}")
        return data
    except Exception as e:
        logger.error(f"东方财富融资融券失败: {e}")
        return {}


def _get_margin_akshare() -> pd.DataFrame:
    """AKShare融资融券"""
    import akshare as ak
    from data import rate_limit
    
    end_date = datetime.now().strftime("%Y%m%d")
    rate_limit("akshare", 2.5)
    try:
        df = ak.stock_margin_sse(start_date=end_date, end_date=end_date)
        logger.info(f"融资融券: {len(df)}条 (AKShare)")
        return df
    except Exception as e:
        logger.error(f"融资融券获取失败: {e}")
        return pd.DataFrame()


def get_stock_news(code: str) -> pd.DataFrame:
    """
    获取个股新闻
    
    Args:
        code: 股票代码, 如 "600519"
    
    Returns:
        DataFrame: 新闻标题, 新闻内容, 发布时间, 文章来源
    """
    if DATA_SOURCE == "eastmoney":
        return _get_stock_news_eastmoney(code)
    else:
        return _get_stock_news_akshare(code)


def _get_stock_news_eastmoney(code: str) -> pd.DataFrame:
    """东方财富个股新闻"""
    from data.eastmoney import get_stock_news as em_get_stock_news
    
    try:
        news = em_get_stock_news(code)
        if not news:
            return pd.DataFrame()
        
        df = pd.DataFrame(news)
        df = df.rename(columns={
            "title": "新闻标题",
            "content": "新闻内容",
            "time": "发布时间",
            "source": "文章来源",
            "url": "新闻链接",
        })
        
        logger.info(f"新闻 {code}: {len(df)}条 (东方财富)")
        return df
    except Exception as e:
        logger.error(f"东方财富新闻失败: {e}")
        return pd.DataFrame()


def _get_stock_news_akshare(code: str) -> pd.DataFrame:
    """AKShare个股新闻"""
    import akshare as ak
    from data import rate_limit
    
    rate_limit("akshare", 2.0)
    try:
        df = ak.stock_news_em(symbol=code)
        logger.info(f"新闻 {code}: {len(df)}条 (AKShare)")
        return df
    except Exception as e:
        logger.error(f"新闻获取失败: {e}")
        return pd.DataFrame()


def get_concept_boards() -> pd.DataFrame:
    """获取概念板块列表及涨跌"""
    import akshare as ak
    from data import rate_limit
    
    rate_limit("akshare", 2.5)
    try:
        df = ak.stock_board_concept_name_em()
        logger.info(f"概念板块: {len(df)}个")
        return df
    except Exception as e:
        logger.error(f"概念板块获取失败: {e}")
        return pd.DataFrame()


def get_industry_boards() -> pd.DataFrame:
    """获取行业板块列表及涨跌"""
    import akshare as ak
    from data import rate_limit
    
    rate_limit("akshare", 2.5)
    try:
        df = ak.stock_board_industry_name_em()
        logger.info(f"行业板块: {len(df)}个")
        return df
    except Exception as e:
        logger.error(f"行业板块获取失败: {e}")
        return pd.DataFrame()


def get_north_flow() -> dict:
    """
    获取北向资金数据（东方财富）

    Returns:
        dict: {net_flow: 净流入(亿), buy: 买入(亿), sell: 卖出(亿), date}
    """
    try:
        import asyncio
        import cloakbrowser

        async def _fetch():
            url = "https://push2.eastmoney.com/api/qt/kamt.rtmin/get?fields1=f1,f2,f3,f4&fields2=f51,f54,f52,f55,f58,f61,f59,f62,f57,f60,f164,f166,f168,f170,f172,f56,f53,f64,f63"
            browser = await cloakbrowser.launch_async(headless=True)
            try:
                page = await browser.new_page()
                await page.goto("https://quote.eastmoney.com/", wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(1)
                result = await page.evaluate(f'''async () => {{
                    const resp = await fetch("{url}");
                    return await resp.json();
                }}''')
                return result
            finally:
                await browser.close()

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(_fetch())
        finally:
            loop.close()

        if not result:
            return {}

        s2n = result.get("data", {}).get("s2n", [])
        # s2n 最后一行: 时间,沪股通净买,沪股通买,沪股通卖,深股通净买,深股通买,深股通卖
        if s2n:
            last = s2n[-1].split(",")
            if len(last) >= 5:
                sh_net = float(last[1]) if last[1] != "-" else 0
                sz_net = float(last[4]) if last[4] != "-" else 0
                return {
                    "net_flow": round((sh_net + sz_net) / 1e8, 2),
                    "sh_net": round(sh_net / 1e8, 2),
                    "sz_net": round(sz_net / 1e8, 2),
                    "date": datetime.now().strftime("%Y-%m-%d"),
                }
        return {}
    except Exception as e:
        logger.warning(f"北向资金获取失败: {e}")
        return {}


def get_margin_balance() -> dict:
    """
    获取两融余额（沪市汇总）

    Returns:
        dict: {date, margin_balance(亿), short_balance(亿)}
    """
    try:
        import akshare as ak
        from data import rate_limit

        rate_limit("akshare", 2.5)
        df = ak.stock_margin_sse(
            start_date=(datetime.now() - timedelta(days=7)).strftime("%Y%m%d"),
            end_date=datetime.now().strftime("%Y%m%d"),
        )
        if df is None or df.empty:
            return {}

        latest = df.iloc[-1]
        return {
            "date": str(latest.get("信用交易日期", ""))[:10],
            "margin_balance": round(float(latest.get("融资余额(元)", 0)) / 1e8, 2),
            "short_balance": round(float(latest.get("融券余量金额(元)", 0)) / 1e8, 2),
        }
    except Exception as e:
        logger.warning(f"两融余额获取失败: {e}")
        return {}


# 测试
if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    
    print("=" * 60)
    print(f"市场数据模块测试 (数据源: {DATA_SOURCE})")
    print("=" * 60)
    
    # 1. 涨停板
    print("\n[1] 涨停板:")
    df = get_limit_up()
    if len(df) > 0:
        cols = [c for c in ["代码", "名称", "涨跌幅", "连板数", "所属行业"] if c in df.columns]
        print(df[cols].head(10).to_string(index=False))
    
    # 2. 龙虎榜
    print("\n[2] 龙虎榜:")
    df = get_dragon_tiger()
    if len(df) > 0:
        cols = [c for c in ["代码", "名称", "涨跌幅", "龙虎榜净买额", "上榜原因"] if c in df.columns]
        print(df[cols].head(5).to_string(index=False))
    
    # 3. 个股新闻
    print("\n[3] 个股新闻:")
    df = get_stock_news("600519")
    if len(df) > 0:
        print(df[["文章来源", "新闻标题"]].head(3).to_string(index=False))
