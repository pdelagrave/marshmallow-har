from datetime import datetime
from functools import partial, namedtuple
from inspect import signature, Parameter
from typing import Generic, TypeVar, List

from marshmallow import Schema, fields, missing


class URL:
    pass


class Raw:
    pass


T = TypeVar('T')


class One(Generic[T]):
    pass


class Many(Generic[T]):
    pass


def kwsift(kw, f):
    '''
    Sifts a keyoword argument dictionary with respect to a function.
    Returns a dictionary with those entries that the given function
    accepts as keyword arguments.
    If the function is found to accept a variadic keyword dictionary
    (**kwargs), the first argument is returned unchanged, since any keyword
    argument is therefore legal.
    '''

    sig = signature(f)
    kw_kinds = {Parameter.KEYWORD_ONLY, Parameter.POSITIONAL_OR_KEYWORD}
    out = {}
    # go backward to catch **kwargs on the first pass
    for name, p in list(sig.parameters.items())[::-1]:
        if p.kind == p.VAR_KEYWORD:
            return kw
        elif p.kind in kw_kinds and name in kw.keys():
            out[name] = kw[name]

    return out


def get_schema_cls_name(model_cls):
    return model_cls.__name__ + 'Schema'


def is_model_init(init):
    '''
    Check if an __init__ callable correponds to one monkeypatched by
    a schema factory.
    '''
    return '_mro_offset' in signature(init).parameters.keys()


st_fieldspec = namedtuple(
    'fieldspec', ('default', 'type', 'req', 'allow_none'))


def schema_metafactory(  # noqa
        *,
        field_namer=lambda x: x,
        schema_base_class=Schema,
        extended_field_map=None,):
    '''
    Creates a domain-specific schema factory.
    '''

    SCHEMA_ATTRNAME = '__schema__'
    MODEL_ATTRNAME = '__model__'

    FIELD_TAB = {
        bool: fields.Boolean,
        str: fields.String,
        URL: fields.Url,
        int: fields.Integer,
        'nested': fields.Nested,
        'list': fields.List,
        datetime: partial(fields.DateTime, format='iso'),
        Raw: fields.Raw,
    }
    FIELD_TAB.update(extended_field_map or {})

    def get_schema_cls(model_cls):
        try:
            return FIELD_TAB[model_cls]
        except KeyError:
            sn = get_schema_cls_name(model_cls)
            try:
                return getattr(model_cls, SCHEMA_ATTRNAME)
            except AttributeError:
                raise ValueError(
                    '''{} does not appear to be a valid model, as it '
                    does not have an autogenerated Schema. Expected to find '
                    {} attribute, did not.'''.format(model_cls, sn))

    def schema_factory(model_cls):
        '''
        Automatically generated schema fields for the model class.

        Uses keyword arguments given to init and their annotations to
        magically figure out what fields to add.

        Arguments:
            model_cls: a stub model class with an appropriately annotated
                __init__ from which to generate a schema.

        Returns:
            model_cls: the patched model class with a __schema__ attribute
                and an attribute setting __init__.

        '''

        base_init = model_cls.__init__

        init_named_kwargs = {
            name: st_fieldspec(
                default=(
                    p.default if p.default is not Parameter.empty else None),
                type=p.annotation,
                req=p.default == p.empty,
                allow_none=p.default is None,
            )
            for name, p in signature(base_init).parameters.items()
            if p.kind == p.KEYWORD_ONLY
        }

        schema_attrs = {}

        for kwname, fspec in init_named_kwargs.items():
            field_args = []
            if issubclass(fspec.type, (Many, One, List)):
                key = 'nested' if not issubclass(fspec.type, List) else 'list'
                nested_type = get_schema_cls(fspec.type.__args__[0])
                field_args.append(nested_type)
            else:
                key = fspec.type

            load_dump_to = getattr(model_cls, 'irregular_names', {}).get(
                kwname, field_namer(kwname),
            )
            field = FIELD_TAB[key](
                *field_args,
                default=fspec.default or missing,
                many=issubclass(fspec.type, (Many, List)),
                required=fspec.req,
                load_from=load_dump_to,
                dump_to=load_dump_to,
                allow_none=fspec.allow_none,
            )

            schema_attrs[kwname] = field

        # mirror the model inhertance structure in the schema, important!
        schema_bases = tuple(
            model_base.__dict__[SCHEMA_ATTRNAME]
            for model_base in model_cls.__mro__
            if SCHEMA_ATTRNAME in model_base.__dict__
        ) + (schema_base_class,)

        schema_cls = type(
            get_schema_cls_name(model_cls), schema_bases, schema_attrs,
        )

        setattr(model_cls, SCHEMA_ATTRNAME, schema_cls)
        setattr(schema_cls, MODEL_ATTRNAME, model_cls)

        def model_init(model_obj, _mro_offset=1, **kwargs):
            '''
            Factor out the mindnumbing 'self.kwarg = kwarg' pattern.

            That should honestly be the default behaviour.
            '''

            # XXX: super(self.__class__, self).__init__ seems to fail
            # in a monkeypatched __init__ such as this one, forcing this kind
            # of manual __mro__ traversal. I'm sure something more sensible
            # can be done. This is the kind of stuff that gives metaprogramming
            # a bad name... blame super()'s super opacity
            model_cls = model_obj.__class__
            next_in_line = model_cls.__mro__[_mro_offset]

            if is_model_init(next_in_line.__init__):
                next_in_line.__init__(
                    model_obj, _mro_offset=_mro_offset + 1,
                    **kwsift(kwargs, next_in_line.__init__),
                )
            elif next_in_line is not object:
                next_in_line.__init__(
                    model_obj, **kwsift(kwargs, next_in_line.__init__)
                )

            for kwname, fspec in init_named_kwargs.items():
                attr = kwargs.get(kwname, fspec.default)

                if issubclass(fspec.type, (Many, List)):
                    attr = attr or []
                if issubclass(fspec.type, Raw):
                    attr = attr or {}
                elif callable(fspec.default):
                    attr = attr or fspec.default()

                setattr(model_obj, kwname, attr)

            base_init(model_obj, **kwsift(kwargs, base_init))

        def model_dump(self, *args, **kwargs):
            strict = kwargs.pop('strict', True)
            schema_ins = self.__schema__(*args, strict=strict, **kwargs)
            return schema_ins.dump(self)

        def model_load(cls, data, *args, **kwargs):
            strict = kwargs.pop('strict', True)
            schema_ins = cls.__schema__(*args, strict=strict, **kwargs)
            return schema_ins.load(data)

        model_cls.dump = model_dump
        model_cls.load = classmethod(model_load)
        model_cls.__init__ = model_init

        return model_cls

    return schema_factory
