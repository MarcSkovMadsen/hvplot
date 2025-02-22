"""
interactive API
"""

import abc
import operator
import sys
from functools import partial
from types import FunctionType, MethodType

import holoviews as hv
import pandas as pd
import panel as pn
import param

from panel.layout import Column, Row, VSpacer, HSpacer
from panel.util import get_method_owner, full_groupby
from panel.widgets.base import Widget

from .converter import HoloViewsConverter
from .util import _flatten, is_tabular, is_xarray, is_xarray_dataarray


def _find_widgets(op):
    widgets = []
    op_args = list(op['args']) + list(op['kwargs'].values())
    op_args = _flatten(op_args)
    for op_arg in op_args:
        # Find widgets introduced as `widget` in an expression
        if 'panel' in sys.modules:
            if isinstance(op_arg, Widget) and op_arg not in widgets:
                widgets.append(op_arg)
        # TODO: Find how to execute this path?
        if isinstance(op_arg, hv.dim):
            for nested_op in op_arg.ops:
                for widget in _find_widgets(nested_op):
                    if widget not in widgets:
                        widgets.append(widget)
        # Find Ipywidgets
        if 'ipywidgets' in sys.modules:
            from ipywidgets import Widget as IPyWidget
            if isinstance(op_arg, IPyWidget) and op_arg not in widgets:
                widgets.append(op_arg)
        # Find widgets introduced as `widget.param.value` in an expression
        if (isinstance(op_arg, param.Parameter) and
            isinstance(op_arg.owner, pn.widgets.Widget) and
            op_arg.owner not in widgets):
            widgets.append(op_arg.owner)
    return widgets


class Interactive:
    """
    `Interactive` is a wrapper around a Python object that lets users create
    interactive pipelines by calling existing APIs on an object with dynamic
    parameters or widgets.

    `Interactive` can be instantiated with an object:
    
    >>> dfi = Interactive(df)

    However the recommended approach is to instantiate it via the
    `.interactive` accessor that is available on a data structure when it has
    been patched, e.g. after executing `import hvplot.pandas`.

    >>> dfi = df.interactive

    The `.interactive` accessor can also be called which allows to pass kwargs.

    >>> dfi = df.interactive()

    How it works
    ------------

    An `Interactive` instance watches what operations are applied to the object.

    To do so, each operation returns a new `Interactive` instance - the creation
    of a new instance being taken care of by the `_clone` method - which allows
    the next operation to be recorded, and so on and so forth. E.g. `dfi.head()`
    first records that the `'head'` attribute is accessed, this is achieved
    by overriding `__getattribute__`. A new interactive object is returned,
    which will then record that it is being called, and that will be called as
    `Interactive` implements `__call__`, which itself returns an `Interactive`
    instance.

    Note that under the hood even more `Interactive` instances may be created,
    but this is the gist of it.

    To be able to watch all the potential operations that may be applied to an
    object, `Interactive` implements on top of `__getattribute__` and
    `__call__`:
    
    - operators such as `__gt__`, `__add__`, etc.
    - the builtin functions `__abs__` and `__round__`
    - `__getitem__`
    - `__array_ufunc__`

    The `_depth` attribute starts at 0 and is incremented by 1 everytime
    a new `Interactive` instance is created part of a chain.
    The root instance in an expression has a `_depth` of 0. An expression can
    consist of multiple chains, such as `dfi[dfi.A > 1]`, as the `Interactive`
    instance is referenced twice in the expression. As a consequence `_depth`
    is not the total count of `Interactive` instance creations of a pipeline,
    it is the count of instances created in outer chain. In the example, that
    would be `dfi[]`. `Interactive` instances don't have references about
    the instances that created them or that they create, they just know their
    current location in a chain thanks to `_depth`. However, as some parameters
    need to be passed down the whole pipeline, they do have to propagate. E.g.
    in `dfi.interactive(width=200)`, `width=200` will be propagated as `kwargs`.

    
    Recording the operations applied to an object in a pipeline is done
    by gradually building a so-called "dim expression", or "dim transform",
    which is an expression language provided by HoloViews. dim transform
    objects are a way to express transforms on `Dataset`s, a `Dataset` being
    another HoloViews object that is a wrapper around common data structures
    such as Pandas/Dask/... Dataframes/Series, Xarray Dataset/DataArray, etc.
    For instance a Python expression such as `(series + 2).head()` can be
    expressed with a dim transform whose repr will be `(dim('*').pd+2).head(2)`,
    effectively showing that the dim transfom has recorded the different
    operations that are meant to be applied to the data.
    The `_transform` attribute stores the dim transform.

    The `_obj` attribute holds the original data structure that feeds the
    pipeline. All the `Interactive` instances created while parsing the
    pipeline share the same `_obj` object. And they all wrap it in a `Dataset`
    instance, and all apply the current dim transform they are aware of to
    the original data structure to compute the intermediate state of the data,
    that is stored it in the `_current` attribute. Doing so is particularly
    useful in Notebook sessions, as this allows to inspect the transformed
    object at any point of the pipeline, and as such provide correct
    auto-completion and docstrings. E.g. executing `dfi.A.max?` in a Notebook
    will correctly return the docstring of the Pandas Series `.max()` method,
    as the pipeline evaluates `dfi.A` to hold a current object `_current` that
    is a Pandas Series, and no longer and DataFrame.
    """

    # TODO: Why?
    __metaclass__ = abc.ABCMeta

    # Hackery to support calls to the classic `.plot` API, see `_get_ax_fn`
    # for more hacks!
    _fig = None

    def __new__(cls, obj, **kwargs):
        # __new__ implemented to support functions as input, e.g. 
        # hvplot.find(foo, widget).interactive().max()
        if 'fn' in kwargs:
            fn = kwargs.pop('fn')
        elif isinstance(obj, (FunctionType, MethodType)):
            fn = pn.panel(obj, lazy=True)
            obj = fn.eval(obj)
        else:
            fn = None
        clss = cls
        for subcls in cls.__subclasses__():
            if subcls.applies(obj):
                clss = subcls
        inst = super(Interactive, cls).__new__(clss)
        inst._obj = obj
        inst._fn = fn
        return inst

    @classmethod
    def applies(cls, obj):
        """
        Subclasses must implement applies and return a boolean to indicates
        wheter the subclass should apply or not to the obj.
        """
        return True

    def __init__(self, obj, transform=None, fn=None, plot=False, depth=0,
                 loc='top_left', center=False, dmap=False, inherit_kwargs={},
                 max_rows=100, method=None, **kwargs):
        # _init is used to prevent to __getattribute__ to execute its
        # specialized code.
        self._init = False
        if self._fn is not None:
            for _, params in full_groupby(self._fn_params, lambda x: id(x.owner)):
                params[0].owner.param.watch(self._update_obj, [p.name for p in params])
        self._method = method
        if transform is None:
            dim = '*'
            transform = hv.util.transform.dim
            if is_xarray(obj):
                transform = hv.util.transform.xr_dim
                if is_xarray_dataarray(obj):
                    dim = obj.name
                if dim is None:
                    raise ValueError(
                        "Cannot use interactive API on DataArray without name."
                        "Assign a name to the DataArray and try again."
                    )
            elif is_tabular(obj):
                transform = hv.util.transform.df_dim
            self._transform = transform(dim)
        else:
            self._transform = transform
        self._plot = plot
        self._depth = depth
        self._loc = loc
        self._center = center
        self._dmap = dmap
        # TODO: What's the real use of inherit_kwargs? So far I've only seen
        # it containing 'ax'
        self._inherit_kwargs = inherit_kwargs
        self._max_rows = max_rows
        self._kwargs = kwargs
        ds = hv.Dataset(self._obj)
        self._current = self._transform.apply(ds, keep_index=True, compute=False)
        self._init = True
        self.hvplot = _hvplot(self)

    def _update_obj(self, *args):
        self._obj = self._fn.eval(self._fn.object)

    @property
    def _fn_params(self):
        if self._fn is None:
            deps = []
        elif isinstance(self._fn, pn.param.ParamFunction):
            dinfo = getattr(self._fn.object, '_dinfo', {})
            deps = list(dinfo.get('dependencies', [])) + list(dinfo.get('kw', {}).values())
        else:
            # TODO: Find how to execute that path?
            parameterized = get_method_owner(self._fn.object)
            deps = parameterized.param.method_dependencies(self._fn.object.__name__)
        return deps

    @property
    def _params(self):
        ps = self._fn_params
        for k, p in self._transform.params.items():
            if k == 'ax' or p in ps:
                continue
            ps.append(p)
        return ps

    @property
    def _callback(self):
        @pn.depends(*self._params)
        def evaluate(*args, **kwargs):
            obj = self._obj
            ds = hv.Dataset(obj)
            transform = self._transform
            if ds.interface.datatype == 'xarray' and is_xarray_dataarray(obj):
                transform = transform.clone(obj.name)
            obj = transform.apply(ds, keep_index=True, compute=False)
            if self._method:
                # E.g. `pi = dfi.A` leads to `pi._method` equal to `'A'`.
                obj = getattr(obj, self._method, obj)
            if self._plot:
                return Interactive._fig
            elif isinstance(obj, pd.DataFrame):
                return pn.pane.DataFrame(obj, max_rows=self._max_rows, **self._kwargs)
            else:
                return obj
        return evaluate

    def _clone(self, transform=None, plot=None, loc=None, center=None,
               dmap=None, copy=False, **kwargs):
        plot = self._plot or plot
        transform = transform or self._transform
        loc = self._loc if loc is None else loc
        center = self._center if center is None else center
        dmap = self._dmap if dmap is None else dmap
        depth = self._depth + 1
        if copy:
            kwargs = dict(self._kwargs, inherit_kwargs=self._inherit_kwargs, method=self._method, **kwargs)
        else:
            kwargs = dict(self._inherit_kwargs, **dict(self._kwargs, **kwargs))
        return type(self)(self._obj, fn=self._fn, transform=transform, plot=plot, depth=depth,
                         loc=loc, center=center, dmap=dmap, **kwargs)

    def _repr_mimebundle_(self, include=[], exclude=[]):
        return self.layout()._repr_mimebundle_()

    def __dir__(self):
        current = self._current
        if self._method:
            current = getattr(current, self._method)
        extras = {attr for attr in dir(current) if not attr.startswith('_')}
        if is_tabular(current) and hasattr(current, 'columns'):
            extras |= set(current.columns)
        try:
            return sorted(set(super().__dir__()) | extras)
        except Exception:
            return sorted(set(dir(type(self))) | set(self.__dict__) | extras)

    def _resolve_accessor(self):
        if not self._method:
            # No method is yet set, as in `dfi.A`, so return a copied clone.
            return self._clone(copy=True)
        # This is executed when one runs e.g. `dfi.A > 1`, in which case after
        # dfi.A the _method 'A' is set (in __getattribute__) which allows
        # _resolve_accessor to keep building the transform dim expression.
        transform = type(self._transform)(self._transform, self._method, accessor=True)
        transform._ns = self._current
        inherit_kwargs = {}
        if self._method == 'plot':
            inherit_kwargs['ax'] = self._get_ax_fn()
        try:
            new = self._clone(transform, inherit_kwargs=inherit_kwargs)
        finally:
            # Reset _method for whatever happens after the accessor has been
            # fully resolved, e.g. whatever happens `dfi.A > 1`.
            self._method = None
        return new

    def __getattribute__(self, name):
        self_dict = super().__getattribute__('__dict__')
        if not self_dict.get('_init'):
            return super().__getattribute__(name)

        current = self_dict['_current']
        method = self_dict['_method']
        if method:
            current = getattr(current, method)
        # Getting all the public attributes available on the current object,
        # e.g. `sum`, `head`, etc.
        extras = [d for d in dir(current) if not d.startswith('_')]
        if name in extras and name not in super().__dir__():
            new = self._resolve_accessor()
            # Setting the method name for a potential use later by e.g. an
            # operator or method, as in `dfi.A > 2`. or `dfi.A.max()`
            new._method = name
            try:
                new.__doc__ = getattr(current, name).__doc__
            except Exception:
                pass
            return new
        return super().__getattribute__(name)

    @staticmethod
    def _get_ax_fn():
        @pn.depends()
        def get_ax():
            from matplotlib.backends.backend_agg import FigureCanvas
            from matplotlib.pyplot import Figure
            Interactive._fig = fig = Figure()
            FigureCanvas(fig)
            return fig.subplots()
        return get_ax

    def __call__(self, *args, **kwargs):
        if self._method is None:
            if self._depth == 0:
                # This code path is entered when initializing an interactive
                # class from the accessor, e.g. with df.interactive(). As
                # calling the accessor df.interactive already returns an
                # Interactive instance.
                return self._clone(*args, **kwargs)
            # TODO: When is this error raised?
            raise AttributeError
        elif self._method == 'plot':
            # This - {ax: get_ax} - is passed as kwargs to the plot method in
            # the dim expression.
            kwargs['ax'] = self._get_ax_fn()
        new = self._clone(copy=True)
        try:
            method = type(new._transform)(new._transform, new._method, accessor=True)
            kwargs = dict(new._inherit_kwargs, **kwargs)
            clone = new._clone(method(*args, **kwargs), plot=new._method == 'plot')
        finally:
            # If an error occurs reset _method anyway so that, e.g. the next
            # attempt in a Notebook, is set appropriately.
            new._method = None
        return clone

    #----------------------------------------------------------------
    # Interactive pipeline APIs
    #----------------------------------------------------------------

    def __array_ufunc__(self, *args, **kwargs):
        # TODO: How to trigger this method?
        new = self._resolve_accessor()
        transform = new._transform
        transform = args[0](transform, *args[3:], **kwargs)
        return new._clone(transform)

    def _apply_operator(self, operator, *args, **kwargs):
        new = self._resolve_accessor()
        transform = new._transform
        transform = type(transform)(transform, operator, *args)
        return new._clone(transform)

    # Builtin functions
    def __abs__(self):
        return self._apply_operator(abs)

    def __round__(self, ndigits=None):
        args = () if ndigits is None else (ndigits,)
        return self._apply_operator(round, *args)

    # Unary operators
    def __neg__(self):
        return self._apply_operator(operator.neg)
    def __not__(self):
        return self._apply_operator(operator.not_)
    def __invert__(self):
        return self._apply_operator(operator.inv)
    def __pos__(self):
        return self._apply_operator(operator.pos)

    # Binary operators
    def __add__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.add, other)
    def __and__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.and_, other)
    def __div__(self, other):
        # TODO: operator.div is only available in Python 2, to be removed.
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.div, other)
    def __eq__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.eq, other)
    def __floordiv__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.floordiv, other)
    def __ge__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.ge, other)
    def __gt__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.gt, other)
    def __le__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.le, other)
    def __lt__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.lt, other)
    def __lshift__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.lshift, other)
    def __mod__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.mod, other)
    def __mul__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.mul, other)
    def __ne__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.ne, other)
    def __or__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.or_, other)
    def __rshift__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.rshift, other)
    def __pow__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.pow, other)
    def __sub__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.sub, other)
    def __truediv__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.truediv, other)

    # Reverse binary operators
    def __radd__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.div, other, reverse=True)
    def __rand__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.and_, other, reverse=True)
    def __rdiv__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.div, other, reverse=True)
    def __rfloordiv__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.floordiv, other, reverse=True)
    def __rlshift__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.rlshift, other)
    def __rmod__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.mod, other, reverse=True)
    def __rmul__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.mul, other, reverse=True)
    def __ror__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.or_, other, reverse=True)
    def __rpow__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.pow, other, reverse=True)
    def __rrshift__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.rrshift, other)
    def __rsub__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.sub, other, reverse=True)
    def __rtruediv__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.truediv, other, reverse=True)

    def __getitem__(self, other):
        other = other._transform if isinstance(other, Interactive) else other
        return self._apply_operator(operator.getitem, other)

    def _plot(self, *args, **kwargs):
        # TODO: Seems totally unused to me, as self._plot is set to a boolean in __init__
        @pn.depends()
        def get_ax():
            from matplotlib.backends.backend_agg import FigureCanvas
            from matplotlib.pyplot import Figure
            Interactive._fig = fig = Figure()
            FigureCanvas(fig)
            return fig.subplots()
        kwargs['ax'] = get_ax
        new = self._resolve_accessor()
        transform = new._transform
        transform = type(transform)(transform, 'plot', accessor=True)
        return new._clone(transform(*args, **kwargs), plot=True)

    #----------------------------------------------------------------
    # Public API
    #----------------------------------------------------------------

    def dmap(self):
        """
        Wraps the output in a DynamicMap. Only valid if the output
        is a HoloViews object.
        """
        return hv.DynamicMap(self._callback)

    def eval(self):
        """
        Returns the current state of the interactive expression. The
        returned object is no longer interactive.
        """
        obj = self._current
        if self._method:
            return getattr(obj, self._method, obj)
        return obj

    def layout(self, **kwargs):
        """
        Returns a layout of the widgets and output arranged according
        to the center and widget location specified in the
        interactive call.
        """
        widget_box = self.widgets()
        panel = self.output()
        loc = self._loc
        if loc in ('left', 'right'):
            widgets = Column(VSpacer(), widget_box, VSpacer())
        elif loc in ('top', 'bottom'):
            widgets = Row(HSpacer(), widget_box, HSpacer())
        elif loc in ('top_left', 'bottom_left'):
            widgets = Row(widget_box, HSpacer())
        elif loc in ('top_right', 'bottom_right'):
            widgets = Row(HSpacer(), widget_box)
        elif loc in ('left_top', 'right_top'):
            widgets = Column(widget_box, VSpacer())
        elif loc in ('left_bottom', 'right_bottom'):
            widgets = Column(VSpacer(), widget_box)
        # TODO: add else and raise error
        center = self._center
        if not widgets:
            if center:
                components = [HSpacer(), panel, HSpacer()]
            else:
                components = [panel]
        elif center:
            if loc.startswith('left'):
                components = [widgets, HSpacer(), panel, HSpacer()]
            elif loc.startswith('right'):
                components = [HSpacer(), panel, HSpacer(), widgets]
            elif loc.startswith('top'):
                components = [HSpacer(), Column(widgets, Row(HSpacer(), panel, HSpacer())), HSpacer()]
            elif loc.startswith('bottom'):
                components = [HSpacer(), Column(Row(HSpacer(), panel, HSpacer()), widgets), HSpacer()]
        else:
            if loc.startswith('left'):
                components = [widgets, panel]
            elif loc.startswith('right'):
                components = [panel, widgets]
            elif loc.startswith('top'):
                components = [Column(widgets, panel)]
            elif loc.startswith('bottom'):
                components = [Column(panel, widgets)]
        return Row(*components, **kwargs)

    def holoviews(self):
        """
        Returns a HoloViews object to render the output of this
        pipeline. Only works if the output of this pipeline is a
        HoloViews object, e.g. from an .hvplot call.
        """
        return hv.DynamicMap(self._callback)

    def output(self):
        """
        Returns the output of the interactive pipeline, which is
        either a HoloViews DynamicMap or a Panel object.

        Returns
        -------
        DynamicMap or Panel object wrapping the interactive output.
        """
        return self.holoviews() if self._dmap else self.panel(**self._kwargs)

    def panel(self, **kwargs):
        """
        Wraps the output in a Panel component.
        """
        return pn.panel(self._callback, **kwargs)

    def widgets(self):
        """
        Returns a Column of widgets which control the interactive output.

        Returns
        -------
        A Column of widgets
        """
        widgets = []
        for p in self._fn_params:
            if (isinstance(p.owner, pn.widgets.Widget) and
                p.owner not in widgets):
                widgets.append(p.owner)
        for op in self._transform.ops:
            for w in _find_widgets(op):
                if w not in widgets:
                    widgets.append(w)
        return pn.Column(*widgets)


class _hvplot:
    _kinds = tuple(HoloViewsConverter._kind_mapping)

    __slots__ = ["_interactive"]

    def __init__(self, _interactive):
        self._interactive = _interactive

    def __call__(self, *args, _kind=None, **kwargs):
        # The underscore in _kind is to not overwrite it
        # if 'kind' is in kwargs and the function
        # is used with partial.
        if _kind and "kind" in kwargs:
            raise TypeError(f"{_kind}() got an unexpected keyword argument 'kind'")
        if _kind:
            kwargs["kind"] = _kind

        new = self._interactive._resolve_accessor()
        transform = new._transform
        transform = type(transform)(transform, 'hvplot', accessor=True)
        dmap = 'kind' not in kwargs or not isinstance(kwargs['kind'], str)
        return new._clone(transform(*args, **kwargs), dmap=dmap)

    def __getattr__(self, attr):
        if attr in self._kinds:
            return partial(self, _kind=attr)
        else:
            raise AttributeError(f"'hvplot' object has no attribute '{attr}'")

    def __dir__(self):
        # This function is for autocompletion
        return self._interactive._obj.hvplot.__all__
