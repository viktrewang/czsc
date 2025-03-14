# -*- coding: utf-8 -*-
"""
author: zengbin93
email: zeng_bin8888@163.com
create_dt: 2023/08/02 22:20
describe: 按持仓权重回测
"""
import numpy as np
import pandas as pd
import plotly.express as px
from tqdm import tqdm
from loguru import logger
from pathlib import Path
from deprecated import deprecated
from typing import Union, AnyStr, Callable
from multiprocessing import cpu_count
from concurrent.futures import ProcessPoolExecutor

import czsc
from czsc.traders.base import CzscTrader
from czsc.utils.io import save_json
from czsc.utils.stats import daily_performance, evaluate_pairs


def get_ensemble_weight(trader: CzscTrader, method: Union[AnyStr, Callable] = "mean"):
    """获取 CzscTrader 中所有 positions 按照 method 方法集成之后的权重

    函数计算逻辑：

    1. 获取 trader 持仓信息并转换为DataFrame:

        - 遍历交易者的每个持仓位置。
        - 将每个位置的持仓信息转换为DataFrame，并合并到一个整体的DataFrame中。
        - 将持仓列重命名为对应的位置名称。

    2. 根据给定的方法计算权重:

        - 如果方法是可调用对象，将持仓信息转换为字典，并传递给该方法进行计算。
        - 如果方法是预定义字符串（"mean"、"max"、"min"、"vote"），根据相应的计算方式计算权重。

    3. 返回包含日期、交易标的、权重和价格的DataFrame:

        - 将计算得到的权重与其他相关列一起组成一个新的DataFrame。
        - 将交易标的信息添加到新的DataFrame中。
        - 返回包含日期、交易标的、权重和价格的DataFrame副本。

    :param trader: CzscTrader
        缠论交易员对象
    :param method: str or callable

        集成方法，可选值包括：'mean', 'max', 'min', 'vote'
        也可以传入自定义的函数，函数的输入为 dict，key 为 position.name，value 为 position.pos, 样例输入：
            {'多头策略A': 1, '多头策略B': 1, '空头策略A': -1}

    :param kwargs:
    :return: pd.DataFrame
        columns = ['dt', 'symbol', 'weight', 'price']
    """
    logger.info(f"trader positions: {[p.name for p in trader.positions]}")

    dfp = pd.DataFrame()
    for p in trader.positions:
        p_pos = pd.DataFrame(p.holds)
        if dfp.empty:
            dfp = p_pos.copy()
        else:
            assert dfp["dt"].equals(p_pos["dt"])
            dfp = dfp.merge(p_pos[["dt", "pos"]], on="dt", how="left")
        dfp.rename(columns={"pos": p.name}, inplace=True)

    pos_cols = [c for c in dfp.columns if c not in ["dt", "weight", "price"]]
    if callable(method):
        dfp["weight"] = dfp[pos_cols].apply(lambda x: method(x.to_dict()), axis=1)
    else:
        method = method.lower()
        if method == "mean":
            dfp["weight"] = dfp[pos_cols].mean(axis=1)
        elif method == "max":
            dfp["weight"] = dfp[pos_cols].max(axis=1)
        elif method == "min":
            dfp["weight"] = dfp[pos_cols].min(axis=1)
        elif method == "vote":
            dfp["weight"] = dfp[pos_cols].apply(lambda x: np.sign(np.sum(x)), axis=1)
        else:
            raise ValueError(f"method {method} not supported")

    dfp["symbol"] = trader.symbol
    logger.info(f"trader weight decribe: {dfp['weight'].describe().round(4).to_dict()}")
    return dfp[["dt", "symbol", "weight", "price"]].copy()


def stoploss_by_direction(dfw, stoploss=0.03, **kwargs):
    """按持仓方向进行止损

    :param dfw: pd.DataFrame, columns = ['dt', 'symbol', 'weight', 'price'], 持仓权重数据，其中

        dt      为K线结束时间，必须是连续的交易时间序列，不允许有时间断层
        symbol  为合约代码，
        weight  为K线结束时间对应的持仓权重，品种之间的权重是独立的，不会互相影响
        price   为结束时间对应的交易价格，可以是当前K线的收盘价，或者下一根K线的开盘价，或者未来N根K线的TWAP、VWAP等

        数据样例如下：
        ===================  ========  ========  =======
        dt                   symbol      weight    price
        ===================  ========  ========  =======
        2019-01-02 09:01:00  DLi9001       0.5   961.695
        2019-01-02 09:02:00  DLi9001       0.25  960.72
        2019-01-02 09:03:00  DLi9001       0.25  962.669
        2019-01-02 09:04:00  DLi9001       0.25  960.72
        2019-01-02 09:05:00  DLi9001       0.25  961.695
        ===================  ========  ========  =======

    :param stoploss: 止损比例
    :param kwargs: 其他参数
    :return: pd.DataFrame，
        columns = ['dt', 'symbol', 'weight', 'raw_weight', 'price', 'returns',
                   'hold_returns', 'min_hold_returns', 'order_id', 'is_stop']
    """
    dfw = dfw.copy()
    dfw["direction"] = np.sign(dfw["weight"])
    dfw["raw_weight"] = dfw["weight"].copy()
    assert stoploss > 0, "止损比例必须大于0"

    rows = []
    for _, dfg in dfw.groupby("symbol"):
        assert isinstance(dfg, pd.DataFrame)
        assert dfg["dt"].is_monotonic_increasing, "dt 必须是递增的时间序列"
        dfg = dfg.sort_values("dt", ascending=True)

        # 按交易方向设置订单号
        dfg["order_id"] = dfg.groupby((dfg["direction"] != dfg["direction"].shift()).cumsum()).ngroup()

        # 按持仓权重计算收益
        dfg["n1b"] = dfg["price"].shift(-1) / dfg["price"] - 1
        dfg["returns"] = dfg["n1b"] * dfg["weight"]
        dfg["hold_returns"] = dfg["returns"].groupby(dfg["order_id"]).cumsum()
        dfg["min_hold_returns"] = dfg.groupby("order_id")["hold_returns"].cummin()

        # 止损：同一个订单下，min_hold_returns < -stoploss时，后续weight置为0
        dfg["is_stop"] = (dfg["min_hold_returns"] < -stoploss) & (dfg["order_id"] == dfg["order_id"].shift(1))
        c1 = dfg["is_stop"].shift(1) & (dfg["order_id"] == dfg["order_id"].shift(1))
        dfg.loc[c1, "weight"] = 0
        dfg["weight"] = np.where(c1, 0, dfg["weight"])
        rows.append(dfg.copy())

    dfw1 = pd.concat(rows, ignore_index=True)
    return dfw1


class WeightBacktest:
    """持仓权重回测

    飞书文档：https://s0cqcxuy3p.feishu.cn/wiki/Pf1fw1woQi4iJikbKJmcYToznxb

    更新日志：

    #### 20241125

    1. 新增 yearly_days 参数，用于指定每年的交易日天数，默认为 252。

    #### 20241205

    1. 新增 weight_type 参数，用于指定输入的持仓权重类别，ts 表示 time series，时序策略；。
    """

    version = "20241205"

    def __init__(self, dfw, digits=2, weight_type="ts", **kwargs) -> None:
        """持仓权重回测

        初始化函数逻辑：

        1. 将传入的kwargs保存在实例变量self.kwargs中。
        2. 复制传入的dfw到实例变量self.dfw。
        3. 检查self.dfw中是否存在空值，如果存在则抛出ValueError异常，并提示"dfw 中存在空值，请先处理"。
        4. 设置实例变量self.digits为传入的digits值。
        5. 从kwargs中获取'fee_rate'参数的值，默认为0.0002，并将其保存在实例变量self.fee_rate中。
        6. 将self.dfw中的 weight 列转换为浮点型，并保留self.digits位小数。
        7. 提取self.dfw中的唯一交易标的符号，并将其保存在实例变量self.symbols中。
        8. 执行backtest()方法进行回测，并将结果保存在实例变量self.results中。

        :param dfw: pd.DataFrame, columns = ['dt', 'symbol', 'weight', 'price'], 持仓权重数据，其中

            dt      为K线结束时间，必须是连续的交易时间序列，不允许有时间断层
            symbol  为合约代码，
            weight  为K线结束时间对应的持仓权重，品种之间的权重是独立的，不会互相影响
            price   为结束时间对应的交易价格，可以是当前K线的收盘价，或者下一根K线的开盘价，或者未来N根K线的TWAP、VWAP等

            数据样例如下：
            ===================  ========  ========  =======
            dt                   symbol      weight    price
            ===================  ========  ========  =======
            2019-01-02 09:01:00  DLi9001       0.5   961.695
            2019-01-02 09:02:00  DLi9001       0.25  960.72
            2019-01-02 09:03:00  DLi9001       0.25  962.669
            2019-01-02 09:04:00  DLi9001       0.25  960.72
            2019-01-02 09:05:00  DLi9001       0.25  961.695
            ===================  ========  ========  =======

        :param digits: int, 权重列保留小数位数
        :param weight_type: str, default 'ts'，持仓权重类别，可选值包括：'ts'、'cs'，分别表示时序策略、截面策略

            ts 表示 time series，时序策略；
            cs 表示 cross section，截面策略。

        :param kwargs:

            - fee_rate: float，单边交易成本，包括手续费与冲击成本, 默认为 0.0002
            - n_jobs: int, 并行计算的进程数，默认为 min(cpu_count() // 2, len(self.symbols))

        """
        self.kwargs = kwargs
        self.dfw = dfw.copy()
        self.dfw["dt"] = pd.to_datetime(self.dfw["dt"])
        if self.dfw.isnull().sum().sum() > 0:
            raise ValueError("dfw 中存在空值, 请先处理")

        self.digits = digits
        self.weight_type = weight_type.lower()
        self.fee_rate = kwargs.get("fee_rate", 0.0002)
        self.dfw["weight"] = self.dfw["weight"].astype("float").round(digits)
        self.symbols = list(self.dfw["symbol"].unique().tolist())
        self._dailys = None
        self.yearly_days = kwargs.pop("yearly_days", 252)
        self.results = self.backtest(n_jobs=kwargs.pop("n_jobs", 1))

    @property
    def stats(self):
        """回测绩效评价"""
        return self.results.get("绩效评价", {})

    @property
    def daily_return(self) -> pd.DataFrame:
        """品种等权费后日收益率"""
        return self.results.get("品种等权日收益", pd.DataFrame())

    @property
    def dailys(self) -> pd.DataFrame:
        """品种每日的交易信息

        columns = ['date', 'symbol', 'edge', 'return', 'cost', 'n1b', 'turnover']

        其中:
            date        交易日，
            symbol      合约代码，
            n1b         品种每日收益率，
            edge        策略每日收益率，
            return      策略每日收益率减去交易成本后的真实收益，
            cost        交易成本
            turnover    当日的单边换手率
        """
        return self._dailys.copy() if self._dailys is not None else pd.DataFrame()

    @property
    def alpha(self) -> pd.DataFrame:
        """策略超额收益

        columns = ['date', '策略', '基准', '超额']
        """
        if self._dailys is None:
            return pd.DataFrame()
        df1 = self._dailys.groupby("date").agg({"return": "mean", "n1b": "mean"})
        df1["alpha"] = df1["return"] - df1["n1b"]
        df1.rename(columns={"return": "策略", "n1b": "基准", "alpha": "超额"}, inplace=True)
        df1 = df1.reset_index()
        return df1

    @property
    def alpha_stats(self):
        """策略超额收益统计"""
        df = self.alpha.copy()
        stats = czsc.daily_performance(df["超额"].to_list(), yearly_days=self.yearly_days)
        stats["开始日期"] = df["date"].min().strftime("%Y-%m-%d")
        stats["结束日期"] = df["date"].max().strftime("%Y-%m-%d")
        return stats

    @property
    def bench_stats(self):
        """基准收益统计"""
        df = self.alpha.copy()
        stats = czsc.daily_performance(df["基准"].to_list(), yearly_days=self.yearly_days)
        stats["开始日期"] = df["date"].min().strftime("%Y-%m-%d")
        stats["结束日期"] = df["date"].max().strftime("%Y-%m-%d")
        return stats

    @property
    def long_daily_return(self):
        """多头每日收益率"""
        df = self.dailys.copy()
        dfv = pd.pivot_table(df, index="date", columns="symbol", values="long_return").fillna(0)

        if self.weight_type == "ts":
            dfv["total"] = dfv.mean(axis=1)
        elif self.weight_type == "cs":
            dfv["total"] = dfv.sum(axis=1)
        else:
            raise ValueError(f"weight_type {self.weight_type} not supported")

        dfv = dfv.reset_index(drop=False)
        return dfv

    @property
    def short_daily_return(self):
        """空头每日收益率"""
        df = self.dailys.copy()
        dfv = pd.pivot_table(df, index="date", columns="symbol", values="short_return").fillna(0)

        if self.weight_type == "ts":
            dfv["total"] = dfv.mean(axis=1)
        elif self.weight_type == "cs":
            dfv["total"] = dfv.sum(axis=1)
        else:
            raise ValueError(f"weight_type {self.weight_type} not supported")

        dfv = dfv.reset_index(drop=False)
        return dfv

    @property
    def long_stats(self):
        """多头收益统计"""
        df = self.long_daily_return.copy()
        stats = czsc.daily_performance(df["total"].to_list(), yearly_days=self.yearly_days)
        stats["开始日期"] = df["date"].min().strftime("%Y-%m-%d")
        stats["结束日期"] = df["date"].max().strftime("%Y-%m-%d")
        return stats

    @property
    def short_stats(self):
        """空头收益统计"""
        df = self.short_daily_return.copy()
        stats = czsc.daily_performance(df["total"].to_list(), yearly_days=self.yearly_days)
        stats["开始日期"] = df["date"].min().strftime("%Y-%m-%d")
        stats["结束日期"] = df["date"].max().strftime("%Y-%m-%d")
        return stats

    def get_symbol_daily(self, symbol):
        """获取某个合约的每日收益率

        函数计算逻辑：

        1. 从实例变量self.dfw中筛选出交易标的为symbol的数据，并复制到新的DataFrame dfs。
        2. 计算每条数据的收益（edge）：权重乘以下一条数据的价格除以当前价格减1。
        3. 计算每条数据的手续费（cost）：当前权重与前一条数据权重之差的绝对值乘以实例变量self.fee_rate。
        4. 计算每条数据扣除手续费后的收益（edge_post_fee）：收益减去手续费。
        5. 根据日期进行分组，并对每组进行求和操作，得到每日的总收益、总扣除手续费后的收益和总手续费。
        6. 重置索引，并将交易标的符号添加到DataFrame中。
        7. 重命名列名，将'edge_post_fee'列改为 return，将'dt'列改为 date。
        8. 选择需要的列，并返回包含日期、交易标的、收益、扣除手续费后的收益和手续费的DataFrame。

        :param symbol: str，合约代码
        :return: pd.DataFrame，品种每日收益率，

            columns = ['date', 'symbol', 'edge', 'return', 'cost', 'n1b']
            其中
                date        交易日，
                symbol      合约代码，
                n1b         品种每日收益率，
                edge        策略每日收益率，
                long_edge   多头每日收益率，
                short_edge  空头每日收益率，
                return      策略每日收益率减去交易成本后的真实收益，
                cost        交易成本
                turnover    当日的单边换手率

            数据样例如下：

                ==========  ========  ============  ============  =======
                date        symbol            edge        return     cost
                ==========  ========  ============  ============  =======
                2019-01-02  DLi9001    0.00230261    0.00195919   0.00085
                2019-01-03  DLi9001    0.00425589    0.00310589   0.00115
                2019-01-04  DLi9001   -0.0014209    -0.0024709    0.00105
                2019-01-07  DLi9001    0.000988305  -0.000111695  0.0011
                2019-01-08  DLi9001   -0.0004743    -0.0016243    0.00115
                ==========  ========  ============  ============  =======
        """
        dfs = self.dfw[self.dfw["symbol"] == symbol].copy()
        dfs["n1b"] = dfs["price"].shift(-1) / dfs["price"] - 1
        dfs["edge"] = dfs["weight"] * dfs["n1b"]
        dfs["turnover"] = abs(dfs["weight"].shift(1) - dfs["weight"])
        dfs["cost"] = dfs["turnover"] * self.fee_rate
        dfs["return"] = dfs["edge"] - dfs["cost"]

        # 分别计算多头和空头的收益
        dfs["long_weight"] = np.where(dfs["weight"] > 0, dfs["weight"], 0)
        dfs["short_weight"] = np.where(dfs["weight"] < 0, dfs["weight"], 0)
        dfs["long_edge"] = dfs["long_weight"] * dfs["n1b"]
        dfs["short_edge"] = dfs["short_weight"] * dfs["n1b"]

        dfs["long_turnover"] = abs(dfs["long_weight"].shift(1) - dfs["long_weight"])
        dfs["short_turnover"] = abs(dfs["short_weight"].shift(1) - dfs["short_weight"])
        dfs["long_cost"] = dfs["long_turnover"] * self.fee_rate
        dfs["short_cost"] = dfs["short_turnover"] * self.fee_rate

        dfs["long_return"] = dfs["long_edge"] - dfs["long_cost"]
        dfs["short_return"] = dfs["short_edge"] - dfs["short_cost"]

        daily = (
            dfs.groupby(dfs["dt"].dt.date)
            .agg(
                {
                    "edge": "sum",
                    "return": "sum",
                    "cost": "sum",
                    "n1b": "sum",
                    "turnover": "sum",
                    "long_edge": "sum",
                    "short_edge": "sum",
                    "long_cost": "sum",
                    "short_cost": "sum",
                    "long_turnover": "sum",
                    "short_turnover": "sum",
                    "long_return": "sum",
                    "short_return": "sum",
                }
            )
            .reset_index()
        )
        daily["symbol"] = symbol
        daily.rename(columns={"dt": "date"}, inplace=True)
        cols = [
            "date",
            "symbol",
            "edge",
            "return",
            "cost",
            "n1b",
            "turnover",
            "long_edge",
            "long_cost",
            "long_return",
            "long_turnover",
            "short_edge",
            "short_cost",
            "short_return",
            "short_turnover",
        ]

        daily = daily[cols].copy()
        return daily

    def get_symbol_pairs(self, symbol):
        """获取某个合约的开平交易记录

        函数计算逻辑：

        1. 从实例变量self.dfw中筛选出交易标的为symbol的数据，并复制到新的DataFrame dfs。
        2. 将权重乘以10的self.digits次方，并转换为整数类型，作为volume列的值。
        3. 生成bar_id列，从1开始递增，与行数对应。
        4. 创建一个空列表operates，用于存储开平仓交易记录。
        5. 定义内部函数__add_operate，用于向operates列表中添加开平仓交易记录。
           函数接受日期dt、bar_id、交易量volume、价格price和操作类型operate作为参数。
           函数根据交易量的绝对值循环添加交易记录到operates列表中。
        6. 将dfs转换为字典列表rows。
        7. 处理第一个行记录。
           - 如果volume大于0，则调用__add_operate函数添加"开多"操作的交易记录。
           - 如果volume小于0，则调用__add_operate函数添加"开空"操作的交易记录。
        8. 处理后续的行记录。
           - 使用zip函数遍历rows[:-1]和rows[1:]，同时获取当前行row1和下一行row2。
           - 根据volume的正负和变化情况，调用__add_operate函数添加对应的开平仓交易记录。
        9. 创建空列表pairs和opens，用于存储交易对和开仓记录。
        10. 遍历operates列表中的交易记录。
            - 如果操作类型为"开多"或"开空"，将交易记录添加到opens列表中，并继续下一次循环。
            - 如果操作类型为"平多"或"平空"，将对应的开仓记录从opens列表中弹出。
              根据开仓和平仓的价格计算盈亏比例，并创建一个交易对字典，将其添加到pairs列表中。
        11. 将pairs列表转换为DataFrame，并返回包含交易标的的开平仓交易记录的DataFrame。

        """
        dfs = self.dfw[self.dfw["symbol"] == symbol].copy()
        dfs["volume"] = (dfs["weight"] * pow(10, self.digits)).astype(int)
        dfs["bar_id"] = list(range(1, len(dfs) + 1))

        # 根据权重变化生成开平仓记录
        operates = []

        def __add_operate(dt, bar_id, volume, price, operate):
            for _ in range(abs(volume)):
                _op = {"bar_id": bar_id, "dt": dt, "price": price, "operate": operate}
                operates.append(_op)

        rows = dfs.to_dict(orient="records")

        # 处理第一个 row
        if rows[0]["volume"] > 0:
            __add_operate(rows[0]["dt"], rows[0]["bar_id"], rows[0]["volume"], rows[0]["price"], operate="开多")
        elif rows[0]["volume"] < 0:
            __add_operate(rows[0]["dt"], rows[0]["bar_id"], rows[0]["volume"], rows[0]["price"], operate="开空")

        # 处理后续 rows
        for row1, row2 in zip(rows[:-1], rows[1:]):
            if row1["volume"] >= 0 and row2["volume"] >= 0:
                # 多头仓位变化对应的操作
                if row2["volume"] > row1["volume"]:
                    __add_operate(
                        row2["dt"], row2["bar_id"], row2["volume"] - row1["volume"], row2["price"], operate="开多"
                    )
                elif row2["volume"] < row1["volume"]:
                    __add_operate(
                        row2["dt"], row2["bar_id"], row1["volume"] - row2["volume"], row2["price"], operate="平多"
                    )

            elif row1["volume"] <= 0 and row2["volume"] <= 0:
                # 空头仓位变化对应的操作
                if row2["volume"] > row1["volume"]:
                    __add_operate(
                        row2["dt"], row2["bar_id"], row1["volume"] - row2["volume"], row2["price"], operate="平空"
                    )
                elif row2["volume"] < row1["volume"]:
                    __add_operate(
                        row2["dt"], row2["bar_id"], row2["volume"] - row1["volume"], row2["price"], operate="开空"
                    )

            elif row1["volume"] >= 0 >= row2["volume"]:
                # 多头转换成空头对应的操作
                __add_operate(row2["dt"], row2["bar_id"], row1["volume"], row2["price"], operate="平多")
                __add_operate(row2["dt"], row2["bar_id"], row2["volume"], row2["price"], operate="开空")

            elif row1["volume"] <= 0 <= row2["volume"]:
                # 空头转换成多头对应的操作
                __add_operate(row2["dt"], row2["bar_id"], row1["volume"], row2["price"], operate="平空")
                __add_operate(row2["dt"], row2["bar_id"], row2["volume"], row2["price"], operate="开多")

        pairs, opens = [], []
        for op in operates:
            if op["operate"] in ["开多", "开空"]:
                opens.append(op)
                continue

            assert op["operate"] in ["平多", "平空"]
            open_op = opens.pop()
            if open_op["operate"] == "开多":
                p_ret = round((op["price"] - open_op["price"]) / open_op["price"] * 10000, 2)
                p_dir = "多头"
            else:
                p_ret = round((open_op["price"] - op["price"]) / open_op["price"] * 10000, 2)
                p_dir = "空头"
            pair = {
                "标的代码": symbol,
                "交易方向": p_dir,
                "开仓时间": open_op["dt"],
                "平仓时间": op["dt"],
                "开仓价格": open_op["price"],
                "平仓价格": op["price"],
                "持仓K线数": op["bar_id"] - open_op["bar_id"] + 1,
                "事件序列": f"{open_op['operate']} -> {op['operate']}",
                "持仓天数": (op["dt"] - open_op["dt"]).days,
                "盈亏比例": p_ret,
            }
            pairs.append(pair)
        df_pairs = pd.DataFrame(pairs)
        return df_pairs

    def process_symbol(self, symbol):
        """处理某个合约的回测数据"""
        daily = self.get_symbol_daily(symbol)
        pairs = self.get_symbol_pairs(symbol)
        return symbol, {"daily": daily, "pairs": pairs}

    def backtest(self, n_jobs=1):
        """回测所有合约的收益率

        函数计算逻辑：

        1. 获取数据：遍历所有合约，调用get_symbol_daily方法获取每个合约的日收益，调用get_symbol_pairs方法获取每个合约的交易流水。

        2. 数据处理：将每个合约的日收益合并为一个DataFrame，使用pd.pivot_table方法将数据重塑为以日期为索引、合约为列、
            收益率为值的表格，并将缺失值填充为0。计算所有合约收益率的平均值，并将该列添加到DataFrame中。将结果存储在res字典中，
            键为合约名，值为包含日行情数据和交易对数据的字典。

        3. 绩效评价：计算回测结果的开始日期和结束日期，调用daily_performance方法评估总收益率的绩效指标。将每个合约的交易对数据
            合并为一个DataFrame，调用evaluate_pairs方法评估交易对的绩效指标。将结果存储在stats字典中，并更新到绩效评价的字典中。

        4. 返回结果：将合约的等权日收益数据和绩效评价结果存储在res字典中，并将该字典作为函数的返回结果。
        """
        n_jobs = min(n_jobs, cpu_count())
        logger.info(f"n_jobs={n_jobs}，将使用 {n_jobs} 个进程进行回测")

        symbols = self.symbols
        res = {}
        if n_jobs <= 1:
            for symbol in tqdm(sorted(symbols), desc="WBT进度", leave=False):
                res[symbol] = self.process_symbol(symbol)[1]
        else:
            with ProcessPoolExecutor(n_jobs) as pool:
                for symbol, res_symbol in tqdm(
                    pool.map(self.process_symbol, sorted(symbols)), desc="WBT进度", total=len(symbols), leave=False
                ):
                    res[symbol] = res_symbol

        self._dailys = pd.concat([v["daily"] for k, v in res.items() if k in symbols], ignore_index=True)

        dret = pd.concat([v["daily"] for k, v in res.items() if k in symbols], ignore_index=True)
        dret = pd.pivot_table(dret, index="date", columns="symbol", values="return").fillna(0)

        if self.weight_type == "ts":
            # 时序策略每日收益为各品种收益的等权
            dret["total"] = dret[list(res.keys())].mean(axis=1)
        elif self.weight_type == "cs":
            # 截面策略每日收益为各品种收益的和
            dret["total"] = dret[list(res.keys())].sum(axis=1)
        else:
            raise ValueError(f"weight_type {self.weight_type} not supported, should be 'ts' or 'cs'")

        # dret 中的 date 对应的是上一日；date 后移一位，对应的才是当日收益
        dret = dret.round(4).reset_index()
        res["品种等权日收益"] = dret

        stats = {"开始日期": dret["date"].min().strftime("%Y%m%d"), "结束日期": dret["date"].max().strftime("%Y%m%d")}
        stats.update(daily_performance(dret["total"], yearly_days=self.yearly_days))
        dfp = pd.concat([v["pairs"] for k, v in res.items() if k in symbols], ignore_index=True)
        pairs_stats = evaluate_pairs(dfp)
        pairs_stats = {k: v for k, v in pairs_stats.items() if k in ["单笔收益", "持仓K线数", "交易胜率", "持仓天数"]}
        stats.update(pairs_stats)

        dfw = self.dfw.copy()
        long_rate = dfw[dfw["weight"] > 0].shape[0] / dfw.shape[0]
        short_rate = dfw[dfw["weight"] < 0].shape[0] / dfw.shape[0]
        stats.update({"多头占比": round(long_rate, 4), "空头占比": round(short_rate, 4)})

        alpha = self.alpha.copy()
        stats["波动比"] = round(alpha["策略"].std() / alpha["基准"].std(), 4)
        stats["与基准波动相关性"] = round(alpha["策略"].corr(alpha["基准"].abs()), 4)
        stats["与基准相关性"] = round(alpha["策略"].corr(alpha["基准"]), 4)
        alpha_short = alpha[alpha["基准"] < 0].copy()
        stats["与基准空头相关性"] = round(alpha_short["策略"].corr(alpha_short["基准"]), 4)
        stats["品种数量"] = len(symbols)

        res["绩效评价"] = stats
        return res

    def report(self, res_path):
        """回测报告"""
        res_path = Path(res_path)
        res_path.mkdir(exist_ok=True, parents=True)
        logger.add(res_path.joinpath("weight_backtest.log"), rotation="1 week")
        logger.info(f"持仓权重回测参数：digits={self.digits}, fee_rate={self.fee_rate}，res_path={res_path}")

        res = self.results
        pd.to_pickle(res, res_path.joinpath("res.pkl"))
        logger.info(f"回测结果已保存到 {res_path.joinpath('res.pkl')}")

        # 品种等权费后日收益率
        dret = res["品种等权日收益"].copy()
        dret.to_excel(res_path.joinpath("daily_return.xlsx"), index=True)
        logger.info(f"品种等权费后日收益率已保存到 {res_path.joinpath('daily_return.xlsx')}")

        # 品种等权费后日收益率资金曲线绘制
        dret = dret.cumsum()
        fig = px.line(dret, y=dret.columns.to_list(), title="费后日收益率资金曲线")
        fig.for_each_trace(lambda trace: trace.update(visible=True if trace.name == "total" else "legendonly"))
        fig.write_html(res_path.joinpath("daily_return.html"))
        logger.info(f"费后日收益率资金曲线已保存到 {res_path.joinpath('daily_return.html')}")

        # 绘制alpha曲线
        alpha = self.alpha.copy()
        alpha[["策略", "基准", "超额"]] = alpha[["策略", "基准", "超额"]].cumsum()
        fig = px.line(alpha, x="date", y=["策略", "基准", "超额"], title="策略超额收益")
        fig.write_html(res_path.joinpath("alpha.html"))

        # 所有开平交易记录的表现
        stats = res["绩效评价"].copy()
        logger.info(f"绩效评价：{stats}")
        save_json(stats, res_path.joinpath("stats.json"))
        logger.info(f"绩效评价已保存到 {res_path.joinpath('stats.json')}")
