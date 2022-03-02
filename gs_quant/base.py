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
from abc import ABC, ABCMeta, abstractmethod
import builtins
from collections import namedtuple
import copy
from dataclasses import Field, InitVar, MISSING, dataclass, field, fields, replace
from dataclasses_json import global_config
from dataclasses_json.core import _is_supported_generic, _decode_generic
import dataclasses_json.core
import datetime as dt
from enum import EnumMeta
from inflection import camelize, underscore
import inspect
import keyword
import logging
import numpy as np
from typing import Iterable, Mapping, Optional, Union

from gs_quant.context_base import ContextBase, ContextMeta
from gs_quant.json_convertors import encode_date_or_str, decode_date_or_str, decode_optional_date, encode_datetime,\
    decode_datetime, decode_float_or_str, decode_instrument, encode_dictable


_logger = logging.getLogger(__name__)

__builtins = set(dir(builtins))
__iskeyword = keyword.iskeyword
__getattribute__ = object.__getattribute__
__setattr__ = object.__setattr__


def is_iterable(o, t):
    return isinstance(o, Iterable) and all(isinstance(it, t) for it in o)


def is_instance_or_iterable(o, t):
    return isinstance(o, t) or is_iterable(o, t)


class RiskKey(namedtuple('RiskKey', ('provider', 'date', 'market', 'params', 'scenario', 'risk_measure'))):

    @property
    def ex_measure(self):
        return RiskKey(self.provider, self.date, self.market, self.params, self.scenario, None)

    @property
    def fields(self):
        return self._fields


class EnumBase:

    @classmethod
    def _missing_(cls: EnumMeta, key):
        if not isinstance(key, str):
            key = str(key)
        return next((m for m in cls.__members__.values() if m.value.lower() == key.lower()), None)

    def __reduce_ex__(self, protocol):
        return self.__class__, (self.value,)

    def __lt__(self: EnumMeta, other):
        return self.value < other.value

    def __repr__(self):
        return self.value


class HashableDict(dict):

    def __hash__(self):
        return hash(tuple(self.items()))


class DictBase(HashableDict):

    _PROPERTIES = set()

    def __init__(self, *args, **kwargs):
        if self._PROPERTIES:
            invalid_arg = next((k for k in kwargs.keys() if k not in self._PROPERTIES), None)
            if invalid_arg is not None:
                raise AttributeError(f"'{self.__class__.__name__}' has no attribute '{invalid_arg}'")

        super().__init__(*args, **{camelize(k, uppercase_first_letter=False): v for k, v in kwargs.items()})

    def __getitem__(self, item):
        return super().__getitem__(camelize(item, uppercase_first_letter=False))

    def __setitem__(self, key, value):
        return super().__setitem__(camelize(key, uppercase_first_letter=False), value)

    def __getattr__(self, item):
        if self._PROPERTIES:
            if underscore(item) in self._PROPERTIES:
                return self.get(item)
        elif item in self:
            return self[item]

        raise AttributeError(f"'{self.__class__.__name__}' has no attribute '{item}'")

    def __setattr__(self, key, value):
        if key in dir(self):
            return super().__setattr__(key, value)
        elif self._PROPERTIES and underscore(key) not in self._PROPERTIES:
            raise AttributeError(f"'{self.__class__.__name__}' has no attribute '{key}'")

        self[key] = value

    @classmethod
    def properties(cls) -> set:
        return cls._PROPERTIES


def fix_args(cls):
    # Rather unfortunate: prior to the refactor to use dataclasses, 'name' was a non-property argument
    # Adding it add post_init() to base to allow it to be passed, puts it as the first postional argument
    # and breaks backward compatibility. This can be fixed using the kw_only argument, which sadly was not added
    # until later versions than Python 3.7 (though it seems to be in the 3.6 backport)
    #
    # Additionally, some classes actually have a field called 'name', we need to use Base.name_ so positional passing
    # continues to work

    init = cls.__init__
    signature = inspect.signature(init)
    add_name = not any(p for p in signature.parameters.values() if p.name == 'name')

    if add_name:
        name_param = inspect.Parameter('name', inspect.Parameter.POSITIONAL_OR_KEYWORD, default=None,
                                       annotation=Optional[str])
        params = tuple(signature.parameters.values()) + (name_param,)
        signature = signature.replace(parameters=params)

    def wrapper(self, *args, name: Optional[str] = None, **kwargs):
        normalised_kwargs = {}

        # Handle legacy use of passing args as camel case

        for arg, value in kwargs.items():
            if not arg.isupper():
                snake_case_arg = underscore(arg)
                if snake_case_arg != arg and snake_case_arg in kwargs:
                    raise ValueError('{} and {} both specified'.format(arg, snake_case_arg))

                arg = snake_case_arg

            arg = cls._field_mappings().get(arg, arg)
            normalised_kwargs[arg] = value

        init(self, *args, **normalised_kwargs)

        if name is not None:
            self.name = name

    wrapper.__name__ = init.__name__
    wrapper.__qualname__ = init.__qualname__
    wrapper.__doc__ = init.__doc__
    wrapper.__module__ = init.__module__

    if add_name:
        cls.__doc__ = cls.__doc__[:-1] + f', name: Union[str, NoneType] = None)'
        wrapper.__annotations__ = {**{'name': Optional[str]}, **init.__annotations__}

    wrapper.__signature__ = signature

    cls.__init__ = wrapper
    cls.__base_dataclass_eq__ = cls.__eq__
    cls.__eq__ = cls.__eq_with_name__

    return cls


@dataclass
class Base(ABC):
    """The base class for all generated classes"""

    name_: InitVar[str] = field(init=False, default=None)

    __fields_by_name = None
    __field_mappings = None

    def __getattr__(self, item):
        fields_by_name = __getattribute__(self, '_fields_by_name')()

        if item.startswith('_') or item in fields_by_name:
            return __getattribute__(self, item)

        if item == 'name':
            return __getattribute__(self, 'name_')

        # Handle setting via camelCase names (legacy behaviour) and field mappings from disallowed names
        snake_case_item = underscore(item)
        field_mappings = __getattribute__(self, '_field_mappings')()
        snake_case_item = field_mappings.get(snake_case_item, snake_case_item)

        try:
            return __getattribute__(self, snake_case_item)
        except AttributeError:
            return __getattribute__(self, item)

    def __setattr__(self, key, value):
        # Handle setting via camelCase names (legacy behaviour)
        snake_case_key = underscore(key)
        snake_case_key = self._field_mappings().get(snake_case_key, snake_case_key)
        fld = self._fields_by_name().get(snake_case_key)

        if fld:
            if not fld.init:
                raise ValueError(f'{key} cannot be set')

            key = snake_case_key
            value = self.__coerce_value(fld.type, value)
            self._property_changed(key, value)
        elif key == 'name':
            key = 'name_'

        __setattr__(self, key, value)

    def __repr__(self):
        if self.name is not None:
            return f'{self.name} ({self.__class__.__name__})'

        return super().__repr__()

    def __eq_with_name__(self, other):
        return isinstance(other, Base) and self.name_ == other.name_ and self.__base_dataclass_eq__(other)

    @classmethod
    def __coerce_value(cls, typ: type, value):
        if isinstance(value, np.generic):
            # Handle numpy types
            return value.item()
        elif hasattr(value, 'tolist'):
            # tolist converts scalar or array to native python type if not already native.
            return value()
        elif typ in (DictBase, Optional[DictBase]) and isinstance(value, Base):
            return value.to_dict()
        if _is_supported_generic(typ):
            return _decode_generic(typ, value, False)
        else:
            return value

    @classmethod
    def _fields_by_name(cls) -> Mapping[str, Field]:
        if cls is Base:
            return {}

        if cls.__fields_by_name is None:
            cls.__fields_by_name = {f.name: f for f in fields(cls)}

        return cls.__fields_by_name

    @classmethod
    def _field_mappings(cls) -> Mapping[str, str]:
        if cls is Base:
            return {}

        if cls.__field_mappings is None:
            field_mappings = {}
            for fld in fields(cls):
                config_fn = fld.metadata.get('dataclasses_json', {}).get('letter_case')
                if config_fn:
                    mapped_name = config_fn('field_name')
                    if mapped_name:
                        field_mappings[mapped_name] = fld.name

            cls.__field_mappings = field_mappings
        return cls.__field_mappings

    def _property_changed(self, prop: str, value):
        pass

    def clone(self, **kwargs):
        """
            Clone this object, overriding specified values

            :param kwargs: property names and values, e.g. swap.clone(fixed_rate=0.01)

            **Examples**

            To change the market data location of the default context:

            >>> from gs_quant.instrument import IRCap
            >>> cap = IRCap('5y', 'GBP')
            >>>
            >>> new_cap = cap.clone(cap_rate=0.01)
        """
        ret = replace(self, **kwargs)
        ret.name_ = self.name_
        return ret

    @classmethod
    def properties(cls) -> set:
        """The public property names of this class"""
        return set(f[:-1] if f[-1] == '_' else f for f in cls._fields_by_name().keys())

    def as_dict(self, as_camel_case: bool = False) -> dict:
        """Dictionary of the public, non-null properties and values"""

        # to_dict() converts all the values to JSON type, does camel case and name mappings
        # asdict() does not convert values or case of the keys or do name mappings

        ret = {}
        field_mappings = {v: k for k, v in self._field_mappings().items()}

        for key in self.__fields_by_name.keys():
            value = __getattribute__(self, key)
            key = field_mappings.get(key, key)

            if value is not None:
                if as_camel_case:
                    key = camelize(key, uppercase_first_letter=False)

                ret[key] = value

        return ret

    @classmethod
    def default_instance(cls):
        """
        Construct a default instance of this type
        """
        required = {f.name: None if f.default == MISSING else f.default for f in fields(cls) if f.init}
        return cls(**required)

    def from_instance(self, instance):
        """
        Copy the values from an existing instance of the same type to our self
        :param instance: from which to copy:
        :return:
        """
        if not isinstance(instance, type(self)):
            raise ValueError('Can only use from_instance with an object of the same type')

        for fld in fields(self.__class__):
            if fld.init:
                __setattr__(self, fld.name, __getattribute__(instance, fld.name))


class Priceable(Base):

    def resolve(self, in_place: bool = True):
        """
        Resolve non-supplied properties of an instrument

        **Examples**

        >>> from gs_quant.instrument import IRSwap
        >>>
        >>> swap = IRSwap('Pay', '10y', 'USD')
        >>> rate = swap.fixedRate

        rate is None

        >>> swap.resolve()
        >>> rate = swap.fixedRate

        rates is now the solved fixed rate
        """
        raise NotImplementedError

    def dollar_price(self):
        """
        Present value in USD

        :return:  a float or a future, depending on whether the current PricingContext is async, or has been entered

        **Examples**

        >>> from gs_quant.instrument import IRCap
        >>>
        >>> cap = IRCap('1y', 'EUR')
        >>> price = cap.dollar_price()

        price is the present value in USD (a float)

        >>> cap_usd = IRCap('1y', 'USD')
        >>> cap_eur = IRCap('1y', 'EUR')
        >>>
        >>> from gs_quant.markets import PricingContext
        >>>
        >>> with PricingContext():
        >>>     price_usd_f = cap_usd.dollar_price()
        >>>     price_eur_f = cap_eur.dollar_price()
        >>>
        >>> price_usd = price_usd_f.result()
        >>> price_eur = price_eur_f.result()

        price_usd_f and price_eur_f are futures, price_usd and price_eur are floats
        """
        raise NotImplementedError

    def price(self):
        """
        Present value in local currency. Note that this is not yet supported on all instruments

        ***Examples**

        >>> from gs_quant.instrument import IRSwap
        >>>
        >>> swap = IRSwap('Pay', '10y', 'EUR')
        >>> price = swap.price()

        price is the present value in EUR (a float)
        """
        raise NotImplementedError

    def calc(self, risk_measure, fn=None):
        """
        Calculate the value of the risk_measure

        :param risk_measure: the risk measure to compute, e.g. IRDelta (from gs_quant.risk)
        :param fn: a function for post-processing results
        :return: a float or dataframe, depending on whether the value is scalar or structured, or a future thereof
        (depending on how PricingContext is being used)

        **Examples**

        >>> from gs_quant.instrument import IRCap
        >>> from gs_quant.risk import IRDelta
        >>>
        >>> cap = IRCap('1y', 'USD')
        >>> delta = cap.calc(IRDelta)

        delta is a dataframe

        >>> from gs_quant.instrument import EqOption
        >>> from gs_quant.risk import EqDelta
        >>>
        >>> option = EqOption('.SPX', '3m', 'ATMF', 'Call', 'European')
        >>> delta = option.calc(EqDelta)

        delta is a float

        >>> from gs_quant.markets import PricingContext
        >>>
        >>> cap_usd = IRCap('1y', 'USD')
        >>> cap_eur = IRCap('1y', 'EUR')

        >>> with PricingContext():
        >>>     usd_delta_f = cap_usd.calc(IRDelta)
        >>>     eur_delta_f = cap_eur.calc(IRDelta)
        >>>
        >>> usd_delta = usd_delta_f.result()
        >>> eur_delta = eur_delta_f.result()

        usd_delta_f and eur_delta_f are futures, usd_delta and eur_delta are dataframes
        """
        raise NotImplementedError


class __ScenarioMeta(ABCMeta, ContextMeta):
    pass


class Scenario(Base, ContextBase, metaclass=__ScenarioMeta):
    pass


class RiskMeasureParameter(Base):
    pass


@dataclass
class InstrumentBase(Base):

    quantity_: InitVar[float] = field(init=False, default=1)

    @property
    @abstractmethod
    def provider(self):
        ...

    @property
    def instrument_quantity(self) -> float:
        return self.quantity_

    @property
    def resolution_key(self) -> Optional[RiskKey]:
        try:
            return self.__resolution_key
        except AttributeError:
            return None

    @property
    def unresolved(self):
        try:
            return self.__unresolved
        except AttributeError:
            return None

    @property
    def metadata(self):
        try:
            return self.__metadata
        except AttributeError:
            return None

    @metadata.setter
    def metadata(self, value):
        self.__metadata = value

    def _property_changed(self, prop: str, value):
        try:
            if self.__resolution_key:
                self.__resolution_key = None
                self.unresolve()
        except AttributeError:
            # Can happen during init
            pass

    def from_instance(self, instance):
        self.__resolution_key = None
        super().from_instance(instance)
        self.__unresolved = instance.__unresolved
        self.__resolution_key = instance.__resolution_key

    def resolved(self, values: dict, resolution_key: RiskKey):
        all_values = self.as_dict(True)
        all_values.update(values)
        new_instrument = self.from_dict(all_values)
        new_instrument.name = self.name
        new_instrument.__unresolved = copy.copy(self)
        new_instrument.__resolution_key = resolution_key
        return new_instrument

    def unresolve(self):
        if self.__resolution_key and self.__unresolved:
            self.from_instance(self.__unresolved)
            self.__resolution_key = None
            self.__unresolved = None


@dataclass
class Market(ABC):

    def __hash__(self):
        return hash(self.market or self.location)

    def __eq__(self, other):
        return (self.market or self.location) == (other.market or other.location)

    def __lt__(self, other):
        return repr(self) < repr(other)

    @property
    @abstractmethod
    def market(self):
        ...

    @property
    @abstractmethod
    def location(self):
        ...

    def to_dict(self):
        return self.market.to_dict()

    def to_dict(self):
        return self.market.to_dict()

    def to_dict(self):
        return self.market.to_dict()


class Sentinel:

    def __init__(self, name: str):
        self.__name = name

    def __eq__(self, other):
        return self.__name == other.__name


def get_enum_value(enum_type: EnumMeta, value: Union[EnumBase, str]):
    if value in (None,):
        return None

    if isinstance(value, enum_type):
        return value

    try:
        enum_value = enum_type(value)
    except ValueError:
        _logger.warning('Setting value to {}, which is not a valid entry in {}'.format(value, enum_type))
        enum_value = value

    return enum_value


# Yes, I know this is a little evil ...
global_config.encoders[dt.date] = dt.date.isoformat
global_config.encoders[Optional[dt.date]] = encode_date_or_str
global_config.decoders[dt.date] = decode_optional_date
global_config.decoders[Optional[dt.date]] = decode_optional_date
global_config.encoders[Union[dt.date, str]] = encode_date_or_str
global_config.encoders[Optional[Union[dt.date, str]]] = encode_date_or_str
global_config.decoders[Union[dt.date, str]] = decode_date_or_str
global_config.decoders[Optional[Union[dt.date, str]]] = decode_date_or_str
global_config.encoders[dt.datetime] = encode_datetime
global_config.encoders[Optional[dt.datetime]] = encode_datetime
global_config.decoders[dt.datetime] = decode_datetime
global_config.decoders[Optional[dt.datetime]] = decode_datetime
global_config.decoders[Union[float, str]] = decode_float_or_str
global_config.decoders[Optional[Union[float, str]]] = decode_float_or_str

global_config.decoders[InstrumentBase] = decode_instrument
global_config.decoders[Optional[InstrumentBase]] = decode_instrument

global_config.encoders[Market] = encode_dictable
global_config.encoders[Optional[Market]] = encode_dictable


def __decode_dataclass(cls, kvs, infer_missing):
    # EXTREMELY unfortunate
    if isinstance(kvs, cls):
        return kvs
    elif hasattr(cls, 'decode_dataclass'):
        return cls.decode_dataclass(kvs)
    else:
        from dataclasses_json.core import _decode_dataclass_orig
        return _decode_dataclass_orig(cls, kvs, infer_missing)


dataclasses_json.core._decode_dataclass_orig = dataclasses_json.core._decode_dataclass
dataclasses_json.core._decode_dataclass = __decode_dataclass
