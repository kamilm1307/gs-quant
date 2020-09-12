"""
Copyright 2019 Goldman Sachs.
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicablNe law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
"""
import copy
import datetime as dt
import logging
from itertools import chain
from typing import Iterable, Optional, Tuple, Union

import numpy as np
import pandas as pd

from gs_quant.api.gs.assets import GsAssetApi
from gs_quant.api.gs.portfolios import GsPortfolioApi
from gs_quant.context_base import nullcontext
from gs_quant.instrument import Instrument
from gs_quant.markets import HistoricalPricingContext, PricingContext
from gs_quant.priceable import PriceableImpl
from gs_quant.risk import RiskMeasure
from gs_quant.risk.results import CompositeResultFuture, PortfolioRiskResult, PortfolioPath, PricingFuture
from gs_quant.target.portfolios import Position, PositionSet

_logger = logging.getLogger(__name__)


class Portfolio(PriceableImpl):
    """A collection of instruments

    Portfolio holds a collection of instruments in order to run pricing and risk scenarios

    """

    def __init__(self,
                 priceables: Optional[Union[PriceableImpl, Iterable[PriceableImpl], dict]] = (),
                 name: Optional[str] = None):
        """
        Creates a portfolio object which can be used to hold instruments

        :param priceables: constructed with an instrument, portfolio, iterable of either, or a dictionary where
            key is name and value is a priceable
        """
        super().__init__()
        if isinstance(priceables, dict):
            priceables_list = []
            for name, priceable in priceables.items():
                priceable.name = name
                priceables_list.append(priceable)
            self.priceables = priceables_list
        else:
            self.priceables = priceables

        self.__name = name
        self.__id = None

    def __getitem__(self, item):
        if isinstance(item, (int, slice)):
            return self.__priceables[item]
        elif isinstance(item, PortfolioPath):
            return item(self, rename_to_parent=True)
        else:
            values = tuple(self[p] for p in self.paths(item))
            return values[0] if len(values) == 1 else values

    def __contains__(self, item):
        if isinstance(item, PriceableImpl):
            return any(item in p.__priceables_lookup for p in self.all_portfolios + (self,))
        elif isinstance(item, str):
            return any(item in p.__priceables_by_name for p in self.all_portfolios + (self,))
        else:
            return False

    def __len__(self):
        return len(self.__priceables)

    def __iter__(self):
        return iter(self.__priceables)

    def __hash__(self):
        hash_code = hash(self.__name) ^ hash(self.__id)
        for priceable in self.__priceables:
            hash_code ^= hash(priceable)

        return hash_code

    def __eq__(self, other):
        if not isinstance(other, Portfolio):
            return False

        for path in self.all_paths:
            try:
                if path(self) != path(other):
                    return False
            except IndexError:
                return False

        return True

    def __add__(self, other):
        if not isinstance(other, Portfolio):
            raise ValueError('Can only add instances of Portfolio')

        return Portfolio(self.__priceables + other.__priceables)

    @property
    def __pricing_context(self) -> PricingContext:
        return PricingContext.current if not PricingContext.current.is_entered else nullcontext()

    @property
    def id(self) -> str:
        return self.__id

    @property
    def name(self) -> str:
        return self.__name

    @property
    def priceables(self) -> Tuple[PriceableImpl, ...]:
        return self.__priceables

    @priceables.setter
    def priceables(self, priceables: Union[PriceableImpl, Iterable[PriceableImpl]]):
        self.__priceables = (priceables,) if isinstance(priceables, PriceableImpl) else tuple(priceables)
        self.__priceables_lookup = {}
        self.__priceables_by_name = {}

        for idx, i in enumerate(self.__priceables):
            self.__priceables_lookup.setdefault(copy.copy(i), []).append(idx)
            if i and i.name:
                self.__priceables_by_name.setdefault(i.name, []).append(idx)

    @priceables.deleter
    def priceables(self):
        self.__priceables = None
        self.__priceables_lookup = None
        self.__priceables_by_name = None

    @property
    def instruments(self) -> Tuple[Instrument, ...]:
        return tuple(set(i for i in self.__priceables if isinstance(i, Instrument)))

    @property
    def all_instruments(self) -> Tuple[Instrument, ...]:
        return tuple(set(chain(self.instruments, chain.from_iterable(p.instruments for p in self.all_portfolios))))

    @property
    def portfolios(self) -> Tuple[PriceableImpl, ...]:
        return tuple(i for i in self.__priceables if isinstance(i, Portfolio))

    @property
    def all_portfolios(self) -> Tuple[PriceableImpl, ...]:
        stack = list(self.portfolios)
        portfolios = set(stack)

        while stack:
            portfolio = stack.pop()
            if portfolio in portfolios:
                continue

            sub_portfolios = portfolio.portfolios
            portfolios.update(sub_portfolios)
            stack.extend(sub_portfolios)

        return tuple(portfolios)

    def subset(self, paths: Iterable[PortfolioPath], name=None):
        return Portfolio(tuple(self[p] for p in paths), name=name)

    @staticmethod
    def __from_internal_positions(id_type: str, positions_id):
        instruments = GsPortfolioApi.get_instruments_by_position_type(id_type, positions_id)
        return Portfolio(instruments, name=positions_id)

    @staticmethod
    def from_eti(eti: str):
        return Portfolio.__from_internal_positions('ETI', eti.replace(',', '%2C'))

    @staticmethod
    def from_book(book: str, book_type: str = 'risk'):
        return Portfolio.__from_internal_positions(book_type, book)

    @staticmethod
    def from_asset_id(asset_id: str, date=None):
        asset = GsAssetApi.get_asset(asset_id)
        response = GsAssetApi.get_asset_positions_for_date(asset_id, date) if date else \
            GsAssetApi.get_latest_positions(asset_id)
        response = response[0] if isinstance(response, tuple) else response
        positions = response.positions if isinstance(response, PositionSet) else response['positions']
        instruments = GsAssetApi.get_instruments_for_positions(positions)
        ret = Portfolio(instruments, name=asset.name)
        ret.__id = asset_id
        return ret

    @staticmethod
    def from_asset_name(name: str):
        asset = GsAssetApi.get_asset_by_name(name)
        return Portfolio.load_from_portfolio_id(asset.id)

    @staticmethod
    def from_portfolio_id(portfolio_id: str, date=None):
        portfolio = GsPortfolioApi.get_portfolio(portfolio_id)
        response = GsPortfolioApi.get_positions_for_date(portfolio_id, date) if date else\
            GsPortfolioApi.get_latest_positions(portfolio_id)
        response = response[0] if isinstance(response, tuple) else response
        positions = response.positions if isinstance(response, PositionSet) else response['positions']
        instruments = GsAssetApi.get_instruments_for_positions(positions)
        ret = Portfolio(instruments, name=portfolio.name)
        ret.__id = portfolio_id
        return ret

    @staticmethod
    def from_portfolio_name(name: str):
        portfolio = GsPortfolioApi.get_portfolio_by_name(name)
        return Portfolio.load_from_portfolio_id(portfolio.id)

    def save(self, overwrite: Optional[bool] = False):
        if self.portfolios:
            raise ValueError('Cannot save portfolios with nested portfolios')

        if self.__id:
            if not overwrite:
                raise ValueError(f'Portfolio with id {id} already exists. Use overwrite=True to overwrite')
        else:
            if not self.__name:
                raise ValueError('name not set')

            try:
                self.__id = GsPortfolioApi.get_portfolio_by_name(self.__name).id
                if not overwrite:
                    raise RuntimeError(
                        f'Portfolio {self.__name} with id {self.__id} already exists. Use overwrite=True to overwrite')
            except ValueError:
                from gs_quant.target.portfolios import Portfolio as MarqueePortfolio
                self.__id = GsPortfolioApi.create_portfolio(MarqueePortfolio('USD', self.__name)).id
                _logger.info(f'Created Marquee portfolio {self.__name} with id {self.__id}')

        position_set = PositionSet(
            position_date=dt.date.today(),
            positions=tuple(Position(asset_id=GsAssetApi.get_or_create_asset_from_instrument(i))
                            for i in self.instruments))

        GsPortfolioApi.update_positions(self.__id, (position_set,))

    @classmethod
    def from_frame(cls, data: pd.DataFrame, mappings: dict = None):
        def get_value(row: pd.Series, attribute: str):
            value = mappings.get(attribute, attribute)
            return value(row) if callable(value) else row.get(value)

        instruments = []
        mappings = mappings or {}
        data = data.replace({np.nan: None})

        for row in (r for _, r in data.iterrows() if any(v for v in r.values if v is not None)):
            instrument = None
            for init_keys in (('asset_class', 'type'), ('$type',)):
                init_values = tuple(filter(None, (get_value(row, k) for k in init_keys)))
                if len(init_keys) == len(init_values):
                    instrument = Instrument.from_dict(dict(zip(init_keys, init_values)))
                    instrument = instrument.from_dict({p: get_value(row, p) for p in instrument.properties()})
                    break

            if instrument:
                instruments.append(instrument)
            else:
                raise ValueError('Neither asset_class/type nor $type specified')

        return cls(instruments)

    @classmethod
    def from_csv(
            cls,
            csv_file: str,
            mappings: Optional[dict] = None,
            date_formats: Optional[list] = None,
    ):
        data = pd.read_csv(csv_file, skip_blank_lines=True).replace({np.nan: None})
        return cls.from_frame(data, mappings, date_formats)

    def append(self, priceables: Union[PriceableImpl, Iterable[PriceableImpl]]):
        self.priceables += ((priceables,) if isinstance(priceables, PriceableImpl) else tuple(priceables))

    def pop(self, item) -> PriceableImpl:
        priceable = self[item]
        self.priceables = [inst for inst in self.instruments if inst != priceable]
        return priceable

    def to_frame(self, mappings: Optional[dict] = None) -> pd.DataFrame:
        def to_records(portfolio: Portfolio) -> list:
            records = []

            for priceable in portfolio.priceables:
                if isinstance(priceable, Portfolio):
                    records.extend(to_records(priceable))
                else:
                    as_dict = priceable.as_dict()
                    if hasattr(priceable, '_type'):
                        as_dict['$type'] = priceable._type

                    records.append(dict(chain(as_dict.items(),
                                              (('instrument', priceable), ('portfolio', portfolio.name)))))

            return records

        df = pd.DataFrame.from_records(to_records(self)).set_index(['portfolio', 'instrument'])
        all_columns = df.columns.to_list()
        columns = sorted(c for c in all_columns if c not in ('asset_class', 'type', '$type'))

        for asset_column in ('$type', 'type', 'asset_class'):
            if asset_column in all_columns:
                columns = [asset_column] + columns

        df = df[columns]
        mappings = mappings or {}

        for key, value in mappings.items():
            if isinstance(value, str):
                df[key] = df[value]
            elif callable(value):
                df[key] = len(df) * [None]
                df[key] = df.apply(value, axis=1)

        return df

    def to_csv(self, csv_file: str, mappings: Optional[dict] = None, ignored_cols: Optional[list] = None):
        port_df = self.to_frame(mappings or {})
        port_df = port_df[np.setdiff1d(port_df.columns, ignored_cols or [])]
        port_df.reset_index(drop=True, inplace=True)

        port_df.to_csv(csv_file)

    @property
    def all_paths(self) -> Tuple[PortfolioPath, ...]:
        paths = ()
        stack = [(None, self)]
        while stack:
            parent, portfolio = stack.pop()

            for idx, priceable in enumerate(portfolio.__priceables):
                path = parent + PortfolioPath(idx) if parent is not None else PortfolioPath(idx)
                if isinstance(priceable, Portfolio):
                    stack.append((path, priceable))
                else:
                    paths += (path,)

        return paths

    def paths(self, key: Union[str, PriceableImpl]) -> Tuple[PortfolioPath, ...]:
        if not isinstance(key, (str, Instrument, Portfolio)):
            raise ValueError('key must be a name or Instrument or Portfolio')

        idx = self.__priceables_by_name.get(key) if isinstance(key, str) else self.__priceables_lookup.get(key)
        paths = tuple(PortfolioPath(i) for i in idx) if idx else ()

        for path, porfolio in ((PortfolioPath(i), p)
                               for i, p in enumerate(self.__priceables) if isinstance(p, Portfolio)):
            paths += tuple(path + sub_path for sub_path in porfolio.paths(key))

        return paths

    def resolve(self, in_place: bool = True) -> Optional[Union[PricingFuture, PriceableImpl, dict]]:
        pricing_context = self.__pricing_context
        with pricing_context:
            futures = [i.resolve(in_place) for i in self.__priceables]

        if not in_place:
            ret = {} if isinstance(PricingContext.current, HistoricalPricingContext) else Portfolio(name=self.name)
            result_future = PricingFuture() if not isinstance(pricing_context, PricingContext) \
                or pricing_context.is_async or pricing_context.is_entered else None

            def cb(future):
                if isinstance(ret, Portfolio):
                    ret.priceables = [f.result() for f in future.futures]
                else:
                    priceables_by_date = {}
                    for future in futures:
                        for date, priceable in future.result().items():
                            priceables_by_date.setdefault(date, []).append(priceable)

                    for date, priceables in priceables_by_date.items():
                        ret[date] = Portfolio(priceables, name=self.name)

                if result_future:
                    result_future.set_result(ret)

            CompositeResultFuture(futures).add_done_callback(cb)
            return result_future or ret

    def calc(self, risk_measure: Union[RiskMeasure, Iterable[RiskMeasure]], fn=None) -> PortfolioRiskResult:
        with self.__pricing_context:
            return PortfolioRiskResult(self,
                                       (risk_measure,) if isinstance(risk_measure, RiskMeasure) else risk_measure,
                                       [p.calc(risk_measure, fn=fn) for p in self.__priceables])
