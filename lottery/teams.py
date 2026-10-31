"""NBA 球队名称映射：竞彩中文简称 / ESPN 缩写 -> 统一键。"""

CN2KEY = {
    "老鹰": "Atlanta", "凯尔特人": "Boston", "篮网": "Brooklyn", "黄蜂": "Charlotte",
    "公牛": "Chicago", "骑士": "Cleveland", "独行侠": "Dallas", "掘金": "Denver",
    "活塞": "Detroit", "勇士": "GoldenState", "火箭": "Houston", "步行者": "Indiana",
    "快船": "LAClippers", "湖人": "LALakers", "灰熊": "Memphis", "热火": "Miami",
    "雄鹿": "Milwaukee", "森林狼": "Minnesota", "鹈鹕": "NewOrleans", "尼克斯": "NewYork",
    "雷霆": "OklahomaCity", "魔术": "Orlando", "76人": "Philadelphia", "太阳": "Phoenix",
    "开拓者": "Portland", "国王": "Sacramento", "马刺": "SanAntonio", "猛龙": "Toronto",
    "爵士": "Utah", "奇才": "Washington",
}
KEY2CN = {value: key for key, value in CN2KEY.items()}

ABBR2KEY = {
    "ATL": "Atlanta", "BOS": "Boston", "BKN": "Brooklyn", "CHA": "Charlotte",
    "CHI": "Chicago", "CLE": "Cleveland", "DAL": "Dallas", "DEN": "Denver",
    "DET": "Detroit", "GS": "GoldenState", "GSW": "GoldenState", "HOU": "Houston",
    "IND": "Indiana", "LAC": "LAClippers", "LAL": "LALakers", "MEM": "Memphis",
    "MIA": "Miami", "MIL": "Milwaukee", "MIN": "Minnesota", "NO": "NewOrleans",
    "NOP": "NewOrleans", "NY": "NewYork", "NYK": "NewYork", "OKC": "OklahomaCity",
    "ORL": "Orlando", "PHI": "Philadelphia", "PHX": "Phoenix", "PHO": "Phoenix",
    "POR": "Portland", "SAC": "Sacramento", "SA": "SanAntonio", "SAS": "SanAntonio",
    "TOR": "Toronto", "UTAH": "Utah", "UTA": "Utah", "WSH": "Washington", "WAS": "Washington",
}
