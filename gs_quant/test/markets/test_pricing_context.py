"""
Copyright 2018 Goldman Sachs.
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
import datetime
import datetime as dt

import pytest
from gs_quant import risk
from gs_quant.common import PayReceive, Currency
from gs_quant.datetime import business_day_offset, today
from gs_quant.errors import MqValueError
from gs_quant.instrument import IRSwap
from gs_quant.markets import PricingContext, CloseMarket, OverlayMarket, MarketDataCoordinate
from gs_quant.markets.portfolio import Portfolio
from gs_quant.risk import RollFwd
from gs_quant.target.common import PricingLocation
from gs_quant.test.utils.test_utils import MockCalc

WEEKEND_DATE = dt.date(2022, 3, 19)


@pytest.fixture
def today_is_saturday(monkeypatch):
    class MockedDatetime:
        @classmethod
        def today(cls):
            return WEEKEND_DATE

    monkeypatch.setattr(dt, 'date', MockedDatetime)


def test_pricing_context(mocker):
    swap1 = IRSwap(PayReceive.Pay, '1y', Currency.EUR, name='EUR1y')
    future_date = business_day_offset(dt.date.today(), 10, roll='forward')
    with MockCalc(mocker):
        with RollFwd(date='10b', realise_fwd=True):
            market = swap1.market()

    with pytest.raises(ValueError):
        # cannot pass in future date into pricing context, use RollFwd instead
        with PricingContext(pricing_date=future_date):
            _ = swap1.calc(risk.Price)

        # cannot pass in market dated in the future into pricing context, use RollFwd instead
        with PricingContext(market=CloseMarket(date=future_date)):
            _ = swap1.calc(risk.Price)

        with PricingContext(market=OverlayMarket(base_market=CloseMarket(date=future_date, location='NYC'),
                                                 market_data=market.result())):
            _ = swap1.calc(risk.Price)


def test_pricing_dates():
    # May be on weekend but doesn't matter for basic test
    future_date = dt.date.today() + dt.timedelta(10)
    yesterday = dt.date.today() - dt.timedelta(1)
    pc = PricingContext(pricing_date=future_date, market=CloseMarket(yesterday))
    assert pc is not None
    with pytest.raises(ValueError, match="pricing_date in the future"):
        PricingContext(pricing_date=future_date)


def test_weekend_dates(today_is_saturday):
    assert dt.date.today() == WEEKEND_DATE  # Check mock worked
    next_monday = WEEKEND_DATE + dt.timedelta(2)
    prev_friday = WEEKEND_DATE - dt.timedelta(1)
    pc = PricingContext(pricing_date=next_monday)
    with pc:
        assert pc.market == CloseMarket(prev_friday, PricingLocation.LDN)


def test_market_data_object():
    coord_val_pair = [
        {'coordinate': {
            'mkt_type': 'IR', 'mkt_asset': 'USD', 'mkt_class': 'Swap', 'mkt_point': ('5y',),
            'mkt_quoting_style': 'ATMRate'}, 'value': 0.9973194889},
        {'coordinate': {
            'mkt_type': 'IR', 'mkt_asset': 'USD', 'mkt_class': 'Swap', 'mkt_point': ('40y',),
            'mkt_quoting_style': 'ATMRate'}, 'value': 'redacted'},
    ]
    coordinates = {MarketDataCoordinate.from_dict(dic['coordinate']): dic['value'] for dic in coord_val_pair}
    overlay_market = OverlayMarket(base_market=CloseMarket(), market_data=coordinates)

    assert overlay_market.coordinates[0] == MarketDataCoordinate.from_dict(coord_val_pair[0]['coordinate'])
    assert overlay_market.redacted_coordinates[0] == MarketDataCoordinate.from_dict(coord_val_pair[1]['coordinate'])


def test_pricing_context_metadata():
    assert len(PricingContext.path) == 0
    assert not PricingContext.has_prior

    c1 = PricingContext(pricing_date=datetime.date(2022, 6, 15))
    c2 = PricingContext(pricing_date=datetime.date(2022, 6, 16))
    c3 = PricingContext(pricing_date=datetime.date(2022, 6, 17))

    PricingContext.current = PricingContext()
    assert len(PricingContext.path) == 1

    PricingContext.current = c1
    assert len(PricingContext.path) == 1

    with c2:
        with pytest.raises(MqValueError):
            PricingContext.current = PricingContext()

        assert PricingContext.current == c2
        assert PricingContext.has_prior and PricingContext.prior == c1
        assert len(PricingContext.path) == 2

        with c3:
            assert PricingContext.current == c3
            assert PricingContext.has_prior and PricingContext.prior == c2
            assert len(PricingContext.path) == 3

    PricingContext.pop()
    assert len(PricingContext.path) == 0


def test_creation():
    c1 = PricingContext(pricing_date=datetime.date(2022, 6, 15))
    # All props except for the initialised one are None. Context is not useable as-is
    assert c1.market is None
    assert c1.market_data_location is None
    assert c1.is_batch is None
    assert c1.use_cache is None
    assert c1._max_concurrent is None

    assert c1.pricing_date == datetime.date(2022, 6, 15)


def test_inheritance():
    c1 = PricingContext(pricing_date=datetime.date(2022, 6, 16), market_data_location='NYC')
    c2 = PricingContext(pricing_date=datetime.date(2022, 7, 1))
    c3 = PricingContext()

    with c1:
        with c2:
            # pricing date is set
            assert c2.pricing_date == datetime.date(2022, 7, 1)
            # market data location is inherited from c1 (the active context)
            assert c2.market_data_location == c1.market_data_location
            # all other props have default values
            assert c2.is_batch is False
            assert c2.use_cache is False
            assert c2._max_concurrent == 1000
            with c3:
                # market data location is inherited from c1 (the active context)
                assert c3.market_data_location == c1.market_data_location
                # pricing date is inherited from c2 (the prior context)
                assert c3.pricing_date == c2.pricing_date
                # all other props have default values
                assert c3.is_batch is False
                assert c3.use_cache is False
                assert c3._max_concurrent == 1000


def test_current_inheritance():
    cur = PricingContext.current
    assert cur.market_data_location is None

    with PricingContext() as pc:
        assert pc.market_data_location == PricingLocation.LDN

    PricingContext.current = PricingContext(market_data_location='TKO')

    # We can set props on the current so that props are inherited globally
    new_cur = PricingContext.current
    assert new_cur.market_data_location == PricingLocation.TKO

    with PricingContext() as pc:
        assert pc.market_data_location == new_cur.market_data_location

    with PricingContext():
        with PricingContext() as pc2:
            assert pc2.market_data_location == new_cur.market_data_location

    # Exit the current
    PricingContext.pop()


def test_cleanup():
    c1 = PricingContext(pricing_date=datetime.date(2022, 4, 6))
    c2 = PricingContext(request_priority=5000)

    with c1:
        with c2:
            assert c2.request_priority == 5000
            # pricing_date is inherited from c1
            assert c2.pricing_date is not None
            assert c2.pricing_date == c1.pricing_date
        # pricing date is cleaned up on exit from c2. request priority remains set
        assert c2.request_priority == 5000
        assert c2.pricing_date is None


def test_market_props():
    # market_data_location cannot conflict with market.location
    with pytest.raises(ValueError):
        PricingContext(market=CloseMarket(date=datetime.date(2022, 4, 6), location='NYC'),
                       pricing_date=datetime.date(2022, 7, 4), market_data_location='TKO')

    # Default pricing date and market location are today and LDN, respectively
    pc = PricingContext()
    with pc:
        assert pc.market_data_location == PricingLocation.LDN
        assert pc.pricing_date == business_day_offset(today(PricingLocation.LDN), 0, roll='preceding')

    # pricing_date and market.datee can be different
    pc = PricingContext(market=CloseMarket(date=datetime.date(2022, 4, 6), location='NYC'),
                        pricing_date=datetime.date(2022, 7, 4))

    with pc:
        assert pc.pricing_date == datetime.date(2022, 7, 4)
        assert pc.market.date == datetime.date(2022, 4, 6)

    # if market is not specified, it is inferred from pricing_date and market_data_location
    pc = PricingContext(pricing_date=datetime.date(2022, 7, 4), market_data_location='TKO')
    assert pc.market is None
    with pc:
        cm = CloseMarket(date=pc.pricing_date, location=pc.market_data_location)
        assert pc.market.date == cm.date
        assert pc.market.location == cm.location

    # market is not inherited
    pc = PricingContext(market=CloseMarket(date=datetime.date(2022, 4, 6), location='NYC'))
    with pc:
        # pc gets market_data_location from its market
        assert pc.market_data_location == PricingLocation.NYC
        with PricingContext() as inner:
            # Inner inherits pricing_date
            assert inner.pricing_date == pc.pricing_date
            # Inner initialises Market with its own pricing_date
            cm = CloseMarket(date=inner.pricing_date)
            assert inner.market.date == cm.date
            # Inner also inherits market_data_location
            assert inner.market_data_location == pc.market_data_location
            # Inner uses its own market_data_location to build its market
            assert inner.market.location == inner.market_data_location


def test_pricing_does_not_affect_context(mocker):
    # Pricing instruments and portfolios uses the current pricing context. This should not affect its properties
    swap1 = IRSwap(PayReceive.Pay, '1y', name='EUR1y')
    swap2 = IRSwap(PayReceive.Pay, '1y', name='EUR2y')
    port = Portfolio([swap1, swap2])
    with MockCalc(mocker):
        cm = CloseMarket(date=datetime.date(2022, 7, 5), location='TKO')
        pc = PricingContext(market_data_location='TKO', pricing_date=datetime.date(2022, 4, 6), market=cm)
        assert pc.market_data_location == PricingLocation.TKO
        assert pc.pricing_date == datetime.date(2022, 4, 6)
        assert pc.market == cm
        assert pc.is_batch is None

        with pc:
            assert pc.is_batch is False
            swap1.resolve()
            swap1.dollar_price()
            swap2.calc(risk.IRDelta)
            port.resolve()
            port.price()
            port.calc(risk.IRDelta)
            assert pc.market_data_location == PricingLocation.TKO
            assert pc.pricing_date == datetime.date(2022, 4, 6)
            assert pc.market == cm
            assert pc.is_batch is False

        assert pc.market_data_location == PricingLocation.TKO
        assert pc.pricing_date == datetime.date(2022, 4, 6)
        assert pc.market == cm
        assert pc.is_batch is None


def test_location_mismatch():
    s = IRSwap()
    with pytest.raises(ValueError):
        with PricingContext(market_data_location='NYC'):
            with PricingContext(market_data_location='TKO'):
                s.price()

    with pytest.raises(ValueError):
        with PricingContext(market=CloseMarket(location='NYC', date=datetime.date(2022, 4, 6))):
            with PricingContext():
                with PricingContext(market=CloseMarket(location='TKO', date=datetime.date(2022, 4, 6))):
                    s.price()
