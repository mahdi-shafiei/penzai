# Copyright 2024 The Penzai Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Core struct abstraction.

A Struct is a PyTree dataclass with a few extra features.

In penzai, Struct is used as the primary way to define any class that should
behave as a JAX pytree, not just neural network modules.
"""

from __future__ import annotations

import abc
import dataclasses
import functools
import inspect
import typing
from typing import Any, Callable, Hashable, Literal, Sequence, Type, TypeVar

import jax
from penzai.core import dataclass_util
from penzai.core import formatting_util
from typing_extensions import dataclass_transform

if typing.TYPE_CHECKING:
  # To avoid circular import we only import this at the top level at typecheck
  # time; it's later imported at runtime.
  from penzai.core import selectors  # pylint: disable=g-bad-import-order


T = TypeVar("T")

# We store metadata on the class object itself, similar to how `dataclasses`
# does, to determine if an object is a penzai PyTree dataclass or not.
PYTREE_DATACLASS_INFO_ATTR = "__penzai_pytree_dataclass_info__"


def is_pytree_dataclass_type(cls: type[Any]) -> bool:
  """Checks if a class was wrapped in the `pytree_dataclass` decorator.

  Note that inheriting from a PyTree dataclass type does NOT produce another
  PyTree dataclass type, because JAX PyTree traversals depend on the specific
  class and not on its base classes.

  Args:
    cls: Class to check.

  Returns:
    True if this specific class was registred with @pytree_dataclass.
  """
  return isinstance(cls, type) and PYTREE_DATACLASS_INFO_ATTR in cls.__dict__


class PyTreeDataclassSafetyError(Exception):
  """Error raised due to pytree dataclass safety checks."""


@dataclass_transform(
    frozen_default=True,  # pylint: disable=unexpected-keyword-arg  # pytype: disable=not-supported-yet
    field_specifiers=(dataclasses.field,),
)
def pytree_dataclass(
    cls: type[Any] | None = None,
    /,
    *,
    has_implicitly_inherited_fields: bool = False,
    use_mutable_proxy_in_init: bool = True,
    overwrite_parent_init: bool = False,
    init: bool = True,
    repr: bool | Literal["auto"] = "auto",  # pylint: disable=redefined-builtin
    eq: bool = True,
    order: bool = False,
    match_args: bool = False,
    kw_only: bool = False,
) -> type[Any] | Callable[[type[Any]], type[Any]]:
  """Decorator for constructing a frozen PyTree dataclass.

  This decorator:
    - transforms the provided class into a frozen dataclass,
    - registers it with JAX as a PyTree node class with keys (but doesn't
      actually define the required methods),
    - runs some safety checks to avoid common dataclass pitfalls,
    - optionally, wraps any user-provided __init__ so that it can set its
      attributes normally even though the dataclass is frozen.

  Registration with JAX uses `jax.tree_util.register_pytree_with_keys_class`.
  This means that `cls` must define an instance method `tree_flatten_with_keys`
  and a class method `tree_unflatten`, as described in the JAX documentation.
  (If applying `pytree_dataclass` to a subclass of `Struct`, implementations of
  these methods are provided for you, and shouldn't be overridden.)

  If `has_implicitly_inherited_fields` is False, this decorator prevents a
  common pitfall with dataclass inheritance, by making sure that the list of
  attributes listed on the class exactly matches the list of fields inferred by
  `dataclasses`. This protects against some unintuitive behavior of
  `dataclasses.dataclass`: by default dataclasses inherit un-annotated fields
  from parent dataclasses and may have annotated fields re-ordered to match
  parent classes also.

  If `overwrite_parent_init` is False, we try to prevent a rare but
  tricky footgun of dataclass inheritance: if a parent class defines
  an __init__ that was not generated by `dataclasses`, dataclasses will happily
  overwrite __init__ with a generated one, even though the caller may be
  expecting to inherit it. In particular, we raise an error if
  `overwrite_parent_init` is False, `init` is True, the class does
  not define `__init__` itself, and the inherited __init__ was neither
  `object.__init__` nor an implementation generated by `pytree_dataclass`. This
  error can be silenced either by setting `overwrite_parent_init=True` or
  `init=False`, depending on which behavior the author intended, or by defining
  __init__ directly.

  The argument `use_mutable_proxy_in_init` determines whether __init__ should
  be modified to allow in-place mutation of the dataclass fields, inspired by a
  similar feature in equinox. This is implemented by constructing a separate
  *mutable subclass* of the class (stored as `cls._MutableInitProxy`), using the
  user-specified __init__ for that subclass, and then copying the fields (and
  only the fields) from this mutable proxy type into the original object.
  This transformation is only done on manually-provided __init__
  implementations, not for the one generated by `dataclasses.dataclass`.
  (Although a bit indirect, this is in some ways safer than writing __init__
  normally for a frozen dataclass, since that can expose a partially-constructed
  dataclass type that won't work properly with JAX.)

  Note that this transformation stores penzai-specific arguments in the
  attribute
  `cls.__penzai_pytree_dataclass_info__`, which is also used to check whether a
  class has been wrapped in this decorator already.

  Args:
    cls: The class to wrap. If provided, transforms this class and returns a
      transformed copy. If not provided, returns a decorator which can be
      applied to a class, similar to the ordinary `dataclasses` decorator.
    has_implicitly_inherited_fields: Whether this dataclass is explicitly opting
      into inheriting dataclass fields from its parent class(es). Usually,
      classes wrapped by @pytree_dataclass are encouraged to explicitly list out
      all fields, including inherited ones.
    use_mutable_proxy_in_init: Whether to wrap any user-provided __init__ so
      that assignments to `self` are possible inside it. See more detailed
      description above.
    overwrite_parent_init: Whether it's OK to overwrite an inherited __init__
      defined in a parent class, even if that __init__ was not generated by
      `pytree_dataclass` (and was not just `object.__init__`).
    init: Whether to generate __init__; see `dataclasses.dataclass`. You should
      usually set this to True unless you want to inherit a specific __init__
      implementation from a parent class. Ignored if your class defines __init__
      directly.
    repr: Whether to generate __repr__. If "auto", generates a __repr__ unless
      this is an instance of `Struct`, which already uses Treescope to represent
      the object. See `dataclasses.dataclass`.
    eq: Whether to generate __eq__; see `dataclasses.dataclass`.
    order: Whether to generate ordering methods; see `dataclasses.dataclass`.
    match_args: Whether to define __match_args__; see `dataclasses.dataclass`.
    kw_only: Whether to declare fields as keyword-only; see
      `dataclasses.dataclass`.

  Returns:
    A transformed version of `cls` if provided, or a decorator which can be
    applied to a class to transform it.

  Raises:
    ValueError: If this class is already a pytree dataclass, or if it already
      has values for reserved properties.
    PyTreeDataclassSafetyError: If `strict_fields` is True and the class does
      not explicitly list its fields in the correct order, or if
      `overwrite_parent_init` is False but we're about to overwrite
      a custom __init__.
  """
  if cls is None:
    # Being used as a decorator with keyword arguments.
    def decorator(cls):
      # Note: Pytype's keyword argument inference is modified by
      # dataclass_transform, so it doesn't interpret this correctly:
      return pytree_dataclass(  # pytype: disable=wrong-keyword-args
          cls,
          init=init,
          repr=repr,
          eq=eq,
          order=order,
          match_args=match_args,
          kw_only=kw_only,
          has_implicitly_inherited_fields=has_implicitly_inherited_fields,
          use_mutable_proxy_in_init=use_mutable_proxy_in_init,
          overwrite_parent_init=overwrite_parent_init,
      )

    return decorator

  if PYTREE_DATACLASS_INFO_ATTR in cls.__dict__:
    raise ValueError("Cannot apply pytree_dataclass twice to the same class!")

  # Check if `cls` directly defines an __init__ that we might need to override.
  original_init = cls.__dict__.get("__init__")

  # Check if we're about to override a custom __init__ in a confusing way.
  if init and original_init is None and not overwrite_parent_init:
    # We are about to overwrite __init__, but we aren't supposed to do that
    # if the parent init was custom.
    if hasattr(cls, PYTREE_DATACLASS_INFO_ATTR):
      # Some parent was a pytree dataclass. It's only safe to overwrite
      # cls.__init__ if cls.__init__ is the one generated by that decorator.
      generated_init = getattr(cls, PYTREE_DATACLASS_INFO_ATTR)[
          "generated_init"
      ]
      if generated_init is None:
        generated_init = object.__init__
    else:
      # The empty __init__ on object is safe to overwrite.
      generated_init = object.__init__

    if cls.__init__ is not generated_init:
      raise PyTreeDataclassSafetyError(
          f"@pytree_dataclass decorator for {cls.__name__} is about to"
          f" overwrite an inherited custom __init__ {cls.__init__} which wasn't"
          " generated by @pytree_dataclass, but `overwrite_parent_init` is not"
          " set. Set `overwrite_parent_init=True` if you want to overwrite the"
          " parent class's init with an automatically-generated one, or"
          " `init=False` if you want to inherit the one from the parent class."
          " Alternatively, you can define your own __init__ manually."
      )

  # Save our configuration params, so that we know this was a ptyree dataclass
  # later.
  setattr(
      cls,
      PYTREE_DATACLASS_INFO_ATTR,
      dict(
          has_implicitly_inherited_fields=has_implicitly_inherited_fields,
          use_mutable_proxy_in_init=use_mutable_proxy_in_init,
          is_init_proxy=False,
      ),
  )

  # Figure out if we should construct a repr.
  if repr == "auto":
    repr = not issubclass(cls, Struct)

  # Transform as a dataclass.
  # Note: Pytype won't be able to infer how this dataclass call works, but it's
  # OK because users only need type inference for the outer decorator.
  cls = dataclasses.dataclass(  # pytype: disable=not-supported-yet
      init=init,
      repr=repr,
      eq=eq,
      order=order,
      match_args=match_args,
      kw_only=kw_only,
      frozen=True,
  )(cls)

  # If we generated an `__init__`, remember it for later safety checks using
  # `overwrite_parent_init`.
  if init and original_init is None:
    getattr(cls, PYTREE_DATACLASS_INFO_ATTR)["generated_init"] = cls.__init__
  else:
    getattr(cls, PYTREE_DATACLASS_INFO_ATTR)["generated_init"] = None

  explicit_annotation_names = inspect.get_annotations(cls).keys()
  actual_fields = dataclasses.fields(cls)
  actual_field_names = [field.name for field in actual_fields]
  if has_implicitly_inherited_fields:
    # Make sure it actually does implicitly inherit something, otherwise this
    # is a confusing annotation.
    inherited_names = set(actual_field_names) - set(explicit_annotation_names)
    if not inherited_names:
      raise PyTreeDataclassSafetyError(
          f"{cls.__name__} was constructed with"
          " `has_implicitly_inherited_fields=True`, but it does not have any"
          " implicitly inherited fields."
      )
  else:
    # We want to make sure that every dataclass field of `cls` is also an
    # explicit annotation, and in the same order.
    # But dataclasses.dataclass does some filtering (related to KW_ONLY), so
    # it's OK if explicit_annotations contains extra annotations that aren't
    # actually fields.
    actual_field_name_set = set(actual_field_names)
    explicit_field_names = [
        name
        for name in explicit_annotation_names
        if name in actual_field_name_set
    ]
    if explicit_field_names != actual_field_names:
      raise PyTreeDataclassSafetyError(
          f"{cls.__name__} has missing or badly-ordered dataclass attributes:"
          f" the explicit field annotations {explicit_field_names} don't match"
          f" the inferred dataclass fields {actual_field_names}.\n\nIn Python,"
          " dataclasses inherit existing attributes (and their ordering) from"
          " parent dataclasses. To prevent accidental misuse, classes"
          " transformed with @pytree_dataclass should usually explicitly list "
          " all of their dataclass fields in the order inferred by the"
          " `dataclass` transformation. You can explicitly opt-in to implicit"
          " field inheritance by passing"
          " `has_implicitly_inherited_fields=True`."
      )

  # Register it as a pytree.
  if not (
      hasattr(cls, "tree_flatten_with_keys") and hasattr(cls, "tree_unflatten")
  ):
    raise AttributeError(
        f"{cls.__name__} must define both `tree_flatten_with_keys` and"
        " `tree_unflatten`, which are required by JAX's"
        " `register_pytree_with_keys_class`. If you'd like these to be"
        " generated automatically, you can subclass `penzai.Struct` in addition"
        " to using the  @pytree_dataclass decorator."
    )
  jax.tree_util.register_pytree_with_keys_class(cls)

  # Maybe replace __init__ with a version that uses a mutable proxy type.
  if use_mutable_proxy_in_init and original_init is not None:
    if "_MutableInitProxy" in cls.__dict__:
      raise ValueError(
          "Cannot create a mutable init proxy if the _MutableInitProxy class"
          " attribute is already defined!"
      )

    # Make a mutable proxy class, which is a subclass of `cls`, but NOT a
    # (explicit) dataclass and NOT a PyTree node. Also rebind __setattr__ to
    # allow mutation of it, and remove __hash__.
    # Then assign the user-specified __init__ to the proxy class.
    class _MutableInitProxy(cls):
      __setattr__ = object.__setattr__
      __hash__ = None
      __init__ = original_init

    _MutableInitProxy.__name__ = f"{cls.__name__}._MutableInitProxy"
    _MutableInitProxy.__qualname__ = f"{cls.__qualname__}._MutableInitProxy"

    # Save the fact that this is an init proxy, so that AbstractStructMetaclass
    # knows it's safe to instantiate (if applicable).
    setattr(
        _MutableInitProxy,
        PYTREE_DATACLASS_INFO_ATTR,
        dict(is_init_proxy=True),
    )

    # Store the mutable class on the (frozen) main class.
    # pylint: disable=protected-access
    cls._MutableInitProxy = _MutableInitProxy
    _MutableInitProxy._MutableInitProxy = None
    # pylint: enable=protected-access

    # Replace the ordinary __init__ of the main class with a wrapped version.
    @functools.wraps(original_init)
    def replacement_init(self, *args, **kwargs):
      # When initializing an instance of the frozen class, first create a
      # mutable proxy, which can initialize itself normally.
      proxy = _MutableInitProxy(*args, **kwargs)
      # Then copy the fields back with object.__setattr__.
      for field_ in dataclasses.fields(cls):
        try:
          value_from_proxy = getattr(proxy, field_.name)
        except AttributeError as exc:
          raise AttributeError(
              f'A value for field "{field_.name}" was not set during __init__!'
              " All dataclass fields must be set in __init__ when using"
              " `pytree_dataclass`."
          ) from exc
        object.__setattr__(self, field_.name, value_from_proxy)

    cls.__init__ = replacement_init

  return cls


class AbstractStructMetaclass(abc.ABCMeta):
  """The metaclass for penzai.Struct and its (possibly abstract) subclasses.

  In addition to ensuring that abstract methods are overridden before a class
  is instantiated, AbstractStructMetaclass also makes sure that the
  `pytree_dataclass` decorator was used. This means any instance of a subclass
  of `Struct` is guaranteed to be both a frozen dataclass and a PyTree node
  class.

  It's allowed to create a subclass of `penzai.Struct` without using
  `pytree_dataclass`, but this subclass will then be considered an abstract
  base class, and will not be possible to instantiate.

  Note that pytree-ness is NOT inherited, so it is not sufficient to inherit
  from some other class that used `pytree_dataclass`; every concrete Struct
  type must be transformed directly before it can be instantiated.

  Other than performing this safety check during initialization, we
  intentionally avoid actually changing the behavior of the class here,
  since metaclass trickery can be difficult to understand. We instead do the
  heavy lifting in `pytree_dataclass`. (This also means you are free to use
  `pytree_dataclass` without `AbstractStructMetaclass`, if you want.)
  """

  def __call__(cls: AbstractStructMetaclass, *args, **kwargs):
    """Creates a new instance of the class.

    Args:
      *args: Arguments to __init__.
      **kwargs: Keyword arguments to __init__.

    Returns:
      A new instance of the class.

    Raises:
      TypeError: If `pytree_dataclass` wasn't called on this class, indicating
        that it's abstract (or that the user forgot @pytree_dataclass).
    """
    if not is_pytree_dataclass_type(cls):
      raise TypeError(
          f"Can't instantiate abstract Struct subclass {cls}. Non-abstract"
          " subclasses of penzai.Struct must be decorated with"
          " @penzai.pytree_dataclass before they can be instantiated."
      )

    return super().__call__(*args, **kwargs)


def is_pytree_node_field(field: dataclasses.Field[Any]) -> bool:
  """Returns True if this field is treated as a PyTree child node by Struct.

  Fields are treated as PyTree nodes by default unless they contain a metadata
  entry with key "pytree_node" and value False.

  Args:
    field: Field to check.

  Returns:
    True if this field should be treated as a PyTree node.
  """
  return field.metadata.get("pytree_node", True)


@dataclasses.dataclass(frozen=True)
class StructStaticMetadata:
  """Container for a struct's static fields.

  Attributes:
    child_field_names: Names of all non-static fields in PyTree serialization
      order.
    static_fields: Values for all fields that are not PyTree nodes.
  """

  child_field_names: list[str]
  static_fields: dict[str, Any]


class Struct(metaclass=AbstractStructMetaclass):
  """Base class for penzai PyTree structures.

  ``Struct`` is the common base class for most objects in penzai. Structs are
  both frozen dataclasses and JAX PyTree nodes, with their fields annotations
  specifying which attributes contain JAX-traversible subtrees or numeric data
  and which attributes do not.

  ``Struct`` is heavily inspired by equinox's `equinox.Module`, and works much
  in the same way. However, there are a few differences:

  * Every non-abstract `Struct` must be explicitly registered as a dataclass
    pytree using the decorator `penzai.pytree_dataclass`, so that readers of the
    code can tell the class's semantics differ from that of an ordinary Python
    class.

  * The `pytree_dataclass` decorator supports additional configuration via
    keyword arguments, similar to the original dataclass decorator, and adds a
    few other features as well. In particular, by default attributes are NOT
    inherited from parent dataclasses, and ``__init__`` is modified to allow
    easier assignment to immutable fields; see `penzai.pytree_dataclass` for
    details.

  * ``__init__`` follows normal dataclass rules: an ``__init__`` will be
    generated unless ``__init__`` is defined or ``init=False`` is passed,
    similar to ordinary dataclass wrappers and in line with common typechecker
    expectations. However, to prevent accidentally overwriting their parent
    class's ``__init__`` instead of inheriting it, subclasses of classes with
    custom ``__init__`` implementations must explicitly opt in to this behavior
    by setting `overwrite_parent_init=True`, or opt out with `init=False`.
    (Equinox modules instead try to always inherit ``__init__`` in this case.)

  * Some convenient common methods for building, destructuring, and visualizing
    structs are defined by default.

  * Some equinox-specific features are not supported. Specifically, bound
    methods are not wrapped in Partial, and Equinox's "wrapped modules" are not
    supported.
  """

  @typing.final
  @classmethod
  def from_attributes(cls: Type[T], **field_values) -> T:
    """Directly instantiates a struct given all of its fields.

    Structs can override ``__init__`` to have arbitrary custom behavior, but
    this may make it difficult to construct new instances of structs with
    particular field values. This function makes it possible to directly
    instantiate an instance of a struct with given attributes.

    (Note: Overriding ``__init__`` in a ``Struct`` subclass is usually
    discouraged.)

    The main purpose of this method is to enable easier serialization and
    deserialization of structs. Callers of this method are responsible for
    maintaining any invariants expected by the class.

    Args:
      **field_values: Values for each of the struct's fields.

    Returns:
      A new instance of the class.
    """
    return dataclass_util.dataclass_from_attributes(cls, **field_values)

  @typing.final
  def attributes_dict(self) -> dict[str, Any]:
    """Constructs a dictionary with all of the fields in the class.

    The result of this should be passable back to `from_attributes` to rebuild
    (a copy of) the object.

    Returns:
      A dictionary containing all of the dataclass fields of the object.
    """
    # `self` is guaranteed to be a dataclass by AbstractStructMetaclass
    assert dataclasses.is_dataclass(self)
    return {
        field.name: getattr(self, field.name)
        for field in dataclasses.fields(self)
    }

  @typing.final
  def select(self) -> selectors.Selection[Struct]:
    """Wraps this struct in a selection, enabling functional-style mutations.

    This is a convenience wrapper around selectors.select to enable easier
    selection, using syntax like::

      struct.select().at(lambda b: b.foo[3].bar).apply(baz)
      struct.select().at_instances_of(Sequential).at_children().apply(qux)

    See documentation for `selectors.Selection` for supported attributes.

    Returns:
      A singleton selection containing this struct.
    """
    # Dynamic import to avoid circular import issues.
    from penzai.core import selectors  # pylint: disable=g-import-not-at-top

    return selectors.select(self)

  def key_for_field(self, field_name: str) -> Hashable:
    """Generates a JAX PyTree key for a given field name.

    This can be overridden if more control over JAX key paths is needed.

    Args:
      field_name: The field name to construct a key for.

    Returns:
      A hashable key to use in JAX PyTree paths.
    """
    return jax.tree_util.GetAttrKey(field_name)

  @typing.final
  def tree_flatten_with_keys(
      self,
  ) -> tuple[Sequence[tuple[Any, Any]], Any]:
    """Flattens this tree node with keys.

    See `jax.tree_util.register_pytree_with_keys_class`.

    This method should not be overridden by subclasses, since
    struct-manipulation code should be able to rely on this implementation (and
    in particular, on ``key_for_field`` producing the JAX keypath keys for each
    field). If you must override this for an advanced use case, consider using
    `pytree_dataclass` without subclassing `Struct`.

    Returns:
      ``(key, child)`` pairs for the node, along with static metadata.
    """
    child_field_names = []
    children = []
    static_fields = {}
    # `self` is guaranteed to be a dataclass by AbstractStructMetaclass
    for field_ in dataclasses.fields(self):  # pytype: disable=wrong-arg-types
      value = getattr(self, field_.name)
      if not is_pytree_node_field(field_):
        static_fields[field_.name] = value
      else:
        child_field_names.append(field_.name)
        children.append((
            self.key_for_field(field_.name),
            value,
        ))
    return children, StructStaticMetadata(child_field_names, static_fields)

  @typing.final
  def tree_flatten(self) -> tuple[Sequence[Any], Any]:
    """Flattens this tree node.

    See `jax.tree_util.register_pytree_with_keys_class`.

    This method should not be overridden by subclasses, since
    struct-manipulation code should be able to rely on this implementation. If
    you must override this for an advanced use case, consider using
    `pytree_dataclass` without subclassing `Struct`.

    Returns:
      Children of the node, along with static metadata.
    """
    child_field_names = []
    children = []
    static_fields = {}
    # `self` is guaranteed to be a dataclass by AbstractStructMetaclass
    for field_ in dataclasses.fields(self):  # pytype: disable=wrong-arg-types
      value = getattr(self, field_.name)
      if not is_pytree_node_field(field_):
        static_fields[field_.name] = value
      else:
        child_field_names.append(field_.name)
        children.append(value)
    return children, StructStaticMetadata(child_field_names, static_fields)

  @typing.final
  @classmethod
  def tree_unflatten(cls, aux_data: Any, children: Sequence[Any]) -> Struct:
    """Unflattens this tree node.

    See `jax.tree_util.register_pytree_with_keys_class`.

    This method should not be overridden by subclasses, since
    struct-manipulation code should be able to rely on this implementation. If
    you must override this for an advanced use case, consider using
    `pytree_dataclass` without subclassing `Struct`.

    Args:
      aux_data: Auxiliary data, returned from the second argument of
        `tree_flatten_with_keys` (or `tree_flatten`).
      children: Sequence of children from the first argument of
        `tree_flatten_with_keys` (or `tree_flatten`).

    Returns:
      An instance of the struct.
    """
    assert isinstance(aux_data, StructStaticMetadata)
    attributes = dict(aux_data.static_fields)
    assert len(children) == len(aux_data.child_field_names)
    for name, child in zip(aux_data.child_field_names, children):
      attributes[name] = child
    return cls.from_attributes(**attributes)

  def treescope_color(self) -> str | tuple[str, str]:
    """Computes a CSS color to display for this object in treescope.

    This function can be overridden to change the color for a particular object
    in treescope, without having to register a new handler.

    Returns:
      A CSS color string to use as a background/highlight color for this object.
      Alternatively, a tuple of (border, fill) CSS colors.
    """
    # By default, we render structs in color if they define __call__.
    if hasattr(self, "__call__"):
      type_string = type(self).__module__ + "." + type(self).__qualname__
      return formatting_util.color_from_string(type_string)
    else:
      return "transparent"

  def __repr__(self):
    """Renders this object with treescope, on a single line."""
    # Defer to Treescope.
    from penzai.treescope import default_renderer  # pylint: disable=g-import-not-at-top

    with default_renderer.using_expansion_strategy(max_height=1):
      return default_renderer.render_to_text(self, ignore_exceptions=True)

  def _repr_pretty_(self, p, cycle):
    """Pretty-prints this object for an IPython pretty-printer."""
    del cycle
    # Defer to Treescope.
    from penzai.treescope import default_renderer  # pylint: disable=g-import-not-at-top

    rendering = default_renderer.render_to_text(self, ignore_exceptions=True)
    for i, line in enumerate(rendering.split("\n")):
      if i:
        p.break_()
      p.text(line)
