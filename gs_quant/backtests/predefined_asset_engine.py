"""
Copyright 2019 Goldman Sachs.
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
"""


from gs_quant.backtests.event import *
from gs_quant.backtests.action_handler import ActionHandlerBaseFactory, ActionHandler
from gs_quant.backtests.actions import Action, AddTradeAction
from gs_quant.backtests.backtest_engine import BacktestBaseEngine
from gs_quant.backtests.backtest_objects import PredefinedAssetBacktest
from gs_quant.backtests.execution_engine import SimulatedExecutionEngine
from gs_quant.backtests.core import ValuationMethod
from gs_quant.backtests.data_sources import DataManager
from gs_quant.backtests.order import *
from gs_quant.datetime import date_range
from collections import deque
from pytz import timezone
from functools import reduce
import datetime as dt
from typing import Union, Tuple
from tqdm import tqdm

# Action Implementations


class AddTradeActionImpl(ActionHandler):
    def __init__(self, action: AddTradeAction):
        super().__init__(action)

    def generate_orders(self, state: dt.datetime, backtest: PredefinedAssetBacktest):

        orders = []
        for pricable in self.action.priceables:
            orders.append(OrderAtMarket(instrument=pricable,
                                        quantity=pricable.instrument_quantity,
                                        generation_time=state,
                                        execution_datetime=state,
                                        source=self.action._name))
            if isinstance(self.action.trade_duration, dt.timedelta):
                # create close order
                orders.append(OrderAtMarket(instrument=pricable,
                                            quantity=pricable.instrument_quantity * -1,
                                            generation_time=state,
                                            execution_datetime=state + self.action.trade_duration,
                                            source=self.action._name))
        return orders

    def apply_action(self, state: dt.datetime, backtest: PredefinedAssetBacktest, info=None):
        orders = self.generate_orders(state, backtest)
        return orders


class PredefinedAssetEngineActionFactory(ActionHandlerBaseFactory):
    def __init__(self, action_impl_map={}):
        self.action_impl_map = action_impl_map

    def get_action_handler(self, action: Action) -> Action:
        if type(action) in self.action_impl_map:
            return self.action_impl_map[type(action)](action)
        raise RuntimeError(f'Action {type(action)} not supported by engine')


class PredefinedAssetEngine(BacktestBaseEngine):

    def get_action_handler(self, action: Action) -> Action:
        handler_factory = PredefinedAssetEngineActionFactory(self.action_impl_map)
        return handler_factory.get_action_handler(action)

    def supports_strategy(self, strategy):
        all_actions = reduce(lambda x, y: x + y, (map(lambda x: x.actions, strategy.triggers)))
        try:
            for x in all_actions:
                self.get_action_handler(x)
        except RuntimeError:
            return False
        return True

    def __init__(self,
                 data_mgr: DataManager = DataManager(),
                 calendars: Union[str, Tuple[str, ...]] = 'CME',
                 tz: timezone = timezone('America/New_York'),
                 valuation_method: ValuationMethod = ValuationMethod(),
                 action_impl_map: dict = {}):
        self.action_impl_map = action_impl_map
        self.calendars = calendars
        self.tz = tz
        self.data_handler = DataHandler(data_mgr, tz=tz)
        self.valuation_method = valuation_method
        self.execution_engine = None

    def _eod_valuation_time(self):
        if self.valuation_method.window:
            return self.valuation_method.window.end
        else:
            return dt.time(23)

    def _timer(self, strategy, start, end):
        dates = date_range(start, end, calendars=self.calendars)
        times = list()
        for trigger in strategy.triggers:
            times.extend(trigger.get_trigger_times())
        times.append(self._eod_valuation_time())
        times = list(dict.fromkeys(times))

        all_times = []
        for d in dates:
            for t in times:
                all_times.append(dt.datetime.combine(d, t))
        return all_times

    def run_backtest(self, strategy, start, end):
        # initialize backtest object
        backtest = PredefinedAssetBacktest(self.data_handler)
        backtest.set_start_date(start)

        # initialize execution engine
        self.execution_engine = SimulatedExecutionEngine(self.data_handler)

        # create timer
        timer = self._timer(strategy, start, end)
        self._run(strategy, timer, backtest)
        return backtest

    def _run(self, strategy, timer, backtest: PredefinedAssetBacktest):
        events = deque()

        for state in tqdm(timer):
            # update to latest data
            self.data_handler.update(state)

            # see if any submitted orders have been executed
            fills = self.execution_engine.ping(state)
            events.extend(fills)

            # generate a market event
            events.append(MarketEvent())

            # create valuation event when it's due for daily valuation
            if state.time() == self._eod_valuation_time():
                events.append(ValuationEvent())

            while events:
                event = events.popleft()

                if event.type == 'Market':  # market event (new mkt data coming in)
                    for trigger in strategy.triggers:
                        trigger_info = trigger.has_triggered(state, backtest)
                        if trigger_info.triggered:
                            for action in trigger.actions:
                                info_dict = trigger_info.info_dict
                                info = info_dict[type(action)] if info_dict and type(action) in info_dict else None
                                orders = self.get_action_handler(action).apply_action(state, backtest, info)
                                backtest.record_orders(orders)
                                events.extend([OrderEvent(o) for o in orders])
                elif event.type == 'Order':  # order event (submit the order to execution engine)
                    self.execution_engine.submit_order(event)
                elif event.type == 'Fill':  # fill event (update backtest with the fill results)
                    backtest.update_fill(event)
                elif event.type == 'Valuation':  # valuation event (calculate daily level)
                    backtest.mark_to_market(state, self.valuation_method)

        return backtest
