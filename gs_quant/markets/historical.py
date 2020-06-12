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
import datetime as dt
from typing import Iterable, Optional, Tuple, Union

from .core import PricingContext
from .markets import CloseMarket, close_market_date
from gs_quant.base import Priceable
from gs_quant.datetime.date import date_range
from gs_quant.risk import RiskMeasure
from gs_quant.risk.results import HistoricalPricingFuture, PricingFuture


class HistoricalPricingContext(PricingContext):

    """
    A context for producing valuations over multiple dates
    """

    def __init__(
            self,
            start: Optional[Union[int, dt.date]] = None,
            end: Optional[Union[int, dt.date]] = None,
            calendars: Union[str, Tuple] = (),
            dates: Optional[Iterable[dt.date]] = None,
            is_async: bool = False,
            is_batch: bool = False,
            use_cache: bool = False,
            visible_to_gs: bool = False,
            csa_term: str = None,
            market_data_location: Optional[str] = None,
            batch_results_timeout: Optional[int] = None):
        """
        A context for producing valuations over multiple dates

        :param start: start date
        :param end: end date (defaults to today)
        :param calendars: holiday calendars
        :param dates: a custom iterable of dates
        :param is_async: return immediately (True) or wait for results (False) (defaults to False)
        :param is_batch: use for calculations expected to run longer than 3 mins, to avoid timeouts.
            It can be used with is_async=True|False (defaults to False)
        :param use_cache: store results in the pricing cache (defaults to False)
        :param visible_to_gs: are the contents of risk requests visible to GS (defaults to False)
        :param csa_term: the csa under which the calculations are made. Default is local ccy ois index
        :param market_data_location: the location for sourcing market data ('NYC', 'LDN' or 'HKG' (defaults to LDN)

        **Examples**

        >>> from gs_quant.instrument import IRSwap
        >>>
        >>> ir_swap = IRSwap('Pay', '10y', 'DKK')
        >>> with HistoricalPricingContext(10):
        >>>     price_f = ir_swap.price()
        >>>
        >>> price_series = price_f.result()
        """
        super().__init__(is_async=is_async, is_batch=is_batch, use_cache=use_cache, visible_to_gs=visible_to_gs,
                         csa_term=csa_term, market_data_location=market_data_location,
                         batch_results_timeout=batch_results_timeout)
        if start is not None:
            if dates is not None:
                raise ValueError('Must supply start or dates, not both')

            if end is None:
                end = dt.date.today()

            self.__date_range = tuple(date_range(start, end, calendars=calendars))
        elif dates is not None:
            self.__date_range = tuple(dates)
        else:
            raise ValueError('Must supply start or dates')

    def calc(self, priceable: Priceable, risk_measure: RiskMeasure) -> PricingFuture:
        futures = []
        for date in self.__date_range:
            with PricingContext(pricing_date=date,
                                market=CloseMarket(location=self.market.location,
                                                   date=close_market_date(self.market.location, date)),
                                is_async=True,
                                csa_term=self.csa_term,
                                use_cache=self.use_cache,
                                visible_to_gs=self.visible_to_gs):
                futures.append(priceable.calc(risk_measure))

        return HistoricalPricingFuture(futures)
